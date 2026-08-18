#!/usr/bin/env python3
"""
Batch upload existing local aircraft images to object storage.

Uploads every image under `image_download.images_dir` to the storage provider
selected by `DEPLOY_TARGET` (S3 on aws, GCS on gcp, the local filesystem
otherwise), keyed under `image_download.s3.prefix` so the resulting keys match
what the scraper writes.

Usage:
    # Preview
    python scripts/upload_images_to_object_storage.py --dry-run

    # Upload everything
    python scripts/upload_images_to_object_storage.py

    # Upload the first 100 images only
    python scripts/upload_images_to_object_storage.py --limit 100
"""

import argparse
import logging
import mimetypes
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.exceptions import StorageError
from src.storage import ObjectStorage, StorageFactory
from src.utils.yaml_config import YAMLConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("upload_images")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def get_local_images(images_dir: str) -> list[Path]:
    """Collect local image files.

    Args:
        images_dir: Directory to scan (non-recursively).

    Returns:
        Sorted list of image paths; empty when the directory is missing.
    """
    directory = Path(images_dir)
    if not directory.is_dir():
        logger.error(f"Images directory not found: {images_dir}")
        return []

    return sorted(p for p in directory.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS)


def upload_one(storage: ObjectStorage, local_path: Path, key: str) -> bool:
    """Upload a single image unless it is already stored.

    Args:
        storage: Target object storage.
        local_path: Local image file.
        key: Destination object key.

    Returns:
        True when the object is present after the call.
    """
    try:
        if storage.exists(key):
            logger.debug(f"Already stored: {key}")
            return True

        content_type = mimetypes.guess_type(local_path.name)[0]
        storage.upload_bytes(key, local_path.read_bytes(), content_type=content_type)
        return True
    except (StorageError, OSError) as e:
        logger.warning(f"Failed to upload {local_path}: {e}")
        return False


def batch_upload(
    config_file: str = "config/config.yaml",
    dry_run: bool = False,
    limit: int = 0,
) -> tuple[int, int]:
    """Upload local images to object storage.

    Args:
        config_file: Path to the config file.
        dry_run: When True, log the planned uploads and change nothing.
        limit: Maximum number of images to process (0 = unlimited).

    Returns:
        Tuple of (success_count, failed_count).
    """
    yaml_config = YAMLConfig(config_file)

    images_dir = yaml_config.get("image_download.images_dir", "data/jetphotos_images")
    prefix = yaml_config.get("image_download.s3.prefix", "data/jetphotos_images").strip("/")
    bucket = yaml_config.get("image_download.s3.bucket", "")

    try:
        storage = StorageFactory.create(yaml_config, bucket=bucket or None)
    except StorageError as e:
        logger.error(f"Object storage unavailable: {e}")
        return 0, 0

    images = get_local_images(images_dir)
    total = len(images)
    if limit > 0:
        images = images[:limit]

    logger.info(
        f"Storage: {type(storage).__name__} | prefix: {prefix} | "
        f"found {total} local images, processing {len(images)}"
    )

    if dry_run:
        logger.info("DRY RUN - no uploads will be performed")
        for image in images[:10]:
            logger.info(f"  Would upload: {image} -> {prefix}/{image.name}")
        if len(images) > 10:
            logger.info(f"  ... and {len(images) - 10} more")
        return 0, 0

    success_count = 0
    failed_count = 0

    for i, local_path in enumerate(images, 1):
        if upload_one(storage, local_path, f"{prefix}/{local_path.name}"):
            success_count += 1
        else:
            failed_count += 1

        if i % 100 == 0:
            logger.info(
                f"Progress: {i}/{len(images)} ({success_count} success, {failed_count} failed)"
            )

    logger.info(f"Upload complete: {success_count} success, {failed_count} failed")
    return success_count, failed_count


def main() -> None:
    """Parse arguments and run the batch upload."""
    parser = argparse.ArgumentParser(description="Upload existing images to object storage")
    parser.add_argument("--config", default="config/config.yaml", help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--limit", type=int, default=0, help="Max images to upload (0=unlimited)")

    args = parser.parse_args()

    _, failed = batch_upload(config_file=args.config, dry_run=args.dry_run, limit=args.limit)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
