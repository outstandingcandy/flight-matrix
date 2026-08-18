"""Sink for FR24 airport scrapers — arrivals, departures, full-airport."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.fr24_airport.models import FR24FlightsResult
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("scraper.sinks.fr24_airport")


class FR24AirportSink:
    """Persist airport arrival/departure rows.

    Writes ``flight_schedules`` and lazily seeds new registrations into
    ``aircraft_static_info`` so downstream JetPhotos / FR24 aircraft scrapers
    can pick them up automatically.
    """

    def __init__(self, database_url: str, flight_type_hint: str = "") -> None:
        self.db_engine: Any | None = None
        self.flight_type_hint = flight_type_hint
        if database_url:
            try:
                self.db_engine = create_engine(database_url, echo=False, pool_pre_ping=True)
            except Exception as e:
                logger.error(f"Failed to initialize DB engine: {e}")

    def on_success(self, task: ScraperTask, result: FR24FlightsResult) -> None:
        if not self.db_engine or not result.flights:
            return
        self._save_flights_to_db(result)
        self._sync_registrations_to_static_info(result)

    def _save_flights_to_db(self, result: FR24FlightsResult) -> None:
        assert self.db_engine is not None

        saved_count = 0
        now = datetime.now(UTC)
        airport_code = result.airport_code.upper()
        if len(airport_code) == 3:
            airport_iata = airport_code
            airport_icao = None
        else:
            airport_icao = airport_code
            airport_iata = None

        try:
            with self.db_engine.connect() as conn:
                for flight in result.flights:
                    if (
                        not flight.flight_id
                        and not flight.flight_number
                        and not flight.aircraft_registration
                    ):
                        continue
                    if not flight.scheduled_time:
                        continue
                    fr24_flight_id = flight.flight_id or flight.flight_number
                    if not fr24_flight_id:
                        continue
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
                                flight_type = EXCLUDED.flight_type,
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
                            "flight_type": flight.flight_type or self.flight_type_hint,
                            "airport_icao": airport_icao or airport_code,
                            "airport_iata": airport_iata,
                            "flight_number": flight.flight_number,
                            "callsign": flight.callsign,
                            "fr24_flight_id": fr24_flight_id,
                            "airline_name": flight.airline_name,
                            "airline_iata": flight.airline_iata,
                            "remote_airport_iata": flight.remote_airport_iata,
                            "remote_airport_name": flight.remote_airport_name,
                            "aircraft_type": flight.aircraft_type,
                            "aircraft_registration": flight.aircraft_registration,
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
                logger.info(f"[{result.airport_code}] Saved {saved_count} flights")
        except SQLAlchemyError as e:
            logger.error(f"[{result.airport_code}] Failed to save flights: {e}")

    def _sync_registrations_to_static_info(self, result: FR24FlightsResult) -> None:
        assert self.db_engine is not None

        invalid_values = {"UNKNOWN", "N/A", "NA", "NONE", "NULL", ""}
        registrations: set[str] = set()
        for flight in result.flights:
            reg = flight.aircraft_registration
            if reg and len(reg) >= 2:
                reg_upper = reg.upper().strip()
                if reg_upper not in invalid_values:
                    registrations.add(reg_upper)
        if not registrations:
            return

        created_count = 0
        try:
            with self.db_engine.connect() as conn:
                for reg in registrations:
                    insert_result = conn.execute(
                        text("""
                            INSERT INTO aircraft_static_info (registration, last_updated, data_source)
                            VALUES (:reg, NOW(), 'fr24_flights')
                            ON CONFLICT (registration) DO NOTHING
                        """),
                        {"reg": reg},
                    )
                    if insert_result.rowcount > 0:
                        created_count += 1
                conn.commit()
            if created_count:
                logger.info(
                    f"[{result.airport_code}] Created {created_count} new "
                    f"aircraft_static_info records from {len(registrations)} regs"
                )
        except SQLAlchemyError as e:
            logger.error(f"[{result.airport_code}] Failed to sync registrations: {e}")

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        pass
