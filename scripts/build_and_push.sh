#!/bin/bash
# Build and push Docker image to ECR
# Usage: ./scripts/build_and_push.sh [tag]

set -e

# Configuration
AWS_REGION="${AWS_REGION:-us-west-2}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_REPO="flight-matrix-scraper"
IMAGE_TAG="${1:-latest}"

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "=== Building Docker Image ==="
echo "Region: ${AWS_REGION}"
echo "Account: ${AWS_ACCOUNT_ID}"
echo "Repository: ${ECR_REPO}"
echo "Tag: ${IMAGE_TAG}"
echo ""

# Navigate to project root
cd "$(dirname "$0")/.."

# Build Docker image
echo "Building image..."
docker build -t ${ECR_REPO}:${IMAGE_TAG} -f Dockerfile.scraper .

# Tag for ECR
docker tag ${ECR_REPO}:${IMAGE_TAG} ${ECR_URI}:${IMAGE_TAG}

# Login to ECR
echo ""
echo "Logging in to ECR..."
aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR_URI}

# Create repository if it doesn't exist
echo ""
echo "Ensuring ECR repository exists..."
aws ecr describe-repositories --repository-names ${ECR_REPO} --region ${AWS_REGION} 2>/dev/null || \
    aws ecr create-repository --repository-name ${ECR_REPO} --region ${AWS_REGION}

# Push to ECR
echo ""
echo "Pushing image to ECR..."
docker push ${ECR_URI}:${IMAGE_TAG}

# Also tag and push as 'latest' if a specific tag was provided
if [ "${IMAGE_TAG}" != "latest" ]; then
    echo "Also tagging as 'latest'..."
    docker tag ${ECR_REPO}:${IMAGE_TAG} ${ECR_URI}:latest
    docker push ${ECR_URI}:latest
fi

echo ""
echo "=== Build and Push Complete ==="
echo "Image URI: ${ECR_URI}:${IMAGE_TAG}"
echo ""
echo "To deploy, run:"
echo "  cd infra/scraper && cdk deploy ScraperDockerStack"
