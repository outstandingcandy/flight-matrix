"""
Filter Management Service

Handles user-specific SQL filter management and execution
for the multi-user subscription system.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from src.data.models import UserCooldown, UserFilter
from src.data.sql_validation import check_dangerous_patterns
from src.geo.geo import haversine_distance
from src.services.base import BaseService
from src.utils.sql_utils import strip_sql_comments

logger = logging.getLogger("filter_service")

# Allowed fields for filtering
ALLOWED_FILTER_FIELDS = {
    "hex",
    "flight_number",
    "registration",
    "aircraft_type",
    "latitude",
    "longitude",
    "altitude_baro",
    "altitude_geom",
    "ground_speed",
    "track",
    "vertical_rate",
    "squawk",
    "emergency",
    "category",
    "country_of_registration",
    "current_country",
    "is_military",
    "is_interesting",
    "snapshot_time",
}


class FilterService(BaseService):
    """Filter management service for multi-user system.

    Handles creation, validation, and execution of user-specific
    SQL filters for aircraft data.

    Attributes:
        db: Database manager instance
    """

    def get_filter(self, filter_id: int) -> UserFilter | None:
        """Get a filter by ID.

        Args:
            filter_id: The filter ID

        Returns:
            UserFilter object if found, None otherwise
        """
        with self.readonly_session() as session:
            return session.query(UserFilter).filter(UserFilter.id == filter_id).first()

    def get_user_filters(self, user_id: int, active_only: bool = True) -> list[UserFilter]:
        """Get all filters for a user.

        Args:
            user_id: The user's ID
            active_only: If True, only return active filters

        Returns:
            List of UserFilter objects
        """
        with self.readonly_session() as session:
            query = session.query(UserFilter).filter(UserFilter.user_id == user_id)

            if active_only:
                query = query.filter(UserFilter.is_active == True)  # noqa: E712

            return query.order_by(UserFilter.priority.desc()).all()

    def create_filter(
        self,
        user_id: int,
        name: str,
        filter_sql: str,
        description: str | None = None,
        priority: int = 0,
    ) -> tuple[UserFilter | None, str | None]:
        """Create a new filter for a user.

        Args:
            user_id: The user's ID
            name: Filter name
            filter_sql: SQL WHERE clause
            description: Optional description
            priority: Filter priority (higher = processed first)

        Returns:
            Tuple of (UserFilter object or None, error message or None)
        """
        # Validate the SQL first
        is_valid, error_msg = self.validate_filter_sql(filter_sql)
        if not is_valid:
            logger.warning(f"Invalid filter SQL for user {user_id}: {error_msg}")
            return None, error_msg

        try:
            with self.session_scope() as session:
                user_filter = UserFilter(
                    user_id=user_id,
                    name=name,
                    description=description,
                    filter_sql=filter_sql,
                    is_active=True,
                    priority=priority,
                )

                session.add(user_filter)
                # Flush before refresh/expunge — expunging a still-pending
                # instance removes it from ``session.new`` so the commit
                # never inserts it. Refresh materialises server-side
                # defaults (created_at) into the instance so ``to_dict``
                # doesn't try to lazy-load off a detached row.
                session.flush()
                session.refresh(user_filter)
                logger.info(f"Created filter '{name}' for user {user_id}")

                session.expunge(user_filter)
                return user_filter, None

        except Exception as e:
            logger.error(f"Error creating filter for user {user_id}: {e}")
            return None, str(e)

    def update_filter(
        self,
        filter_id: int,
        name: str | None = None,
        filter_sql: str | None = None,
        description: str | None = None,
        is_active: bool | None = None,
        priority: int | None = None,
    ) -> bool:
        """Update a filter.

        Args:
            filter_id: The filter ID
            name: New name (optional)
            filter_sql: New SQL clause (optional)
            description: New description (optional)
            is_active: New active status (optional)
            priority: New priority (optional)

        Returns:
            True if update successful, False otherwise
        """
        try:
            with self.session_scope() as session:
                user_filter = session.query(UserFilter).filter(UserFilter.id == filter_id).first()

                if not user_filter:
                    logger.warning(f"Filter {filter_id} not found")
                    return False

                if filter_sql is not None:
                    is_valid, error_msg = self.validate_filter_sql(filter_sql)
                    if not is_valid:
                        logger.warning(f"Invalid filter SQL: {error_msg}")
                        return False
                    user_filter.filter_sql = filter_sql

                if name is not None:
                    user_filter.name = name
                if description is not None:
                    user_filter.description = description
                if is_active is not None:
                    user_filter.is_active = is_active
                if priority is not None:
                    user_filter.priority = priority

                logger.info(f"Updated filter {filter_id}")
                return True

        except Exception as e:
            logger.error(f"Error updating filter {filter_id}: {e}")
            return False

    def delete_filter(self, filter_id: int) -> bool:
        """Delete a filter.

        Args:
            filter_id: The filter ID

        Returns:
            True if deletion successful, False otherwise
        """
        try:
            with self.session_scope() as session:
                user_filter = session.query(UserFilter).filter(UserFilter.id == filter_id).first()

                if not user_filter:
                    logger.warning(f"Filter {filter_id} not found")
                    return False

                session.delete(user_filter)
                logger.info(f"Deleted filter {filter_id}")
                return True

        except Exception as e:
            logger.error(f"Error deleting filter {filter_id}: {e}")
            return False

    def validate_filter_sql(self, filter_sql: str) -> tuple[bool, str | None]:
        """Validate a filter SQL clause for safety.

        Args:
            filter_sql: SQL WHERE clause to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not filter_sql or not filter_sql.strip():
            return False, "Filter SQL cannot be empty"

        # Check for dangerous patterns (shared policy with SQLFilterEngine).
        hit = check_dangerous_patterns(filter_sql)
        if hit:
            return False, f"SQL contains prohibited pattern: {hit}"

        # Try to parse and validate the SQL structure
        try:
            # Test the SQL by building a test query
            test_query = f"SELECT 1 FROM aircraft_snapshots WHERE ({filter_sql}) LIMIT 1"
            with self.readonly_session() as session:
                # Use EXPLAIN to validate without executing
                if self.db.is_postgres:
                    session.execute(text(f"EXPLAIN {test_query}"))
                else:
                    # SQLite doesn't support EXPLAIN QUERY PLAN for validation
                    session.execute(text(test_query))
                return True, None

        except Exception as e:
            return False, f"Invalid SQL syntax: {e!s}"

    def execute_user_filters(
        self, user_id: int, limit: int = 1000, max_age_hours: float | None = None
    ) -> list[dict[str, Any]]:
        """Execute all active filters for a user.

        Args:
            user_id: The user's ID
            limit: Maximum number of results
            max_age_hours: Only return snapshots from the last N hours (optional)

        Returns:
            List of aircraft matching the user's filters
        """
        filters = self.get_user_filters(user_id, active_only=True)

        if not filters:
            logger.debug(f"No active filters for user {user_id}")
            return []

        # Combine all filter conditions with OR
        filter_conditions = []
        for f in filters:
            # Strip SQL comments and trailing commas
            cleaned_sql = strip_sql_comments(f.filter_sql)
            filter_conditions.append(f"({cleaned_sql})")

        combined_sql = " OR ".join(filter_conditions)

        # Add time restriction if specified
        if max_age_hours is not None:
            if self.db.is_postgres:
                time_filter = f"snapshot_time >= NOW() - INTERVAL '{max_age_hours} hours'"
            else:
                time_filter = f"snapshot_time >= datetime('now', '-{max_age_hours} hours')"
            combined_sql = f"({combined_sql}) AND ({time_filter})"
            logger.debug(f"Applied time filter: {max_age_hours} hours")

        # Execute the filter query
        return self.db.execute_filter_query(combined_sql, limit)

    def test_filter(
        self, filter_sql: str, limit: int = 10
    ) -> tuple[bool, list[dict[str, Any]], str | None]:
        """Test a filter SQL and return sample results.

        Args:
            filter_sql: SQL WHERE clause to test
            limit: Maximum number of sample results

        Returns:
            Tuple of (success, results, error_message)
        """
        # Validate first
        is_valid, error_msg = self.validate_filter_sql(filter_sql)
        if not is_valid:
            return False, [], error_msg

        try:
            results = self.db.execute_filter_query(filter_sql, limit)
            return True, results, None
        except Exception as e:
            return False, [], f"Execution error: {e!s}"

    # =========================================================================
    # User Cooldown Management
    # =========================================================================

    def should_report_for_user(
        self,
        user_id: int,
        aircraft_hex: str,
        lat: float | None,
        lon: float | None,
        cooldown_hours: float,
        min_move_km: float = 1.0,
    ) -> bool:
        """Check if a report should be sent to a specific user.

        Args:
            user_id: The user's ID
            aircraft_hex: ICAO hex code
            lat: Current latitude
            lon: Current longitude
            cooldown_hours: Minimum hours between reports
            min_move_km: Minimum distance aircraft must move

        Returns:
            True if report should be sent
        """
        try:
            with self.readonly_session() as session:
                cooldown = (
                    session.query(UserCooldown)
                    .filter(
                        UserCooldown.user_id == user_id,
                        UserCooldown.aircraft_hex == aircraft_hex,
                    )
                    .first()
                )

                if not cooldown:
                    return True  # First time seeing this aircraft for this user

                # Check time-based cooldown
                time_diff = datetime.now() - cooldown.last_report_time
                if time_diff < timedelta(hours=cooldown_hours):
                    logger.info(
                        f"Cooldown active for user {user_id}, aircraft {aircraft_hex}, time left: {timedelta(hours=cooldown_hours) - time_diff}"
                    )
                    return False

                # Check distance-based cooldown
                if cooldown.last_latitude and cooldown.last_longitude and lat and lon:
                    distance = haversine_distance(
                        float(cooldown.last_latitude),
                        float(cooldown.last_longitude),
                        lat,
                        lon,
                    )
                    if distance < min_move_km:
                        logger.info(
                            f"Aircraft {aircraft_hex} has not moved enough for user {user_id}: {distance:.2f} km"
                        )
                        return False

                return True

        except Exception as e:
            logger.error(
                f"Error checking cooldown for user {user_id}, aircraft {aircraft_hex}: {e}"
            )
            return True  # Allow on error

    def update_user_cooldown(
        self,
        user_id: int,
        aircraft_hex: str,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> bool:
        """Update cooldown record after sending a report to a user.

        Args:
            user_id: The user's ID
            aircraft_hex: ICAO hex code
            latitude: Aircraft latitude
            longitude: Aircraft longitude

        Returns:
            True if update successful
        """
        try:
            with self.session_scope() as session:
                cooldown = (
                    session.query(UserCooldown)
                    .filter(
                        UserCooldown.user_id == user_id,
                        UserCooldown.aircraft_hex == aircraft_hex,
                    )
                    .first()
                )

                if cooldown:
                    cooldown.last_report_time = datetime.now()
                    # ``Numeric`` columns type as ``Decimal | None`` on the
                    # ORM side; the caller hands us plain floats and
                    # SQLAlchemy coerces on the way to the DB. Cast to
                    # ``Decimal`` here to keep the type annotation honest.
                    cooldown.last_latitude = Decimal(str(latitude))
                    cooldown.last_longitude = Decimal(str(longitude))
                    cooldown.report_count = (cooldown.report_count or 0) + 1
                else:
                    cooldown = UserCooldown(
                        user_id=user_id,
                        aircraft_hex=aircraft_hex,
                        last_report_time=datetime.now(),
                        last_latitude=latitude,
                        last_longitude=longitude,
                        report_count=1,
                    )
                    session.add(cooldown)

                return True

        except Exception as e:
            logger.error(
                f"Error updating cooldown for user {user_id}, aircraft {aircraft_hex}: {e}"
            )
            return False

    def get_user_cooldown_status(self, user_id: int, aircraft_hex: str) -> dict[str, Any] | None:
        """Get cooldown status for a specific aircraft and user.

        Args:
            user_id: The user's ID
            aircraft_hex: ICAO hex code

        Returns:
            Cooldown status dictionary or None
        """
        with self.readonly_session() as session:
            cooldown = (
                session.query(UserCooldown)
                .filter(
                    UserCooldown.user_id == user_id,
                    UserCooldown.aircraft_hex == aircraft_hex,
                )
                .first()
            )

            if not cooldown:
                return None

            hours_since = (datetime.now() - cooldown.last_report_time).total_seconds() / 3600

            return {
                "aircraft_hex": cooldown.aircraft_hex,
                "last_report_time": cooldown.last_report_time.isoformat(),
                "hours_since_last_report": hours_since,
                "last_latitude": float(cooldown.last_latitude) if cooldown.last_latitude else None,
                "last_longitude": float(cooldown.last_longitude)
                if cooldown.last_longitude
                else None,
                "report_count": cooldown.report_count,
            }

    def cleanup_user_cooldowns(self, user_id: int, max_age_hours: float = 24.0) -> int:
        """Clean up old cooldown records for a user.

        Args:
            user_id: The user's ID
            max_age_hours: Remove records older than this

        Returns:
            Number of records deleted
        """
        try:
            with self.session_scope() as session:
                cutoff_time = datetime.now() - timedelta(hours=max_age_hours)

                result = (
                    session.query(UserCooldown)
                    .filter(
                        UserCooldown.user_id == user_id,
                        UserCooldown.last_report_time < cutoff_time,
                    )
                    .delete()
                )

                return result

        except Exception as e:
            logger.error(f"Error cleaning up cooldowns for user {user_id}: {e}")
            return 0
