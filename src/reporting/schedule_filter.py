"""
Schedule Filter Engine - Flight schedule-based aircraft filtering

This module implements Mode B of the dual-mode filtering architecture,
filtering aircraft based on their scheduled flights to/from target airports.
"""

import logging

from sqlalchemy import text

from src.utils.database import DatabaseManager
from src.utils.sql_utils import strip_sql_comments
from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("schedule_filter")


class ScheduleFilterEngine:
    """Schedule-based filter engine for flight schedules.

    Finds aircraft that have scheduled flights to/from target airports
    within a configurable time window, enabling proactive tracking of
    aircraft before they appear in real-time position data.

    Attributes:
        db: Database manager instance
        config: YAML configuration manager
    """

    def __init__(self, database_manager: DatabaseManager, config_manager: YAMLConfig | None = None):
        """Initialize the schedule filter engine.

        Args:
            database_manager: Database manager instance
            config_manager: YAML configuration manager
        """
        self.db = database_manager
        self.config = config_manager

    def get_schedule_config(self) -> dict:
        """Get schedule-based filter configuration.

        Returns:
            Configuration dict with schedule filter settings
        """
        if not self.config:
            return {}

        filter_modes = self.config.get("reporting.filter_modes", {})
        return filter_modes.get("schedule_based", {})

    def is_enabled(self) -> bool:
        """Check if schedule-based filtering is enabled.

        Returns:
            True if enabled
        """
        config = self.get_schedule_config()
        return config.get("enabled", False)

    def find_target_aircraft(
        self, custom_sql_filter: str | None = None, limit: int = 10000
    ) -> list[dict]:
        """Find aircraft with scheduled flights to/from target airports.

        Queries the flight_schedules table for flights within the time window
        and joins with aircraft_snapshots to get current position data.

        Args:
            custom_sql_filter: Optional SQL WHERE clause to apply to aircraft
                              (typically reused from snapshot_based filter)
            limit: Maximum number of results

        Returns:
            List of aircraft dictionaries matching the schedule criteria
        """
        config = self.get_schedule_config()

        if not config.get("enabled", False):
            logger.debug("Schedule-based filtering is disabled")
            return []

        # Get configuration values
        lookahead_hours = config.get("lookahead_hours", 8.0)
        lookbehind_hours = config.get("lookbehind_hours", 2.0)
        target_airports = config.get("target_airports", [])
        flight_types = config.get("flight_types", ["arrival", "departure"])

        if not target_airports:
            logger.warning("No target airports configured for schedule-based filtering")
            return []

        logger.info(
            f"Schedule filter: airports={target_airports}, "
            f"window=-{lookbehind_hours}h to +{lookahead_hours}h, "
            f"types={flight_types}"
        )

        # Build the query
        session = self.db.get_session()
        try:
            # Build airport list for SQL IN clause
            airport_placeholders = ", ".join([f":airport_{i}" for i in range(len(target_airports))])
            airport_params = {f"airport_{i}": airport for i, airport in enumerate(target_airports)}

            # Build flight type list for SQL IN clause
            type_placeholders = ", ".join([f":type_{i}" for i in range(len(flight_types))])
            type_params = {f"type_{i}": ftype for i, ftype in enumerate(flight_types)}

            # Build the custom SQL filter condition
            aircraft_filter_sql = ""
            if (
                custom_sql_filter
                and custom_sql_filter.strip()
                and custom_sql_filter.strip() != "1=1"
            ):
                # Convert boolean syntax for PostgreSQL compatibility
                converted_sql = self.db._convert_boolean_syntax(custom_sql_filter)
                aircraft_filter_sql = f"AND ({converted_sql})"

            # Use dialect-specific datetime intervals
            if self.db.is_postgres:
                time_condition = f"""
                    scheduled_time BETWEEN
                        NOW() - INTERVAL '{lookbehind_hours} hours'
                        AND NOW() + INTERVAL '{lookahead_hours} hours'
                """
                snapshot_time_condition = "snapshot_time >= NOW() - INTERVAL '1 hour'"
            else:
                # SQLite
                time_condition = f"""
                    scheduled_time BETWEEN
                        datetime('now', '-{lookbehind_hours} hours')
                        AND datetime('now', '+{lookahead_hours} hours')
                """
                snapshot_time_condition = "snapshot_time >= datetime('now', '-1 hour')"

            # Main query: find aircraft with scheduled flights to target airports
            query = f"""
                WITH target_schedules AS (
                    SELECT DISTINCT
                        aircraft_registration,
                        airport_iata,
                        remote_airport_iata,
                        scheduled_time,
                        flight_type,
                        flight_number,
                        airline_name,
                        status
                    FROM flight_schedules
                    WHERE airport_iata IN ({airport_placeholders})
                      AND flight_type IN ({type_placeholders})
                      AND {time_condition}
                      AND aircraft_registration IS NOT NULL
                      AND aircraft_registration != ''
                      AND status NOT IN ('Landed', 'Cancelled')
                )
                SELECT DISTINCT ON (snap.hex)
                    snap.hex,
                    snap.flight_number AS flight,
                    snap.registration AS r,
                    snap.aircraft_type AS t,
                    snap.latitude AS lat,
                    snap.longitude AS lon,
                    snap.altitude_baro AS alt_baro,
                    snap.altitude_geom AS alt_geom,
                    snap.ground_speed AS gs,
                    snap.track,
                    snap.vertical_rate AS baro_rate,
                    snap.squawk,
                    snap.emergency,
                    snap.category,
                    snap.country_of_registration,
                    snap.current_country,
                    snap.is_military,
                    snap.is_interesting,
                    snap.snapshot_time,
                    ts.airport_iata AS schedule_airport,
                    ts.remote_airport_iata AS schedule_remote_airport,
                    ts.scheduled_time AS schedule_time,
                    ts.flight_type AS schedule_type,
                    ts.flight_number AS schedule_flight_number,
                    ts.status AS schedule_status
                FROM aircraft_snapshots snap
                INNER JOIN target_schedules ts
                    ON snap.registration = ts.aircraft_registration
                WHERE {snapshot_time_condition}
                    {aircraft_filter_sql}
                ORDER BY snap.hex, snap.snapshot_time DESC
                LIMIT :limit
            """

            # Combine all parameters
            params = {**airport_params, **type_params, "limit": limit}

            result = session.execute(text(query), params)
            rows = result.fetchall()

            # Convert to list of dicts
            aircraft_list = []
            for row in rows:
                aircraft = dict(row._mapping)
                aircraft_list.append(aircraft)

            logger.info(f"Schedule filter found {len(aircraft_list)} aircraft")
            return aircraft_list

        except Exception as e:
            logger.error(f"Error executing schedule filter: {e}")
            return []
        finally:
            session.close()

    def get_custom_sql_filter(self) -> str | None:
        """Get custom SQL filter from snapshot_based configuration.

        Returns the custom SQL filter if reuse_snapshot_filters is enabled.
        Strips SQL comments (--) to avoid syntax errors when inlining.

        Returns:
            SQL WHERE clause string or None
        """
        schedule_config = self.get_schedule_config()

        if not schedule_config.get("reuse_snapshot_filters", False):
            return None

        if not self.config:
            return None

        # Get custom SQL from snapshot-based filters
        filters_config = self.config.get("reporting.filters", {})
        custom_sql = filters_config.get("custom_sql", "")

        if custom_sql and custom_sql.strip():
            logger.debug("Reusing custom_sql filter from snapshot_based configuration")
            # Strip SQL comments (-- style) to avoid issues when inlining
            cleaned_sql = strip_sql_comments(custom_sql)
            return cleaned_sql.strip()

        return None

    def execute_filter(self, limit: int = 10000) -> list[dict]:
        """Execute the schedule-based filter.

        Convenience method that combines configuration reading and filtering.

        Args:
            limit: Maximum number of results

        Returns:
            List of aircraft dictionaries matching the schedule criteria
        """
        custom_sql = self.get_custom_sql_filter()
        return self.find_target_aircraft(custom_sql_filter=custom_sql, limit=limit)
