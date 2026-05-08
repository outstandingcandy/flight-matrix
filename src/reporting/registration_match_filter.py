"""
Registration Match Filter Engine - Mode C

This module implements Mode C of the filtering architecture,
matching aircraft from FR24 realtime positions (aircraft_realtime_positions)
that are missing registration numbers with ADS-B data (aircraft_snapshots)
using time and spatial correlation.
"""

import logging

from sqlalchemy import text

from src.utils.database import DatabaseManager
from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("registration_match_filter")


class RegistrationMatchFilterEngine:
    """Registration match filter engine for aircraft without registration.

    Correlates aircraft_realtime_positions (FR24 data) with aircraft_snapshots
    (ADS-B data) to find matching registrations for aircraft that are missing
    registration numbers in the FR24 data.

    Match criteria:
    - Time: within configurable window (default ±3 minutes)
    - Distance: within configurable radius (default <5km)
    - Aircraft type: must match if both have type information
    - Altitude: within configurable difference (default <2000ft)

    Attributes:
        db: Database manager instance
        config: YAML configuration manager
    """

    def __init__(
        self,
        database_manager: DatabaseManager,
        config_manager: YAMLConfig | None = None,
    ):
        """Initialize the registration match filter engine.

        Args:
            database_manager: Database manager instance
            config_manager: YAML configuration manager
        """
        self.db = database_manager
        self.config = config_manager

    def get_match_config(self) -> dict:
        """Get registration match filter configuration.

        Returns:
            Configuration dict with match filter settings
        """
        if not self.config:
            return {}

        filter_modes = self.config.get("reporting.filter_modes", {})
        return filter_modes.get("registration_match_based", {})

    def is_enabled(self) -> bool:
        """Check if registration match filtering is enabled.

        Returns:
            True if enabled
        """
        config = self.get_match_config()
        return config.get("enabled", False)

    def execute_filter(self, limit: int = 1000) -> list[dict]:
        """Execute match query and return matched aircraft.

        Queries aircraft_realtime_positions for entries without registration,
        then joins with aircraft_snapshots to find matching registrations
        based on time and spatial correlation.

        Args:
            limit: Maximum number of results

        Returns:
            List of aircraft dictionaries with matched registrations
        """
        config = self.get_match_config()

        if not config.get("enabled", False):
            logger.debug("Registration match filtering is disabled")
            return []

        # Get configuration values
        time_window_minutes = config.get("time_window_minutes", 3.0)
        max_distance_km = config.get("max_distance_km", 5.0)
        max_altitude_diff_ft = config.get("max_altitude_diff_ft", 2000)
        max_age_hours = config.get("max_age_hours", 1.0)

        logger.info(
            f"Registration match filter: time_window=±{time_window_minutes}min, "
            f"max_distance={max_distance_km}km, max_altitude_diff={max_altitude_diff_ft}ft, "
            f"max_age={max_age_hours}h"
        )

        if not self.db.is_postgres:
            logger.warning("Registration match filter only supports PostgreSQL")
            return []

        session = self.db.get_session()
        try:
            # Convert lat/lon degree threshold from km
            # At equator, 1 degree ~ 111km, so for 5km: ~0.045 degrees
            # We use 0.08 degrees (~9km) as a bounding box for initial filtering
            # then apply precise distance calculation
            lat_lon_threshold = 0.08

            # Optimized query: first filter FR24 positions to small candidate set,
            # then join with snapshots. This avoids full table scan on large tables.
            query = f"""
                WITH fr24_candidates AS (
                    -- Step 1: Get recent FR24 positions missing registration (small subset)
                    SELECT DISTINCT ON (fr24_id)
                        fr24_id, flight_number, aircraft_type,
                        latitude, longitude, altitude,
                        origin_iata, destination_iata, scraped_at
                    FROM aircraft_realtime_positions
                    WHERE scraped_at >= NOW() - INTERVAL '{max_age_hours} hours'
                      AND (registration IS NULL OR registration = '')
                      AND latitude IS NOT NULL
                      AND longitude IS NOT NULL
                    ORDER BY fr24_id, scraped_at DESC
                ),
                snapshot_candidates AS (
                    -- Step 2: Get recent snapshots with registration (for join)
                    SELECT DISTINCT ON (hex)
                        hex, registration, aircraft_type,
                        latitude, longitude, altitude_baro,
                        is_military, is_interesting,
                        country_of_registration, current_country,
                        snapshot_time
                    FROM aircraft_snapshots
                    WHERE snapshot_time >= NOW() - INTERVAL '{max_age_hours + (time_window_minutes / 60.0)} hours'
                      AND registration IS NOT NULL
                      AND registration != ''
                      AND latitude IS NOT NULL
                      AND longitude IS NOT NULL
                    ORDER BY hex, snapshot_time DESC
                ),
                matched AS (
                    -- Step 3: Join candidates with spatial and temporal matching
                    SELECT DISTINCT ON (n.fr24_id)
                        s.hex,
                        s.registration AS r,
                        s.aircraft_type AS t,
                        n.latitude AS lat,
                        n.longitude AS lon,
                        s.altitude_baro AS alt_baro,
                        n.flight_number AS flight,
                        n.origin_iata,
                        n.destination_iata,
                        s.is_military,
                        s.is_interesting,
                        s.country_of_registration,
                        s.current_country,
                        n.fr24_id,
                        n.scraped_at,
                        111.0 * SQRT(
                            POWER(n.latitude - s.latitude, 2) +
                            POWER((n.longitude - s.longitude) * COS(RADIANS(n.latitude)), 2)
                        ) AS match_distance_km,
                        ABS(COALESCE(n.altitude, 0) - COALESCE(s.altitude_baro, 0)) AS altitude_diff_ft
                    FROM fr24_candidates n
                    JOIN snapshot_candidates s ON
                        -- Time window
                        s.snapshot_time BETWEEN
                            n.scraped_at - INTERVAL '{time_window_minutes} minutes'
                            AND n.scraped_at + INTERVAL '{time_window_minutes} minutes'
                        -- Spatial bounding box
                        AND s.latitude BETWEEN n.latitude - {lat_lon_threshold}
                            AND n.latitude + {lat_lon_threshold}
                        AND s.longitude BETWEEN n.longitude - {lat_lon_threshold}
                            AND n.longitude + {lat_lon_threshold}
                        -- Aircraft type match (if available)
                        AND (n.aircraft_type IS NULL OR n.aircraft_type = ''
                             OR s.aircraft_type = n.aircraft_type)
                    WHERE
                        -- Precise distance filter
                        111.0 * SQRT(
                            POWER(n.latitude - s.latitude, 2) +
                            POWER((n.longitude - s.longitude) * COS(RADIANS(n.latitude)), 2)
                        ) < {max_distance_km}
                        -- Altitude difference filter
                        AND ABS(COALESCE(n.altitude, 0) - COALESCE(s.altitude_baro, 0))
                            < {max_altitude_diff_ft}
                    ORDER BY n.fr24_id, 111.0 * SQRT(
                        POWER(n.latitude - s.latitude, 2) +
                        POWER((n.longitude - s.longitude) * COS(RADIANS(n.latitude)), 2)
                    ) ASC
                )
                SELECT * FROM matched
                LIMIT :limit
            """

            result = session.execute(text(query), {"limit": limit})
            rows = result.fetchall()

            # Convert to list of dicts with standard aircraft format
            aircraft_list = []
            for row in rows:
                row_dict = dict(row._mapping)

                # Build standardized aircraft dict
                aircraft = {
                    "hex": row_dict.get("hex"),
                    "r": row_dict.get("r"),  # registration
                    "t": row_dict.get("t"),  # aircraft_type
                    "lat": float(row_dict["lat"]) if row_dict.get("lat") else None,
                    "lon": float(row_dict["lon"]) if row_dict.get("lon") else None,
                    "alt_baro": row_dict.get("alt_baro"),
                    "flight": row_dict.get("flight"),
                    "is_military": row_dict.get("is_military"),
                    "is_interesting": row_dict.get("is_interesting"),
                    "country_of_registration": row_dict.get("country_of_registration"),
                    "current_country": row_dict.get("current_country"),
                    "origin_iata": row_dict.get("origin_iata"),
                    "destination_iata": row_dict.get("destination_iata"),
                    # Match metadata
                    "match_source": "registration_match",
                    "match_distance_km": float(row_dict.get("match_distance_km", 0)),
                    "altitude_diff_ft": int(row_dict.get("altitude_diff_ft", 0)),
                }

                aircraft_list.append(aircraft)

            logger.info(f"Registration match filter found {len(aircraft_list)} aircraft")
            return aircraft_list

        except Exception as e:
            logger.error(f"Error executing registration match filter: {e}")
            return []
        finally:
            session.close()
