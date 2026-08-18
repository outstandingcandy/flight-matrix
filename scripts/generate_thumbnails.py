#!/usr/bin/env python3
"""
Generate thumbnails for aircraft images held in object storage.

The storage backend follows `DEPLOY_TARGET` (S3 on aws, GCS on gcp, the local
filesystem otherwise).

Usage:
    python scripts/generate_thumbnails.py --count          # Count images needing thumbnails
    python scripts/generate_thumbnails.py --limit 100     # Process 100 images
    python scripts/generate_thumbnails.py --all           # Process all images
    python scripts/generate_thumbnails.py --workers 4     # Use 4 parallel workers
"""

import argparse
import io
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.exceptions import StorageError
from src.storage import ObjectStorage, StorageFactory
from src.utils.yaml_config import YAMLConfig

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("thumbnail_generator")

SOURCE_PREFIX = "data/jetphotos_images/"
THUMB_PREFIX = "data/jetphotos_thumbnails/"
THUMB_SIZE = (400, 300)  # width, height
THUMB_QUALITY = 85
THUMB_CACHE_CONTROL = "public, max-age=31536000"  # 1 year cache
SOURCE_EXTENSIONS = (".jpg", ".jpeg", ".png")


class ThumbnailGenerator:
    """Resizes stored aircraft images into CDN-cacheable thumbnails."""

    def __init__(self, storage: ObjectStorage) -> None:
        """Initialize the generator.

        Args:
            storage: Object storage holding both source images and thumbnails.
        """
        self.storage = storage

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
        return {
            key.replace(THUMB_PREFIX, "").replace("_thumb", "_full")
            for key in self.storage.list_keys(THUMB_PREFIX)
        }

    def get_thumbnail_key(self, source_key: str) -> str:
        """Convert a source image key to its thumbnail key.

        Args:
            source_key: Key of the full-size image.

        Returns:
            Key the thumbnail is stored under.
        """
        filename = source_key.replace(SOURCE_PREFIX, "")
        # Replace _full with _thumb in filename
        thumb_filename = filename.replace("_full_", "_thumb_")
        return THUMB_PREFIX + thumb_filename

    def generate_thumbnail(self, source_key: str) -> bool:
        """Generate and upload the thumbnail for a single image.

        Args:
            source_key: Key of the full-size image.

        Returns:
            True when the thumbnail was written.
        """
        try:
            thumb_key = self.get_thumbnail_key(source_key)
            image_data = self.storage.download_bytes(source_key)

            img = Image.open(io.BytesIO(image_data))

            # Convert to RGB if necessary (for PNG with alpha)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # Create thumbnail maintaining aspect ratio
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=THUMB_QUALITY, optimize=True)

            self.storage.upload_bytes(
                thumb_key,
                buffer.getvalue(),
                content_type="image/jpeg",
                cache_control=THUMB_CACHE_CONTROL,
            )

            return True

        except (StorageError, OSError, ValueError) as e:
            logger.error(f"Failed to generate thumbnail for {source_key}: {e}")
            return False

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
        success = 0
        failed = 0
        total = len(images)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self.generate_thumbnail, img): img for img in images}

            for i, future in enumerate(as_completed(futures), 1):
                source_key = futures[future]
                try:
                    if future.result():
                        success += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(f"Exception processing {source_key}: {e}")
                    failed += 1

                if i % 100 == 0 or i == total:
                    logger.info(f"Progress: {i}/{total} ({success} success, {failed} failed)")

        return success, failed


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

    generator = ThumbnailGenerator(storage)

    if args.count:
        pending = generator.get_pending_images()
        print(f"\nImages pending thumbnail generation: {len(pending)}")
        return

    if not args.limit and not args.all:
        parser.print_help()
        return

    # Get pending images
    pending = generator.get_pending_images()

    if not pending:
        print("No images need thumbnail generation!")
        return

    # Limit if specified
    if args.limit:
        pending = pending[: args.limit]

    print(f"\nProcessing {len(pending)} images with {args.workers} workers...")

    success, failed = generator.process_batch(pending, workers=args.workers)

    print(f"\n{'=' * 50}")
    print("Thumbnail Generation Complete:")
    print(f"  Success: {success}")
    print(f"  Failed: {failed}")
    print(f"  Total: {len(pending)}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
