"""Repository for aircraft snapshots.

Handles ingestion (batch inserts with parallel geocoding), query
(filter-expression evaluation, flight-track retrieval, statistics), and
cleanup of old data.

Constructed from a SessionLocal + engine dialect flag, so the repository
is decoupled from `DatabaseManager`'s lifecycle.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.aircraft.classification import AircraftClassification
from src.data.models import AircraftSnapshot

logger = logging.getLogger("database.snapshot_repo")


class SnapshotRepository:
    """Read and write paths for `aircraft_snapshots` and friends."""

    def __init__(self, session_factory: sessionmaker, *, is_postgres: bool) -> None:
        self._session_factory = session_factory
        self._is_postgres = is_postgres

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _safe_altitude(value: Any) -> int | None:
        """Convert altitude to int; treat 'ground' as 0 and invalid as None."""
        if value is None:
            return None
        if isinstance(value, str):
            if value.lower() == "ground":
                return 0
            try:
                return int(float(value))
            except (ValueError, TypeError):
                return None
        if isinstance(value, (int, float)):
            return int(value)
        return None

    @staticmethod
    def _safe_string(value: Any, max_length: int) -> str | None:
        """Trim, truncate, and return None for empties."""
        if value is None:
            return None
        text_value: str = value if isinstance(value, str) else str(value)
        text_value = text_value.strip()
        if not text_value:
            return None
        return text_value[:max_length]

    @staticmethod
    def _parse_json(data: Any) -> dict | None:
        try:
            return json.loads(data) if isinstance(data, str) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    @staticmethod
    def _is_military(data: dict) -> bool:
        return AircraftClassification.is_military(data)

    @staticmethod
    def _is_interesting(data: dict) -> bool:
        return AircraftClassification.is_interesting(data)

    @staticmethod
    def _calculate_current_country(latitude: float | None, longitude: float | None) -> str | None:
        if latitude is None or longitude is None:
            return None
        try:
            from src.geo.locator import get_geo_locator

            return get_geo_locator().get_country_from_coordinates(latitude, longitude)
        except Exception as e:
            logger.warning(
                f"Failed to calculate current country for ({latitude}, {longitude}): {e}"
            )
            return None

    @staticmethod
    def _convert_boolean_syntax(where_clause: str) -> str:
        """Convert `is_military = 1` to `is_military = true` for Postgres."""
        boolean_fields = ["is_military", "is_interesting"]
        result = where_clause
        for field in boolean_fields:
            result = re.sub(rf"\b{field}\s*=\s*1\b", f"{field} = true", result, flags=re.IGNORECASE)
            result = re.sub(
                rf"\b{field}\s*=\s*0\b", f"{field} = false", result, flags=re.IGNORECASE
            )
        return result

    # ---------------------------------------------------------------------
    # Ingestion
    # ---------------------------------------------------------------------

    def batch_insert(self, aircraft_data_list: list[dict]) -> int:
        """Insert a batch of ADS-B readings, creating static-info rows as needed.

        Geocoding is performed in a single batch call; everything else is
        synchronous.
        """
        if not aircraft_data_list:
            return 0

        start_time = time.time()
        logger.info(f"Starting batch insert for {len(aircraft_data_list)} aircraft")

        snapshots = self._create_snapshots_with_batch_geocoding(aircraft_data_list)

        session = self._session_factory()
        try:
            session.add_all(snapshots)
            session.commit()
            logger.info(
                f"Batch inserted {len(snapshots)} aircraft snapshots in "
                f"{(time.time() - start_time) * 1000:.2f}ms"
            )
            self._auto_create_static_info(aircraft_data_list)
            return len(snapshots)
        except Exception as e:
            session.rollback()
            logger.error(f"Error batch inserting aircraft data: {e}")
            return 0
        finally:
            session.close()

    def _auto_create_static_info(self, aircraft_data_list: list[dict]) -> int:
        """Create placeholder `aircraft_static_info` rows for unseen registrations."""
        if not aircraft_data_list:
            return 0

        registrations: set[str] = set()
        aircraft_map: dict[str, dict] = {}
        for data in aircraft_data_list:
            reg = (data.get("r") or "").strip()
            if reg and reg != "None":
                registrations.add(reg)
                aircraft_map[reg] = data

        if not registrations:
            return 0

        session = self._session_factory()
        try:
            placeholders = ", ".join([f":reg{i}" for i in range(len(registrations))])
            params = {f"reg{i}": reg for i, reg in enumerate(registrations)}
            existing = session.execute(
                text(
                    f"SELECT registration FROM aircraft_static_info "
                    f"WHERE registration IN ({placeholders})"
                ),
                params,
            ).fetchall()
            existing_regs = {row[0] for row in existing}
            new_regs = registrations - existing_regs

            if not new_regs:
                return 0

            created = 0
            for reg in new_regs:
                data = aircraft_map.get(reg, {})
                hex_code = (data.get("hex") or "").strip().lower() or None
                aircraft_type = (data.get("t") or "").strip() or None
                try:
                    session.execute(
                        text(
                            "INSERT INTO aircraft_static_info "
                            "(registration, hex_code, aircraft_type, last_updated) "
                            "VALUES (:reg, :hex, :type, CURRENT_TIMESTAMP) "
                            "ON CONFLICT (registration) DO NOTHING"
                        ),
                        {"reg": reg, "hex": hex_code, "type": aircraft_type},
                    )
                    created += 1
                except Exception as e:
                    logger.debug(f"Failed to create static info for {reg}: {e}")

            session.commit()
            if created > 0:
                logger.info(f"Auto-created {created} new aircraft static info records")
            return created
        except Exception as e:
            session.rollback()
            logger.warning(f"Error auto-creating static info: {e}")
            return 0
        finally:
            session.close()

    def _create_snapshots_with_batch_geocoding(
        self, aircraft_data_list: list[dict]
    ) -> list[AircraftSnapshot]:
        """Build `AircraftSnapshot` rows. Geocoding is done in a single batch call."""
        coords_to_lookup: list[tuple[float, float]] = []
        coord_indices: list[int] = []
        for i, data in enumerate(aircraft_data_list):
            lat = data.get("lat")
            lon = data.get("lon")
            if lat is not None and lon is not None:
                coords_to_lookup.append((lat, lon))
                coord_indices.append(i)

        country_results: dict[int, str | None] = {}
        if coords_to_lookup:
            try:
                from src.geo.locator import get_geo_locator

                geo_locator = get_geo_locator()
                countries = geo_locator._batch_search(coords_to_lookup)
                for i, country in enumerate(countries):
                    if i < len(coord_indices):
                        country_results[coord_indices[i]] = country
            except Exception as e:
                logger.warning(f"Batch geocoding failed: {e}")

        snapshots: list[AircraftSnapshot] = []
        for i, data in enumerate(aircraft_data_list):
            try:
                try:
                    from src.analysis.flight_agent import FlightData

                    flight_data = FlightData.from_dict(data)
                    country_of_registration = flight_data.country_of_registration
                except Exception:
                    country_of_registration = None

                snapshots.append(
                    AircraftSnapshot(
                        hex=self._safe_string(data.get("hex"), 6) or "",
                        flight_number=self._safe_string(data.get("flight"), 10),
                        registration=self._safe_string(data.get("r"), 20),
                        aircraft_type=self._safe_string(data.get("t"), 10),
                        latitude=data.get("lat"),
                        longitude=data.get("lon"),
                        altitude_baro=self._safe_altitude(data.get("alt_baro")),
                        altitude_geom=self._safe_altitude(data.get("alt_geom")),
                        ground_speed=data.get("gs"),
                        track=data.get("track"),
                        vertical_rate=data.get("baro_rate"),
                        squawk=self._safe_string(data.get("squawk"), 4),
                        emergency=self._safe_string(data.get("emergency"), 20),
                        category=self._safe_string(data.get("category"), 2),
                        country_of_registration=self._safe_string(country_of_registration, 50),
                        current_country=self._safe_string(country_results.get(i), 50),
                        is_military=self._is_military(data),
                        is_interesting=self._is_interesting(data),
                        raw_data=data,
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to create snapshot for aircraft {i}: {e}")
                continue

        return snapshots

    # ---------------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------------

    def execute_filter_query(self, where_clause: str, limit: int = 1000) -> list[dict]:
        """Run a WHERE-clause filter and return the latest snapshot per hex.

        Returns dicts matching the historical JSON shape used by the reporting
        pipeline (keys: hex, flight, r, t, lat, lon, alt_baro, gs, …).
        """
        session = self._session_factory()
        try:
            where_clause = self._convert_boolean_syntax(where_clause)
            cols = (
                "hex, flight_number, registration, aircraft_type, latitude, "
                "longitude, altitude_baro, ground_speed, current_country, "
                "country_of_registration, is_military, snapshot_time, raw_data"
            )
            if self._is_postgres:
                # Postgres supports the DISTINCT ON shortcut.
                query = text(f"""
                    SELECT * FROM (
                        SELECT DISTINCT ON (hex) {cols}
                        FROM aircraft_snapshots
                        WHERE ({where_clause})
                        ORDER BY hex, snapshot_time DESC
                    ) sub
                    ORDER BY snapshot_time DESC
                    LIMIT :limit_count
                """)
            else:
                # Portable equivalent: latest snapshot per hex via MAX join.
                # Works on SQLite (which lacks DISTINCT ON) and Postgres alike.
                query = text(f"""
                    SELECT {cols}
                    FROM aircraft_snapshots s
                    JOIN (
                        SELECT hex AS _hex, MAX(snapshot_time) AS _ts
                        FROM aircraft_snapshots
                        WHERE ({where_clause})
                        GROUP BY hex
                    ) latest
                      ON s.hex = latest._hex AND s.snapshot_time = latest._ts
                    ORDER BY s.snapshot_time DESC
                    LIMIT :limit_count
                """)
            result = session.execute(query, {"limit_count": limit})

            aircraft: list[dict] = []
            for row in result:
                registration = (
                    row.registration if row.registration not in [None, "None", ""] else None
                )
                payload = {
                    "hex": row.hex,
                    "flight": row.flight_number,
                    "r": registration,
                    "t": row.aircraft_type,
                    "lat": float(row.latitude) if row.latitude else None,
                    "lon": float(row.longitude) if row.longitude else None,
                    "alt_baro": row.altitude_baro,
                    "gs": float(row.ground_speed) if row.ground_speed else None,
                    "current_country": row.current_country,
                    "country_of_registration": row.country_of_registration,
                    "is_military": row.is_military,
                    "timestamp": row.snapshot_time.isoformat()
                    if hasattr(row.snapshot_time, "isoformat")
                    else row.snapshot_time,
                }
                if row.raw_data:
                    raw = (
                        row.raw_data
                        if isinstance(row.raw_data, dict)
                        else self._parse_json(row.raw_data)
                    )
                    if raw:
                        payload.update(raw)
                        payload["r"] = registration
                aircraft.append(payload)

            logger.info(f"Filter query returned {len(aircraft)} aircraft")
            return aircraft
        except Exception as e:
            logger.error(f"Error executing filter query: {e}")
            return []
        finally:
            session.close()

    def get_flight_tracks_by_registration(
        self,
        registration: str,
        limit: int = 500,
        start_time: int | None = None,
    ) -> list[dict]:
        """Return track points (lat/lon/alt/etc.) for a registration."""
        session = self._session_factory()
        try:
            query = session.query(AircraftSnapshot).filter(
                AircraftSnapshot.registration == registration
            )
            if start_time:
                start_dt = datetime.fromtimestamp(start_time, tz=UTC).replace(tzinfo=None)
                query = query.filter(AircraftSnapshot.snapshot_time >= start_dt)

            snapshots = query.order_by(AircraftSnapshot.snapshot_time.desc()).limit(limit).all()

            tracks: list[dict] = []
            for snap in snapshots:
                if snap.latitude is None or snap.longitude is None:
                    continue
                tracks.append(
                    {
                        "lat": float(snap.latitude),
                        "lon": float(snap.longitude),
                        "alt_baro": snap.altitude_baro,
                        "timestamp": snap.snapshot_time.timestamp() if snap.snapshot_time else None,
                        "datetime": snap.snapshot_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                        if snap.snapshot_time
                        else None,
                        "ground_speed": float(snap.ground_speed) if snap.ground_speed else None,
                        "track": float(snap.track) if snap.track else None,
                        "vertical_rate": snap.vertical_rate,
                        "current_country": snap.current_country,
                        "squawk": snap.squawk,
                        "flight_number": snap.flight_number,
                    }
                )

            logger.info(f"Found {len(tracks)} track points for registration {registration}")
            if tracks:
                logger.info(f"Sample track point: {tracks[0]}")
            return tracks
        except Exception as e:
            logger.error(f"Error getting flight tracks for registration {registration}: {e}")
            return []
        finally:
            session.close()

    def has_active_flight_track(self, aircraft_hex: str, minutes: int = 10) -> bool:
        """Is this aircraft moving (ground_speed>0 or vertical_rate!=0) recently?

        Fail-open: returns True on error so the caller (report pipeline) does
        not silently drop reports.
        """
        session = self._session_factory()
        try:
            if self._is_postgres:
                time_condition = f"snapshot_time >= NOW() - INTERVAL '{minutes} minutes'"
            else:
                time_condition = f"snapshot_time >= datetime('now', '-{minutes} minutes')"

            query = text(f"""
                SELECT COUNT(*) as active_count
                FROM aircraft_snapshots
                WHERE hex = :hex
                  AND {time_condition}
                  AND (
                      (ground_speed IS NOT NULL AND ground_speed > 0)
                      OR (vertical_rate IS NOT NULL AND vertical_rate != 0)
                  )
            """)
            row = session.execute(query, {"hex": aircraft_hex}).fetchone()
            active = (row.active_count if row else 0) > 0
            logger.debug(
                f"Flight track check for {aircraft_hex}: "
                f"{'active' if active else 'inactive'} in last {minutes} min"
            )
            return active
        except Exception as e:
            logger.error(f"Error checking flight track for {aircraft_hex}: {e}")
            return True
        finally:
            session.close()

    def get_statistics(self, database_url: str) -> dict:
        """Return row counts and a few derived metrics."""
        session = self._session_factory()
        try:
            one_hour_ago = datetime.now() - timedelta(hours=1)
            return {
                "total_snapshots": session.query(AircraftSnapshot).count(),
                "recent_snapshots_1h": session.query(AircraftSnapshot)
                .filter(AircraftSnapshot.snapshot_time >= one_hour_ago)
                .count(),
                "unique_aircraft_total": session.query(AircraftSnapshot.hex).distinct().count(),
                "military_aircraft_total": session.query(AircraftSnapshot)
                .filter(AircraftSnapshot.is_military == True)  # noqa: E712 - SQLAlchemy Column
                .count(),
                "database_url": database_url,
            }
        except Exception as e:
            logger.error(f"Error getting statistics: {e}")
            return {}
        finally:
            session.close()

    def cleanup_old_data(self, hours_to_keep: int = 24) -> None:
        """Delete snapshots older than `hours_to_keep` hours."""
        session = self._session_factory()
        try:
            cutoff = datetime.now() - timedelta(hours=hours_to_keep)
            deleted = (
                session.query(AircraftSnapshot)
                .filter(AircraftSnapshot.snapshot_time < cutoff)
                .delete()
            )
            session.commit()
            logger.info(f"Cleaned up {deleted} old aircraft snapshots")
        except Exception as e:
            session.rollback()
            logger.error(f"Error cleaning up old data: {e}")
        finally:
            session.close()
