"""Writes for scraped airport arrival/departure boards.

Two callers need exactly the same insert:

- `src.scraper.sinks.fr24_airport_sink.FR24AirportSink`, used when the scraper
  runs next to the database and can write to it directly.
- `src.web.routes.ingest`, which accepts the same rows over HTTP from a scraper
  that cannot reach the database at all — the browser-driven `fr24_airport`
  scraper runs on a workstation, while Aurora only listens on the web host's
  loopback interface.

Keeping the statement in one place is the point: the upsert's conflict target
has to match `idx_flight_schedule_unique` exactly, and a second copy that drifts
from it fails at runtime rather than at import.

Nothing here imports the scraper submodule. `ScrapedFlight` is a structural type,
so both the submodule's `FlightData` model and the Pydantic model the ingest
route validates satisfy it without either side depending on the other.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger("data.flight_schedule_repo")

__all__ = [
    "ScrapedFlight",
    "build_row",
    "registrations_of",
    "resolve_airport_codes",
    "seed_registrations",
    "upsert_flight_schedules",
]

# Registration placeholders the boards use for "not published yet". Stored as-is
# they would each seed a bogus `aircraft_static_info` row that the JetPhotos and
# FR24 aircraft scrapers would then keep trying to look up.
INVALID_REGISTRATIONS = frozenset({"UNKNOWN", "N/A", "NA", "NONE", "NULL", ""})

# Column order used by both the INSERT and `build_row`. The row dict's keys are
# the bind parameter names, so the two must stay in step.
ROW_FIELDS = (
    "flight_type",
    "airport_icao",
    "airport_iata",
    "flight_number",
    "callsign",
    "fr24_flight_id",
    "airline_name",
    "airline_iata",
    "remote_airport_iata",
    "remote_airport_name",
    "aircraft_type",
    "aircraft_registration",
    "scheduled_time",
    "estimated_time",
    "actual_time",
    "status",
    "terminal",
    "gate",
    "scraped_at",
)

# The conflict target is `idx_flight_schedule_unique`, a UNIQUE index on
# (fr24_flight_id, date(scheduled_time), flight_type). Both PostgreSQL and
# SQLite accept this statement unchanged, including `DATE(...)` in the target and
# the `EXCLUDED` pseudo-row, so no dialect branch is needed.
_UPSERT_SQL = text("""
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
        airport_icao = EXCLUDED.airport_icao,
        airport_iata = EXCLUDED.airport_iata,
        flight_number = EXCLUDED.flight_number,
        callsign = EXCLUDED.callsign,
        airline_name = EXCLUDED.airline_name,
        airline_iata = EXCLUDED.airline_iata,
        remote_airport_iata = EXCLUDED.remote_airport_iata,
        remote_airport_name = EXCLUDED.remote_airport_name,
        aircraft_type = EXCLUDED.aircraft_type,
        aircraft_registration = EXCLUDED.aircraft_registration,
        estimated_time = EXCLUDED.estimated_time,
        actual_time = EXCLUDED.actual_time,
        status = EXCLUDED.status,
        terminal = EXCLUDED.terminal,
        gate = EXCLUDED.gate,
        scraped_at = EXCLUDED.scraped_at
""")

_SEED_REGISTRATION_SQL = text("""
    INSERT INTO aircraft_static_info (registration, last_updated, data_source)
    VALUES (:registration, :last_updated, 'fr24_flights')
    ON CONFLICT (registration) DO NOTHING
""")


class ScrapedFlight(Protocol):
    """One row of a scraped arrivals or departures board.

    Structural counterpart of the scraper submodule's
    `resilient_scraper.scrapers.aviation.fr24_aircraft.models.FlightData`.
    """

    flight_type: str | None
    flight_number: str | None
    callsign: str | None
    airline_name: str | None
    airline_iata: str | None
    remote_airport_iata: str | None
    remote_airport_name: str | None
    aircraft_type: str | None
    aircraft_registration: str | None
    scheduled_time: datetime | None
    estimated_time: datetime | None
    actual_time: datetime | None
    status: str | None
    terminal: str | None
    gate: str | None
    flight_id: str | None


def resolve_airport_codes(conn: Connection, airport_code: str) -> tuple[str | None, str | None]:
    """Split a scraped airport code into its IATA and ICAO forms.

    The scraper is driven by whatever code the task carried — `PEK` for FR24,
    `ZBAA` elsewhere — but `flight_schedules` has a column for each, and
    `airport_icao` is `String(4)`. Writing the 3-letter code into it (which this
    path used to do) both truncates on a strict database and makes every
    ICAO-keyed query miss.

    Args:
        conn: Open connection, used to look up the missing half in `airports`.
        airport_code: Code as scraped, in either form.

    Returns:
        An `(iata, icao)` pair. The half that could not be resolved is None
        rather than a guess; no caller benefits from a fabricated code.
    """
    code = airport_code.strip().upper()
    if len(code) == 4:
        row = conn.execute(
            text("SELECT iata_code FROM airports WHERE icao_code = :code"), {"code": code}
        ).fetchone()
        return (row[0] if row and row[0] else None), code
    if len(code) == 3:
        row = conn.execute(
            text("SELECT icao_code FROM airports WHERE iata_code = :code"), {"code": code}
        ).fetchone()
        return code, (row[0] if row and row[0] else None)
    return None, None


def build_row(
    flight: ScrapedFlight,
    *,
    airport_iata: str | None,
    airport_icao: str | None,
    flight_type_hint: str,
    scraped_at: datetime,
) -> dict[str, Any] | None:
    """Turn one scraped board row into bind parameters for the upsert.

    Args:
        flight: The scraped row.
        airport_iata: IATA code of the airport whose board this is, or None.
        airport_icao: ICAO code of the same airport, or None.
        flight_type_hint: `"arrival"` or `"departure"`, used when the scraper
            could not tell from the page which board it read.
        scraped_at: Timestamp recorded on the row.

    Returns:
        A dict keyed by :data:`ROW_FIELDS`, or None when the row cannot be
        stored: without a scheduled time there is nothing to key it by, and
        without an id, number or registration there is nothing to identify.
    """
    if not flight.scheduled_time:
        return None
    fr24_flight_id = flight.flight_id or flight.flight_number
    if not fr24_flight_id:
        return None

    return {
        "flight_type": flight.flight_type or flight_type_hint,
        "airport_icao": airport_icao,
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
        "scraped_at": scraped_at,
    }


def upsert_flight_schedules(conn: Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert or refresh scraped board rows.

    Re-scraping the same board is the normal case — the scheduler revisits a
    tier-1 hub every five minutes — so a row already present is updated with the
    newer status, gate and estimate rather than duplicated.

    Rows are sent one statement at a time rather than as an executemany. A board
    scraped with several "load more" clicks can legitimately contain the same
    flight twice, and both dialects reject a single statement that resolves the
    same conflict twice ("ON CONFLICT DO UPDATE command cannot affect row a
    second time"); separate statements make the later row simply win.

    Args:
        conn: Open connection. The caller owns the transaction.
        rows: Bind-parameter dicts from :func:`build_row`.

    Returns:
        The number of rows written.
    """
    written = 0
    for row in rows:
        conn.execute(_UPSERT_SQL, dict(row))
        written += 1
    return written


def registrations_of(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Collect the storable aircraft registrations from prepared rows.

    Args:
        rows: Bind-parameter dicts from :func:`build_row`.

    Returns:
        Upper-cased registrations, with the board's placeholders and anything
        too short to be a tail number dropped.
    """
    found: set[str] = set()
    for row in rows:
        raw = row.get("aircraft_registration")
        if not isinstance(raw, str):
            continue
        registration = raw.strip().upper()
        if len(registration) >= 2 and registration not in INVALID_REGISTRATIONS:
            found.add(registration)
    return found


def seed_registrations(conn: Connection, registrations: Iterable[str], now: datetime) -> int:
    """Create `aircraft_static_info` rows for registrations not seen before.

    This is how new tails enter the system: the JetPhotos and FR24 aircraft
    scrapers poll that table, so a registration seen on a board today gets its
    photos and details fetched without anyone adding it by hand.

    `last_updated` is set because the OpenSearch sync reads it as its watermark
    (see `src/search/CLAUDE.md`); a row inserted without it stays invisible to
    search.

    Args:
        conn: Open connection. The caller owns the transaction.
        registrations: Registrations from :func:`registrations_of`.
        now: Value for `last_updated`.

    Returns:
        The number of rows actually created.
    """
    created = 0
    for registration in registrations:
        result = conn.execute(
            _SEED_REGISTRATION_SQL, {"registration": registration, "last_updated": now}
        )
        # rowcount is 0 for a row that already existed, because of DO NOTHING.
        if result.rowcount > 0:
            created += 1
    return created
