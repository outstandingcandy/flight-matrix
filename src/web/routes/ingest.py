"""Ingest blueprint: the write path for scrapers with no database access.

The `fr24_airport` scraper drives a real, non-headless Chromium (the site is
Cloudflare-protected), which the web host cannot afford to run — so it runs on a
workstation instead. That workstation cannot reach Aurora, which listens only on
the web host's loopback interface, and neither opening the port nor tunnelling is
on the table. So the scraper posts its rows here and the web process, which is
already next to the database, performs the same upsert its co-located sink would.

The statement itself lives in `src.data.flight_schedule_repo`, shared with
`src.scraper.sinks.fr24_airport_sink`.

Authentication is a dedicated shared secret in `api.ingest_token`, not a user's
`users.api_key`: this is a machine, and `UserService.authenticate_by_api_key`
additionally requires an active subscription. With no token configured the
endpoint answers 503, so there is no state in which it accepts an unauthenticated
write.
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.data.flight_schedule_repo import (
    build_row,
    registrations_of,
    resolve_airport_codes,
    seed_registrations,
    upsert_flight_schedules,
)
from src.web.errors import api_error

logger = logging.getLogger("web.ingest")
bp = Blueprint("ingest", __name__, url_prefix="/api/ingest")

TOKEN_HEADER = "X-Ingest-Token"

# One request carries one airport's board. FR24 returns a few hundred rows for a
# large hub, and the client splits anything bigger, so this bounds the work a
# single request can ask for without rejecting a legitimate board.
MAX_FLIGHTS_PER_BATCH = 500


def _shared(key: str) -> Any:
    """Read one of the objects `web_app.init_app()` publishes on the Flask app.

    `DB_MANAGER` and `APP_CONFIG` are set there. Reading them off `current_app`
    rather than importing `web_app` avoids two traps: the import is circular
    (`web_app` registers this blueprint), and `python web_app.py` runs that module
    as `__main__`, so `import web_app` would bind a second, uninitialised copy.

    Args:
        key: Config key to read.

    Returns:
        The object, or None when the app was never initialised.
    """
    return current_app.config.get(key)


class IngestFlight(BaseModel):
    """One scraped board row, as posted by the scraper.

    Field-for-field the submodule's `FlightData`, which is what the client
    serialises. Unknown keys are ignored rather than rejected so a submodule that
    grows a field does not start failing against an older web deploy.
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


def _configured_token() -> str:
    """Return the ingest shared secret, or an empty string when unset.

    Read per request rather than cached at import: the web process is long-lived
    and rotating the secret should not need a restart. The environment wins over
    the config file so a deploy can set it without editing YAML.
    """
    token = os.environ.get("INGEST_API_TOKEN", "").strip()
    if token:
        return token

    config = _shared("APP_CONFIG")
    if config is None:
        return ""
    return str(config.get("api.ingest_token", "") or "").strip()


def _authorised() -> tuple[Response, int] | None:
    """Check the request's token.

    Returns:
        None when the caller may proceed, otherwise the response to return:
        503 when no token is configured (the endpoint is closed, not open), 401
        when the header is missing or wrong. Neither body echoes any part of a
        token.
    """
    expected = _configured_token()
    if not expected:
        logger.error("Ingest request refused: api.ingest_token is not configured")
        return jsonify({"success": False, "error": "Ingest is not configured"}), 503

    presented = request.headers.get(TOKEN_HEADER, "")
    if not presented or not hmac.compare_digest(presented, expected):
        logger.warning("Ingest request refused: invalid %s header", TOKEN_HEADER)
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    return None


@bp.post("/flight-schedules")
def ingest_flight_schedules() -> tuple[Response, int]:
    """Upsert one airport's scraped arrival/departure board.

    Body:
        `{"airport_code": "PEK", "flight_type_hint": "arrival",
          "flights": [{…FlightData…}, …]}`

    Returns:
        `{"success": true, "written": N, "skipped": M, "registrations_created": K}`
        with 200; 400 for a body that is not an object, 401/503 from
        :func:`_authorised`, 422 for a body that fails validation.
    """
    refusal = _authorised()
    if refusal is not None:
        return refusal

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "A JSON object body is required"}), 400

    try:
        batch = IngestBatch.model_validate(payload)
    except ValidationError as e:
        # Field names and constraints, not the values — the body may hold
        # nothing secret, but there is no reason to reflect it either.
        problems = [
            {"field": ".".join(str(part) for part in error["loc"]), "problem": error["msg"]}
            for error in e.errors()
        ]
        logger.warning("Ingest body rejected: %s", problems)
        return jsonify(
            {"success": False, "error": "Invalid request body", "details": problems}
        ), 422

    db_manager = _shared("DB_MANAGER")
    if db_manager is None:
        return jsonify({"success": False, "error": "Database is not initialised"}), 503

    now = datetime.now(UTC)
    try:
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
    except Exception as e:
        return api_error(e, f"Error ingesting flight schedules for {batch.airport_code}")

    logger.info(
        "Ingested %s flights for %s (%s skipped, %s new registrations)",
        written,
        batch.airport_code,
        len(batch.flights) - written,
        created,
    )
    return (
        jsonify(
            {
                "success": True,
                "airport_iata": airport_iata,
                "airport_icao": airport_icao,
                "written": written,
                "skipped": len(batch.flights) - written,
                "registrations_created": created,
            }
        ),
        200,
    )
