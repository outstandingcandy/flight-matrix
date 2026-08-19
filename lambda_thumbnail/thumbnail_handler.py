"""
S3 Trigger Lambda for Thumbnail Generation

This Lambda function is triggered by S3 PUT events when new images are uploaded
to the jetphotos_images folder. It automatically generates thumbnails and uploads
them to the jetphotos_thumbnails folder.

Trigger: S3 PUT events on data/jetphotos_images/*

This handler deliberately talks to boto3 directly rather than going through
`src.storage`: `scripts/deploy_thumbnail_lambda.sh` zips this single file, so
the deployment package has no `src/` tree, and the function only ever runs on
the aws deployment target behind an S3 event notification.

It is now a redundant second producer rather than the only one. `src.media.
thumbnails` holds the vendor-neutral definition, and every target generates the
thumbnail at ingestion time in `JetPhotosSink`, so this function normally finds
its work already done, and skips it on the head_object check below. Keeping it costs
nothing and still covers objects that reach the bucket by some other route.
Because it is a copy, keep it in sync with `src/media/thumbnails.py`: THUMB_SIZE,
THUMB_QUALITY, the `_full_` -> `_thumb_` key mapping, and the Cache-Control
header must match.
"""

import io
import os
import logging
import urllib.parse
import boto3
from PIL import Image

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration
THUMB_SIZE = (400, 300)  # width, height
THUMB_QUALITY = 85
SOURCE_PREFIX = 'data/jetphotos_images/'
THUMB_PREFIX = 'data/jetphotos_thumbnails/'

# Initialize S3 client
s3_client = boto3.client('s3')


def get_thumbnail_key(source_key: str) -> str:
    """Convert source image key to thumbnail key.

    Examples:
        data/jetphotos_images/N12345_full_123.jpg -> data/jetphotos_thumbnails/N12345_thumb_123.jpg
    """
    # Extract filename from source key
    filename = source_key.replace(SOURCE_PREFIX, '')

    # Replace _full_ with _thumb_ in filename
    thumb_filename = filename.replace('_full_', '_thumb_')

    # If no _full_ in filename, add _thumb suffix before extension
    if thumb_filename == filename:
        name, ext = os.path.splitext(filename)
        thumb_filename = f"{name}_thumb{ext}"

    return THUMB_PREFIX + thumb_filename


def generate_thumbnail(bucket: str, source_key: str) -> bool:
    """Generate and upload thumbnail for the source image.

    Args:
        bucket: S3 bucket name
        source_key: S3 key of the source image

    Returns:
        True if successful, False otherwise
    """
    try:
        thumb_key = get_thumbnail_key(source_key)

        # Check if thumbnail already exists
        try:
            s3_client.head_object(Bucket=bucket, Key=thumb_key)
            logger.info(f"Thumbnail already exists: {thumb_key}")
            return True
        except s3_client.exceptions.ClientError as e:
            if e.response['Error']['Code'] != '404':
                raise
            # Thumbnail doesn't exist, proceed to generate

        # Download source image
        logger.info(f"Downloading source image: s3://{bucket}/{source_key}")
        response = s3_client.get_object(Bucket=bucket, Key=source_key)
        image_data = response['Body'].read()

        # Open and resize image
        img = Image.open(io.BytesIO(image_data))

        # JPEG has no alpha channel and no palette; "L" stays as a smaller
        # greyscale JPEG. Same condition as src/media/thumbnails.py.
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')

        # Create thumbnail maintaining aspect ratio
        img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)

        # Save to buffer
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=THUMB_QUALITY, optimize=True)
        buffer.seek(0)

        # Upload thumbnail to S3
        s3_client.put_object(
            Bucket=bucket,
            Key=thumb_key,
            Body=buffer,
            ContentType='image/jpeg',
            CacheControl='public, max-age=31536000'  # 1 year cache
        )

        logger.info(f"Thumbnail generated and uploaded: s3://{bucket}/{thumb_key}")
        return True

    except Exception as e:
        logger.error(f"Failed to generate thumbnail for {source_key}: {e}")
        return False


def lambda_handler(event, context):
    """Lambda handler for S3 PUT events.

    Expected event format (S3 notification):
    {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "bucket-name"},
                    "object": {"key": "data/jetphotos_images/N12345_full_123.jpg"}
                }
            }
        ]
    }
    """
    logger.info(f"Received event: {event}")

    results = {
        'processed': 0,
        'success': 0,
        'failed': 0,
        'skipped': 0
    }

    for record in event.get('Records', []):
        # Get S3 bucket and key from the event
        s3_info = record.get('s3', {})
        bucket = s3_info.get('bucket', {}).get('name')
        key = s3_info.get('object', {}).get('key')

        if not bucket or not key:
            logger.warning(f"Invalid record, missing bucket or key: {record}")
            continue

        # URL decode the key (S3 events URL-encode special characters)
        key = urllib.parse.unquote_plus(key)

        # Skip if not an image file
        if not key.lower().endswith(('.jpg', '.jpeg', '.png')):
            logger.info(f"Skipping non-image file: {key}")
            results['skipped'] += 1
            continue

        # Skip if not in the source prefix
        if not key.startswith(SOURCE_PREFIX):
            logger.info(f"Skipping file outside source prefix: {key}")
            results['skipped'] += 1
            continue

        # Skip if this is already a thumbnail (avoid infinite loop)
        if THUMB_PREFIX in key or '_thumb_' in key or '_thumb.' in key:
            logger.info(f"Skipping thumbnail file: {key}")
            results['skipped'] += 1
            continue

        results['processed'] += 1

        if generate_thumbnail(bucket, key):
            results['success'] += 1
        else:
            results['failed'] += 1

    logger.info(f"Processing complete: {results}")

    return {
        'statusCode': 200,
        'body': results
    }
