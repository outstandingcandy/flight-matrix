#!/usr/bin/env python3
"""
CDK Entry Point for Flight Matrix Unified Stack

Supports two modes:
1. Import mode (default): Uses existing VPC, Database, S3, CloudFront
2. Fresh mode: Creates all resources from scratch (set FRESH_DEPLOY=true)

Usage:
    cdk synth                    # Generate CloudFormation template
    cdk diff                     # View changes
    cdk deploy                   # Deploy stack
    cdk destroy                  # Destroy stack

Environment Variables:
    DB_PASSWORD     - Database password (required, min 16 chars)
    DB_USERNAME     - Database username (default: aircraft_admin)
    DB_NAME         - Database name (default: aircraft_data)
    ENVIRONMENT     - Deployment environment (default: prod)
    AWS_REGION      - AWS region (default: us-east-1)
    FRESH_DEPLOY    - Set to "true" to create all new resources

    Cognito Authentication:
    ENABLE_COGNITO_AUTH     - Set to "true" to create Cognito resources automatically
    COGNITO_DOMAIN_PREFIX   - Cognito domain prefix (default: flight-matrix-{env})

    Or provide existing Cognito configuration:
    COGNITO_USER_POOL_ID    - Cognito User Pool ID
    COGNITO_CLIENT_ID       - Cognito App Client ID
    COGNITO_CLIENT_SECRET   - Cognito App Client Secret
    COGNITO_DOMAIN          - Cognito domain (e.g., flight-matrix.auth.us-west-2.amazoncognito.com)
    COGNITO_CALLBACK_URL    - OAuth2 callback URL
    COGNITO_LOGOUT_URL      - Logout redirect URL
    FLASK_SECRET_KEY        - Flask session secret key

    Access Control:
    When ENABLE_COGNITO_AUTH is true, the following Cognito Groups are created:
    - admins: Full access to all protected resources
    - flight-schedules-viewers: Access to /flight-schedules page

    Custom Domain:
    APP_DOMAIN          - Custom domain (e.g., example.com) for correct URL generation
"""

import os
import sys
from pathlib import Path

import yaml
from aws_cdk import App, Environment

# Add project root to path for imports
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from infra.unified_stack import FlightMatrixUnifiedStack


def get_env_or_default(key: str, default: str) -> str:
    """Get environment variable or return default value."""
    return os.environ.get(key, default)


def get_env_int(key: str, default: int) -> int:
    """Get environment variable as integer."""
    value = os.environ.get(key)
    if value is None:
        return default
    return int(value)


def get_env_bool(key: str, default: bool) -> bool:
    """Get environment variable as boolean."""
    value = os.environ.get(key)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes")


def load_yaml_config() -> dict:
    """Load configuration from config.yaml."""
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def validate_config() -> dict:
    """Validate and return configuration from environment variables and config.yaml.

    Priority: Environment variables > config.yaml > hardcoded defaults
    """
    db_password = os.environ.get("DB_PASSWORD", "")

    if not db_password:
        print("ERROR: DB_PASSWORD environment variable is required")
        print("Set it in .env file or export DB_PASSWORD=your_password")
        sys.exit(1)

    if len(db_password) < 16:
        print("ERROR: DB_PASSWORD must be at least 16 characters")
        sys.exit(1)

    # Load config.yaml for scraper deployment settings
    yaml_config = load_yaml_config()
    deployment = yaml_config.get("scraper", {}).get("deployment", {})

    config = {
        "environment": get_env_or_default("ENVIRONMENT", "prod"),
        "db_username": get_env_or_default("DB_USERNAME", "aircraft_admin"),
        "db_password": db_password,
        "db_name": get_env_or_default("DB_NAME", "aircraft_data"),
        # Scraper configuration: env vars override config.yaml, which overrides defaults
        "scraper_min_capacity": get_env_int("SCRAPER_MIN_CAPACITY", deployment.get("min_workers", 1)),
        "scraper_max_capacity": get_env_int("SCRAPER_MAX_CAPACITY", deployment.get("max_workers", 5)),
        "scraper_desired_capacity": get_env_int("SCRAPER_DESIRED_CAPACITY", deployment.get("desired_workers", 2)),
        "scraper_instance_type": get_env_or_default("SCRAPER_INSTANCE_TYPE", deployment.get("instance_type", "t3.medium")),
        "enable_nat_gateway": get_env_bool("ENABLE_NAT_GATEWAY", True),
        "fresh_deploy": get_env_bool("FRESH_DEPLOY", False),
    }

    # Log scraper configuration source
    print(f"Scraper configuration: min={config['scraper_min_capacity']}, "
          f"max={config['scraper_max_capacity']}, desired={config['scraper_desired_capacity']}, "
          f"type={config['scraper_instance_type']}")

    return config


# Existing infrastructure configuration
# All values are read from environment variables for security
# Set these in .env file (not committed to git)
EXISTING_INFRA = {
    "vpc_id": os.environ.get("VPC_ID", ""),
    "db_endpoint": os.environ.get("DB_ENDPOINT", ""),
    "db_security_group_id": os.environ.get("DB_SECURITY_GROUP_ID", ""),
    "services_security_group_id": os.environ.get("SERVICES_SECURITY_GROUP_ID", ""),
    "s3_bucket_name": os.environ.get("S3_BUCKET_NAME", ""),
    "cloudfront_distribution_id": os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", ""),
    "cloudfront_domain": os.environ.get("CLOUDFRONT_DOMAIN", ""),
    # Private subnets have NAT gateway access - needed for Lambda DNS resolution
    "private_subnet_ids": os.environ.get("PRIVATE_SUBNET_IDS", "").split(",") if os.environ.get("PRIVATE_SUBNET_IDS") else [],
    "isolated_subnet_ids": os.environ.get("ISOLATED_SUBNET_IDS", "").split(",") if os.environ.get("ISOLATED_SUBNET_IDS") else [],
    "public_subnet_ids": os.environ.get("PUBLIC_SUBNET_IDS", "").split(",") if os.environ.get("PUBLIC_SUBNET_IDS") else [],
}

# Cognito authentication configuration
COGNITO_CONFIG = {
    # Set to True to create Cognito resources automatically
    "enable_cognito_auth": get_env_bool("ENABLE_COGNITO_AUTH", False),
    "domain_prefix": os.environ.get("COGNITO_DOMAIN_PREFIX", ""),
    # Or provide existing Cognito configuration
    "user_pool_id": os.environ.get("COGNITO_USER_POOL_ID", ""),
    "client_id": os.environ.get("COGNITO_CLIENT_ID", ""),
    "client_secret": os.environ.get("COGNITO_CLIENT_SECRET", ""),
    "domain": os.environ.get("COGNITO_DOMAIN", ""),
    "callback_url": os.environ.get("COGNITO_CALLBACK_URL", ""),
    "logout_url": os.environ.get("COGNITO_LOGOUT_URL", ""),
    "flask_secret_key": os.environ.get("FLASK_SECRET_KEY", ""),
    # JWKS for offline JWT verification (no network access needed)
    "jwks": os.environ.get("COGNITO_JWKS", ""),
}

# Application domain configuration
# Used for correct URL generation when behind API Gateway custom domain
APP_DOMAIN = os.environ.get("APP_DOMAIN", "")


def main() -> None:
    """Create and synthesize the CDK app."""
    # Load configuration
    config = validate_config()

    # Get AWS environment
    aws_account = os.environ.get("CDK_DEFAULT_ACCOUNT") or os.environ.get("AWS_ACCOUNT_ID")
    aws_region = os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_REGION", "us-east-1")

    if not aws_account:
        print("WARNING: AWS_ACCOUNT_ID not set, using CDK default account lookup")

    env = Environment(
        account=aws_account,
        region=aws_region,
    )

    # Create CDK app
    app = App()

    # Cognito configuration for both modes
    cognito_kwargs = {}
    if COGNITO_CONFIG["enable_cognito_auth"]:
        # Create Cognito resources automatically (includes Groups)
        cognito_kwargs = {
            "enable_cognito_auth": True,
            "cognito_domain_prefix": COGNITO_CONFIG["domain_prefix"] or None,
            "flask_secret_key": COGNITO_CONFIG["flask_secret_key"] or None,
        }
        print("Cognito authentication enabled (will create resources + groups)")
    elif COGNITO_CONFIG["user_pool_id"]:
        # Use existing Cognito resources
        cognito_kwargs = {
            "cognito_user_pool_id": COGNITO_CONFIG["user_pool_id"],
            "cognito_client_id": COGNITO_CONFIG["client_id"],
            "cognito_client_secret": COGNITO_CONFIG["client_secret"] or None,
            "cognito_domain": COGNITO_CONFIG["domain"],
            "cognito_callback_url": COGNITO_CONFIG["callback_url"],
            "cognito_logout_url": COGNITO_CONFIG["logout_url"],
            "cognito_jwks": COGNITO_CONFIG["jwks"] or None,  # Offline JWT verification
            "flask_secret_key": COGNITO_CONFIG["flask_secret_key"] or None,
        }
        print("Cognito authentication configured (using existing resources)")
        if COGNITO_CONFIG["jwks"]:
            print("  JWKS configured for offline JWT verification")

    # Determine deploy mode
    if config["fresh_deploy"]:
        print("FRESH_DEPLOY mode: Creating all new resources")
        # Fresh deployment - create everything new
        FlightMatrixUnifiedStack(
            app,
            "FlightMatrix",
            env=env,
            environment=config["environment"],
            db_username=config["db_username"],
            db_password=config["db_password"],
            db_name=config["db_name"],
            scraper_min_capacity=config["scraper_min_capacity"],
            scraper_max_capacity=config["scraper_max_capacity"],
            scraper_desired_capacity=config["scraper_desired_capacity"],
            scraper_instance_type=config["scraper_instance_type"],
            enable_nat_gateway=config["enable_nat_gateway"],
            app_domain=APP_DOMAIN or None,
            **cognito_kwargs,
        )
    else:
        print("IMPORT mode: Using existing VPC, Database, S3, CloudFront")
        # Import existing resources
        FlightMatrixUnifiedStack(
            app,
            "FlightMatrix",
            env=env,
            environment=config["environment"],
            db_username=config["db_username"],
            db_password=config["db_password"],
            db_name=config["db_name"],
            # Import existing resources
            existing_vpc_id=EXISTING_INFRA["vpc_id"],
            existing_db_endpoint=EXISTING_INFRA["db_endpoint"],
            existing_db_security_group_id=EXISTING_INFRA["db_security_group_id"],
            existing_s3_bucket_name=EXISTING_INFRA["s3_bucket_name"],
            existing_cloudfront_distribution_id=EXISTING_INFRA["cloudfront_distribution_id"],
            existing_cloudfront_domain=EXISTING_INFRA["cloudfront_domain"],
            existing_private_subnet_ids=EXISTING_INFRA["private_subnet_ids"],
            existing_isolated_subnet_ids=EXISTING_INFRA["isolated_subnet_ids"],
            existing_public_subnet_ids=EXISTING_INFRA["public_subnet_ids"],
            existing_services_security_group_id=EXISTING_INFRA["services_security_group_id"],
            # Scraper configuration
            scraper_min_capacity=config["scraper_min_capacity"],
            scraper_max_capacity=config["scraper_max_capacity"],
            scraper_desired_capacity=config["scraper_desired_capacity"],
            scraper_instance_type=config["scraper_instance_type"],
            # Custom domain for URL generation
            app_domain=APP_DOMAIN or None,
            **cognito_kwargs,
        )

    app.synth()


if __name__ == "__main__":
    main()
