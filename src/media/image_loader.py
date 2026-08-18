"""Load a stored aircraft image from object storage or the local filesystem.

`aircraft_images.image_path` is an object-storage key such as
``data/jetphotos_images/B-1234_001.jpg``. On the ``aws`` and ``gcp`` targets the
file lives only in the bucket; on ``local`` it is on disk under the working
directory. Rows also outlive target switches, so a stored path may be a full
public URL written under a previous provider.

Both the report path (`src.media.service`) and the LLM analysis path
(`src.services.aircraft_analysis_service`) need to resolve those cases the same
way, which is why the logic lives here rather than in either caller.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from src.core.exceptions import StorageError
from src.storage.base import ObjectStorage

logger = logging.getLogger("media.image_loader")

__all__ = ["load_image_bytes"]


def load_image_bytes(
    image_path: str,
    storage: ObjectStorage | None = None,
    local_dirs: Sequence[str] = (),
) -> bytes | None:
    """Read a stored image, trying object storage before the local filesystem.

    Storage is consulted first because on the cloud targets it is the only
    place the file exists, and a same-named local file would be a stale
    leftover from a previous local run.

    Args:
        image_path: The value stored in ``aircraft_images.image_path`` — an
            object key, a local path, or a full public URL.
        storage: Object storage to read from, or ``None`` to use local files
            only (the ``local`` target, or a misconfigured provider).
        local_dirs: Extra directories to look for the file's basename in, for
            paths stored before the ``data/``-prefixed key convention.

    Returns:
        The raw image bytes, or ``None`` when the image is in neither place.
        Callers get ``None`` rather than an exception because a missing photo
        must degrade the report, not fail it.
    """
    if not image_path:
        return None

    if storage is not None:
        key = storage.strip_public_prefix(image_path)
        try:
            return storage.download_bytes(key)
        except StorageError as e:
            # Includes ObjectNotFoundError: fall through to the local disk,
            # which is where a locally-scraped image that was never uploaded
            # would be.
            logger.debug("Image %s not readable from object storage: %s", key, e)

    return _load_local(image_path, local_dirs)


def _load_local(image_path: str, local_dirs: Sequence[str]) -> bytes | None:
    """Read the first of several candidate local paths that exists.

    Args:
        image_path: Stored path, used as-is and relative to the working
            directory.
        local_dirs: Directories to try the basename in.

    Returns:
        The file's bytes, or ``None`` if no candidate could be read.
    """
    basename = os.path.basename(image_path)
    candidates = [
        image_path,
        *(os.path.join(d, basename) for d in local_dirs),
        os.path.join(os.getcwd(), image_path),
    ]

    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "rb") as f:
                return f.read()
        except OSError as e:
            logger.error("Failed to read local image (%s): %s", candidate, e)

    logger.warning("Image not found in object storage or locally: %s", image_path)
    return None
