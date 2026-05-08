#!/usr/bin/env python3
"""
Generate thumbnails for aircraft images stored in S3.

Usage:
    python scripts/generate_thumbnails.py --count          # Count images needing thumbnails
    python scripts/generate_thumbnails.py --limit 100     # Process 100 images
    python scripts/generate_thumbnails.py --all           # Process all images
    python scripts/generate_thumbnails.py --workers 4     # Use 4 parallel workers
"""

import argparse
import boto3
import io
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('thumbnail_generator')

# Configuration (read from env; S3_BUCKET_NAME is required)
S3_BUCKET = os.environ.get('S3_BUCKET_NAME') or ''
S3_REGION = os.environ.get('AWS_REGION', 'us-east-1')
if not S3_BUCKET:
    raise SystemExit('S3_BUCKET_NAME must be set in the environment')
SOURCE_PREFIX = 'data/jetphotos_images/'
THUMB_PREFIX = 'data/jetphotos_thumbnails/'
THUMB_SIZE = (400, 300)  # width, height
THUMB_QUALITY = 85


class ThumbnailGenerator:
    def __init__(self):
        self.s3 = boto3.client('s3', region_name=S3_REGION)

    def list_source_images(self) -> list:
        """List all source images in S3."""
        images = []
        paginator = self.s3.get_paginator('list_objects_v2')

        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=SOURCE_PREFIX):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('.jpg') or key.endswith('.jpeg') or key.endswith('.png'):
                    images.append(key)

        return images

    def list_existing_thumbnails(self) -> set:
        """List existing thumbnails in S3."""
        thumbnails = set()
        paginator = self.s3.get_paginator('list_objects_v2')

        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=THUMB_PREFIX):
            for obj in page.get('Contents', []):
                key = obj['Key']
                # Extract original filename from thumbnail path
                filename = key.replace(THUMB_PREFIX, '').replace('_thumb', '_full')
                thumbnails.add(filename)

        return thumbnails

    def get_thumbnail_key(self, source_key: str) -> str:
        """Convert source image key to thumbnail key."""
        filename = source_key.replace(SOURCE_PREFIX, '')
        # Replace _full with _thumb in filename
        thumb_filename = filename.replace('_full_', '_thumb_')
        return THUMB_PREFIX + thumb_filename

    def generate_thumbnail(self, source_key: str) -> bool:
        """Generate and upload thumbnail for a single image."""
        try:
            thumb_key = self.get_thumbnail_key(source_key)

            # Download source image
            response = self.s3.get_object(Bucket=S3_BUCKET, Key=source_key)
            image_data = response['Body'].read()

            # Open and resize image
            img = Image.open(io.BytesIO(image_data))

            # Convert to RGB if necessary (for PNG with alpha)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            # Create thumbnail maintaining aspect ratio
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)

            # Save to buffer
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=THUMB_QUALITY, optimize=True)
            buffer.seek(0)

            # Upload to S3
            self.s3.put_object(
                Bucket=S3_BUCKET,
                Key=thumb_key,
                Body=buffer,
                ContentType='image/jpeg',
                CacheControl='public, max-age=31536000'  # 1 year cache
            )

            return True

        except Exception as e:
            logger.error(f"Failed to generate thumbnail for {source_key}: {e}")
            return False

    def get_pending_images(self) -> list:
        """Get list of images that need thumbnails."""
        logger.info("Listing source images...")
        source_images = self.list_source_images()
        logger.info(f"Found {len(source_images)} source images")

        logger.info("Listing existing thumbnails...")
        existing_thumbs = self.list_existing_thumbnails()
        logger.info(f"Found {len(existing_thumbs)} existing thumbnails")

        # Find images without thumbnails
        pending = []
        for source_key in source_images:
            filename = source_key.replace(SOURCE_PREFIX, '')
            if filename not in existing_thumbs:
                pending.append(source_key)

        return pending

    def process_batch(self, images: list, workers: int = 4) -> tuple:
        """Process a batch of images with parallel workers."""
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


def main():
    parser = argparse.ArgumentParser(description='Generate thumbnails for aircraft images')
    parser.add_argument('--count', action='store_true', help='Count images needing thumbnails')
    parser.add_argument('--limit', type=int, help='Process up to N images')
    parser.add_argument('--all', action='store_true', help='Process all pending images')
    parser.add_argument('--workers', type=int, default=8, help='Number of parallel workers (default: 8)')

    args = parser.parse_args()

    generator = ThumbnailGenerator()

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
        pending = pending[:args.limit]

    print(f"\nProcessing {len(pending)} images with {args.workers} workers...")

    success, failed = generator.process_batch(pending, workers=args.workers)

    print(f"\n{'='*50}")
    print(f"Thumbnail Generation Complete:")
    print(f"  Success: {success}")
    print(f"  Failed: {failed}")
    print(f"  Total: {len(pending)}")
    print(f"{'='*50}")


if __name__ == '__main__':
    main()
