#!/bin/bash
#
# Flight Matrix Unified Deployment Script
#
# Commands:
#   ./deploy.sh deploy      # Full deployment (first time or update)
#   ./deploy.sh update      # Code update (Lambda + Workers + S3)
#   ./deploy.sh destroy     # Destroy all resources
#   ./deploy.sh status      # View deployment status
#   ./deploy.sh synth       # Generate CloudFormation template
#   ./deploy.sh diff        # View changes
#   ./deploy.sh fetch-jwks  # Fetch JWKS for offline JWT verification
#
# Configuration:
#   Set environment variables in .env file or export them directly
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Configuration
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_APP="python3 cdk_app.py"
# Prefer .env.prod (stage-aware layout); fall back to legacy .env.
if [ -f "${PROJECT_ROOT}/.env.prod" ]; then
    ENV_FILE="${PROJECT_ROOT}/.env.prod"
else
    ENV_FILE="${PROJECT_ROOT}/.env"
fi
OUTPUTS_FILE="${PROJECT_ROOT}/cdk-outputs.json"

# Existing infrastructure is discovered from `.env` at runtime via
# $S3_BUCKET_NAME and $CLOUDFRONT_DISTRIBUTION_ID. Those are referenced inside
# the functions that need them, after load_env() has sourced the env file.

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${CYAN}[STEP]${NC} $1"
}

# Show usage
show_usage() {
    echo "Flight Matrix Unified Deployment"
    echo ""
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  deploy       Full deployment (creates or updates all resources)"
    echo "  update       Quick update (Lambda + Workers + S3 static files)"
    echo ""
    echo -e "  ${CYAN}webapp${NC}        Update webapp only (Lambda + S3 + CloudFront)"
    echo -e "  ${CYAN}webapp-env${NC}    Update Lambda env vars only (fast, no rebuild)"
    echo -e "  ${CYAN}scraper${NC}       Update scraper workers only (ASG refresh)"
    echo ""
    echo "  destroy      Destroy all resources (requires confirmation)"
    echo "  status       Show deployment status and outputs"
    echo "  synth        Generate CloudFormation template"
    echo "  diff         Show pending changes"
    echo "  create-user  Create a Cognito user and assign to groups"
    echo "  fetch-jwks   Fetch JWKS from Cognito and add to .env file"
    echo ""
    echo "Environment Variables (.env file):"
    echo "  DB_PASSWORD              Database password (required, min 16 chars)"
    echo "  DB_USERNAME              Database username (default: aircraft_admin)"
    echo "  DB_NAME                  Database name (default: aircraft_data)"
    echo "  ENVIRONMENT              Deployment environment (default: prod)"
    echo "  AWS_REGION               AWS region (default: us-east-1)"
    echo "  APP_DOMAIN               Custom domain for URL generation (e.g. example.com)"
    echo "  SCRAPER_MIN_CAPACITY     Min scraper instances (default: 1)"
    echo "  SCRAPER_MAX_CAPACITY     Max scraper instances (default: 5)"
    echo "  SCRAPER_DESIRED_CAPACITY Desired scraper instances (default: 2)"
    echo ""
    echo "Cognito Authentication (set ENABLE_COGNITO_AUTH=true to create):"
    echo "  ENABLE_COGNITO_AUTH      Set to 'true' to create Cognito resources"
    echo "  COGNITO_DOMAIN_PREFIX    Cognito domain prefix (default: flight-matrix-{env})"
    echo "  COGNITO_JWKS             JWKS JSON for offline JWT verification (use fetch-jwks)"
    echo "  FLASK_SECRET_KEY         Flask session secret key"
    echo ""
}

# Check prerequisites
check_prerequisites() {
    log_step "Checking prerequisites..."

    local missing_tools=()

    command -v aws >/dev/null 2>&1 || missing_tools+=("aws-cli")
    command -v cdk >/dev/null 2>&1 || missing_tools+=("aws-cdk")
    command -v docker >/dev/null 2>&1 || missing_tools+=("docker")
    command -v python3 >/dev/null 2>&1 || missing_tools+=("python3")

    if [ ${#missing_tools[@]} -ne 0 ]; then
        log_error "Missing required tools: ${missing_tools[*]}"
        echo ""
        echo "Installation:"
        echo "  aws-cli:  pip install awscli"
        echo "  aws-cdk:  npm install -g aws-cdk"
        echo "  docker:   https://docs.docker.com/get-docker/"
        exit 1
    fi

    # Check Docker daemon
    if ! docker info >/dev/null 2>&1; then
        log_error "Docker is not running or permission denied"
        exit 1
    fi

    # Check AWS credentials
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        log_error "AWS credentials not configured"
        echo "Run 'aws configure' to set up credentials"
        exit 1
    fi

    log_success "Prerequisites OK"
}

# Load environment variables
load_env() {
    log_step "Loading environment..."

    if [ -f "$ENV_FILE" ]; then
        log_info "Loading from $ENV_FILE"
        set -a
        # shellcheck source=/dev/null
        source "$ENV_FILE"
        set +a
    else
        log_warning ".env file not found, using environment variables"
    fi

    # Set defaults
    export AWS_REGION="${AWS_REGION:-us-east-1}"
    export ENVIRONMENT="${ENVIRONMENT:-prod}"

    log_success "Environment loaded"
}

# Validate configuration
validate_config() {
    log_step "Validating configuration..."

    if [ -z "$DB_PASSWORD" ]; then
        log_warning "DB_PASSWORD not set"
        echo -n "Enter database password (min 16 chars): "
        read -r -s DB_PASSWORD
        echo ""
        export DB_PASSWORD
    fi

    if [ ${#DB_PASSWORD} -lt 16 ]; then
        log_error "DB_PASSWORD must be at least 16 characters"
        exit 1
    fi

    log_success "Configuration valid"
}

# Sync code to lambda_code directory
sync_lambda_code() {
    log_step "Syncing Lambda code..."

    local lambda_dir="${PROJECT_ROOT}/lambda_code"

    mkdir -p "${lambda_dir}/src"
    mkdir -p "${lambda_dir}/web_templates"
    mkdir -p "${lambda_dir}/web_static/js"
    mkdir -p "${lambda_dir}/web_static/css"

    cp -r "${PROJECT_ROOT}/src/"* "${lambda_dir}/src/" 2>/dev/null || true
    cp "${PROJECT_ROOT}/lambda_handler.py" "${lambda_dir}/"
    cp "${PROJECT_ROOT}/web_app.py" "${lambda_dir}/"
    cp "${PROJECT_ROOT}/config.yaml" "${lambda_dir}/" 2>/dev/null || true
    cp -r "${PROJECT_ROOT}/web_templates/"* "${lambda_dir}/web_templates/" 2>/dev/null || true
    cp -r "${PROJECT_ROOT}/web_static/"* "${lambda_dir}/web_static/" 2>/dev/null || true

    log_success "Code synced"
}

# Fetch JWKS from Cognito and save to .env
fetch_jwks() {
    log_step "Fetching JWKS from Cognito..."

    local user_pool_id="${COGNITO_USER_POOL_ID:-}"
    local region="${AWS_REGION:-us-east-1}"

    # Try to get user pool ID from outputs
    if [ -z "$user_pool_id" ]; then
        if [ -f "$OUTPUTS_FILE" ]; then
            user_pool_id=$(jq -r '.FlightMatrix.CognitoUserPoolId // empty' "$OUTPUTS_FILE")
        fi
    fi

    if [ -z "$user_pool_id" ]; then
        log_error "COGNITO_USER_POOL_ID not set and not found in outputs."
        log_info "Deploy with ENABLE_COGNITO_AUTH=true first, or set COGNITO_USER_POOL_ID"
        exit 1
    fi

    # Extract region from user pool ID if present
    if [[ "$user_pool_id" == *"_"* ]]; then
        region="${user_pool_id%%_*}"
    fi

    local jwks_url="https://cognito-idp.${region}.amazonaws.com/${user_pool_id}/.well-known/jwks.json"
    log_info "Fetching JWKS from: $jwks_url"

    local jwks
    jwks=$(curl -s "$jwks_url")

    if [ -z "$jwks" ] || ! echo "$jwks" | jq -e '.keys' >/dev/null 2>&1; then
        log_error "Failed to fetch valid JWKS"
        exit 1
    fi

    # Compact JSON (single line)
    local jwks_compact
    jwks_compact=$(echo "$jwks" | jq -c .)

    log_success "JWKS fetched successfully"
    echo ""
    echo "JWKS (compact):"
    echo "$jwks_compact"
    echo ""

    # Check if .env exists and update/add COGNITO_JWKS
    if [ -f "$ENV_FILE" ]; then
        # Remove existing COGNITO_JWKS line if present
        if grep -q "^COGNITO_JWKS=" "$ENV_FILE"; then
            # Use sed to replace the line
            sed -i "/^COGNITO_JWKS=/d" "$ENV_FILE"
            log_info "Removed existing COGNITO_JWKS from .env"
        fi
    fi

    # Append to .env
    echo "COGNITO_JWKS='${jwks_compact}'" >> "$ENV_FILE"
    log_success "COGNITO_JWKS added to .env file"
    log_info "Run './deploy.sh update' to deploy with offline JWT verification"
}

# Create user in Cognito and assign to groups
create_cognito_user() {
    log_step "Creating Cognito user..."

    local user_pool_id="${COGNITO_USER_POOL_ID:-}"
    local email="${1:-}"
    local groups="${2:-}"
    local temp_password="${3:-TempPass123!}"

    # Try to get user pool ID from outputs
    if [ -z "$user_pool_id" ]; then
        if [ -f "$OUTPUTS_FILE" ]; then
            user_pool_id=$(jq -r '.FlightMatrix.CognitoUserPoolId // empty' "$OUTPUTS_FILE")
        fi
    fi

    if [ -z "$user_pool_id" ]; then
        log_error "COGNITO_USER_POOL_ID not set and not found in outputs."
        log_info "Deploy with ENABLE_COGNITO_AUTH=true first, or set COGNITO_USER_POOL_ID"
        exit 1
    fi

    if [ -z "$email" ]; then
        echo -n "Enter email for user: "
        read -r email
    fi

    if [ -z "$email" ]; then
        log_error "Email is required"
        exit 1
    fi

    if [ -z "$groups" ]; then
        echo ""
        echo "Available groups:"
        echo "  1) admins - Full access to all protected resources"
        echo "  2) flight-schedules-viewers - Access to /flight-schedules page"
        echo "  3) both - Add to both groups"
        echo ""
        echo -n "Select group(s) [1/2/3]: "
        read -r group_choice

        case "$group_choice" in
            1) groups="admins" ;;
            2) groups="flight-schedules-viewers" ;;
            3) groups="admins,flight-schedules-viewers" ;;
            *)
                log_error "Invalid choice"
                exit 1
                ;;
        esac
    fi

    log_info "Creating user: $email in pool: $user_pool_id"

    # Get login URL from environment variables (.env file)
    local login_url=""
    local cognito_domain="${COGNITO_DOMAIN:-}"
    local client_id="${COGNITO_CLIENT_ID:-}"
    local callback_url="${COGNITO_CALLBACK_URL:-}"

    # Build login URL if all required variables are set
    if [ -n "$cognito_domain" ] && [ -n "$client_id" ] && [ -n "$callback_url" ]; then
        login_url="https://${cognito_domain}/login?client_id=${client_id}&response_type=code&scope=email+openid+profile&redirect_uri=${callback_url}"
    fi

    # Create the user (send invitation email automatically)
    aws cognito-idp admin-create-user \
        --user-pool-id "$user_pool_id" \
        --username "$email" \
        --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
        --temporary-password "$temp_password" \
        --desired-delivery-mediums EMAIL

    log_success "User created: $email"
    log_info "Invitation email sent to: $email"

    # Add user to groups
    IFS=',' read -ra GROUP_ARRAY <<< "$groups"
    for group in "${GROUP_ARRAY[@]}"; do
        group=$(echo "$group" | xargs)  # Trim whitespace
        log_info "Adding user to group: $group"
        aws cognito-idp admin-add-user-to-group \
            --user-pool-id "$user_pool_id" \
            --username "$email" \
            --group-name "$group" 2>/dev/null || {
                log_warning "Failed to add to group '$group' (group may not exist yet)"
            }
    done

    echo ""
    log_success "User setup complete!"
    echo ""
    log_info "User Details:"
    log_info "  Email: $email"
    log_info "  Temporary password: $temp_password"
    log_info "  Groups: $groups"
    echo ""
    log_info "An invitation email has been sent to the user with:"
    log_info "  - Username"
    log_info "  - Temporary password"
    if [ -n "$login_url" ]; then
        echo ""
        log_info "Login URL:"
        echo "  $login_url"
    fi
    echo ""
    log_info "User will be prompted to change password on first login"
}

# CDK synth
cdk_synth() {
    log_step "Generating CloudFormation template..."

    cd "$PROJECT_ROOT"
    cdk synth --app "$CDK_APP" --output cdk.out

    log_success "Template generated at cdk.out/"
}

# CDK diff
cdk_diff() {
    log_step "Checking changes..."

    cd "$PROJECT_ROOT"
    cdk diff --app "$CDK_APP" || true

    log_success "Diff complete"
}

# CDK deploy
cdk_deploy() {
    log_step "Deploying infrastructure..."

    cd "$PROJECT_ROOT"
    cdk deploy --app "$CDK_APP" \
        --require-approval never \
        --outputs-file "$OUTPUTS_FILE"

    log_success "Infrastructure deployed"
}

# Sync static files to S3
sync_static_files() {
    log_step "Syncing static files to S3..."

    # Get bucket name from outputs or use existing
    local bucket_name
    if [ -f "$OUTPUTS_FILE" ]; then
        bucket_name=$(jq -r '.FlightMatrix.S3BucketName // empty' "$OUTPUTS_FILE")
    fi

    if [ -z "$bucket_name" ]; then
        bucket_name="${S3_BUCKET_NAME:-}"
    fi

    if [ -z "$bucket_name" ]; then
        log_error "S3 bucket not found in outputs and S3_BUCKET_NAME is unset"
        return 1
    fi

    log_info "S3 Bucket: $bucket_name"

    # Sync JS files
    if [ -d "${PROJECT_ROOT}/web_static/js" ]; then
        aws s3 sync "${PROJECT_ROOT}/web_static/js/" "s3://${bucket_name}/static/js/" \
            --delete --cache-control "max-age=3600"
    fi

    # Sync CSS files
    if [ -d "${PROJECT_ROOT}/web_static/css" ]; then
        aws s3 sync "${PROJECT_ROOT}/web_static/css/" "s3://${bucket_name}/static/css/" \
            --delete --cache-control "max-age=3600"
    fi

    # Sync images if exist
    if [ -d "${PROJECT_ROOT}/web_static/images" ]; then
        aws s3 sync "${PROJECT_ROOT}/web_static/images/" "s3://${bucket_name}/static/images/" \
            --delete --cache-control "max-age=86400"
    fi

    log_success "Static files synced"
}

# Invalidate CloudFront cache
invalidate_cache() {
    log_step "Invalidating CloudFront cache..."

    # Get distribution ID from outputs or use existing
    local distribution_id
    if [ -f "$OUTPUTS_FILE" ]; then
        distribution_id=$(jq -r '.FlightMatrix.CloudFrontDistributionId // empty' "$OUTPUTS_FILE")
    fi

    if [ -z "$distribution_id" ]; then
        distribution_id="${CLOUDFRONT_DISTRIBUTION_ID:-}"
    fi

    if [ -z "$distribution_id" ]; then
        log_warning "CloudFront distribution ID not found, skipping cache invalidation"
        return
    fi

    local invalidation_id
    invalidation_id=$(aws cloudfront create-invalidation \
        --distribution-id "$distribution_id" \
        --paths "/static/*" "/*" \
        --query 'Invalidation.Id' \
        --output text)

    log_info "Invalidation ID: $invalidation_id"

    # Wait for completion (with timeout)
    local wait_time=0
    local max_wait=120

    while [ $wait_time -lt $max_wait ]; do
        local status
        status=$(aws cloudfront get-invalidation \
            --distribution-id "$distribution_id" \
            --id "$invalidation_id" \
            --query 'Invalidation.Status' \
            --output text)

        if [ "$status" = "Completed" ]; then
            log_success "Cache invalidated"
            return
        fi

        sleep 5
        wait_time=$((wait_time + 5))
        echo -n "."
    done

    echo ""
    log_warning "Cache invalidation still in progress"
}

# Trigger ASG instance refresh
refresh_workers() {
    log_step "Refreshing scraper workers..."

    # Get ASG name from outputs
    local asg_name
    if [ -f "$OUTPUTS_FILE" ]; then
        asg_name=$(jq -r '.FlightMatrix.ASGName // empty' "$OUTPUTS_FILE")
    fi

    if [ -z "$asg_name" ]; then
        log_warning "ASG name not found, skipping worker refresh"
        return
    fi

    # Start instance refresh
    aws autoscaling start-instance-refresh \
        --auto-scaling-group-name "$asg_name" \
        --preferences '{"MinHealthyPercentage": 50, "InstanceWarmup": 300}' \
        >/dev/null 2>&1 || {
            log_warning "Instance refresh already in progress or failed to start"
            return
        }

    log_success "Worker refresh started (rolling update in progress)"
}

# Show deployment status
show_status() {
    log_step "Deployment Status"
    echo ""

    if [ ! -f "$OUTPUTS_FILE" ]; then
        log_warning "No deployment outputs found. Run './deploy.sh deploy' first."
        return
    fi

    echo "Stack: FlightMatrix"
    echo ""

    # Parse and display outputs
    echo "API Gateway:"
    jq -r '.FlightMatrix.ApiUrl // "Not deployed"' "$OUTPUTS_FILE" | sed 's/^/  /'

    echo ""
    echo "CloudFront CDN:"
    jq -r '.FlightMatrix.CloudFrontURL // "Not deployed"' "$OUTPUTS_FILE" | sed 's/^/  /'

    echo ""
    echo "Database:"
    jq -r '.FlightMatrix.DatabaseEndpoint // "Not deployed"' "$OUTPUTS_FILE" | sed 's/^/  /'

    echo ""
    echo "S3 Bucket:"
    jq -r '.FlightMatrix.S3BucketName // "Not deployed"' "$OUTPUTS_FILE" | sed 's/^/  /'

    echo ""
    echo "VPC:"
    jq -r '.FlightMatrix.VPCId // "Not deployed"' "$OUTPUTS_FILE" | sed 's/^/  /'

    echo ""
    echo "Scraper ASG:"
    jq -r '.FlightMatrix.ASGName // "Not deployed"' "$OUTPUTS_FILE" | sed 's/^/  /'

    echo ""
}

# Destroy stack
destroy_stack() {
    log_warning "This will DESTROY all resources including:"
    echo "  - VPC and networking"
    echo "  - Aurora PostgreSQL database (snapshot will be created)"
    echo "  - Lambda function"
    echo "  - API Gateway"
    echo "  - S3 bucket (will be retained)"
    echo "  - CloudFront distribution"
    echo "  - EC2 scraper instances"
    echo ""

    echo -n "Type 'yes' to confirm destruction: "
    read -r confirm

    if [ "$confirm" != "yes" ]; then
        log_info "Destruction cancelled"
        exit 0
    fi

    log_step "Destroying stack..."

    cd "$PROJECT_ROOT"
    cdk destroy --app "$CDK_APP" --force

    log_success "Stack destroyed"
}

# Show deployment result
show_result() {
    echo ""
    echo "=========================================="
    echo -e "${GREEN}Deployment Complete!${NC}"
    echo "=========================================="
    echo ""

    show_status

    echo ""
    echo "Tip: Press Ctrl+Shift+R to force refresh browser cache"
    echo ""
}

# Full deploy command
cmd_deploy() {
    log_info "Starting full deployment..."

    check_prerequisites
    load_env
    validate_config
    sync_lambda_code
    cdk_deploy
    sync_static_files
    invalidate_cache

    show_result
}

# Update command (both webapp and scraper)
cmd_update() {
    log_info "Starting quick update..."

    check_prerequisites
    load_env
    validate_config
    sync_lambda_code
    cdk_deploy
    refresh_workers
    sync_static_files
    invalidate_cache

    show_result
}

# Update Lambda function directly (without CDK)
update_lambda_direct() {
    log_step "Updating Lambda function directly..."

    local lambda_name="flight-matrix-unified-prod"
    local lambda_dir="${PROJECT_ROOT}/lambda_code"

    # Build Docker image locally
    log_info "Building Docker image..."
    docker build --provenance=false -t flight-matrix-lambda:latest -f Dockerfile.lambda "$PROJECT_ROOT" || {
        log_error "Docker build failed"
        return 1
    }

    # Get ECR repository URI (AWS_ACCOUNT_ID is required)
    if [ -z "${AWS_ACCOUNT_ID:-}" ]; then
        AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '')"
    fi
    if [ -z "$AWS_ACCOUNT_ID" ]; then
        log_error "AWS_ACCOUNT_ID is not set and could not be determined via STS"
        return 1
    fi
    local ecr_repo="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/cdk-hnb659fds-container-assets-${AWS_ACCOUNT_ID}-${AWS_REGION}"

    # Login to ECR
    log_info "Logging in to ECR..."
    aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$ecr_repo" || {
        log_error "ECR login failed"
        return 1
    }

    # Tag and push image
    local image_tag="webapp-$(date +%Y%m%d-%H%M%S)"
    log_info "Pushing image with tag: $image_tag"
    docker tag flight-matrix-lambda:latest "${ecr_repo}:${image_tag}"
    docker push "${ecr_repo}:${image_tag}" || {
        log_error "Docker push failed"
        return 1
    }

    # Update Lambda function
    log_info "Updating Lambda function: $lambda_name"
    aws lambda update-function-code \
        --function-name "$lambda_name" \
        --image-uri "${ecr_repo}:${image_tag}" \
        --query 'FunctionArn' \
        --output text || {
        log_error "Lambda update failed"
        return 1
    }

    # Wait for update to complete
    log_info "Waiting for Lambda update to complete..."
    aws lambda wait function-updated --function-name "$lambda_name"

    log_success "Lambda function updated"
}

# Update Lambda environment variables only
update_lambda_env() {
    log_step "Updating Lambda environment variables..."

    local lambda_name="flight-matrix-unified-prod"

    # Build environment variables JSON using jq to handle escaping properly
    local env_vars
    env_vars=$(jq -n \
        --arg database_url "${DATABASE_URL:-}" \
        --arg s3_bucket "${S3_BUCKET_NAME:-}" \
        --arg cloudfront "${CLOUDFRONT_DOMAIN:-}" \
        --arg stage "${ENVIRONMENT:-prod}" \
        --arg pool_id "${COGNITO_USER_POOL_ID:-}" \
        --arg client_id "${COGNITO_CLIENT_ID:-}" \
        --arg client_secret "${COGNITO_CLIENT_SECRET:-}" \
        --arg cognito_domain "${COGNITO_DOMAIN:-}" \
        --arg callback_url "${COGNITO_CALLBACK_URL:-}" \
        --arg logout_url "${COGNITO_LOGOUT_URL:-}" \
        --arg jwks "${COGNITO_JWKS:-}" \
        --arg flask_secret "${FLASK_SECRET_KEY:-}" \
        --arg app_domain "${APP_DOMAIN:-}" \
        '{
            "Variables": {
                "DATABASE_URL": $database_url,
                "S3_BUCKET_NAME": $s3_bucket,
                "CLOUDFRONT_DOMAIN": $cloudfront,
                "CONFIG_PATH": "/var/task/config.yaml",
                "STAGE": $stage,
                "COGNITO_USER_POOL_ID": $pool_id,
                "COGNITO_CLIENT_ID": $client_id,
                "COGNITO_CLIENT_SECRET": $client_secret,
                "COGNITO_DOMAIN": $cognito_domain,
                "COGNITO_CALLBACK_URL": $callback_url,
                "COGNITO_LOGOUT_URL": $logout_url,
                "COGNITO_JWKS": $jwks,
                "FLASK_SECRET_KEY": $flask_secret,
                "APP_DOMAIN": $app_domain
            }
        }')

    aws lambda update-function-configuration \
        --function-name "$lambda_name" \
        --environment "$env_vars" \
        --query 'FunctionArn' \
        --output text || {
        log_error "Lambda environment update failed"
        return 1
    }

    log_success "Lambda environment updated"
}

# Webapp only update (Lambda + S3 + CloudFront) - without affecting scraper
cmd_webapp() {
    log_info "Starting webapp update (Lambda only, no scraper)..."

    check_prerequisites
    load_env
    validate_config
    sync_lambda_code
    update_lambda_direct
    sync_static_files
    invalidate_cache

    echo ""
    echo "=========================================="
    echo -e "${GREEN}Webapp Deployment Complete!${NC}"
    echo "=========================================="
    echo ""
    echo "API Gateway:"
    jq -r '.FlightMatrix.ApiUrl // "Not deployed"' "$OUTPUTS_FILE" 2>/dev/null | sed 's/^/  /' || echo "  Not available"
    echo ""
    echo "CloudFront CDN:"
    jq -r '.FlightMatrix.CloudFrontURL // "Not deployed"' "$OUTPUTS_FILE" 2>/dev/null | sed 's/^/  /' || echo "  Not available"
    echo ""
    echo "Tip: Press Ctrl+Shift+R to force refresh browser cache"
    echo ""
}

# Quick webapp environment update (env vars only, no code rebuild)
cmd_webapp_env() {
    log_info "Updating webapp environment variables only..."

    check_prerequisites
    load_env
    update_lambda_env

    log_success "Environment variables updated"
}

# Scraper only update (CDK deploy + ASG refresh)
cmd_scraper() {
    log_info "Starting scraper update (full config update)..."

    check_prerequisites
    load_env
    validate_config
    sync_lambda_code

    # Deploy infrastructure to update ASG config (desired capacity, Docker image, etc.)
    cdk_deploy

    # Trigger rolling replacement of worker instances
    refresh_workers

    echo ""
    echo "=========================================="
    echo -e "${GREEN}Scraper Update Complete!${NC}"
    echo "=========================================="
    echo ""
    echo "Scraper ASG:"
    jq -r '.FlightMatrix.ASGName // "Not deployed"' "$OUTPUTS_FILE" 2>/dev/null | sed 's/^/  /' || echo "  Not available"
    echo ""

    # Show current ASG status
    local asg_name
    asg_name=$(jq -r '.FlightMatrix.ASGName // empty' "$OUTPUTS_FILE" 2>/dev/null)
    if [ -n "$asg_name" ]; then
        echo "ASG Status:"
        aws autoscaling describe-auto-scaling-groups \
            --auto-scaling-group-names "$asg_name" \
            --query 'AutoScalingGroups[0].{Desired:DesiredCapacity,Min:MinSize,Max:MaxSize,Running:length(Instances)}' \
            --output table 2>/dev/null || echo "  Unable to fetch status"
        echo ""
    fi

    log_info "Rolling update in progress. Workers will be replaced gradually."
    echo ""
}

# Main
main() {
    echo ""
    echo "=========================================="
    echo "  Flight Matrix Unified Deployment"
    echo "=========================================="
    echo ""

    local command="${1:-}"

    case "$command" in
        deploy)
            cmd_deploy
            ;;
        update)
            cmd_update
            ;;
        webapp)
            cmd_webapp
            ;;
        webapp-env)
            cmd_webapp_env
            ;;
        scraper)
            cmd_scraper
            ;;
        destroy)
            check_prerequisites
            load_env
            destroy_stack
            ;;
        status)
            show_status
            ;;
        synth)
            check_prerequisites
            load_env
            validate_config
            cdk_synth
            ;;
        diff)
            check_prerequisites
            load_env
            validate_config
            cdk_diff
            ;;
        create-user)
            check_prerequisites
            load_env
            create_cognito_user "${2:-}" "${3:-}" "${4:-}"
            ;;
        fetch-jwks)
            check_prerequisites
            load_env
            fetch_jwks
            ;;
        ""|--help|-h)
            show_usage
            ;;
        *)
            log_error "Unknown command: $command"
            echo ""
            show_usage
            exit 1
            ;;
    esac
}

main "$@"
