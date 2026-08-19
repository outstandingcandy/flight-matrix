"""Sink for JetPhotos scraper — updates ``aircraft_static_info`` + ``aircraft_images``.

Also the place a freshly downloaded image gets its thumbnail. That belongs here
rather than in the scraper because the sink is the layer that knows about this
deployment's storage, and it runs on every target — unlike the AWS-only
``lambda_thumbnail`` function, which was the only on-demand producer and left
``gcp`` and ``local`` deployments serving broken thumbnail URLs until somebody
ran ``scripts/generate_thumbnails.py`` by hand.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.jetphotos.models import (
    ImageMetadata,
    JetPhotosResult,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from src.core.exceptions import StorageError
from src.storage.base import ObjectStorage

if TYPE_CHECKING:
    from src.media.thumbnails import ThumbnailService

logger = logging.getLogger("scraper.sinks.jetphotos")


class JetPhotosSink:
    """Persist JetPhotos downloads into flight-matrix tables.

    Args:
        database_url: SQLAlchemy URL. An empty value disables the DB writes.
        storage: Object storage for this deployment target, used by
            :meth:`store_object` and to write thumbnails. ``None`` disables
            both, leaving the backfill scripts as the only producers.
        images_dir: Directory the scraper downloads into, searched for a source
            image whose stored key is not resolvable as-is.
    """

    def __init__(
        self,
        database_url: str,
        storage: ObjectStorage | None = None,
        images_dir: str = "",
    ) -> None:
        self.storage = storage
        self.db_engine: Any | None = None
        if database_url:
            try:
                self.db_engine = create_engine(database_url, echo=False, pool_pre_ping=True)
            except Exception as e:
                logger.error(f"Failed to initialize DB engine: {e}")

        self.thumbnails: ThumbnailService | None = None
        if storage is not None:
            try:
                from src.media.thumbnails import ThumbnailService as _ThumbnailService
            except ImportError as e:
                # Pillow. Imported here rather than at module scope so a runtime
                # image built before it was declared loses thumbnails instead of
                # failing to load the sink and taking every scraper with it.
                logger.warning(f"Thumbnails disabled ({e}); install pillow to enable them")
            else:
                local_dirs: Sequence[str] = (images_dir,) if images_dir else ()
                # prefer_local: the scraper has just written the file to
                # images_dir, so reading it back out of the bucket would be a
                # wasted round trip.
                self.thumbnails = _ThumbnailService(storage, local_dirs, prefer_local=True)

    # Callback wired into scraper config as ``upload_callback``
    def store_object(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> bool:
        """Write scraped bytes to this deployment's object storage.

        The scraper's own upload path is boto3, so on the ``gcp`` and ``local``
        targets it silently stored nothing: downloaded images kept only their
        local copy and the saved page HTML that ``src/scraper/reextractor.py``
        re-reads was never written at all. Routing it through here makes the
        same keys appear on whichever provider is configured.

        Args:
            key: Object key chosen by the scraper.
            data: Raw bytes to store.
            content_type: MIME type to record with the object.
            cache_control: ``Cache-Control`` header to serve the object with.

        Returns:
            True if the bytes were stored.
        """
        if self.storage is None:
            return False

        try:
            self.storage.upload_bytes(key, data, content_type, cache_control)
        except StorageError as e:
            logger.warning(f"Failed to store {key}: {e}")
            return False
        return True

    # Callback wired into scraper config as ``persist_images_callback``
    def persist_images(
        self,
        registration: str,
        image_paths: list[str],
        images_metadata: list[ImageMetadata],
    ) -> None:
        if self.db_engine:
            if image_paths:
                self._sync_to_aircraft_static_info(registration, image_paths)
            if images_metadata:
                self._save_images_metadata(registration, images_metadata)

        # Last, and never conditional on the writes above succeeding: a
        # thumbnail is derived data, so it is worth having even if the metadata
        # write failed, and its absence must not hold up the row.
        if image_paths and self.thumbnails is not None:
            self._generate_thumbnails(registration, image_paths)

    def _generate_thumbnails(self, registration: str, image_paths: list[str]) -> None:
        """Write a thumbnail for each image this scrape stored.

        Args:
            registration: Aircraft registration, for logging.
            image_paths: Object keys of the images just downloaded.
        """
        assert self.thumbnails is not None
        written = sum(1 for path in image_paths if self.thumbnails.ensure_thumbnail(path))
        if written < len(image_paths):
            logger.warning(
                f"[{registration}] Wrote {written}/{len(image_paths)} thumbnail(s); "
                "the rest will be picked up by scripts/generate_thumbnails.py"
            )
        else:
            logger.info(f"[{registration}] Wrote {written} thumbnail(s)")

    def _sync_to_aircraft_static_info(self, registration: str, image_paths: list[str]) -> None:
        assert self.db_engine is not None
        try:
            with self.db_engine.connect() as conn:
                result = conn.execute(
                    text("""
                        UPDATE aircraft_static_info
                        SET images_downloaded = true,
                            images_updated_at = :updated_at
                        WHERE registration = :registration
                    """),
                    {
                        "registration": registration,
                        "updated_at": datetime.now(UTC),
                    },
                )
                conn.commit()
                if result.rowcount > 0:
                    logger.info(f"[{registration}] Updated images_downloaded flag")
                else:
                    logger.warning(f"[{registration}] Not found in aircraft_static_info")
        except SQLAlchemyError as e:
            logger.error(f"[{registration}] Failed to sync images_downloaded: {e}")

    def _save_images_metadata(
        self, registration: str, images_metadata: list[ImageMetadata]
    ) -> None:
        assert self.db_engine is not None
        saved_count = 0
        try:
            with self.db_engine.connect() as conn:
                aircraft_id_result = conn.execute(
                    text("SELECT id FROM aircraft_static_info WHERE registration = :r"),
                    {"r": registration},
                ).fetchone()
                aircraft_id = aircraft_id_result[0] if aircraft_id_result else None

                max_order_result = conn.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(display_order), 0)
                        FROM aircraft_images
                        WHERE registration = :r
                        """
                    ),
                    {"r": registration},
                ).fetchone()
                current_max_order = max_order_result[0] if max_order_result else 0
                has_existing_images = current_max_order > 0

                for meta in images_metadata:
                    if not meta.jetphotos_id:
                        logger.warning(f"[{registration}] Skipping image without jetphotos_id")
                        continue

                    existing = conn.execute(
                        text("SELECT id FROM aircraft_images WHERE jetphotos_id = :j"),
                        {"j": meta.jetphotos_id},
                    ).fetchone()

                    if existing:
                        has_new_data = any(
                            [
                                meta.photographer,
                                meta.photo_date,
                                meta.upload_date,
                                meta.location,
                                meta.notes,
                                meta.camera,
                                meta.views is not None,
                                meta.likes is not None,
                                meta.badges,
                                meta.html_s3_path,
                            ]
                        )
                        if has_new_data:
                            conn.execute(
                                text(
                                    """
                                    UPDATE aircraft_images
                                    SET photographer = COALESCE(:photographer, photographer),
                                        photo_date = COALESCE(:photo_date, photo_date),
                                        upload_date = COALESCE(:upload_date, upload_date),
                                        location = COALESCE(:location, location),
                                        airport_icao = COALESCE(:airport_icao, airport_icao),
                                        airport_name = COALESCE(:airport_name, airport_name),
                                        notes = COALESCE(:notes, notes),
                                        camera = COALESCE(:camera, camera),
                                        views = COALESCE(:views, views),
                                        likes = COALESCE(:likes, likes),
                                        badges = COALESCE(:badges, badges),
                                        html_s3_path = COALESCE(:html_s3_path, html_s3_path),
                                        updated_at = :updated_at
                                    WHERE jetphotos_id = :jetphotos_id
                                    """
                                ),
                                {
                                    "photographer": meta.photographer,
                                    "photo_date": meta.photo_date,
                                    "upload_date": meta.upload_date,
                                    "location": meta.location,
                                    "airport_icao": meta.airport_icao,
                                    "airport_name": meta.airport_name,
                                    "notes": meta.notes,
                                    "camera": meta.camera,
                                    "views": meta.views,
                                    "likes": meta.likes,
                                    "badges": meta.badges,
                                    "html_s3_path": meta.html_s3_path,
                                    "updated_at": datetime.now(UTC),
                                    "jetphotos_id": meta.jetphotos_id,
                                },
                            )
                            conn.commit()
                            saved_count += 1
                        continue

                    display_order = current_max_order + saved_count + 1
                    is_primary = (display_order == 1) and not has_existing_images

                    conn.execute(
                        text(
                            """
                            INSERT INTO aircraft_images (
                                registration, aircraft_id, image_path, source_url, source,
                                photographer, photo_date, upload_date,
                                location, airport_icao, airport_name,
                                file_size_bytes, jetphotos_id, notes,
                                camera, views, likes, badges, html_s3_path,
                                display_order, is_primary,
                                created_at, updated_at
                            ) VALUES (
                                :registration, :aircraft_id, :image_path, :source_url, :source,
                                :photographer, :photo_date, :upload_date,
                                :location, :airport_icao, :airport_name,
                                :file_size_bytes, :jetphotos_id, :notes,
                                :camera, :views, :likes, :badges, :html_s3_path,
                                :display_order, :is_primary,
                                :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            "registration": registration,
                            "aircraft_id": aircraft_id,
                            "image_path": meta.image_path,
                            "source_url": meta.source_url,
                            "source": "jetphotos",
                            "photographer": meta.photographer,
                            "photo_date": meta.photo_date,
                            "upload_date": meta.upload_date,
                            "location": meta.location,
                            "airport_icao": meta.airport_icao,
                            "airport_name": meta.airport_name,
                            "file_size_bytes": meta.file_size_bytes,
                            "jetphotos_id": meta.jetphotos_id,
                            "notes": meta.notes,
                            "camera": meta.camera,
                            "views": meta.views,
                            "likes": meta.likes,
                            "badges": meta.badges,
                            "html_s3_path": meta.html_s3_path,
                            "display_order": display_order,
                            "is_primary": is_primary,
                            "created_at": datetime.now(UTC),
                            "updated_at": datetime.now(UTC),
                        },
                    )
                    saved_count += 1

                conn.commit()

                if saved_count > 0:
                    logger.info(f"[{registration}] Saved {saved_count} image(s) to aircraft_images")
        except SQLAlchemyError as e:
            logger.error(f"[{registration}] Failed to save image metadata: {e}")

    # Keep a minimal on_success so bind_sink() sees something to chain.
    # Actual DB writes fire through persist_images, triggered by the scraper.
    def on_success(self, task: ScraperTask, result: JetPhotosResult) -> None:
        pass

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        pass
