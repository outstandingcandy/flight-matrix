"""Sink for FR24 airport scrapers — arrivals, departures, full-airport."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.fr24_airport.models import FR24FlightsResult
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from src.data.flight_schedule_repo import (
    build_row,
    registrations_of,
    resolve_airport_codes,
    seed_registrations,
    upsert_flight_schedules,
)

logger = logging.getLogger("scraper.sinks.fr24_airport")


class FR24AirportSink:
    """Persist airport arrival/departure rows.

    Writes ``flight_schedules`` and lazily seeds new registrations into
    ``aircraft_static_info`` so downstream JetPhotos / FR24 aircraft scrapers
    can pick them up automatically.

    The SQL lives in :mod:`src.data.flight_schedule_repo`, shared with the
    ingest API route that serves scrapers with no database access.
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

        now = datetime.now(UTC)
        try:
            with self.db_engine.connect() as conn:
                airport_iata, airport_icao = resolve_airport_codes(conn, result.airport_code)
                rows = [
                    row
                    for flight in result.flights
                    if (
                        row := build_row(
                            flight,
                            airport_iata=airport_iata,
                            airport_icao=airport_icao,
                            flight_type_hint=self.flight_type_hint,
                            scraped_at=now,
                        )
                    )
                    is not None
                ]
                saved = upsert_flight_schedules(conn, rows)
                registrations = registrations_of(rows)
                created = seed_registrations(conn, registrations, now)
                conn.commit()
        except SQLAlchemyError as e:
            logger.error(f"[{result.airport_code}] Failed to save flights: {e}")
            return

        if saved:
            logger.info(f"[{result.airport_code}] Saved {saved} flights")
        if created:
            logger.info(
                f"[{result.airport_code}] Created {created} new "
                f"aircraft_static_info records from {len(registrations)} regs"
            )

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        pass
