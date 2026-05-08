#!/bin/bash
# Deploy Thumbnail Generation Lambda with S3 Trigger
# This Lambda automatically generates thumbnails when new images are uploaded to S3

set -e

REGION="${AWS_REGION:-us-east-1}"
FUNCTION_NAME="flight-matrix-thumbnail-generator"
ROLE_NAME="flight-matrix-lambda-role"
S3_BUCKET="${S3_BUCKET_NAME:?S3_BUCKET_NAME must be set in env}"
SOURCE_PREFIX="data/jetphotos_images/"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LAMBDA_DIR="$PROJECT_ROOT/lambda_thumbnail"

echo "=========================================="
echo "Deploying Thumbnail Generation Lambda"
echo "=========================================="

# Get AWS account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

echo "Account ID: $ACCOUNT_ID"
echo "Role ARN: $ROLE_ARN"
echo "S3 Bucket: $S3_BUCKET"

# Create deployment package
echo ""
echo "Creating deployment package..."
cd "$LAMBDA_DIR"

# Create a temporary directory for the package
TEMP_DIR=$(mktemp -d)
cp thumbnail_handler.py "$TEMP_DIR/"

# Add Pillow library (using the layer instead)
# Note: We'll use a Lambda layer for Pillow to keep the package small

cd "$TEMP_DIR"
zip -r deployment.zip .
mv deployment.zip "$PROJECT_ROOT/"
cd "$PROJECT_ROOT"
rm -rf "$TEMP_DIR"

echo "Deployment package created: deployment.zip"

# Check if function exists
echo ""
echo "Checking if Lambda function exists..."
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" 2>/dev/null; then
    echo "Updating existing function..."
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb://deployment.zip \
        --region "$REGION"
else
    echo "Creating new Lambda function..."

    # Use existing dependencies layer (contains Pillow)
    LAYER_ARN="arn:aws:lambda:us-east-1:${ACCOUNT_ID}:layer:flight-matrix-dependencies:1"
    echo "Using existing layer: $LAYER_ARN"

    # Create the function
    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.12 \
        --handler thumbnail_handler.lambda_handler \
        --role "$ROLE_ARN" \
        --zip-file fileb://deployment.zip \
        --timeout 30 \
        --memory-size 512 \
        --layers "$LAYER_ARN" \
        --description "Generates thumbnails for aircraft images uploaded to S3" \
        --region "$REGION"

    echo "Lambda function created"
fi

# Clean up
rm -f deployment.zip

# Add S3 trigger permission
echo ""
echo "Adding S3 trigger permission..."
aws lambda add-permission \
    --function-name "$FUNCTION_NAME" \
    --statement-id "s3-trigger-permission" \
    --action "lambda:InvokeFunction" \
    --principal s3.amazonaws.com \
    --source-arn "arn:aws:s3:::${S3_BUCKET}" \
    --source-account "$ACCOUNT_ID" \
    --region "$REGION" 2>/dev/null || echo "Permission already exists"

# Configure S3 bucket notification
echo ""
echo "Configuring S3 bucket notification..."

LAMBDA_ARN=$(aws lambda get-function \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    --query 'Configuration.FunctionArn' \
    --output text)

# Create notification configuration
cat > /tmp/s3-notification.json << EOF
{
    "LambdaFunctionConfigurations": [
        {
            "Id": "ThumbnailGeneratorTrigger",
            "LambdaFunctionArn": "${LAMBDA_ARN}",
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {
                "Key": {
                    "FilterRules": [
                        {
                            "Name": "prefix",
                            "Value": "${SOURCE_PREFIX}"
                        }
                    ]
                }
            }
        }
    ]
}
EOF

aws s3api put-bucket-notification-configuration \
    --bucket "$S3_BUCKET" \
    --notification-configuration file:///tmp/s3-notification.json \
    --region "$REGION"

rm -f /tmp/s3-notification.json

echo ""
echo "=========================================="
echo "Deployment Complete!"
echo "=========================================="
echo ""
echo "Lambda Function: $FUNCTION_NAME"
echo "S3 Bucket: $S3_BUCKET"
echo "Trigger Prefix: $SOURCE_PREFIX"
echo ""
echo "The Lambda will now automatically generate thumbnails"
echo "whenever new images are uploaded to:"
echo "  s3://${S3_BUCKET}/${SOURCE_PREFIX}"
echo ""
echo "Thumbnails will be saved to:"
echo "  s3://${S3_BUCKET}/data/jetphotos_thumbnails/"
echo ""
