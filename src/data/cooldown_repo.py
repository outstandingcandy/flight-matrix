"""Repository for `report_cooldowns`.

Encodes the cooldown rules that prevent duplicate reports for the same
aircraft:

- A cooldown key is `"<aircraft_hex><key_suffix>"`; `key_suffix` lets the
  three filter modes (snapshot, schedule, regmatch) maintain independent
  cooldowns.
- A report is allowed when either the time window has elapsed *and* the
  aircraft has moved at least `min_move_km`, or when we've never seen this
  cooldown key before.
- Failures fail-open: on any DB error we allow the report, so a broken DB
  doesn't silently suppress alerts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.geo.geo import haversine_distance

logger = logging.getLogger("database.cooldown_repo")


class CooldownRepository:
    """CRUD + policy for the `report_cooldowns` table."""

    def __init__(self, session_factory: sessionmaker, *, is_postgres: bool) -> None:
        self._session_factory = session_factory
        self._is_postgres = is_postgres

    def should_generate_report(
        self,
        aircraft_hex: str,
        lat: float | None,
        lon: float | None,
        cooldown_hours: float,
        min_move_km: float,
        key_suffix: str = "",
    ) -> bool:
        """Apply the cooldown rule. See module docstring."""
        cooldown_key = f"{aircraft_hex}{key_suffix}"
        session = self._session_factory()
        try:
            result = session.execute(
                text(
                    "SELECT last_report_time, last_latitude, last_longitude "
                    "FROM report_cooldowns WHERE aircraft_hex = :hex"
                ),
                {"hex": cooldown_key},
            ).fetchone()
            if not result:
                return True  # First sighting.

            last_time = result.last_report_time
            if isinstance(last_time, str):
                last_time = datetime.fromisoformat(last_time)
            cooldown_expired = datetime.now() - last_time > timedelta(hours=cooldown_hours)
            if not cooldown_expired:
                return False

            if result.last_latitude and result.last_longitude and lat and lon:
                distance = haversine_distance(
                    float(result.last_latitude), float(result.last_longitude), lat, lon
                )
                return distance >= min_move_km
            return True
        except Exception as e:
            logger.error(f"Error checking report cooldown: {e}")
            return True  # Fail-open.
        finally:
            session.close()

    def update(
        self,
        aircraft_hex: str,
        latitude: float | None,
        longitude: float | None,
        key_suffix: str = "",
    ) -> None:
        """Record a just-sent report — timestamp, position, increment count."""
        cooldown_key = f"{aircraft_hex}{key_suffix}"
        session = self._session_factory()
        try:
            result = session.execute(
                text(
                    "UPDATE report_cooldowns "
                    "SET last_report_time = CURRENT_TIMESTAMP, "
                    "    last_latitude = :lat, "
                    "    last_longitude = :lon, "
                    "    report_count = report_count + 1, "
                    "    updated_at = CURRENT_TIMESTAMP "
                    "WHERE aircraft_hex = :hex"
                ),
                {"hex": cooldown_key, "lat": latitude, "lon": longitude},
            )
            if result.rowcount == 0:
                session.execute(
                    text(
                        "INSERT INTO report_cooldowns "
                        "(aircraft_hex, last_report_time, last_latitude, "
                        " last_longitude, report_count) "
                        "VALUES (:hex, CURRENT_TIMESTAMP, :lat, :lon, 1)"
                    ),
                    {"hex": cooldown_key, "lat": latitude, "lon": longitude},
                )
            session.commit()
            logger.debug(f"Updated cooldown for aircraft {cooldown_key}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating report cooldown: {e}")
        finally:
            session.close()

    def get_status(
        self,
        aircraft_hex: str,
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict:
        """Return a diagnostic dict describing the cooldown state.

        Used by /admin routes to render cooldown tables.
        """
        session = self._session_factory()
        try:
            result = session.execute(
                text(
                    "SELECT last_report_time, last_latitude, last_longitude, report_count "
                    "FROM report_cooldowns WHERE aircraft_hex = :hex"
                ),
                {"hex": aircraft_hex},
            ).fetchone()
            if not result:
                return {"has_previous_report": False}

            last_time = result.last_report_time
            if isinstance(last_time, str):
                last_time = datetime.fromisoformat(last_time)
            hours_since = (datetime.now() - last_time).total_seconds() / 3600

            status: dict = {
                "has_previous_report": True,
                "last_report_time": last_time.isoformat() if last_time else None,
                "hours_since_last_report": hours_since,
                "report_count": result.report_count,
            }
            if lat and lon and result.last_latitude and result.last_longitude:
                status["distance_moved_km"] = haversine_distance(
                    float(result.last_latitude), float(result.last_longitude), lat, lon
                )
            return status
        except Exception as e:
            logger.error(f"Error getting cooldown status: {e}")
            return {"has_previous_report": False}
        finally:
            session.close()

    def cleanup_old(self, max_age_hours: float = 24.0) -> None:
        """Delete cooldown rows older than `max_age_hours`."""
        session = self._session_factory()
        try:
            if self._is_postgres:
                sql = (
                    "DELETE FROM report_cooldowns "
                    f"WHERE last_report_time < NOW() - INTERVAL '{int(max_age_hours)} hours'"
                )
            else:
                sql = (
                    "DELETE FROM report_cooldowns "
                    f"WHERE last_report_time < datetime('now', '-{int(max_age_hours)} hours')"
                )
            result = session.execute(text(sql))
            session.commit()
            if result.rowcount > 0:
                logger.info(f"Cleaned up {result.rowcount} old cooldown records")
        except Exception as e:
            session.rollback()
            logger.error(f"Error cleaning up old cooldowns: {e}")
        finally:
            session.close()
