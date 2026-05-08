"""
SQL Filter Engine - executes filter queries using custom SQL.
"""

import logging
import re

from src.data.sql_validation import DangerousSQLError, assert_safe_where_clause
from src.utils.sql_utils import strip_sql_comments

logger = logging.getLogger("sql_filter")


class SQLFilterEngine:
    """SQL Filter Engine - executes filter queries using custom SQL."""

    def __init__(self, database_manager, config_manager=None):
        self.db = database_manager
        self.config = config_manager

    def build_where_clause(self) -> str:
        """Build SQL WHERE clause from config.yaml."""
        try:
            conditions = []
            filter_conditions = []  # Stores actual filter conditions (custom_sql and flight_number_prefixes)

            # Load filter configuration from config.yaml (filters merged under reporting)
            if self.config:
                filters_config = self.config.get("reporting.filters", {})

                # Data quality filter: check whether lat/lon information is required
                require_position = filters_config.get("require_position", True)
                if require_position:
                    conditions.append("(latitude IS NOT NULL AND longitude IS NOT NULL)")
                    logger.info("Position filter enabled: requiring lat/lon data")

                # Flight number prefix filter
                flight_prefixes = filters_config.get("flight_number_prefixes", [])
                if flight_prefixes:
                    prefix_conditions = self._build_flight_prefix_conditions(flight_prefixes)
                    if prefix_conditions:
                        filter_conditions.append(prefix_conditions)
                        logger.info(f"Flight number prefix filter enabled: {flight_prefixes}")

                # Custom SQL filter
                sql_config = filters_config.get("custom_sql", "")
                if sql_config:
                    # Strip comments and trailing commas before validation
                    cleaned_sql = strip_sql_comments(sql_config)
                    validated_sql = self._validate_custom_sql(cleaned_sql)
                    filter_conditions.append(f"({validated_sql})")

                # Combine all filter conditions with OR (any condition satisfied is enough)
                if filter_conditions:
                    combined_filters = " OR ".join(filter_conditions)
                    conditions.append(f"({combined_filters})")

            # Combine all conditions
            if conditions:
                return " AND ".join(conditions)

            # If no configuration, return a clause matching all records
            logger.warning("No filter configuration found, returning default filter")
            return "1=1"

        except Exception as e:
            logger.error(f"Error building WHERE clause: {e}")
            return "1=1"  # Safe default that matches all records

    def _build_flight_prefix_conditions(self, prefixes: list[str]) -> str:
        """
        Build flight number prefix filter conditions.

        Args:
            prefixes: List of flight number prefixes, e.g. ['VIP', 'GAF', 'SAM']

        Returns:
            SQL condition string, e.g. "(flight_number LIKE 'VIP%' OR flight_number LIKE 'GAF%')"
        """
        if not prefixes:
            return ""

        # Validate prefixes; only alphanumeric characters allowed
        valid_prefixes = []
        for prefix in prefixes:
            if prefix and re.match(r"^[A-Za-z0-9]+$", prefix):
                valid_prefixes.append(prefix.upper())
            else:
                logger.warning(f"Invalid flight number prefix skipped: {prefix}")

        if not valid_prefixes:
            return ""

        # Build LIKE conditions
        like_conditions = [f"flight_number LIKE '{prefix}%'" for prefix in valid_prefixes]
        return f"({' OR '.join(like_conditions)})"

    def _validate_custom_sql(self, sql: str) -> str:
        """Validate and sanitize custom SQL to prevent SQL injection.

        Delegates to `src.data.sql_validation` so the admin-filter and
        per-user-filter paths share one policy.
        """
        try:
            return assert_safe_where_clause(sql)
        except DangerousSQLError as e:
            logger.warning(str(e))
            raise ValueError(str(e)) from e

    def execute_filter(self, limit: int = 10000) -> list[dict]:
        """Execute the filter query."""
        where_clause = self.build_where_clause()
        logger.info(f"Executing filter with WHERE clause: {where_clause}")

        return self.db.execute_filter_query(where_clause, limit)

    def execute_filter_with_time_limit(
        self, max_age_hours: float = 1.0, limit: int = 10000
    ) -> list[dict]:
        """Execute a filter query with a time limit.

        Args:
            max_age_hours: Only query snapshot data from the last N hours
            limit: Maximum number of records to return

        Returns:
            List of aircraft data matching the filter
        """
        where_clause = self.build_where_clause()

        # Add time restriction
        if self.db.is_postgres:
            time_filter = f"snapshot_time >= NOW() - INTERVAL '{max_age_hours} hours'"
        else:
            # SQLite
            time_filter = f"snapshot_time >= datetime('now', '-{max_age_hours} hours')"

        # Combine with existing WHERE clause
        combined_where = f"({where_clause}) AND ({time_filter})"
        logger.info(
            f"Executing filter with time limit ({max_age_hours}h): {combined_where[:200]}..."
        )

        return self.db.execute_filter_query(combined_where, limit)

    def validate_filter_config(self) -> bool:
        """Validate the filter configuration."""
        try:
            where_clause = self.build_where_clause()
            return where_clause != "1=1"  # If default is returned, configuration may be invalid
        except Exception as e:
            logger.error(f"Invalid filter configuration: {e}")
            return False
