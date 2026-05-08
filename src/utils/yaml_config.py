"""
YAML Configuration System - supports complex hierarchical configuration structures
YAML Configuration System for aircraft tracking with SQL filtering
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger("yaml_config")


class YAMLConfig:
    """YAML Configuration Manager - hierarchical config with recall and filtering support"""

    def __init__(self, config_file: str = "config.yaml"):
        self.config_file = config_file
        self.config = {}

        # Load environment variables from .env file
        # Try to find .env in the same directory as config file, or project root
        # Use override=True to ensure .env values take priority over system env vars
        config_dir = Path(config_file).parent.absolute()
        env_path = config_dir / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=True)
        else:
            # Fallback to default behavior (current working directory)
            load_dotenv(override=True)

        self._load_config()

    def _load_config(self):
        """Load YAML config file, supports the includes directive"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, encoding="utf-8") as f:
                    self.config = yaml.safe_load(f) or {}

                # Process includes directive
                if "includes" in self.config:
                    base_dir = Path(self.config_file).parent
                    for include_path in self.config.pop("includes"):
                        full_path = base_dir / include_path
                        if full_path.exists():
                            with open(full_path, encoding="utf-8") as f:
                                include_config = yaml.safe_load(f) or {}
                            self._deep_merge(self.config, include_config)
                        else:
                            logger.warning(f"Include file not found: {full_path}")

                logger.info(f"Loaded YAML config from {self.config_file}")
            else:
                logger.info(f"Config file {self.config_file} not found, using defaults")
                self._create_default_config()
        except Exception as e:
            logger.error(f"Error loading YAML config: {e}")
            self._create_default_config()

    def _create_default_config(self):
        """Create default configuration"""
        self.config = self._get_default_config()
        self.save_config()

    def _get_default_config(self) -> dict:
        """Get default config structure"""
        return {
            # Base configuration
            "database": {
                "url": "sqlite:///aircraft_data.db",
                "cleanup_interval_hours": 24,
                "max_connections": 10,
            },
            # API configuration
            "api": {
                "adsb_api_key": "${ADSB_API_KEY}",
                "adsb_api_url": "https://adsbexchange-com1.p.rapidapi.com/v2",
                "update_interval": 300,
                "timeout": 30,
            },
            # Email configuration
            "email": {
                "provider": "aws_ses",  # smtp | aws_ses
                "smtp": {
                    "server": "smtp.gmail.com",
                    "port": 587,
                    "sender": "${SENDER_EMAIL}",
                    "password": "${EMAIL_PASSWORD}",
                },
                "aws_ses": {
                    "region": "${AWS_REGION}",
                    "sender": "${AWS_SES_SENDER_EMAIL}",
                    "access_key_id": "${AWS_ACCESS_KEY_ID}",
                    "secret_access_key": "${AWS_SECRET_ACCESS_KEY}",
                },
                "recipients": ["${RECIPIENT_EMAIL}"],
                "features": {
                    "enable_maps": True,
                    "enable_aircraft_images": True,
                    "enable_flight_analysis": True,
                    "enable_tavily_search": True,
                },
            },
            # LLM configuration
            "llm": {
                "provider": "anthropic",  # anthropic | openai | aws_bedrock
                "anthropic_api_key": "${ANTHROPIC_API_KEY}",
                "openai_api_key": "${OPENAI_API_KEY}",
                "aws_bedrock_region": "us-east-1",
                "tavily_api_key": "${TAVILY_API_KEY}",
            },
            # Recall configuration
            "recall": {
                "sources": {
                    "specific_flights": [],
                    "specific_registrations": [],
                    "military_global": False,
                    "aircraft_types": [],
                    "regional_scan": [],
                    "global_scan": False,
                },
                "strategy": {
                    "update_interval": 300,
                    "max_aircraft_per_call": 1000,
                    "parallel_requests": True,
                    "cache_duration": 60,
                },
            },
            # Filter configuration
            "filters": {
                "mode": "custom_sql",
                "custom_sql": "is_military = 1 OR aircraft_type IN ('B742', 'IL76') OR registration LIKE 'N1%'",
            },
            # Reporting configuration
            "reporting": {
                "enable_report_cooldown": True,
                "cooldown_hours": 1.0,
                "cleanup_interval_cycles": 12,
                "max_reports_per_aircraft": 24,
            },
        }

    def save_config(self):
        """Save configuration to YAML file"""
        try:
            # Ensure directory exists
            config_dir = Path(self.config_file).parent
            config_dir.mkdir(parents=True, exist_ok=True)

            with open(self.config_file, "w", encoding="utf-8") as f:
                yaml.dump(
                    self.config,
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                    indent=2,
                )
            logger.info(f"Saved config to {self.config_file}")
        except Exception as e:
            logger.error(f"Error saving config: {e}")

    # Class-level cache for AWS Account ID
    _aws_account_id_cache: str | None = None

    def get(self, key_path: str, default: Any = None) -> Any:
        """Get config value, supports dot-separated paths"""
        try:
            keys = key_path.split(".")
            value = self.config

            for key in keys:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    return default

            # Handle environment variable substitution
            if isinstance(value, str) and "${" in value:
                return self._resolve_env_vars(value, default)

            return value
        except Exception:
            return default

    def _resolve_env_vars(self, value: str, default: Any = None) -> Any:
        """Resolve environment variables in a string value.

        Handles both full-value variables (${VAR}) and embedded variables
        (prefix-${VAR}-suffix).

        Args:
            value: String potentially containing ${VAR} patterns
            default: Default value if resolution fails

        Returns:
            Resolved string with all variables replaced
        """
        import re

        result = value
        # Find all ${VAR} patterns
        pattern = r"\$\{([^}]+)\}"
        matches = re.findall(pattern, value)

        if not matches:
            return value

        for env_key in matches:
            env_value = os.getenv(env_key)

            if not env_value:
                # Special handling for AWS_ACCOUNT_ID - auto-fetch via STS
                if env_key == "AWS_ACCOUNT_ID":
                    env_value = self._get_aws_account_id()

            if env_value:
                result = result.replace(f"${{{env_key}}}", env_value)
            else:
                # If any required variable is not resolved, return default
                # for full-value variables, or leave placeholder for embedded
                if value == f"${{{env_key}}}":
                    return default
                # For embedded variables, log warning but continue
                logger.warning(f"Environment variable {env_key} not set")

        return result

    def _get_aws_account_id(self) -> str | None:
        """Get AWS Account ID via STS, with caching."""
        # Return cached value if available
        if YAMLConfig._aws_account_id_cache:
            return YAMLConfig._aws_account_id_cache

        try:
            import boto3

            # Create STS client - uses default credential chain
            # (env vars, ~/.aws/credentials, IAM role, etc.)
            sts = boto3.client("sts")
            response = sts.get_caller_identity()
            account_id = response.get("Account")

            if account_id:
                # Cache for future use
                YAMLConfig._aws_account_id_cache = account_id
                logger.info(f"Auto-detected AWS Account ID: {account_id[:4]}****")
                return account_id

            return None
        except Exception as e:
            logger.warning(f"Failed to auto-detect AWS Account ID via STS: {e}")
            return None

    def set(self, key_path: str, value: Any):
        """Set config value, supports dot-separated paths"""
        try:
            keys = key_path.split(".")
            current = self.config

            # Navigate to the parent dictionary
            for key in keys[:-1]:
                if key not in current:
                    current[key] = {}
                current = current[key]

            # Set the final value
            current[keys[-1]] = value
            logger.info(f"Set config {key_path} = {value}")
        except Exception as e:
            logger.error(f"Error setting config {key_path}: {e}")

    def get_database_config(self) -> dict:
        """Get database config, supports IAM authentication"""
        config_url = self.get("database.url", "sqlite:///aircraft_data.db")

        # Resolve database URL, supports env var substitution and IAM auth
        db_url = self._resolve_database_url(config_url)

        return {
            "url": db_url,
            "cleanup_enabled": self.get("database.cleanup_enabled", False),
            "cleanup_interval_hours": self.get("database.cleanup_interval_hours", 24),
            "max_connections": self.get("database.max_connections", 10),
        }

    def _resolve_database_url(self, config_url: str) -> str:
        """Resolve database URL, supports password auth and IAM auth"""
        # SQLite requires no special handling
        if config_url.startswith("sqlite"):
            return config_url

        # Support DB_HOST env var to override database host
        db_host = os.environ.get("DB_HOST", "")
        if db_host:
            import re

            # Replace host portion in URL: @oldhost: -> @newhost:
            config_url = re.sub(r"@[^:]+:", f"@{db_host}:", config_url)
            logger.info(f"Database host overridden by DB_HOST env: {db_host}")

        # Check whether to use IAM authentication
        use_iam_auth = os.environ.get("USE_IAM_AUTH", "").lower() == "true"
        db_password = os.environ.get("DB_PASSWORD", "")

        # Check if URL already contains a password (format: user:password@host)
        import re

        has_password_in_url = (
            bool(re.search(r"://[^:]+:[^@]+@", config_url)) and "${DB_PASSWORD}" not in config_url
        )

        # If URL already has a password and IAM is not forced, return directly
        if has_password_in_url and not use_iam_auth:
            return config_url

        # If env var password is available and IAM is not forced, substitute password
        if db_password and not use_iam_auth:
            resolved_url = config_url.replace("${DB_PASSWORD}", db_password)
            return resolved_url

        # Only use IAM auth when explicitly required
        if use_iam_auth and "postgresql" in config_url:
            try:
                import re

                from src.utils.rds_iam_auth import get_iam_connection_url

                # Parse host, port, database name from config_url
                # Format: postgresql+psycopg2://user:pass@host:port/database
                match = re.search(r"@([^:]+):(\d+)/([^?]+)", config_url)
                if match:
                    host = match.group(1)
                    port = int(match.group(2))
                    database = match.group(3)
                    iam_user = os.environ.get("DB_IAM_USER", "scraper_iam")
                    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

                    logger.info(f"Using IAM authentication for {host}")
                    return get_iam_connection_url(host, port, database, iam_user, region)
            except ImportError:
                logger.warning("IAM auth module not available, falling back to password auth")
            except Exception as e:
                logger.error(f"IAM auth failed: {e}, falling back to password auth")

        # Fallback: try env var substitution
        if db_password:
            return config_url.replace("${DB_PASSWORD}", db_password)

        logger.warning("Environment variable DB_PASSWORD not set")
        return config_url

    def get_api_config(self) -> dict:
        """Get API configuration"""
        return {
            "adsb_api_key": self.get("api.adsb_api_key", ""),
            "adsb_api_url": self.get(
                "api.adsb_api_url", "https://adsbexchange-com1.p.rapidapi.com/v2"
            ),
            "update_interval": self.get("api.update_interval", 300),
            "timeout": self.get("api.timeout", 30),
        }

    def get_email_config(self) -> dict:
        """Get email configuration"""
        provider = self.get("email.provider", "smtp")

        # Parse recipients - supports comma-separated string or list
        recipients_raw = self.get("email.recipients", "")
        if isinstance(recipients_raw, str):
            recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
        elif isinstance(recipients_raw, list):
            recipients = recipients_raw
        else:
            recipients = []

        config = {
            "provider": provider,
            "recipients": recipients,
            "features": self.get("email.features", {}),
        }

        if provider == "smtp":
            config["smtp"] = {
                "server": self.get("email.smtp.server", ""),
                "port": self.get("email.smtp.port", 587),
                "sender": self.get("email.smtp.sender", ""),
                "password": self.get("email.smtp.password", ""),
            }
        elif provider == "aws_ses":
            aws_config = self.get_aws_config()
            config["aws_ses"] = {
                "region": aws_config["region"],
                "sender": self.get("email.aws_ses.sender", ""),
                "access_key_id": aws_config["access_key_id"],
                "secret_access_key": aws_config["secret_access_key"],
            }

        return config

    def get_aws_config(self) -> dict:
        """Get unified AWS configuration"""
        return {
            "region": self.get("aws.region", "us-west-2"),
            "access_key_id": self.get("aws.access_key_id", ""),
            "secret_access_key": self.get("aws.secret_access_key", ""),
        }

    def get_llm_config(self) -> dict:
        """Get LLM configuration"""
        aws_config = self.get_aws_config()
        return {
            "provider": self.get("llm.provider", "aws_bedrock"),
            "bedrock_model_id": self.get(
                "llm.bedrock_model_id", "anthropic.claude-sonnet-4-20250514-v1:0"
            ),
            "aws_region": aws_config["region"],
            "anthropic_api_key": self.get("llm.anthropic_api_key", ""),
            "openai_api_key": self.get("llm.openai_api_key", ""),
            "tavily_api_key": self.get("llm.tavily_api_key", ""),
            "system_prompt": self.get("llm.system_prompt", ""),
        }

    def get_aircraft_types_config(self) -> dict:
        """Get aircraft type identification configuration"""
        return {
            "chinese_military": self.get("aircraft_types.chinese_military", []),
            "us_military": self.get("aircraft_types.us_military", []),
            "russian_military": self.get("aircraft_types.russian_military", []),
            "european_military": self.get("aircraft_types.european_military", []),
        }

    def get_recall_config(self) -> dict:
        """Get recall configuration"""
        return {
            "sources": self.get("recall.sources", {}),
            "strategy": self.get("recall.strategy", {}),
        }

    def get_filter_config(self) -> dict:
        """Get filter configuration"""
        return {
            "mode": self.get("reporting.filters.mode", "custom_sql"),
            "require_position": self.get("reporting.filters.require_position", True),
            "custom_sql": self.get("reporting.filters.custom_sql", ""),
        }

    def get_reporting_config(self) -> dict:
        """Get reporting configuration"""
        return self.get("reporting", {})

    def get_templates_config(self) -> dict:
        """Get HTML template configuration"""
        return {
            "report_html": self.get("templates.report_html", ""),
            "error_html": self.get("templates.error_html", ""),
        }

    # =========================================================================
    # Multi-User Subscription System Configuration
    # =========================================================================

    def is_multi_user_enabled(self) -> bool:
        """Check if multi-user mode is enabled.

        Returns:
            True if multi_user.enabled is True in config
        """
        return self.get("multi_user.enabled", False)

    def get_multi_user_config(self) -> dict:
        """Get multi-user system configuration.

        Returns:
            Multi-user configuration dictionary with defaults
        """
        return {
            "enabled": self.get("multi_user.enabled", False),
            "defaults": {
                "cooldown_hours": self.get("multi_user.defaults.cooldown_hours", 12.0),
                "min_move_distance_km": self.get("multi_user.defaults.min_move_distance_km", 1.0),
            },
        }

    def get_subscription_tiers_config(self) -> dict:
        """Get subscription tier configurations.

        Returns:
            Dictionary of tier configurations with defaults
        """
        default_tiers = {
            "basic": {
                "name": "基础版",
                "daily_report_limit": 10,
                "monthly_report_limit": 100,
                "features": {
                    "enable_maps": True,
                    "enable_aircraft_images": True,
                },
                "cooldown_hours": 24.0,
                "max_filters": 3,
            },
            "premium": {
                "name": "高级版",
                "daily_report_limit": 50,
                "monthly_report_limit": 500,
                "features": {
                    "enable_maps": True,
                    "enable_aircraft_images": True,
                },
                "cooldown_hours": 6.0,
                "max_filters": 10,
            },
            "enterprise": {
                "name": "企业版",
                "daily_report_limit": -1,
                "monthly_report_limit": -1,
                "features": {
                    "enable_maps": True,
                    "enable_aircraft_images": True,
                },
                "cooldown_hours": 1.0,
                "max_filters": -1,
            },
        }

        # Get configured tiers or use defaults
        configured_tiers = self.get("subscription_tiers", {})
        if configured_tiers:
            # Merge with defaults (configured values override defaults)
            for tier_name, tier_config in configured_tiers.items():
                if tier_name in default_tiers:
                    # Merge features separately
                    if "features" in tier_config:
                        default_tiers[tier_name]["features"].update(tier_config["features"])
                        tier_config = {k: v for k, v in tier_config.items() if k != "features"}
                    default_tiers[tier_name].update(tier_config)
                else:
                    default_tiers[tier_name] = tier_config

        return default_tiers

    def get_tier_config(self, tier: str) -> dict:
        """Get configuration for a specific subscription tier.

        Args:
            tier: Tier name (basic, premium, enterprise)

        Returns:
            Tier configuration dictionary
        """
        tiers = self.get_subscription_tiers_config()
        return tiers.get(tier, tiers.get("basic", {}))

    def add_custom_rule(self, rule_name: str, description: str, config: dict):
        """Add a custom rule"""
        rule_config = {
            "description": description,
            "enabled": True,
            "config": config,
            "created_at": datetime.now().isoformat(),
        }

        self.set(f"predefined_rules.{rule_name}", rule_config)
        logger.info(f"Added custom rule: {rule_name}")

    def validate_config(self) -> list[str]:
        """Validate configuration, returns a list of errors"""
        errors = []

        # Validate required API keys
        if not self.get("api.adsb_api_key"):
            errors.append("Missing ADSB API key")

        # Validate email configuration
        email_provider = self.get("email.provider")
        if email_provider == "smtp":
            if not self.get("email.smtp.sender"):
                errors.append("Missing SMTP sender email")
            if not self.get("email.smtp.password"):
                errors.append("Missing SMTP password")
        elif email_provider == "aws_ses":
            if not self.get("email.aws_ses.sender"):
                errors.append("Missing AWS SES sender email")

        if not self.get("email.recipients"):
            errors.append("No email recipients configured")

        # Validate recall configuration
        recall_sources = self.get("recall.sources", {})
        if not any(recall_sources.values()):
            errors.append("No recall sources configured")

        # Validate database configuration
        db_url = self.get("database.url")
        if not db_url:
            errors.append("Missing database URL")

        return errors

    def reload_config(self):
        """Reload the configuration file"""
        logger.info("Reloading configuration...")
        self._load_config()

    def export_config(self, export_file: str):
        """Export configuration to the specified file"""
        try:
            with open(export_file, "w", encoding="utf-8") as f:
                yaml.dump(
                    self.config,
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                    indent=2,
                )
            logger.info(f"Exported config to {export_file}")
        except Exception as e:
            logger.error(f"Error exporting config: {e}")

    def import_config(self, import_file: str):
        """Import configuration from the specified file"""
        try:
            with open(import_file, encoding="utf-8") as f:
                imported_config = yaml.safe_load(f)

            # Merge configs (imported config overrides existing)
            self._deep_merge(self.config, imported_config)

            logger.info(f"Imported config from {import_file}")
            self.save_config()
        except Exception as e:
            logger.error(f"Error importing config: {e}")

    def _deep_merge(self, base: dict, update: dict):
        """Deep merge two dictionaries"""
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def get_config_summary(self) -> dict:
        """Get configuration summary"""
        return {
            "database_url": self.get("database.url"),
            "email_provider": self.get("email.provider"),
            "llm_provider": self.get("llm.provider"),
            "filter_mode": self.get("reporting.filters.mode"),
            "recall_sources_count": len([s for s in self.get("recall.sources", {}).values() if s]),
            "recipients_count": len(self.get("email.recipients", [])),
            "config_file": self.config_file,
        }


def create_sample_config(output_file: str = "config.yaml"):
    """Create a sample configuration file"""
    config_manager = YAMLConfig(output_file)

    # Set some sample values
    config_manager.set("recall.sources.specific_registrations", ["N123AB", "86-0022"])
    config_manager.set("recall.sources.military_global", True)
    config_manager.set(
        "reporting.filters.custom_sql", "is_military = 1 OR aircraft_type IN ('B742', 'IL76')"
    )

    config_manager.save_config()
    logger.info(f"Created sample config at {output_file}")


if __name__ == "__main__":
    # Create a sample configuration file
    create_sample_config("sample_config.yaml")
