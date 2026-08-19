#!/usr/bin/env python3
"""
Backfill thumbnails for aircraft images held in object storage.

New images get their thumbnail at ingestion time, in `JetPhotosSink`. This
script is the catch-up pass for images stored before that existed, or whose
thumbnail failed to write. The generation itself lives in `src.media.thumbnails`
so that both paths agree on naming, size, quality and cache-control.

The storage backend follows `DEPLOY_TARGET` (S3 on aws, GCS on gcp, the local
filesystem otherwise).

Usage:
    python scripts/generate_thumbnails.py --count          # Count images needing thumbnails
    python scripts/generate_thumbnails.py --limit 100     # Process 100 images
    python scripts/generate_thumbnails.py --all           # Process all images
    python scripts/generate_thumbnails.py --workers 4     # Use 4 parallel workers
"""

import argparse
import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.exceptions import StorageError
from src.media.thumbnails import (
    SOURCE_EXTENSIONS,
    SOURCE_PREFIX,
    THUMB_PREFIX,
    ThumbnailService,
    source_name_from_thumbnail_key,
)
from src.storage import ObjectStorage, StorageFactory
from src.utils.yaml_config import YAMLConfig

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("thumbnail_generator")


class ThumbnailBackfill:
    """Finds stored aircraft images with no thumbnail and generates them."""

    def __init__(self, storage: ObjectStorage) -> None:
        """Initialize the backfill.

        Args:
            storage: Object storage holding both source images and thumbnails.
        """
        self.storage = storage
        self.thumbnails = ThumbnailService(storage)

    def list_source_images(self) -> list[str]:
        """List all source image keys.

        Returns:
            Keys of every source image with a supported extension.
        """
        return [
            key for key in self.storage.list_keys(SOURCE_PREFIX) if key.endswith(SOURCE_EXTENSIONS)
        ]

    def list_existing_thumbnails(self) -> set[str]:
        """List the source filenames that already have a thumbnail.

        Returns:
            Source filenames (thumbnail names mapped back to `_full`).
        """
        return {source_name_from_thumbnail_key(key) for key in self.storage.list_keys(THUMB_PREFIX)}

    def get_pending_images(self) -> list[str]:
        """Find images that have no thumbnail yet.

        Returns:
            Source keys still needing a thumbnail.
        """
        logger.info("Listing source images...")
        source_images = self.list_source_images()
        logger.info(f"Found {len(source_images)} source images")

        logger.info("Listing existing thumbnails...")
        existing_thumbs = self.list_existing_thumbnails()
        logger.info(f"Found {len(existing_thumbs)} existing thumbnails")

        # Find images without thumbnails
        pending = []
        for source_key in source_images:
            filename = source_key.replace(SOURCE_PREFIX, "")
            if filename not in existing_thumbs:
                pending.append(source_key)

        return pending

    def process_batch(self, images: list[str], workers: int = 4) -> tuple[int, int]:
        """Process a batch of images with parallel workers.

        Args:
            images: Source keys to process.
            workers: Thread pool size.

        Returns:
            Tuple of (success_count, failed_count).
        """
        # skip_existing is off: `get_pending_images` has already diffed the two
        # prefixes, so a per-image existence check would only add a request each.
        return self.thumbnails.ensure_thumbnails(images, skip_existing=False, workers=workers)


def main() -> None:
    """Parse arguments and run thumbnail generation."""
    parser = argparse.ArgumentParser(description="Generate thumbnails for aircraft images")
    parser.add_argument("--count", action="store_true", help="Count images needing thumbnails")
    parser.add_argument("--limit", type=int, help="Process up to N images")
    parser.add_argument("--all", action="store_true", help="Process all pending images")
    parser.add_argument("--config", default="config/config.yaml", help="Config file path")
    parser.add_argument(
        "--workers", type=int, default=8, help="Number of parallel workers (default: 8)"
    )

    args = parser.parse_args()

    try:
        storage = StorageFactory.create(YAMLConfig(args.config))
    except StorageError as e:
        raise SystemExit(f"Object storage unavailable: {e}") from e

    backfill = ThumbnailBackfill(storage)

    if args.count:
        pending = backfill.get_pending_images()
        print(f"\nImages pending thumbnail generation: {len(pending)}")
        return

    if not args.limit and not args.all:
        parser.print_help()
        return

    # Get pending images
    pending = backfill.get_pending_images()

    if not pending:
        print("No images need thumbnail generation!")
        return

    # Limit if specified
    if args.limit:
        pending = pending[: args.limit]

    print(f"\nProcessing {len(pending)} images with {args.workers} workers...")

    success, failed = backfill.process_batch(pending, workers=args.workers)

    print(f"\n{'=' * 50}")
    print("Thumbnail Generation Complete:")
    print(f"  Success: {success}")
    print(f"  Failed: {failed}")
    print(f"  Total: {len(pending)}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
