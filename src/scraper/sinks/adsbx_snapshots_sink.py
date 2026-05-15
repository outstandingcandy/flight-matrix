"""Sink that writes ADSBx scraper results into ``aircraft_snapshots``.

This is the seam that lets the ADSBx scraper stand in for — or run alongside —
the RapidAPI-backed ``track_service``. Both producers feed the same table with
the same dict shape, so the geocoding / ``is_military`` / ``is_interesting`` /
``raw_data`` / static-info bootstrapping already provided by
``SnapshotRepository.batch_insert`` runs identically for either source.

Wired in via YAML: ``scraper.scrapers.adsbx_map.target: snapshots`` selects
this sink instead of :class:`ADSBxMapSink` (which writes to the
military-only ``adsbx_military_positions`` table).
"""

from __future__ import annotations

import logging
from typing import Any

from resilient_scraper.models import ScraperTask
from resilient_scraper.scrapers.aviation.adsbx_map.models import (
    ADSBxMapAircraftData,
    ADSBxMapResult,
)

logger = logging.getLogger("scraper.sinks.adsbx_snapshots")


class ADSBxSnapshotsSink:
    """Persist ``ADSBxMapResult.aircraft`` into ``aircraft_snapshots``.

    The scraper emits Pydantic rows shaped for a military-only table; this
    sink re-packs each row back into the RapidAPI JSON shape that
    :meth:`SnapshotRepository.batch_insert` consumes. The RapidAPI track
    service and this sink are then interchangeable from the repo's point of
    view.
    """

    def __init__(self, database_url: str) -> None:
        self._db: Any | None = None
        if not database_url:
            return
        try:
            # Import locally so this module stays cheap to import in
            # environments that don't have the web app's dependencies.
            from src.data.db_manager import DatabaseManager

            self._db = DatabaseManager(database_url=database_url)
        except Exception as e:
            logger.error(f"Failed to initialize DatabaseManager: {e}")

    def on_success(self, task: ScraperTask, result: ADSBxMapResult) -> None:
        if self._db is None or not result.aircraft:
            return

        rows = [self._to_rapidapi_shape(ac) for ac in result.aircraft]
        rows = [r for r in rows if r]
        if not rows:
            return

        try:
            inserted = self._db.batch_insert_aircraft(rows)
        except Exception as e:
            logger.error(f"[{task.task_key}] batch_insert failed: {e}")
            return

        logger.info(
            f"[{task.task_key}] Saved {inserted}/{len(rows)} ADSBx snapshots"
        )

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        # Worker already logged the failure; no DB write on this path.
        pass

    # ------------------------------------------------------------------

    @staticmethod
    def _to_rapidapi_shape(ac: ADSBxMapAircraftData) -> dict[str, Any] | None:
        """Map our Pydantic row back to the RapidAPI key set.

        The RapidAPI response uses short keys (``r``, ``t``, ``gs``, etc.);
        :meth:`SnapshotRepository._create_snapshots_with_batch_geocoding`
        reads those directly. We keep the mapping mechanical so the two
        producers stay swappable.
        """
        if not ac.hex:
            return None

        row: dict[str, Any] = {
            "hex": ac.hex,
            "flight": ac.flight,
            "r": ac.registration,
            "t": ac.aircraft_type,
            "desc": ac.type_description,
            "lat": ac.latitude,
            "lon": ac.longitude,
            "alt_baro": ac.altitude_baro,
            "alt_geom": ac.altitude_geom,
            "gs": ac.ground_speed,
            "track": ac.track,
            "baro_rate": ac.vertical_rate,
            "squawk": ac.squawk,
            "category": ac.category,
            "emergency": ac.emergency,
            "dbFlags": ac.db_flags,
        }
        # Drop None entries so batch_insert's downstream classifiers see
        # the same sparse dicts RapidAPI would have produced.
        return {k: v for k, v in row.items() if v is not None}
