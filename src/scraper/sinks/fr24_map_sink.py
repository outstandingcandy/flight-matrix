"""Sink for FR24 map scraper — persists aircraft positions to DB."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.fr24_map.models import FR24MapResult
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("scraper.sinks.fr24_map")


class FR24MapSink:
    """Persists ``FR24MapResult.aircraft`` to ``aircraft_realtime_positions``."""

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

        create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS aircraft_realtime_positions (
            id {pk},
            fr24_id VARCHAR(32),
            flight_number VARCHAR(16),
            callsign VARCHAR(16),
            registration VARCHAR(16),
            aircraft_type VARCHAR(8),
            latitude {dbl},
            longitude {dbl},
            altitude INTEGER,
            ground_speed INTEGER,
            heading INTEGER,
            vertical_speed INTEGER,
            squawk VARCHAR(8),
            origin_iata VARCHAR(4),
            destination_iata VARCHAR(4),
            on_ground BOOLEAN DEFAULT FALSE,
            fr24_timestamp {ts},
            scraped_at {ts_default},
            scrape_task_key VARCHAR(64),
            UNIQUE (fr24_id, fr24_timestamp)
        );

        CREATE INDEX IF NOT EXISTS idx_aircraft_realtime_fr24_id
            ON aircraft_realtime_positions(fr24_id);
        CREATE INDEX IF NOT EXISTS idx_aircraft_realtime_registration
            ON aircraft_realtime_positions(registration);
        CREATE INDEX IF NOT EXISTS idx_aircraft_realtime_scraped_at
            ON aircraft_realtime_positions(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_aircraft_realtime_flight_number
            ON aircraft_realtime_positions(flight_number);
        """

        try:
            with self.db_engine.connect() as conn:
                for statement in create_table_sql.strip().split(";"):
                    if statement.strip():
                        conn.execute(text(statement))
                conn.commit()
        except SQLAlchemyError as e:
            logger.error(f"Failed to create aircraft_realtime_positions: {e}")

    def on_success(self, task: ScraperTask, result: FR24MapResult) -> None:
        if not self.db_engine or not result.aircraft:
            return

        now = datetime.now(UTC)
        batch_data = []
        for ac in result.aircraft:
            if not ac.fr24_id:
                continue
            batch_data.append(
                {
                    "fr24_id": ac.fr24_id,
                    "flight_number": ac.flight_number,
                    "callsign": ac.callsign,
                    "registration": ac.registration,
                    "aircraft_type": ac.aircraft_type,
                    "latitude": ac.latitude,
                    "longitude": ac.longitude,
                    "altitude": ac.altitude,
                    "ground_speed": ac.ground_speed,
                    "heading": ac.heading,
                    "vertical_speed": ac.vertical_speed,
                    "squawk": ac.squawk,
                    "origin_iata": ac.origin_iata,
                    "destination_iata": ac.destination_iata,
                    "on_ground": ac.on_ground,
                    "fr24_timestamp": ac.timestamp or now,
                    "scraped_at": now,
                    "scrape_task_key": result.task_key,
                }
            )

        if not batch_data:
            return

        try:
            with self.db_engine.connect() as conn:
                conn.execute(
                    text("""
                        INSERT INTO aircraft_realtime_positions (
                            fr24_id, flight_number, callsign, registration,
                            aircraft_type, latitude, longitude, altitude,
                            ground_speed, heading, vertical_speed, squawk,
                            origin_iata, destination_iata, on_ground,
                            fr24_timestamp, scraped_at, scrape_task_key
                        ) VALUES (
                            :fr24_id, :flight_number, :callsign, :registration,
                            :aircraft_type, :latitude, :longitude, :altitude,
                            :ground_speed, :heading, :vertical_speed, :squawk,
                            :origin_iata, :destination_iata, :on_ground,
                            :fr24_timestamp, :scraped_at, :scrape_task_key
                        )
                        ON CONFLICT (fr24_id, fr24_timestamp)
                        DO UPDATE SET
                            flight_number = EXCLUDED.flight_number,
                            callsign = EXCLUDED.callsign,
                            registration = EXCLUDED.registration,
                            aircraft_type = EXCLUDED.aircraft_type,
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            altitude = EXCLUDED.altitude,
                            ground_speed = EXCLUDED.ground_speed,
                            heading = EXCLUDED.heading,
                            vertical_speed = EXCLUDED.vertical_speed,
                            squawk = EXCLUDED.squawk,
                            origin_iata = EXCLUDED.origin_iata,
                            destination_iata = EXCLUDED.destination_iata,
                            on_ground = EXCLUDED.on_ground,
                            scraped_at = EXCLUDED.scraped_at
                    """),
                    batch_data,
                )
                conn.commit()
            logger.info(
                f"[{task.task_key}] Saved {len(batch_data)}/{len(result.aircraft)} "
                "aircraft positions"
            )
        except SQLAlchemyError as e:
            logger.error(f"[{task.task_key}] Failed to save positions: {e}")

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        # No DB actions on failure — just let the worker handle retry/status.
        pass
