"""Configuration validation health checks.

This module verifies that the flight-matrix configuration is properly
set up, including config file existence, parseability, environment
variables, API keys, and database URL validity.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from tests.deployment_check.base import BaseHealthCheck, CheckResult, CheckStatus

logger = logging.getLogger("deployment_check.config")


class ConfigHealthCheck(BaseHealthCheck):
    """Health checks for configuration validation.

    Checks performed:
    - Config file exists
    - Config file is parseable YAML
    - Required environment variables are set
    - API keys are not placeholders
    - Database URL is valid
    - Email configuration is complete
    - At least one recall source is configured
    """

    category = "Configuration"

    def __init__(self, config_file: str = "config.yaml", config: Any | None = None) -> None:
        """Initialize configuration health check.

        Args:
            config_file: Path to the configuration file.
            config: Optional pre-loaded YAMLConfig instance.
        """
        super().__init__(config)
        self.config_file = config_file

    async def run(self) -> list[CheckResult]:
        """Execute all configuration checks.

        Returns:
            List of CheckResult objects for each configuration check.
        """
        results: list[CheckResult] = []

        # Check config file exists
        results.append(self._check_config_file_exists())

        # If file doesn't exist, skip remaining checks
        if results[-1].status == CheckStatus.FAIL:
            return results

        # Check config file is parseable
        results.append(self._check_config_file_parseable())

        # If not parseable, skip remaining checks
        if results[-1].status == CheckStatus.FAIL:
            return results

        # Check environment variables
        results.extend(self._check_env_vars())

        # Check API keys are not placeholders
        results.extend(self._check_api_keys())

        # Check database URL
        results.append(self._check_database_url())

        # Check email configuration
        results.append(self._check_email_config())

        # Check recall sources
        results.append(self._check_recall_sources())

        return results

    def _check_config_file_exists(self) -> CheckResult:
        """Check that the configuration file exists.

        Returns:
            CheckResult indicating if config file exists.
        """
        start_time = time.perf_counter()
        config_path = Path(self.config_file)

        if config_path.exists():
            return self._pass(
                "Config file exists",
                f"Found: {self.config_file}",
                start_time,
                {"path": str(config_path.absolute())},
            )
        return self._fail(
            "Config file exists",
            f"File not found: {self.config_file}",
            start_time,
        )

    def _check_config_file_parseable(self) -> CheckResult:
        """Check that the configuration file is valid YAML.

        Returns:
            CheckResult indicating if config file is parseable.
        """
        start_time = time.perf_counter()

        try:
            from src.utils.yaml_config import YAMLConfig

            self.config = YAMLConfig(self.config_file)
            return self._pass(
                "Config file parseable",
                "YAML syntax valid",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "Config file parseable",
                f"YAML parse error: {e}",
                start_time,
            )

    def _check_env_vars(self) -> list[CheckResult]:
        """Check that required environment variables are set.

        Returns:
            List of CheckResult objects for each environment variable.
        """
        results: list[CheckResult] = []

        # Check for optional but important env vars that may be referenced in config
        env_vars_to_check = [
            ("ADSB_API_KEY", True),  # Required if using ADS-B Exchange
            ("DATABASE_URL", False),  # Optional - can be in config directly
            ("AWS_REGION", False),  # Optional - defaults available
            ("TAVILY_API_KEY", False),  # Optional - only needed if using Tavily
        ]

        for var_name, required in env_vars_to_check:
            start_time = time.perf_counter()
            value = os.environ.get(var_name)

            if value:
                # Mask the value for security
                masked = f"{value[:4]}..." if len(value) > 4 else "***"
                results.append(
                    self._pass(
                        f"ENV: {var_name}",
                        f"Set ({masked})",
                        start_time,
                    )
                )
            elif required:
                results.append(
                    self._fail(
                        f"ENV: {var_name}",
                        "Not set (required)",
                        start_time,
                    )
                )
            else:
                results.append(
                    self._skip(
                        f"ENV: {var_name}",
                        "Not set (optional)",
                        start_time,
                    )
                )

        return results

    def _check_api_keys(self) -> list[CheckResult]:
        """Check that API keys are not placeholder values.

        Returns:
            List of CheckResult objects for API key validation.
        """
        results: list[CheckResult] = []

        if not self.config:
            return results

        placeholder_patterns = [
            r"^your[_-]?",
            r"^xxx+$",
            r"^placeholder",
            r"^changeme",
            r"^TODO",
            r"^\$\{.*\}$",  # Unresolved env var
        ]

        api_keys_to_check = [
            ("api.adsb_api_key", "ADS-B API Key"),
            ("llm.api_key", "LLM API Key"),
        ]

        for config_key, display_name in api_keys_to_check:
            start_time = time.perf_counter()

            try:
                value = self.config.get(config_key)

                if not value:
                    results.append(
                        self._skip(
                            f"API Key: {display_name}",
                            "Not configured",
                            start_time,
                        )
                    )
                    continue

                # Check for placeholder patterns
                is_placeholder = any(
                    re.match(pattern, str(value), re.IGNORECASE) for pattern in placeholder_patterns
                )

                if is_placeholder:
                    results.append(
                        self._fail(
                            f"API Key: {display_name}",
                            "Contains placeholder value",
                            start_time,
                        )
                    )
                else:
                    masked = f"{str(value)[:8]}..." if len(str(value)) > 8 else "***"
                    results.append(
                        self._pass(
                            f"API Key: {display_name}",
                            f"Valid format ({masked})",
                            start_time,
                        )
                    )
            except Exception as e:
                results.append(
                    self._fail(
                        f"API Key: {display_name}",
                        f"Error reading: {e}",
                        start_time,
                    )
                )

        return results

    def _check_database_url(self) -> CheckResult:
        """Check that the database URL is properly formatted.

        Returns:
            CheckResult indicating if database URL is valid.
        """
        start_time = time.perf_counter()

        if not self.config:
            return self._fail(
                "Database URL valid",
                "Config not loaded",
                start_time,
            )

        try:
            db_config = self.config.get_database_config()
            db_url = db_config.get("url", "")

            if not db_url:
                return self._fail(
                    "Database URL valid",
                    "Database URL not configured",
                    start_time,
                )

            # Check URL format
            valid_prefixes = [
                "postgresql://",
                "postgresql+psycopg2://",
                "postgres://",
                "mysql://",
                "mysql+pymysql://",
                "sqlite:///",
            ]

            if any(db_url.startswith(prefix) for prefix in valid_prefixes):
                # Mask credentials in URL for display
                masked_url = re.sub(
                    r"://[^:]+:[^@]+@",
                    "://***:***@",
                    db_url,
                )
                return self._pass(
                    "Database URL valid",
                    f"Format valid ({masked_url[:50]}...)"
                    if len(masked_url) > 50
                    else f"Format valid ({masked_url})",
                    start_time,
                    {"dialect": db_url.split("://")[0]},
                )

            return self._fail(
                "Database URL valid",
                f"Invalid URL prefix. Expected one of: {', '.join(valid_prefixes)}",
                start_time,
            )

        except Exception as e:
            return self._fail(
                "Database URL valid",
                f"Error validating: {e}",
                start_time,
            )

    def _check_email_config(self) -> CheckResult:
        """Check that email configuration is complete.

        Returns:
            CheckResult indicating if email config is valid.
        """
        start_time = time.perf_counter()

        if not self.config:
            return self._fail(
                "Email config complete",
                "Config not loaded",
                start_time,
            )

        try:
            email_config = self.config.get_email_config()
            provider = email_config.get("provider", "smtp")
            missing_fields: list[str] = []

            if provider == "aws_ses":
                # AWS SES: check aws_ses.sender or from_address
                aws_ses_config = email_config.get("aws_ses", {})
                if not aws_ses_config.get("sender") and not email_config.get("from_address"):
                    missing_fields.append("aws_ses.sender")
            else:  # SMTP
                # SMTP: check nested smtp config
                smtp_config = email_config.get("smtp", {})
                if not smtp_config.get("server"):
                    missing_fields.append("smtp.server")
                if not smtp_config.get("port"):
                    missing_fields.append("smtp.port")
                if not smtp_config.get("sender") and not email_config.get("from_address"):
                    missing_fields.append("smtp.sender")

            # Check recipients
            recipients = email_config.get("recipients", [])
            if not recipients:
                missing_fields.append("recipients")

            if missing_fields:
                return self._fail(
                    "Email config complete",
                    f"Missing fields: {', '.join(missing_fields)}",
                    start_time,
                    {"provider": provider, "missing": missing_fields},
                )

            recipient_count = len(recipients) if isinstance(recipients, list) else 1
            return self._pass(
                "Email config complete",
                f"Provider: {provider}, Recipients: {recipient_count}",
                start_time,
                {"provider": provider, "recipient_count": recipient_count},
            )

        except Exception as e:
            return self._fail(
                "Email config complete",
                f"Error reading email config: {e}",
                start_time,
            )

    def _check_recall_sources(self) -> CheckResult:
        """Check that at least one recall source is configured.

        Returns:
            CheckResult indicating if recall sources are configured.
        """
        start_time = time.perf_counter()

        if not self.config:
            return self._fail(
                "Recall sources configured",
                "Config not loaded",
                start_time,
            )

        try:
            recall_config = self.config.get_recall_config()
            sources = recall_config.get("sources", {})

            active_sources: list[str] = []

            # Check each source type
            if sources.get("registrations"):
                active_sources.append("registrations")
            if sources.get("military"):
                active_sources.append("military")
            if sources.get("aircraft_types"):
                active_sources.append("aircraft_types")
            if sources.get("regions"):
                active_sources.append("regions")

            if not active_sources:
                return self._warn(
                    "Recall sources configured",
                    "No recall sources defined (system will not track any aircraft)",
                    start_time,
                )

            return self._pass(
                "Recall sources configured",
                f"Active: {', '.join(active_sources)}",
                start_time,
                {"sources": active_sources, "count": len(active_sources)},
            )

        except Exception as e:
            return self._fail(
                "Recall sources configured",
                f"Error reading recall config: {e}",
                start_time,
            )
