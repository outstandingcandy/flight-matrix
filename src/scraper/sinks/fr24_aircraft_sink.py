"""Sink for FR24 aircraft scraper — persists flights to ``flight_schedules``."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.fr24_aircraft.models import FR24AircraftResult
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("scraper.sinks.fr24_aircraft")


class FR24AircraftSink:
    """Persist aircraft-centric flight history into ``flight_schedules``."""

    def __init__(self, database_url: str) -> None:
        self.db_engine: Any | None = None
        if database_url:
            try:
                self.db_engine = create_engine(
                    database_url, echo=False, pool_pre_ping=True
                )
            except Exception as e:
                logger.error(f"Failed to initialize DB engine: {e}")

    def on_success(self, task: ScraperTask, result: FR24AircraftResult) -> None:
        if not self.db_engine or not result.flights:
            return

        saved_count = 0
        now = datetime.now(UTC)
        try:
            with self.db_engine.connect() as conn:
                for flight in result.flights:
                    if not flight.flight_id and not flight.flight_number:
                        continue
                    if not flight.scheduled_time:
                        continue
                    fr24_flight_id = flight.flight_id or flight.flight_number
                    if not fr24_flight_id:
                        continue
                    flight_type = flight.flight_type or "aircraft_schedule"
                    conn.execute(
                        text("""
                            INSERT INTO flight_schedules (
                                flight_type, airport_icao, airport_iata,
                                flight_number, callsign, fr24_flight_id,
                                airline_name, airline_iata,
                                remote_airport_iata, remote_airport_name,
                                aircraft_type, aircraft_registration,
                                scheduled_time, estimated_time, actual_time,
                                status, terminal, gate, scraped_at
                            ) VALUES (
                                :flight_type, :airport_icao, :airport_iata,
                                :flight_number, :callsign, :fr24_flight_id,
                                :airline_name, :airline_iata,
                                :remote_airport_iata, :remote_airport_name,
                                :aircraft_type, :aircraft_registration,
                                :scheduled_time, :estimated_time, :actual_time,
                                :status, :terminal, :gate, :scraped_at
                            )
                            ON CONFLICT (fr24_flight_id, DATE(scheduled_time), flight_type)
                            DO UPDATE SET
                                scheduled_time = EXCLUDED.scheduled_time,
                                flight_number = EXCLUDED.flight_number,
                                callsign = EXCLUDED.callsign,
                                airline_name = EXCLUDED.airline_name,
                                airline_iata = EXCLUDED.airline_iata,
                                aircraft_type = EXCLUDED.aircraft_type,
                                aircraft_registration = EXCLUDED.aircraft_registration,
                                estimated_time = EXCLUDED.estimated_time,
                                actual_time = EXCLUDED.actual_time,
                                status = EXCLUDED.status,
                                terminal = EXCLUDED.terminal,
                                gate = EXCLUDED.gate,
                                scraped_at = EXCLUDED.scraped_at
                        """),
                        {
                            "flight_type": flight_type,
                            "airport_icao": None,
                            "airport_iata": None,
                            "flight_number": flight.flight_number,
                            "callsign": flight.callsign,
                            "fr24_flight_id": fr24_flight_id,
                            "airline_name": flight.airline_name,
                            "airline_iata": flight.airline_iata,
                            "remote_airport_iata": flight.remote_airport_iata,
                            "remote_airport_name": flight.remote_airport_name,
                            "aircraft_type": flight.aircraft_type or result.aircraft_type,
                            "aircraft_registration": result.aircraft_registration,
                            "scheduled_time": flight.scheduled_time,
                            "estimated_time": flight.estimated_time,
                            "actual_time": flight.actual_time,
                            "status": flight.status,
                            "terminal": flight.terminal,
                            "gate": flight.gate,
                            "scraped_at": now,
                        },
                    )
                    saved_count += 1
                conn.commit()
            if saved_count:
                logger.info(
                    f"[{result.aircraft_registration}] Saved {saved_count} flights"
                )
        except SQLAlchemyError as e:
            logger.error(
                f"[{result.aircraft_registration}] Failed to save flights: {e}"
            )

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        pass
