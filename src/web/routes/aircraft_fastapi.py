"""FastAPI port of the ``/api/aircraft/*`` and ``/api/flight/*`` routes.

Stage 0 lift-and-shift. Handler bodies keep the exact SQL, response
shape and error-handling behaviour they had in Flask. What changes:

- Framework primitives — ``request.args`` becomes typed FastAPI query
  parameters; ``jsonify({...})`` becomes a plain ``dict`` return that
  FastAPI serialises.
- Global-state access — ``db_manager`` and helper functions are still
  defined on the ``web_app`` module and read its module-level globals.
  Rather than duplicating them here, migrated handlers delegate: the
  FastAPI lifespan calls ``web_app.init_app()``, so ``web_app.db_manager``
  and ``web_app.config`` are populated the same way they are when the
  Flask entry runs, and the helpers see valid globals. When the whole
  migration is done, those helpers move to a neutral module and the
  Flask module goes away with them.
- Error responses — bare ``except Exception`` no longer needs to call
  ``api_error(exc, ctx)``; FastAPI's global exception handler in
  :mod:`app` already returns the same client-safe body.

The router has no path prefix on purpose. Some routes in this file live
under ``/api/aircraft/*`` and some under ``/api/flight/*`` — a single
router with fully-qualified paths keeps them together instead of forcing
a second module just for one FR24 pass-through.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger("web.aircraft")

router = APIRouter(tags=["aircraft"])


@router.get("/api/aircraft/recent", name="aircraft_recent")
async def get_recent_aircraft(
    hours: int = Query(1, ge=1, le=168),
    limit: int = Query(50, ge=1, le=1000),
) -> dict[str, Any]:
    """List aircraft seen in the last ``hours`` hours.

    Same behaviour as the Flask version at ``web_app.py:853``.
    """
    from web_app import (
        batch_get_images_from_static_info,
        convert_utc_to_beijing,
        db_manager,
        transform_image_paths,
    )

    recent_time = datetime.now() - timedelta(hours=hours)
    recent_time_str = recent_time.strftime("%Y-%m-%d %H:%M:%S")
    where_clause = f"snapshot_time >= '{recent_time_str}'"
    logger.info("Recent aircraft query: %s", where_clause)

    results = db_manager.execute_filter_query(where_clause, limit)
    logger.info("Recent query returned %d results", len(results))

    registrations = [r.get("r") for r in results if r.get("r")]
    static_images = batch_get_images_from_static_info(registrations)

    for result in results:
        if result.get("timestamp"):
            result["timestamp"] = convert_utc_to_beijing(result["timestamp"])
        result["timezone"] = "Asia/Shanghai"
        reg = result.get("r")
        if reg and reg in static_images:
            result["image_path_1"] = static_images[reg].get("image_path_1")
            result["image_path_2"] = static_images[reg].get("image_path_2")
            result["image_path_3"] = static_images[reg].get("image_path_3")
        transform_image_paths(result)

    return {"success": True, "data": results, "count": len(results)}


@router.get("/api/aircraft/search", name="aircraft_search")
async def search_aircraft(
    request: Request,
    registration: str = Query("", description="Registration substring (LIKE %reg%)"),
    hex: str = Query("", description="ICAO Mode-S hex (exact)"),
    aircraft_type: str = Query("", description="Aircraft type substring (LIKE %type%)"),
    start_date: str | None = Query(None, description="Beijing-timezone start (inclusive)"),
    end_date: str | None = Query(None, description="Beijing-timezone end (inclusive)"),
    is_military: str | None = Query(None, description="'true' / 'false' — narrow to military"),
    limit: int = Query(100, ge=1, le=5000),
) -> dict[str, Any]:
    """Search snapshot rows by registration / hex / type / time range.

    Same SQL, same param binding, same military-flag literal handling as
    the Flask version at ``web_app.py:610``. The unbound-vs-bound comment
    there still applies: user strings go through ``:name`` params to
    prevent SQL injection; the numeric ``is_military`` value stays a
    literal because ``SnapshotRepository`` rewrites ``= 1`` to ``= true``
    for Postgres by pattern-matching the digit.
    """
    from web_app import (
        batch_get_images_from_static_info,
        convert_beijing_to_utc,
        convert_utc_to_beijing,
        db_manager,
        transform_image_paths,
    )

    registration = registration.strip()
    hex_code = hex.strip()
    aircraft_type = aircraft_type.strip()

    logger.info(
        "Search parameters — registration:'%s' hex:'%s' aircraft_type:'%s'",
        registration,
        hex_code,
        aircraft_type,
    )
    logger.info("All request args: %s", dict(request.query_params))

    conditions: list[str] = []
    params: dict[str, Any] = {}

    if registration:
        conditions.append("registration LIKE :registration")
        params["registration"] = f"%{registration}%"

    if hex_code:
        conditions.append("hex = :hex_code")
        params["hex_code"] = hex_code

    if aircraft_type:
        conditions.append("aircraft_type LIKE :aircraft_type")
        params["aircraft_type"] = f"%{aircraft_type}%"

    if is_military is not None:
        is_mil_value = 1 if is_military.lower() == "true" else 0
        conditions.append(f"is_military = {is_mil_value}")

    if start_date:
        try:
            start_dt_utc = convert_beijing_to_utc(start_date)
            if start_dt_utc:
                start_time_str = start_dt_utc.strftime("%Y-%m-%d %H:%M:%S")
                conditions.append(f"snapshot_time >= '{start_time_str}'")
        except ValueError:
            pass

    if end_date:
        try:
            end_dt_utc = convert_beijing_to_utc(end_date)
            if end_dt_utc:
                end_time_str = end_dt_utc.strftime("%Y-%m-%d %H:%M:%S")
                conditions.append(f"snapshot_time <= '{end_time_str}'")
        except ValueError:
            pass

    conditions.append(
        "registration IS NOT NULL AND registration != '' AND registration != 'None'"
    )

    has_search_conditions = any(
        [registration, hex_code, aircraft_type, start_date, end_date, is_military]
    )
    if not has_search_conditions:
        default_start = datetime.now() - timedelta(hours=24)
        default_start_str = default_start.strftime("%Y-%m-%d %H:%M:%S")
        conditions.append(f"snapshot_time >= '{default_start_str}'")

    where_clause = " AND ".join(conditions)
    logger.info("Search conditions: %s", conditions)
    logger.info("Where clause: %s", where_clause)

    results = db_manager.execute_filter_query(where_clause, limit, params)
    logger.info("Query returned %d results", len(results))
    logger.info("First few results: %s", [r.get("r") for r in results[:3]])

    registrations = [r.get("r") for r in results if r.get("r")]
    static_images = batch_get_images_from_static_info(registrations)

    for result in results:
        if result.get("timestamp"):
            result["timestamp"] = convert_utc_to_beijing(result["timestamp"])
        result["timezone"] = "Asia/Shanghai"
        reg = result.get("r")
        if reg and reg in static_images:
            result["image_path_1"] = static_images[reg].get("image_path_1")
            result["image_path_2"] = static_images[reg].get("image_path_2")
            result["image_path_3"] = static_images[reg].get("image_path_3")
        transform_image_paths(result)

    return {"success": True, "data": results, "count": len(results)}


@router.get("/api/aircraft/tracks/{registration}", name="aircraft_tracks")
async def get_aircraft_tracks(
    registration: str,
    start_time: str | None = Query(None, description="Beijing time or epoch seconds"),
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, Any]:
    """Historical track points for one registration.

    Same behaviour as the Flask version at ``web_app.py:727``. The
    ``start_time`` param accepts either a Beijing-time string or a Unix
    epoch integer, defaulting to 7 days ago.
    """
    from datetime import UTC as _UTC

    from web_app import BEIJING_TZ, convert_beijing_to_utc, db_manager

    start_timestamp: int | None = None
    if start_time:
        try:
            start_dt_utc = convert_beijing_to_utc(start_time)
            if start_dt_utc:
                start_timestamp = int(start_dt_utc.timestamp())
        except (ValueError, TypeError):
            try:
                start_timestamp = int(start_time)
            except (ValueError, TypeError):
                pass

    if start_timestamp is None:
        start_timestamp = int((datetime.now() - timedelta(days=7)).timestamp())

    logger.info(
        "Getting tracks for %s, limit=%d, start_time=%d",
        registration,
        limit,
        start_timestamp,
    )
    tracks = db_manager.get_flight_tracks_by_registration(
        registration, limit=limit, start_time=start_timestamp
    )
    logger.info("Retrieved %d track points", len(tracks))

    for track in tracks:
        if track.get("timestamp"):
            utc_dt = datetime.fromtimestamp(track["timestamp"], tz=_UTC)
            beijing_dt = utc_dt.astimezone(BEIJING_TZ)
            track["timestamp_beijing"] = beijing_dt.strftime("%Y-%m-%d %H:%M:%S")
            track["timezone"] = "Asia/Shanghai"

    return {
        "success": True,
        "registration": registration,
        "tracks": tracks,
        "count": len(tracks),
    }


@router.get("/api/flight/trail/{fr24_id}", name="flight_trail")
async def get_fr24_flight_trail(fr24_id: str) -> dict[str, Any]:
    """FR24 clickhandler pass-through — returns the same trail + flight_info shape.

    Same behaviour as the Flask version at ``web_app.py:780``. Uses
    ``requests`` synchronously; FastAPI runs sync handlers in a thread
    pool, so this doesn't block the event loop. Migrating to ``httpx``
    async is stage 1 material.
    """
    url = f"https://data-live.flightradar24.com/clickhandler/?version=1.5&flight={fr24_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Origin": "https://www.flightradar24.com",
        "Referer": "https://www.flightradar24.com/",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.exceptions.Timeout:
        logger.error("FR24 API timeout for flight %s", fr24_id)
        raise HTTPException(
            status_code=504, detail={"success": False, "error": "FR24 API timeout"}
        ) from None

    if response.status_code != 200:
        logger.warning("FR24 API returned status %d for flight %s", response.status_code, fr24_id)
        raise HTTPException(
            status_code=502,
            detail={"success": False, "error": f"FR24 API error: {response.status_code}"},
        )

    data = response.json()
    trail = data.get("trail", [])
    flight_info = {
        "flight_number": data.get("identification", {}).get("number", {}).get("default"),
        "callsign": data.get("identification", {}).get("callsign"),
        "origin": data.get("airport", {}).get("origin", {}).get("code", {}).get("iata"),
        "destination": data.get("airport", {}).get("destination", {}).get("code", {}).get("iata"),
        "aircraft_type": data.get("aircraft", {}).get("model", {}).get("code"),
        "registration": data.get("aircraft", {}).get("registration"),
        "status": data.get("status", {}).get("text"),
    }
    tracks = [
        {
            "lat": point.get("lat"),
            "lon": point.get("lng"),
            "alt_baro": point.get("alt"),
            "ground_speed": point.get("spd"),
            "heading": point.get("hd"),
            "timestamp": point.get("ts"),
        }
        for point in trail
    ]

    return {
        "success": True,
        "fr24_id": fr24_id,
        "flight_info": flight_info,
        "tracks": tracks,
        "count": len(tracks),
    }
