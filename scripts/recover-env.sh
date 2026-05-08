#!/bin/bash
# Recover .env file from AWS resources
# Usage: ./scripts/recover-env.sh > .env

set -e

echo "# Flight Matrix Environment Configuration"
echo "# Recovered from AWS on $(date)"
echo ""

# Get Lambda environment variables (contains DB_URL, S3, CloudFront)
echo "# === From Lambda ==="
LAMBDA_ENV=$(aws lambda get-function-configuration \
    --function-name flight-matrix-unified-prod \
    --region us-east-1 \
    --query 'Environment.Variables' \
    --output json 2>/dev/null)

if [ -n "$LAMBDA_ENV" ]; then
    # Extract DATABASE_URL and parse it
    DB_URL=$(echo "$LAMBDA_ENV" | jq -r '.DATABASE_URL // empty')
    if [ -n "$DB_URL" ]; then
        # Parse: postgresql+psycopg2://user:pass@host:port/db
        DB_PASSWORD=$(echo "$DB_URL" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')
        DB_HOST=$(echo "$DB_URL" | sed -n 's|.*@\([^:]*\):.*|\1|p')
        echo "DB_PASSWORD=$DB_PASSWORD"
        echo "DB_HOST=$DB_HOST"
    fi

    S3_BUCKET=$(echo "$LAMBDA_ENV" | jq -r '.S3_BUCKET_NAME // empty')
    CF_DOMAIN=$(echo "$LAMBDA_ENV" | jq -r '.CLOUDFRONT_DOMAIN // empty')

    [ -n "$S3_BUCKET" ] && echo "S3_BUCKET_NAME=$S3_BUCKET"
    [ -n "$CF_DOMAIN" ] && echo "CLOUDFRONT_DOMAIN=$CF_DOMAIN"
fi

echo ""
echo "# === Infrastructure IDs ==="

# VPC
VPC_ID=$(aws ec2 describe-vpcs \
    --filters "Name=tag:Name,Values=*FlightMatrix*" \
    --query 'Vpcs[0].VpcId' \
    --output text 2>/dev/null)
[ "$VPC_ID" != "None" ] && [ -n "$VPC_ID" ] && echo "VPC_ID=$VPC_ID"

# CloudFront Distribution ID
CF_DIST_ID=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?contains(Origins.Items[0].DomainName, 'flight-matrix')].Id | [0]" \
    --output text 2>/dev/null)
[ "$CF_DIST_ID" != "None" ] && [ -n "$CF_DIST_ID" ] && echo "CLOUDFRONT_DISTRIBUTION_ID=$CF_DIST_ID"

# RDS Endpoint
DB_ENDPOINT=$(aws rds describe-db-clusters \
    --query "DBClusters[?contains(DBClusterIdentifier, 'flight-matrix')].Endpoint | [0]" \
    --region us-east-1 \
    --output text 2>/dev/null)
[ "$DB_ENDPOINT" != "None" ] && [ -n "$DB_ENDPOINT" ] && echo "DB_ENDPOINT=$DB_ENDPOINT"

echo ""
echo "# === API Keys (MANUAL ENTRY REQUIRED) ==="
echo "# These cannot be recovered from AWS - check your password manager"
echo "ADSB_API_KEY=<your-adsb-api-key>"
echo "TAVILY_API_KEY=<your-tavily-api-key>"

echo ""
echo "# === Subnet IDs (run manually if needed) ==="
echo "# aws ec2 describe-subnets --filters 'Name=vpc-id,Values=$VPC_ID' --query 'Subnets[].SubnetId'"
