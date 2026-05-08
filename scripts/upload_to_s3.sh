#!/bin/bash
# Upload static files and aircraft images to S3
# Run this AFTER sam deploy creates the S3 bucket

set -e  # Exit on error

echo "==============================================="
echo "Uploading Static Files to S3"
echo "==============================================="

# Check if AWS_ACCOUNT_ID is set
if [ -z "$AWS_ACCOUNT_ID" ]; then
    echo "Getting AWS Account ID..."
    export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    echo "AWS Account ID: $AWS_ACCOUNT_ID"
fi

# Configuration
BUCKET_NAME="flight-matrix-static-${AWS_ACCOUNT_ID}"
REGION="${AWS_REGION:-us-west-2}"

echo "Bucket: $BUCKET_NAME"
echo "Region: $REGION"
echo ""

# Check if bucket exists
echo "Checking if S3 bucket exists..."
if aws s3 ls "s3://${BUCKET_NAME}" 2>&1 | grep -q 'NoSuchBucket'; then
    echo "Bucket does not exist. Creating bucket..."
    aws s3 mb "s3://${BUCKET_NAME}" --region "${REGION}"
    echo "✓ Bucket created"
else
    echo "✓ Bucket already exists"
fi

# Upload static files (CSS, JS, fonts)
if [ -d "web_static" ]; then
    echo ""
    echo "Uploading web_static/ directory..."
    aws s3 sync web_static/ "s3://${BUCKET_NAME}/static/" \
        --exclude "*.pyc" \
        --exclude "__pycache__/*" \
        --exclude ".DS_Store" \
        --cache-control "public, max-age=31536000" \
        --region "${REGION}" \
        --delete

    echo "✓ Static files uploaded with 1-year cache"
else
    echo "WARNING: web_static/ directory not found, skipping static files upload"
fi

# Upload data directory (aircraft images - 3.9GB)
if [ -d "data" ]; then
    echo ""
    echo "Uploading data/ directory (aircraft images - this may take several minutes)..."
    echo "Progress:"

    # Upload with progress indication (exclude maps - they are embedded in emails)
    aws s3 sync data/ "s3://${BUCKET_NAME}/data/" \
        --exclude "*.log" \
        --exclude "*.tmp" \
        --exclude "__pycache__/*" \
        --exclude ".DS_Store" \
        --exclude "maps/*" \
        --cache-control "public, max-age=86400" \
        --region "${REGION}"

    echo "✓ Data files uploaded with 24-hour cache"
else
    echo "WARNING: data/ directory not found, skipping data upload"
fi

# Get bucket info
echo ""
echo "==============================================="
echo "Upload Complete!"
echo "==============================================="
BUCKET_SIZE=$(aws s3 ls "s3://${BUCKET_NAME}" --recursive --summarize --human-readable 2>/dev/null | grep "Total Size" | awk '{print $3, $4}')
OBJECT_COUNT=$(aws s3 ls "s3://${BUCKET_NAME}" --recursive --summarize 2>/dev/null | grep "Total Objects" | awk '{print $3}')

echo "Bucket: s3://${BUCKET_NAME}"
echo "Total Size: ${BUCKET_SIZE:-unknown}"
echo "Total Objects: ${OBJECT_COUNT:-unknown}"
echo ""
echo "Files are now accessible via:"
echo "  - S3: https://${BUCKET_NAME}.s3.amazonaws.com/"
echo "  - CloudFront: (check SAM outputs for CloudFront domain)"
echo ""
echo "Next steps:"
echo "  1. Update CloudFront distribution if needed"
echo "  2. Test static file access from your application"
echo "==============================================="
