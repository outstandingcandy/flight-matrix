#!/usr/bin/env python3
"""
Batch upload existing local images to S3.

This script uploads all images in data/jetphotos_images to S3
and updates the database paths to use S3 keys.
"""

import os
import sys
import logging
import argparse
from pathlib import Path
from typing import List, Tuple

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.image_service import ImageService

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_local_images(images_dir: str) -> List[str]:
    """Get all local image files."""
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif'}
    images = []

    for file in Path(images_dir).iterdir():
        if file.suffix.lower() in image_extensions:
            images.append(str(file))

    return sorted(images)


def batch_upload(
    config_file: str = "config.yaml",
    dry_run: bool = False,
    limit: int = 0
) -> Tuple[int, int]:
    """Upload local images to S3.

    Args:
        config_file: Path to config file
        dry_run: If True, only show what would be done
        limit: Max images to upload (0 = unlimited)

    Returns:
        Tuple of (success_count, failed_count)
    """
    service = ImageService(config_file)

    if not service.s3_enabled:
        logger.error("S3 is not enabled in configuration")
        return 0, 0

    if not service.s3_client:
        logger.error("S3 client failed to initialize")
        return 0, 0

    logger.info(f"S3 Bucket: {service.s3_bucket}")
    logger.info(f"S3 Prefix: {service.s3_prefix}")

    # Get local images
    images = get_local_images(service.images_dir)
    total = len(images)

    if limit > 0:
        images = images[:limit]

    logger.info(f"Found {total} local images, will process {len(images)}")

    if dry_run:
        logger.info("DRY RUN - no actual uploads will be performed")
        for img in images[:10]:
            filename = os.path.basename(img)
            s3_key = f"{service.s3_prefix}/{filename}"
            logger.info(f"  Would upload: {img} -> s3://{service.s3_bucket}/{s3_key}")
        if len(images) > 10:
            logger.info(f"  ... and {len(images) - 10} more")
        return 0, 0

    success_count = 0
    failed_count = 0

    for i, local_path in enumerate(images, 1):
        filename = os.path.basename(local_path)

        # Check if already on S3
        s3_key = f"{service.s3_prefix}/{filename}"
        try:
            service.s3_client.head_object(Bucket=service.s3_bucket, Key=s3_key)
            logger.debug(f"[{i}/{len(images)}] Already exists on S3: {filename}")
            success_count += 1
            continue
        except Exception:
            pass  # Not on S3, will upload

        # Upload
        result = service.upload_to_s3(local_path)
        if result:
            success_count += 1
            if i % 100 == 0:
                logger.info(f"Progress: {i}/{len(images)} uploaded ({success_count} success, {failed_count} failed)")
        else:
            failed_count += 1
            logger.warning(f"Failed to upload: {local_path}")

    logger.info(f"Upload complete: {success_count} success, {failed_count} failed")
    return success_count, failed_count


def main():
    parser = argparse.ArgumentParser(description='Upload existing images to S3')
    parser.add_argument('--config', default='config/config.yaml', help='Config file path')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done')
    parser.add_argument('--limit', type=int, default=0, help='Max images to upload (0=unlimited)')

    args = parser.parse_args()

    success, failed = batch_upload(
        config_file=args.config,
        dry_run=args.dry_run,
        limit=args.limit
    )

    if failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
