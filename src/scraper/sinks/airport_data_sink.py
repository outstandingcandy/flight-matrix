"""Sink for airport-data.com scraper.

Provides the `persist_aircraft_callback` and `add_task_callback` that the
submodule scraper invokes mid-run, plus an ``on_success`` that's currently a
no-op since the scraper already persists per-page.
"""

from __future__ import annotations

import logging
from typing import Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.airport_data.models import (
    AirportDataAircraftData,
    AirportDataResult,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("scraper.sinks.airport_data")


class AirportDataSink:
    """Persist airport-data.com aircraft rows into ``aircraft_static_info``.

    Callers should wire this sink's callbacks into the scraper config:

        sink = AirportDataSink(database_url, task_queue)
        scraper = AirportDataScraper({
            **scraper_config,
            "persist_aircraft_callback": sink.persist_aircraft,
            "add_task_callback": sink.add_tasks,
        })
        bind_sink(scraper, sink)
    """

    def __init__(self, database_url: str, task_queue: Any | None = None) -> None:
        self.db_engine: Any | None = None
        self.task_queue = task_queue
        if database_url:
            try:
                self.db_engine = create_engine(database_url)
            except Exception as e:
                logger.error(f"Failed to initialize DB engine: {e}")

    def persist_aircraft(self, aircraft_list: list[AirportDataAircraftData]) -> int:
        """Upsert into ``aircraft_static_info`` (shared + ``ad_`` columns)."""
        if not self.db_engine or not aircraft_list:
            return 0

        updated = 0
        try:
            with self.db_engine.connect() as conn:
                for aircraft in aircraft_list:
                    try:
                        conn.execute(
                            text(
                                """
                                INSERT INTO aircraft_static_info (
                                    registration, manufacturer, model, serial_number,
                                    year_built, hex_code, owner,
                                    ad_status, ad_owner, ad_engines, ad_seats,
                                    ad_location, ad_delivery_date, ad_updated_at,
                                    data_source, last_updated
                                ) VALUES (
                                    :registration, :manufacturer, :model, :serial_number,
                                    :year_built, :hex_code, :owner,
                                    :ad_status, :ad_owner, :ad_engines, :ad_seats,
                                    :ad_location, :ad_delivery_date, CURRENT_TIMESTAMP,
                                    'airport_data', CURRENT_TIMESTAMP
                                )
                                ON CONFLICT (registration) DO UPDATE SET
                                    manufacturer = COALESCE(
                                        EXCLUDED.manufacturer,
                                        aircraft_static_info.manufacturer
                                    ),
                                    model = COALESCE(
                                        EXCLUDED.model,
                                        aircraft_static_info.model
                                    ),
                                    serial_number = COALESCE(
                                        EXCLUDED.serial_number,
                                        aircraft_static_info.serial_number
                                    ),
                                    year_built = COALESCE(
                                        EXCLUDED.year_built,
                                        aircraft_static_info.year_built
                                    ),
                                    hex_code = COALESCE(
                                        EXCLUDED.hex_code,
                                        aircraft_static_info.hex_code
                                    ),
                                    owner = COALESCE(
                                        EXCLUDED.owner,
                                        aircraft_static_info.owner
                                    ),
                                    ad_status = COALESCE(
                                        EXCLUDED.ad_status,
                                        aircraft_static_info.ad_status
                                    ),
                                    ad_owner = COALESCE(
                                        EXCLUDED.ad_owner,
                                        aircraft_static_info.ad_owner
                                    ),
                                    ad_engines = COALESCE(
                                        EXCLUDED.ad_engines,
                                        aircraft_static_info.ad_engines
                                    ),
                                    ad_seats = COALESCE(
                                        EXCLUDED.ad_seats,
                                        aircraft_static_info.ad_seats
                                    ),
                                    ad_location = COALESCE(
                                        EXCLUDED.ad_location,
                                        aircraft_static_info.ad_location
                                    ),
                                    ad_delivery_date = COALESCE(
                                        EXCLUDED.ad_delivery_date,
                                        aircraft_static_info.ad_delivery_date
                                    ),
                                    ad_updated_at = CURRENT_TIMESTAMP,
                                    data_source = 'airport_data',
                                    last_updated = CURRENT_TIMESTAMP
                                """
                            ),
                            {
                                "registration": aircraft.registration,
                                "manufacturer": aircraft.manufacturer,
                                "model": aircraft.model,
                                "serial_number": aircraft.serial_number,
                                "year_built": aircraft.year_built,
                                "hex_code": aircraft.mode_s_code,
                                "owner": aircraft.owner,
                                "ad_status": aircraft.status,
                                "ad_owner": aircraft.owner,
                                "ad_engines": aircraft.engines,
                                "ad_seats": aircraft.seats,
                                "ad_location": aircraft.location,
                                "ad_delivery_date": aircraft.delivery_date,
                            },
                        )
                        updated += 1
                    except SQLAlchemyError as e:
                        logger.error(f"Failed to update {aircraft.registration}: {e}")
                        continue
                conn.commit()
        except SQLAlchemyError as e:
            logger.error(f"Database error: {e}")

        if updated:
            logger.info(f"Updated {updated} records in aircraft_static_info")
        return updated

    def add_tasks(self, tasks: list[dict[str, Any]]) -> int:
        """Push derived follow-up tasks back into the application queue."""
        if not self.task_queue or not tasks:
            return 0
        added = self.task_queue.add_tasks_bulk(tasks)
        return int(added or 0)

    def on_success(self, task: ScraperTask, result: AirportDataResult) -> None:
        # Per-page persistence happens mid-run via ``persist_aircraft``; this
        # hook is intentionally empty so no records get double-written.
        pass

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        pass
