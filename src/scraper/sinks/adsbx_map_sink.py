"""Sink for ADS-B Exchange map scraper — writes to ``adsbx_military_positions``."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.adsbx_map.models import ADSBxMapResult
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("scraper.sinks.adsbx_map")


class ADSBxMapSink:
    """Persists ``ADSBxMapResult.aircraft`` to ``adsbx_military_positions``."""

    def __init__(self, database_url: str) -> None:
        self.db_engine: Any | None = None
        if database_url:
            try:
                self.db_engine = create_engine(
                    database_url, echo=False, pool_pre_ping=True
                )
                self._ensure_table_exists()
            except Exception as e:
                logger.error(f"Failed to initialize DB engine: {e}")

    def _ensure_table_exists(self) -> None:
        if not self.db_engine:
            return

        is_postgres = self.db_engine.dialect.name == "postgresql"
        if is_postgres:
            pk = "SERIAL PRIMARY KEY"
            ts = "TIMESTAMP WITH TIME ZONE"
            ts_default = "TIMESTAMP WITH TIME ZONE DEFAULT NOW()"
            dbl = "DOUBLE PRECISION"
        else:
            pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
            ts = "DATETIME"
            ts_default = "DATETIME DEFAULT CURRENT_TIMESTAMP"
            dbl = "REAL"

        create_sql = f"""
        CREATE TABLE IF NOT EXISTS adsbx_military_positions (
            id {pk},
            hex VARCHAR(10),
            flight VARCHAR(16),
            registration VARCHAR(16),
            aircraft_type VARCHAR(8),
            type_description VARCHAR(128),
            latitude {dbl},
            longitude {dbl},
            altitude_baro INTEGER,
            altitude_geom INTEGER,
            ground_speed {dbl},
            track {dbl},
            heading {dbl},
            vertical_rate INTEGER,
            squawk VARCHAR(8),
            category VARCHAR(4),
            emergency VARCHAR(16),
            db_flags INTEGER,
            mil BOOLEAN DEFAULT FALSE,
            on_ground BOOLEAN DEFAULT FALSE,
            country VARCHAR(64),
            messages INTEGER,
            rssi {dbl},
            feed_timestamp {ts},
            scraped_at {ts_default},
            scrape_task_key VARCHAR(64),
            UNIQUE (hex, feed_timestamp)
        );

        CREATE INDEX IF NOT EXISTS idx_adsbx_mil_hex
            ON adsbx_military_positions(hex);
        CREATE INDEX IF NOT EXISTS idx_adsbx_mil_scraped_at
            ON adsbx_military_positions(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_adsbx_mil_mil_scraped
            ON adsbx_military_positions(mil, scraped_at);
        CREATE INDEX IF NOT EXISTS idx_adsbx_mil_registration
            ON adsbx_military_positions(registration);
        """

        try:
            with self.db_engine.connect() as conn:
                for statement in create_sql.strip().split(";"):
                    if statement.strip():
                        conn.execute(text(statement))
                conn.commit()
        except SQLAlchemyError as e:
            logger.error(f"Failed to create adsbx_military_positions: {e}")

    def on_success(self, task: ScraperTask, result: ADSBxMapResult) -> None:
        if not self.db_engine or not result.aircraft:
            return

        now = datetime.now(UTC)
        batch: list[dict[str, Any]] = []
        for ac in result.aircraft:
            if not ac.hex:
                continue
            batch.append(
                {
                    "hex": ac.hex,
                    "flight": ac.flight,
                    "registration": ac.registration,
                    "aircraft_type": ac.aircraft_type,
                    "type_description": ac.type_description,
                    "latitude": ac.latitude,
                    "longitude": ac.longitude,
                    "altitude_baro": ac.altitude_baro,
                    "altitude_geom": ac.altitude_geom,
                    "ground_speed": ac.ground_speed,
                    "track": ac.track,
                    "heading": ac.heading,
                    "vertical_rate": ac.vertical_rate,
                    "squawk": ac.squawk,
                    "category": ac.category,
                    "emergency": ac.emergency,
                    "db_flags": ac.db_flags,
                    "mil": ac.mil,
                    "on_ground": ac.on_ground,
                    "country": ac.country,
                    "messages": ac.messages,
                    "rssi": ac.rssi,
                    "feed_timestamp": ac.timestamp or now,
                    "scraped_at": now,
                    "scrape_task_key": result.task_key,
                }
            )

        if not batch:
            return

        try:
            with self.db_engine.connect() as conn:
                conn.execute(
                    text("""
                        INSERT INTO adsbx_military_positions (
                            hex, flight, registration, aircraft_type,
                            type_description, latitude, longitude,
                            altitude_baro, altitude_geom, ground_speed, track,
                            heading, vertical_rate, squawk, category,
                            emergency, db_flags, mil, on_ground, country,
                            messages, rssi, feed_timestamp, scraped_at,
                            scrape_task_key
                        ) VALUES (
                            :hex, :flight, :registration, :aircraft_type,
                            :type_description, :latitude, :longitude,
                            :altitude_baro, :altitude_geom, :ground_speed, :track,
                            :heading, :vertical_rate, :squawk, :category,
                            :emergency, :db_flags, :mil, :on_ground, :country,
                            :messages, :rssi, :feed_timestamp, :scraped_at,
                            :scrape_task_key
                        )
                        ON CONFLICT (hex, feed_timestamp)
                        DO UPDATE SET
                            flight = EXCLUDED.flight,
                            registration = EXCLUDED.registration,
                            aircraft_type = EXCLUDED.aircraft_type,
                            type_description = EXCLUDED.type_description,
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            altitude_baro = EXCLUDED.altitude_baro,
                            altitude_geom = EXCLUDED.altitude_geom,
                            ground_speed = EXCLUDED.ground_speed,
                            track = EXCLUDED.track,
                            heading = EXCLUDED.heading,
                            vertical_rate = EXCLUDED.vertical_rate,
                            squawk = EXCLUDED.squawk,
                            category = EXCLUDED.category,
                            emergency = EXCLUDED.emergency,
                            db_flags = EXCLUDED.db_flags,
                            mil = EXCLUDED.mil,
                            on_ground = EXCLUDED.on_ground,
                            country = EXCLUDED.country,
                            messages = EXCLUDED.messages,
                            rssi = EXCLUDED.rssi,
                            scraped_at = EXCLUDED.scraped_at
                    """),
                    batch,
                )
                conn.commit()
            logger.info(
                f"[{task.task_key}] Saved {len(batch)}/{len(result.aircraft)} "
                "ADSBx positions"
            )
        except SQLAlchemyError as e:
            logger.error(f"[{task.task_key}] Failed to save positions: {e}")

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        # No DB actions on failure — the worker handles retries/status.
        pass
