"""Sink for JetPhotos scraper — updates ``aircraft_static_info`` + ``aircraft_images``."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.jetphotos.models import (
    ImageMetadata,
    JetPhotosResult,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("scraper.sinks.jetphotos")


class JetPhotosSink:
    """Persist JetPhotos downloads into flight-matrix tables."""

    def __init__(self, database_url: str) -> None:
        self.db_engine: Any | None = None
        if database_url:
            try:
                self.db_engine = create_engine(database_url, echo=False, pool_pre_ping=True)
            except Exception as e:
                logger.error(f"Failed to initialize DB engine: {e}")

    # Callback wired into scraper config as ``persist_images_callback``
    def persist_images(
        self,
        registration: str,
        image_paths: list[str],
        images_metadata: list[ImageMetadata],
    ) -> None:
        if not self.db_engine:
            return
        if image_paths:
            self._sync_to_aircraft_static_info(registration, image_paths)
        if images_metadata:
            self._save_images_metadata(registration, images_metadata)

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
