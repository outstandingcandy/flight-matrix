"""
RDS IAM Authentication utility for Aurora PostgreSQL.

This module provides functions to connect to Aurora PostgreSQL using IAM authentication,
eliminating the need for static database passwords.

Usage:
    from src.utils.rds_iam_auth import get_iam_connection_url

    # Get connection URL with IAM token
    db_url = get_iam_connection_url(
        host="your-cluster.cluster-xxx.us-west-2.rds.amazonaws.com",
        port=5432,
        database="aircraft_data",
        user="scraper_iam",
        region="us-west-2"
    )

    # Use with SQLAlchemy
    engine = create_engine(db_url)

Requirements:
    - Aurora cluster must have IAM authentication enabled
    - Database user must have rds_iam role granted
    - EC2 instance must have rds-db:connect permission

References:
    https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html
"""

import logging
import os
from typing import Any
from urllib.parse import quote_plus

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)


def get_rds_auth_token(
    host: str,
    port: int,
    user: str,
    region: str | None = None,
) -> str:
    """
    Generate an IAM authentication token for RDS connection.

    The token is valid for 15 minutes but connections established with
    the token can last longer.

    Args:
        host: RDS endpoint hostname
        port: Database port (usually 5432 for PostgreSQL)
        user: Database username (must have rds_iam role)
        region: AWS region (auto-detected if not provided)

    Returns:
        Authentication token string
    """
    if region is None:
        region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

    config = Config(
        connect_timeout=5,
        read_timeout=5,
        retries={"max_attempts": 3},
    )

    client = boto3.client("rds", region_name=region, config=config)

    token = client.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=user,
        Region=region,
    )

    logger.debug(f"Generated RDS IAM auth token for {user}@{host}:{port}")
    return token


def get_iam_connection_url(
    host: str,
    port: int,
    database: str,
    user: str,
    region: str | None = None,
    ssl_mode: str = "require",
) -> str:
    """
    Generate a PostgreSQL connection URL with IAM authentication token.

    Args:
        host: RDS endpoint hostname
        port: Database port
        database: Database name
        user: Database username
        region: AWS region
        ssl_mode: SSL mode (require, verify-ca, verify-full)

    Returns:
        PostgreSQL connection URL with embedded IAM token
    """
    token = get_rds_auth_token(host, port, user, region)

    # URL-encode the token as it contains special characters
    encoded_token = quote_plus(token)

    url = (
        f"postgresql+psycopg2://{user}:{encoded_token}@{host}:{port}/{database}?sslmode={ssl_mode}"
    )

    return url


def create_iam_engine(
    host: str,
    port: int,
    database: str,
    user: str,
    region: str | None = None,
    **engine_kwargs: Any,
) -> Any:
    """
    Create a SQLAlchemy engine with IAM authentication.

    This function creates an engine with a custom connection creator that
    generates fresh IAM tokens for each new connection, ensuring tokens
    don't expire during long-running applications.

    Args:
        host: RDS endpoint hostname
        port: Database port
        database: Database name
        user: Database username
        region: AWS region
        **engine_kwargs: Additional arguments for create_engine

    Returns:
        SQLAlchemy Engine instance
    """
    from sqlalchemy import create_engine, event

    # Base URL without password (token will be added dynamically)
    base_url = f"postgresql+psycopg2://{user}@{host}:{port}/{database}?sslmode=require"

    engine = create_engine(base_url, **engine_kwargs)

    @event.listens_for(engine, "do_connect")
    def provide_token(dialect: Any, conn_rec: Any, cargs: list, cparams: dict) -> None:
        """Inject fresh IAM token before each connection."""
        token = get_rds_auth_token(host, port, user, region)
        cparams["password"] = token

    logger.info(f"Created IAM-authenticated engine for {database}@{host}")
    return engine


def is_iam_auth_enabled() -> bool:
    """
    Check if IAM authentication should be used.

    Returns True if:
    - USE_IAM_AUTH environment variable is set to 'true'
    - Or DB_PASSWORD is not set/empty
    """
    use_iam = os.environ.get("USE_IAM_AUTH", "").lower() == "true"
    no_password = not os.environ.get("DB_PASSWORD")

    return use_iam or no_password


def get_database_url(
    config_url: str | None = None,
    host: str | None = None,
    port: int = 5432,
    database: str = "aircraft_data",
    user: str | None = None,
    region: str | None = None,
) -> str:
    """
    Get database URL, automatically choosing between password and IAM auth.

    If DB_PASSWORD environment variable is set, uses password authentication.
    Otherwise, uses IAM authentication.

    Args:
        config_url: URL from config file (may contain ${DB_PASSWORD} placeholder)
        host: Database host (required if not using config_url with IAM)
        port: Database port
        database: Database name
        user: Database user (for IAM auth)
        region: AWS region

    Returns:
        Database connection URL
    """
    db_password = os.environ.get("DB_PASSWORD", "")

    # If password is available and config_url is provided, use password auth
    if db_password and config_url:
        # Replace placeholder with actual password
        return config_url.replace("${DB_PASSWORD}", db_password)

    # Use IAM authentication
    if host is None:
        # Try to extract host from config_url
        if config_url and "@" in config_url:
            # Parse host from URL like postgresql://user:pass@host:port/db
            import re

            match = re.search(r"@([^:]+):(\d+)/(\w+)", config_url)
            if match:
                host = match.group(1)
                port = int(match.group(2))
                database = match.group(3)
            else:
                raise ValueError("Cannot extract host from config_url for IAM auth")
        else:
            raise ValueError("host is required for IAM authentication")

    if user is None:
        user = os.environ.get("DB_IAM_USER", "scraper_iam")

    logger.info(f"Using IAM authentication for database connection to {host}")
    return get_iam_connection_url(host, port, database, user, region)
