"""FastAPI port of :mod:`src.web.routes.ingest`.

Pilot for the ``feat/fastapi-migration`` branch. Same endpoint, same
payload shape, same authentication semantics as the Flask version — but
built on ``APIRouter`` and ``Depends`` so we can prove the migration
pattern on one route before tackling the other 93.

The Flask blueprint stays registered on ``web_app.py`` during the
migration, so a rollback is a one-line change in ``app.py`` (drop the
``include_router``) and traffic keeps landing on the Flask route.

Once every blueprint is migrated and Flask is retired, this module
replaces the Flask one and its comment header goes away.
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.data.flight_schedule_repo import (
    build_row,
    registrations_of,
    resolve_airport_codes,
    seed_registrations,
    upsert_flight_schedules,
)

logger = logging.getLogger("web.ingest")

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

TOKEN_HEADER = "X-Ingest-Token"

# One request carries one airport's board. FR24 returns a few hundred rows for a
# large hub, and the client splits anything bigger, so this bounds the work a
# single request can ask for without rejecting a legitimate board.
MAX_FLIGHTS_PER_BATCH = 500


class IngestFlight(BaseModel):
    """One scraped board row, as posted by the scraper.

    Field-for-field the submodule's ``FlightData``. ``extra="ignore"`` so a
    submodule that grows a field does not start failing against an older
    web deploy.
    """

    model_config = ConfigDict(extra="ignore")

    flight_type: str | None = None
    flight_number: str | None = None
    callsign: str | None = None
    airline_name: str | None = None
    airline_iata: str | None = None
    remote_airport_iata: str | None = None
    remote_airport_name: str | None = None
    aircraft_type: str | None = None
    aircraft_registration: str | None = None
    scheduled_time: datetime | None = None
    estimated_time: datetime | None = None
    actual_time: datetime | None = None
    status: str | None = None
    terminal: str | None = None
    gate: str | None = None
    flight_id: str | None = None


class IngestBatch(BaseModel):
    """One airport's board."""

    model_config = ConfigDict(extra="ignore")

    airport_code: str = Field(min_length=3, max_length=4)
    flight_type_hint: str = ""
    flights: list[IngestFlight] = Field(max_length=MAX_FLIGHTS_PER_BATCH)


class IngestResponse(BaseModel):
    """Response schema for ``POST /api/ingest/flight-schedules``."""

    success: bool = True
    airport_iata: str | None = None
    airport_icao: str | None = None
    written: int = 0
    skipped: int = 0
    registrations_created: int = 0


def _configured_token(request: Request) -> str:
    """Return the ingest shared secret, or an empty string when unset.

    Read per request rather than cached: the process is long-lived and
    rotating the secret should not need a restart. The environment wins
    over the config file so a deploy can set it without editing YAML.
    """
    token = os.environ.get("INGEST_API_TOKEN", "").strip()
    if token:
        return token

    config = getattr(request.app.state, "config", None)
    if config is None:
        return ""
    return str(config.get("api.ingest_token", "") or "").strip()


async def verify_ingest_token(
    request: Request,
    x_ingest_token: Annotated[str, Header(alias=TOKEN_HEADER)] = "",
) -> None:
    """Compare ``X-Ingest-Token`` to the configured secret in constant time.

    503 when no token is configured (endpoint closed, not open), 401 when
    the header is missing or wrong. Neither body echoes any part of a
    token.
    """
    expected = _configured_token(request)
    if not expected:
        logger.error("Ingest request refused: api.ingest_token is not configured")
        raise HTTPException(
            status_code=503,
            detail={"success": False, "error": "Ingest is not configured"},
        )

    if not x_ingest_token or not hmac.compare_digest(x_ingest_token, expected):
        logger.warning("Ingest request refused: invalid %s header", TOKEN_HEADER)
        raise HTTPException(
            status_code=401,
            detail={"success": False, "error": "Unauthorized"},
        )


@router.post(
    "/flight-schedules",
    response_model=IngestResponse,
    dependencies=[Depends(verify_ingest_token)],
    name="ingest_flight_schedules",
)
async def ingest_flight_schedules(batch: IngestBatch, request: Request) -> IngestResponse:
    """Upsert one airport's scraped arrival/departure board.

    Body:
        ``{"airport_code": "PEK", "flight_type_hint": "arrival",
        "flights": [{…FlightData…}, …]}``

    Errors:
        401 / 503 from :func:`verify_ingest_token`; 422 for validation
        (FastAPI auto-generated); 500 falls through the global handler in
        :mod:`app` which returns the same client-safe body the Flask
        ``api_error`` used to emit.
    """
    db_manager = getattr(request.app.state, "db_manager", None)
    if db_manager is None:
        raise HTTPException(
            status_code=503,
            detail={"success": False, "error": "Database is not initialised"},
        )

    now = datetime.now(UTC)
    with db_manager.engine.connect() as conn:
        airport_iata, airport_icao = resolve_airport_codes(conn, batch.airport_code)
        rows: list[dict[str, Any]] = []
        for flight in batch.flights:
            row = build_row(
                flight,
                airport_iata=airport_iata,
                airport_icao=airport_icao,
                flight_type_hint=batch.flight_type_hint,
                scraped_at=now,
            )
            if row is not None:
                rows.append(row)

        written = upsert_flight_schedules(conn, rows)
        registrations = registrations_of(rows)
        created = seed_registrations(conn, registrations, now)
        conn.commit()

    logger.info(
        "Ingested %s flights for %s (%s skipped, %s new registrations)",
        written,
        batch.airport_code,
        len(batch.flights) - written,
        created,
    )
    return IngestResponse(
        success=True,
        airport_iata=airport_iata,
        airport_icao=airport_icao,
        written=written,
        skipped=len(batch.flights) - written,
        registrations_created=created,
    )
