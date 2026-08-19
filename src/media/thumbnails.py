"""Thumbnail generation for stored aircraft images, on any deployment target.

Thumbnails are not optional decoration: the aircraft galleries, the flight
schedule rows and the aircraft detail page all request the *thumbnail* URL, and
they derive it in the browser by string-rewriting the full-size key
(``web_static/js/app.js``, ``web_static/js/aircraft_detail.js``,
``web_templates/flight_schedules.html``). Nothing server-side tells the page
whether that object exists, so an image with no thumbnail is a broken ``<img>``
rather than a fall back to the full-size photo.

Until now the only paths that produced one were both AWS-specific: the
``lambda_thumbnail`` function generating on demand behind CloudFront, and
``scripts/generate_thumbnails.py`` run by hand as a backfill. On the ``gcp`` and
``local`` targets a freshly scraped image therefore had no thumbnail at all
until somebody remembered to run the script.

This module is the vendor-neutral replacement. It depends only on
:class:`~src.storage.base.ObjectStorage`, so the same code writes to S3, GCS or
the local filesystem, and it owns the single definition of the naming, size,
quality and cache-control that the frontend, the Lambda and the backfill script
all have to agree on.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from src.core.exceptions import StorageError
from src.media.image_loader import load_image_bytes
from src.storage.base import ObjectStorage

logger = logging.getLogger("media.thumbnails")

__all__ = [
    "SOURCE_EXTENSIONS",
    "SOURCE_PREFIX",
    "THUMB_CACHE_CONTROL",
    "THUMB_PREFIX",
    "THUMB_QUALITY",
    "THUMB_SIZE",
    "ThumbnailService",
    "canonical_source_key",
    "render_thumbnail",
    "source_name_from_thumbnail_key",
    "thumbnail_key",
]

# Key prefixes. Both halves of the rewrite have to match what the frontend does,
# so neither can be made configurable without changing the templates too.
SOURCE_PREFIX = "data/jetphotos_images/"
THUMB_PREFIX = "data/jetphotos_thumbnails/"

# Bounding box, not an exact size: `Image.thumbnail` preserves aspect ratio, so
# a 3:2 photo comes out 400x267.
THUMB_SIZE = (400, 300)
THUMB_QUALITY = 85

# A thumbnail key contains the source image's timestamp, so a given key's
# content never changes and it can be cached for a year.
THUMB_CACHE_CONTROL = "public, max-age=31536000"

SOURCE_EXTENSIONS = (".jpg", ".jpeg", ".png")

# Thumbnails are always JPEG regardless of the source format, because the key
# the frontend derives keeps the source extension while the bytes behind it are
# re-encoded. Serving them as image/jpeg is what browsers actually get.
THUMB_CONTENT_TYPE = "image/jpeg"


def canonical_source_key(stored_path: str) -> str | None:
    """Reduce a stored image path to the object key it lives under.

    ``aircraft_images.image_path`` holds whatever the writer of the row put
    there: usually a bare key, but also full public URLs from a previous
    provider and absolute local paths from local runs. Cutting at
    :data:`SOURCE_PREFIX` normalises all three, including CDN hosts that
    :meth:`ObjectStorage.strip_public_prefix` cannot recognise because they are
    not a provider's own URL shape.

    Args:
        stored_path: Key, URL or local path of a full-size image.

    Returns:
        The object key, or ``None`` when the path is not a jetphotos image —
        another prefix, or a non-image file such as the scraped HTML stored
        alongside. Neither has a thumbnail the frontend would ever request, so
        generating one would store an object nothing reads.
    """
    index = stored_path.find(SOURCE_PREFIX)
    if index < 0:
        return None
    key = stored_path[index:]
    if not key.lower().endswith(SOURCE_EXTENSIONS):
        return None
    return key


def thumbnail_key(source_key: str) -> str | None:
    """Return the key a source image's thumbnail is stored under.

    Mirrors the rewrite the frontend performs, in the same order: swap the
    prefix, then ``_full_`` for ``_thumb_``. A filename without ``_full_`` is
    left alone by both sides, which still agree.

    Args:
        source_key: Key, URL or local path of the full-size image, for example
            ``data/jetphotos_images/B-1234_full_1772277610.jpg``.

    Returns:
        The thumbnail key, or ``None`` for anything
        :func:`canonical_source_key` does not recognise as a source image.
    """
    key = canonical_source_key(source_key)
    if key is None:
        return None
    return key.replace(SOURCE_PREFIX, THUMB_PREFIX).replace("_full_", "_thumb_")


def source_name_from_thumbnail_key(thumb_key: str) -> str:
    """Return the source filename a thumbnail key was derived from.

    The inverse of :func:`thumbnail_key`, used to diff the two prefixes when
    backfilling in bulk — listing both prefixes once is far cheaper than an
    existence check per image.

    Args:
        thumb_key: Key of a stored thumbnail.

    Returns:
        The bare filename of the source image, without :data:`SOURCE_PREFIX`.
    """
    return thumb_key.replace(THUMB_PREFIX, "").replace("_thumb", "_full")


def render_thumbnail(
    image_bytes: bytes,
    size: tuple[int, int] = THUMB_SIZE,
    quality: int = THUMB_QUALITY,
) -> bytes:
    """Resize an image into JPEG thumbnail bytes.

    Args:
        image_bytes: Encoded source image, in any format Pillow can read.
        size: Bounding box the result is fitted inside, preserving aspect ratio.
        quality: JPEG quality.

    Returns:
        The encoded JPEG thumbnail.

    Raises:
        OSError: If the bytes are not a decodable image, or encoding fails.
        ValueError: If Pillow rejects the requested size or quality.
    """
    img: Image.Image = Image.open(io.BytesIO(image_bytes))

    # JPEG has no alpha channel and no palette. "L" is left alone because a
    # greyscale JPEG is valid and smaller than its RGB copy.
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    img.thumbnail(size, Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


class ThumbnailService:
    """Writes thumbnails for stored aircraft images through object storage.

    Args:
        storage: Destination for the thumbnails, and the fallback source for
            images that are not on this host's disk.
        local_dirs: Directories to look for a source image's basename in,
            passed through to :func:`~src.media.image_loader.load_image_bytes`.
        prefer_local: Read the source from disk before object storage. Set this
            on the ingestion path, where the scraper has just written the file
            locally and the bucket copy — if the upload even happened — is a
            copy of it. Leave it off when working against a bucket the local
            host has no copy of.
    """

    def __init__(
        self,
        storage: ObjectStorage,
        local_dirs: Sequence[str] = (),
        prefer_local: bool = False,
    ) -> None:
        self.storage = storage
        self.local_dirs = tuple(local_dirs)
        self.prefer_local = prefer_local

    def ensure_thumbnail(self, source_key: str, skip_existing: bool = True) -> str | None:
        """Generate and upload one image's thumbnail unless it already exists.

        Args:
            source_key: Object key — or public URL, or local path — of the
                full-size image, as stored in ``aircraft_images.image_path``.
            skip_existing: Check storage first and do nothing if the thumbnail
                is already there. Turn this off for a bulk run that has already
                diffed the two prefixes, so it does not pay for a second check
                per image.

        Returns:
            The key the thumbnail is stored under, or ``None`` when the source
            has no derivable thumbnail key, its bytes could not be read, or the
            upload failed. Failures are logged rather than raised: a missing
            thumbnail costs one broken image on one page, and must not fail the
            scrape or the database write that triggered it.
        """
        key = canonical_source_key(source_key)
        thumb_key = thumbnail_key(key) if key else None
        if key is None or thumb_key is None:
            logger.debug("No thumbnail convention for %s; skipping", source_key)
            return None

        if skip_existing:
            try:
                if self.storage.exists(thumb_key):
                    logger.debug("Thumbnail already present: %s", thumb_key)
                    return thumb_key
            except StorageError as e:
                # Could not tell either way; regenerating is cheap and
                # idempotent, whereas skipping would leave a broken image.
                logger.debug("Could not check %s, regenerating: %s", thumb_key, e)

        image_bytes = load_image_bytes(
            key,
            self.storage,
            self.local_dirs,
            prefer_local=self.prefer_local,
        )
        if image_bytes is None:
            logger.warning("No source bytes for %s; no thumbnail written", key)
            return None

        try:
            data = render_thumbnail(image_bytes)
        except (OSError, ValueError) as e:
            logger.error("Failed to render thumbnail for %s: %s", key, e)
            return None

        try:
            self.storage.upload_bytes(
                thumb_key,
                data,
                content_type=THUMB_CONTENT_TYPE,
                cache_control=THUMB_CACHE_CONTROL,
            )
        except StorageError as e:
            logger.error("Failed to upload thumbnail %s: %s", thumb_key, e)
            return None

        logger.info("Wrote thumbnail %s (%d bytes)", thumb_key, len(data))
        return thumb_key

    def ensure_thumbnails(
        self,
        source_keys: Iterable[str],
        skip_existing: bool = True,
        workers: int = 4,
    ) -> tuple[int, int]:
        """Generate thumbnails for many images concurrently.

        The work is network-bound at both ends and the resize releases the GIL,
        so threads are the right shape for it.

        Args:
            source_keys: Object keys of the full-size images.
            skip_existing: See :meth:`ensure_thumbnail`.
            workers: Thread pool size.

        Returns:
            Tuple of (written, failed). "Written" includes thumbnails found to
            be already present when ``skip_existing`` is set.
        """
        keys = list(source_keys)
        written = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.ensure_thumbnail, key, skip_existing): key for key in keys
            }
            for done, future in enumerate(as_completed(futures), 1):
                try:
                    if future.result():
                        written += 1
                    else:
                        failed += 1
                except Exception as e:
                    # A thread must not take the whole batch down with it.
                    logger.error("Thumbnail worker failed for %s: %s", futures[future], e)
                    failed += 1

                if done % 100 == 0 or done == len(keys):
                    logger.info(
                        "Thumbnails: %d/%d (%d written, %d failed)",
                        done,
                        len(keys),
                        written,
                        failed,
                    )

        return written, failed
