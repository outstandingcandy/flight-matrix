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

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import text

from src.auth.dependencies import require_login

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


# ---------------------------------------------------------------------------
# Batch 2 — types + unique + static series
# ---------------------------------------------------------------------------


def _extract_ai_flags(ai_analysis: Any) -> dict[str, Any]:
    """Pull the four ai_analysis booleans + summary in one place.

    web_app.py duplicates this shape in `/api/aircraft/static`,
    `/api/aircraft/static/batch` and `/api/aircraft/static/<reg>`; here
    we deduplicate. Accepts both a dict (JSONB from Postgres) and a JSON
    string (SQLite / legacy rows).
    """
    if not ai_analysis:
        return {"is_military": False, "is_government": False, "is_vip": False, "summary": None}
    if isinstance(ai_analysis, str):
        try:
            data = json.loads(ai_analysis)
        except json.JSONDecodeError:
            data = {}
    elif isinstance(ai_analysis, dict):
        data = ai_analysis
    else:
        data = {}
    return {
        "is_military": data.get("is_military", False),
        "is_government": data.get("is_government", False),
        "is_vip": data.get("is_vip", False),
        "summary": data.get("summary"),
    }


@router.get("/api/aircraft/types", name="aircraft_types")
async def get_aircraft_types() -> dict[str, Any]:
    """Top 50 aircraft types by snapshot count over the last 7 days.

    Same query as ``web_app.py:893``. ``get_aircraft_type_name`` still
    lives on the Flask module (a static Chinese-name lookup dict);
    delegated to keep this handler in lockstep with the Flask version.
    """
    from web_app import db_manager, get_aircraft_type_name

    session = db_manager.get_session()
    try:
        result = session.execute(
            text(
                """
                SELECT aircraft_type, COUNT(*) as count
                FROM aircraft_snapshots
                WHERE aircraft_type IS NOT NULL
                  AND aircraft_type != ''
                  AND snapshot_time >= datetime('now', '-7 days')
                GROUP BY aircraft_type
                ORDER BY count DESC
                LIMIT 50
                """
            )
        )
        aircraft_types = [
            {
                "code": row.aircraft_type,
                "count": row.count,
                "name": get_aircraft_type_name(row.aircraft_type),
            }
            for row in result
        ]
        return {"success": True, "aircraft_types": aircraft_types, "count": len(aircraft_types)}
    finally:
        session.close()


@router.get("/api/aircraft/types/{type_code}", name="aircraft_type_info")
async def get_aircraft_type_info(
    type_code: str,
    _user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    """Stats for one aircraft type. Same shape as ``web_app.py:989``.

    Login-gated on the Flask side (``@login_required``) and here.
    """
    from web_app import db_manager, get_aircraft_type_name

    type_code_upper = type_code.upper()
    session = db_manager.get_session()
    try:
        stats_row = session.execute(
            text(
                """
                SELECT
                    COUNT(*) as total_aircraft,
                    COUNT(DISTINCT CASE WHEN ai.registration IS NOT NULL THEN asi.registration END) as aircraft_with_images
                FROM aircraft_static_info asi
                LEFT JOIN aircraft_images ai ON asi.registration = ai.registration
                WHERE asi.aircraft_type = :type_code
                """
            ),
            {"type_code": type_code_upper},
        ).fetchone()
        return {
            "success": True,
            "type_code": type_code_upper,
            "name": get_aircraft_type_name(type_code_upper),
            "total_aircraft": stats_row.total_aircraft if stats_row else 0,
            "aircraft_with_images": stats_row.aircraft_with_images if stats_row else 0,
        }
    finally:
        session.close()


@router.get("/api/aircraft/types/{type_code}/instances", name="aircraft_type_instances")
async def get_aircraft_type_instances(
    type_code: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    _user: dict[str, Any] = Depends(require_login),
) -> dict[str, Any]:
    """Paginated aircraft-of-this-type list, photo-bearing rows first.

    Same query (correlated subquery for cross-dialect portability) as
    ``web_app.py:1030``. Login-gated.
    """
    from web_app import db_manager, get_image_url

    type_code_upper = type_code.upper()
    session = db_manager.get_session()
    try:
        aircraft_result = session.execute(
            text(
                """
                SELECT
                    asi.registration,
                    asi.aircraft_type,
                    asi.owner,
                    asi.operator,
                    asi.hex_code,
                    (SELECT image_path FROM aircraft_images
                     WHERE registration = asi.registration
                     ORDER BY display_order ASC, created_at DESC
                     LIMIT 1) AS image_path
                FROM aircraft_static_info asi
                WHERE asi.aircraft_type = :type_code
                ORDER BY
                    CASE WHEN (SELECT image_path FROM aircraft_images
                               WHERE registration = asi.registration
                               LIMIT 1) IS NOT NULL THEN 0 ELSE 1 END,
                    asi.registration
                LIMIT :limit OFFSET :offset
                """
            ),
            {"type_code": type_code_upper, "limit": limit, "offset": offset},
        )
        aircraft_list = [
            {
                "registration": row.registration,
                "aircraft_type": row.aircraft_type,
                "owner": row.owner,
                "operator": row.operator,
                "hex_code": row.hex_code,
                "image_url": get_image_url(row.image_path) if row.image_path else None,
            }
            for row in aircraft_result
        ]
        return {
            "success": True,
            "aircraft": aircraft_list,
            "offset": offset,
            "limit": limit,
            "has_more": len(aircraft_list) == limit,
        }
    finally:
        session.close()


@router.get("/api/aircraft/unique", name="aircraft_unique")
async def get_unique_aircraft(days: int = Query(7, ge=1, le=365)) -> dict[str, Any]:
    """Distinct aircraft seen in the last ``days`` days.

    ORM query (unlike sibling raw-SQL handlers), same as
    ``web_app.py:1109``.
    """
    from src.data.models import AircraftSnapshot
    from web_app import db_manager

    session = db_manager.get_session()
    try:
        cutoff_time = datetime.now() - timedelta(days=days)
        unique_aircraft = (
            session.query(
                AircraftSnapshot.registration,
                AircraftSnapshot.aircraft_type,
                AircraftSnapshot.hex,
                AircraftSnapshot.is_military,
                AircraftSnapshot.country_of_registration,
            )
            .filter(
                AircraftSnapshot.snapshot_time >= cutoff_time,
                AircraftSnapshot.registration.isnot(None),
            )
            .distinct()
            .limit(1000)
            .all()
        )
        result = [
            {
                "registration": a.registration,
                "aircraft_type": a.aircraft_type,
                "hex": a.hex,
                "is_military": a.is_military,
                "country_of_registration": a.country_of_registration,
            }
            for a in unique_aircraft
        ]
        return {"success": True, "aircraft": result, "count": len(result)}
    finally:
        session.close()


def _static_info_row_to_dict(row: Any) -> dict[str, Any]:
    """Shape used by both list and batch static-info endpoints.

    Extracted so a future column addition lands in one place. Not applied
    to the single-registration endpoint below because that one already
    returns a superset of fields (livery, attention, hit_count, etc.).
    """
    from web_app import convert_utc_to_beijing

    flags = _extract_ai_flags(row.ai_analysis)
    return {
        "id": row.id,
        "registration": row.registration,
        "hex": row.hex_code,
        "owner": row.owner,
        "operator": row.operator,
        "aircraft_model": row.model,
        "aircraft_type": row.aircraft_type,
        "manufacturer": row.manufacturer,
        "serial_number": row.serial_number,
        "year_built": row.year_built,
        "country": row.country_of_registration,
        "is_military": flags["is_military"],
        "is_government": flags["is_government"],
        "is_vip": flags["is_vip"],
        "summary": flags["summary"],
        "updated_at": convert_utc_to_beijing(str(row.last_updated)) if row.last_updated else None,
        "has_images": bool(row.images_downloaded),
    }


@router.get("/api/aircraft/static", name="aircraft_static_list")
async def get_all_static_info() -> dict[str, Any]:
    """List up to 1000 static-info rows, most-recently-updated first.

    Same query and shape as ``web_app.py:1160``.
    """
    from web_app import db_manager

    session = db_manager.get_session()
    try:
        result = session.execute(
            text(
                """
                SELECT
                    id, registration, hex_code, owner, operator,
                    model, manufacturer, serial_number, year_built,
                    country_of_registration, aircraft_type, ai_analysis,
                    last_updated, data_source, images_downloaded
                FROM aircraft_static_info
                ORDER BY last_updated DESC NULLS LAST
                LIMIT 1000
                """
            )
        )
        aircraft_list = [_static_info_row_to_dict(row) for row in result]
        return {"success": True, "data": aircraft_list, "count": len(aircraft_list)}
    finally:
        session.close()


@router.post("/api/aircraft/static/batch", name="aircraft_static_batch")
async def get_batch_static_info(
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Bulk static-info fetch by registration list. Cap at 500 per call.

    Same as ``web_app.py:1230``. ``payload["registrations"]`` must be a
    non-empty list; 400 on missing key.
    """
    from web_app import db_manager

    if not isinstance(payload, dict) or "registrations" not in payload:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "Missing registrations list in request body"},
        )

    registrations = payload["registrations"]
    if not isinstance(registrations, list) or len(registrations) == 0:
        return {"success": True, "data": [], "count": 0}

    if len(registrations) > 500:
        registrations = registrations[:500]

    session = db_manager.get_session()
    try:
        placeholders = ", ".join([f":reg{i}" for i in range(len(registrations))])
        params = {f"reg{i}": reg for i, reg in enumerate(registrations)}

        result = session.execute(
            text(
                f"""
                SELECT
                    id, registration, hex_code, owner, operator,
                    model, manufacturer, serial_number, year_built,
                    country_of_registration, aircraft_type, ai_analysis,
                    last_updated, data_source, images_downloaded
                FROM aircraft_static_info
                WHERE registration IN ({placeholders})
                ORDER BY registration
                """  # noqa: S608 — placeholders are :name binds, not user text
            ),
            params,
        )
        aircraft_list = [_static_info_row_to_dict(row) for row in result]
        return {"success": True, "data": aircraft_list, "count": len(aircraft_list)}
    finally:
        session.close()


@router.get("/api/aircraft/static/stats", name="aircraft_static_stats")
async def get_static_info_stats() -> dict[str, Any]:
    """Rollup: total, military, government, vip, by_country, by_manufacturer.

    Same query as ``web_app.py:1430``.
    """
    from web_app import db_manager

    session = db_manager.get_session()
    try:
        total = session.execute(text("SELECT COUNT(*) FROM aircraft_static_info")).scalar() or 0
        military = (
            session.execute(
                text("SELECT COUNT(*) FROM aircraft_static_info WHERE is_military = 1")
            ).scalar()
            or 0
        )
        government = (
            session.execute(
                text("SELECT COUNT(*) FROM aircraft_static_info WHERE is_government = 1")
            ).scalar()
            or 0
        )
        vip = (
            session.execute(
                text("SELECT COUNT(*) FROM aircraft_static_info WHERE is_vip = 1")
            ).scalar()
            or 0
        )
        country_stats = session.execute(
            text(
                """
                SELECT country, COUNT(*) as count
                FROM aircraft_static_info
                WHERE country IS NOT NULL AND country != ''
                GROUP BY country
                ORDER BY count DESC
                """
            )
        ).fetchall()
        manufacturer_stats = session.execute(
            text(
                """
                SELECT manufacturer, COUNT(*) as count
                FROM aircraft_static_info
                WHERE manufacturer IS NOT NULL AND manufacturer != ''
                GROUP BY manufacturer
                ORDER BY count DESC
                """
            )
        ).fetchall()
        return {
            "success": True,
            "stats": {
                "total": total,
                "military": military,
                "government": government,
                "vip": vip,
                "by_country": [{"country": r[0], "count": r[1]} for r in country_stats],
                "by_manufacturer": [
                    {"manufacturer": r[0], "count": r[1]} for r in manufacturer_stats
                ],
            },
        }
    finally:
        session.close()


@router.get("/api/aircraft/static/{registration}", name="aircraft_static_one")
async def get_static_info(registration: str) -> dict[str, Any]:
    """Full static-info row for one registration, superset of the list shape.

    Same query + response shape as ``web_app.py:1319``, including the
    ``livery_*``, ``attention_*``, ``anomalies``, ``flight_pattern``,
    ``recommended_actions``, ``hit_count``, and image-path fields the
    list shape doesn't return. 404 when the registration has no row.

    Route order matters: ``/static/{registration}`` must come *after*
    ``/static/stats`` and ``/static/batch`` in this file so ``stats`` /
    ``batch`` don't get swallowed as a ``registration``.
    """
    from web_app import convert_utc_to_beijing, db_manager, get_image_url

    session = db_manager.get_session()
    try:
        result = session.execute(
            text("SELECT * FROM aircraft_static_info WHERE registration = :reg"),
            {"reg": registration},
        ).fetchone()

        if not result:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "error": f"No static info found for registration: {registration}",
                },
            )

        raw_data = result._mapping if hasattr(result, "_mapping") else dict(result)

        data: dict[str, Any] = {
            "id": raw_data.get("id"),
            "registration": raw_data.get("registration"),
            "hex": raw_data.get("hex_code"),
            "aircraft_type": raw_data.get("aircraft_type"),
            "aircraft_model": raw_data.get("model"),
            "owner": raw_data.get("owner"),
            "operator": raw_data.get("operator"),
            "manufacturer": raw_data.get("manufacturer"),
            "serial_number": raw_data.get("serial_number"),
            "year_built": raw_data.get("year_built"),
            "country": raw_data.get("country_of_registration"),
            "data_source": raw_data.get("data_source"),
            "updated_at": (
                convert_utc_to_beijing(str(raw_data["last_updated"]))
                if raw_data.get("last_updated")
                else None
            ),
            "organization": raw_data.get("organization"),
            "livery_type": raw_data.get("livery_type"),
            "livery_name": raw_data.get("livery_name"),
            "livery_description": raw_data.get("livery_description"),
            "special_markings": raw_data.get("special_markings"),
            "attention_level": raw_data.get("attention_level"),
            "attention_reason": raw_data.get("attention_reason"),
            "intelligence_summary": raw_data.get("intelligence_summary"),
            "anomalies": raw_data.get("anomalies"),
            "flight_pattern": raw_data.get("flight_pattern"),
            "recommended_actions": raw_data.get("recommended_actions"),
            "hit_count": raw_data.get("hit_count"),
            "images_downloaded": raw_data.get("images_downloaded"),
            "images_updated_at": (
                convert_utc_to_beijing(str(raw_data["images_updated_at"]))
                if raw_data.get("images_updated_at")
                else None
            ),
        }

        flags = _extract_ai_flags(raw_data.get("ai_analysis"))
        data["is_military"] = flags["is_military"]
        data["is_government"] = flags["is_government"]
        data["is_vip"] = flags["is_vip"]
        data["summary"] = flags["summary"]
        # These two only surface on the single-registration endpoint.
        ai_raw = raw_data.get("ai_analysis")
        if ai_raw:
            if isinstance(ai_raw, str):
                try:
                    ai_data = json.loads(ai_raw)
                except json.JSONDecodeError:
                    ai_data = {}
            else:
                ai_data = ai_raw if isinstance(ai_raw, dict) else {}
            data["tags"] = ai_data.get("tags", [])
            data["previous_owners"] = ai_data.get("previous_owners")

        images_result = session.execute(
            text(
                """
                SELECT image_path FROM aircraft_images
                WHERE registration = :reg
                ORDER BY display_order LIMIT 3
                """
            ),
            {"reg": registration},
        ).fetchall()
        image_paths = [row[0] for row in images_result if row[0]]
        data["image_path_1"] = get_image_url(image_paths[0]) if len(image_paths) > 0 else None
        data["image_path_2"] = get_image_url(image_paths[1]) if len(image_paths) > 1 else None
        data["image_path_3"] = get_image_url(image_paths[2]) if len(image_paths) > 2 else None

        return {"success": True, "data": data}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Batch 3 — {identifier} series + HTML detail shells
#
# Route ordering matters: these all come *after* the concrete
# ``/api/aircraft/static``, ``/api/aircraft/types``, ``/api/aircraft/unique``,
# ``/api/aircraft/recent``, ``/api/aircraft/search`` handlers above, so
# ``{identifier}`` doesn't swallow them. FastAPI resolves routes by
# registration order, so appending here is enough.
# ---------------------------------------------------------------------------


@router.get("/api/aircraft/{identifier}/live", name="aircraft_live")
async def get_aircraft_live(identifier: str) -> dict[str, Any]:
    """Latest snapshot for one aircraft. Same as ``web_app.py:2263``.

    ``identifier`` matches on either registration or hex-code, delegated
    to :meth:`AircraftService.get_aircraft_live_position`.
    """
    from src.services.aircraft_service import AircraftService
    from web_app import config, convert_utc_to_beijing, db_manager, transform_image_paths

    session = db_manager.get_session()
    try:
        aircraft_service = AircraftService(session, config.config if config else {})
        result = aircraft_service.get_aircraft_live_position(identifier)
        if not result:
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": f"Aircraft not found: {identifier}"},
            )
        if result.get("snapshot_time"):
            result["snapshot_time"] = convert_utc_to_beijing(result["snapshot_time"])
        result["timezone"] = "Asia/Shanghai"
        transform_image_paths(result)
        return {"success": True, "aircraft": result}
    finally:
        session.close()


@router.get("/api/aircraft/{identifier}/details", name="aircraft_details_api")
async def get_aircraft_details_api(identifier: str) -> dict[str, Any]:
    """Full detail row for one aircraft. Same as ``web_app.py:2292``."""
    from src.services.aircraft_service import AircraftService
    from web_app import config, convert_utc_to_beijing, db_manager, transform_image_paths

    session = db_manager.get_session()
    try:
        aircraft_service = AircraftService(session, config.config if config else {})
        result = aircraft_service.get_aircraft_details(identifier)
        if not result:
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": f"Aircraft not found: {identifier}"},
            )
        if result.get("last_updated"):
            result["last_updated"] = convert_utc_to_beijing(result["last_updated"])
        transform_image_paths(result)
        return {"success": True, "aircraft": result}
    finally:
        session.close()


@router.get("/api/aircraft/{identifier}/history", name="aircraft_history_api")
async def get_aircraft_history_api(
    identifier: str,
    date: str | None = Query(None, description="Beijing-date YYYY-MM-DD"),
    start_time: str | None = Query(None, description="Beijing-time inclusive lower bound"),
    end_time: str | None = Query(None, description="Beijing-time inclusive upper bound"),
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, Any]:
    """Historical track points with optional date / time-window filters.

    Same as ``web_app.py:2320``.
    """
    from src.services.aircraft_service import AircraftService
    from web_app import config, convert_beijing_to_utc, convert_utc_to_beijing, db_manager

    parsed_date = None
    if date:
        try:
            parsed_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            pass

    parsed_start = convert_beijing_to_utc(start_time) if start_time else None
    parsed_end = convert_beijing_to_utc(end_time) if end_time else None

    session = db_manager.get_session()
    try:
        aircraft_service = AircraftService(session, config.config if config else {})
        track_points = aircraft_service.get_aircraft_history(
            identifier, parsed_date, parsed_start, parsed_end, limit
        )
        for point in track_points:
            if point.get("datetime"):
                point["datetime_beijing"] = convert_utc_to_beijing(point["datetime"])
            point["timezone"] = "Asia/Shanghai"
        return {
            "success": True,
            "identifier": identifier,
            "tracks": track_points,
            "count": len(track_points),
        }
    finally:
        session.close()


@router.get("/api/aircraft/{identifier}/flight-dates", name="aircraft_flight_dates")
async def get_aircraft_flight_dates(
    identifier: str,
    days_back: int = Query(30, ge=1, le=365),
) -> dict[str, Any]:
    """Distinct dates with flight records over the last ``days_back`` days.

    Same as ``web_app.py:2374``.
    """
    from src.services.aircraft_service import AircraftService
    from web_app import config, db_manager

    session = db_manager.get_session()
    try:
        aircraft_service = AircraftService(session, config.config if config else {})
        dates = aircraft_service.get_aircraft_flight_dates(identifier, days_back)
        return {
            "success": True,
            "identifier": identifier,
            "dates": dates,
            "count": len(dates),
        }
    finally:
        session.close()


@router.get("/api/aircraft/{registration}/recent-flights", name="aircraft_recent_flights")
async def get_aircraft_recent_flights(registration: str) -> dict[str, Any]:
    """Last 10 arrival/departure rows from flight_schedules for this aircraft.

    Same as ``web_app.py:2480``. Note the path param name is
    ``registration`` here (upper-cased) while sibling routes call it
    ``identifier`` (accepts hex too). Kept literal so the OpenAPI spec
    matches the Flask URL.
    """
    from web_app import _to_iso, db_manager

    session = db_manager.get_session()
    try:
        result = session.execute(
            text(
                """
                SELECT
                    flight_type,
                    airport_iata,
                    remote_airport_iata,
                    remote_airport_name,
                    scheduled_time,
                    status
                FROM flight_schedules
                WHERE aircraft_registration = :reg
                ORDER BY scheduled_time DESC
                LIMIT 10
                """
            ),
            {"reg": registration.upper()},
        ).fetchall()

        flights = [
            {
                "flight_type": row[0],
                "airport_iata": row[1],
                "remote_airport_iata": row[2],
                "remote_airport_name": row[3],
                "scheduled_time": _to_iso(row[4]),
                "status": row[5],
            }
            for row in result
        ]
        return {
            "success": True,
            "registration": registration.upper(),
            "flights": flights,
            "count": len(flights),
        }
    finally:
        session.close()


@router.get("/api/aircraft/{identifier}/images", name="aircraft_images_api")
async def get_aircraft_images_api(identifier: str) -> dict[str, Any]:
    """Image list for one aircraft — bare-URL list + richer metadata list.

    Same as ``web_app.py:2533``. ``identifier`` may be a registration or
    hex; a hex lookup falls back to the most recent snapshot's
    registration.
    """
    from web_app import _to_iso, db_manager, get_image_url

    registration = identifier.upper()
    session = db_manager.get_session()
    try:
        # Try hex lookup first — if identifier is a hex it maps to a
        # registration via the most recent snapshot.
        hex_lookup = session.execute(
            text(
                """
                SELECT registration
                FROM aircraft_snapshots
                WHERE hex = :hex AND registration IS NOT NULL AND registration != ''
                ORDER BY snapshot_time DESC
                LIMIT 1
                """
            ),
            {"hex": identifier.lower()},
        ).fetchone()
        if hex_lookup and hex_lookup[0]:
            registration = hex_lookup[0]

        images_result = session.execute(
            text(
                """
                SELECT
                    image_path, photographer, photo_date, location,
                    airport_name, notes, display_order, is_primary,
                    jetphotos_id, source_url, upload_date
                FROM aircraft_images
                WHERE registration = :reg
                AND image_path IS NOT NULL
                AND image_path != ''
                ORDER BY photo_date DESC NULLS LAST
                """
            ),
            {"reg": registration},
        ).fetchall()

        image_urls: list[str] = []
        images_with_metadata: list[dict[str, Any]] = []
        for row in images_result:
            url = get_image_url(row[0])
            if url:
                image_urls.append(url)
                images_with_metadata.append(
                    {
                        "url": url,
                        "photographer": row[1],
                        "photo_date": _to_iso(row[2]),
                        "location": row[3],
                        "airport_name": row[4],
                        "notes": row[5],
                        "display_order": row[6],
                        "is_primary": row[7],
                        "jetphotos_id": row[8],
                        "source_url": row[9],
                        "upload_date": _to_iso(row[10]),
                    }
                )
        return {
            "success": True,
            "identifier": identifier,
            "registration": registration,
            "images": image_urls,
            "images_with_metadata": images_with_metadata,
            "count": len(image_urls),
        }
    finally:
        session.close()


@router.get("/api/aircraft/{identifier}/static-info", name="aircraft_static_info_api")
async def get_aircraft_static_info_api(identifier: str) -> dict[str, Any]:
    """Static info by registration OR hex — narrower shape than
    ``/api/aircraft/static/{registration}``.

    Same as ``web_app.py:2614``. This endpoint predates the more
    complete ``/static/{registration}`` route above; both are kept for
    backward compat with clients that were already using either.
    """
    from web_app import db_manager, get_image_url

    registration = identifier.upper()
    session = db_manager.get_session()
    try:
        result = session.execute(
            text(
                """
                SELECT
                    registration, hex_code, owner, operator,
                    manufacturer, model, aircraft_type,
                    serial_number, year_built, country_of_registration,
                    organization, livery_type
                FROM aircraft_static_info
                WHERE registration = :reg OR hex_code = :hex
                """
            ),
            {"reg": registration, "hex": identifier.lower()},
        ).fetchone()

        if not result:
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": "Static info not found"},
            )

        images_result = session.execute(
            text(
                """
                SELECT image_path FROM aircraft_images
                WHERE registration = :reg
                ORDER BY display_order LIMIT 3
                """
            ),
            {"reg": result[0]},
        ).fetchall()
        image_paths = [get_image_url(row[0]) for row in images_result if row[0]]

        static_info = {
            "registration": result[0],
            "hex_code": result[1],
            "owner": result[2],
            "operator": result[3] or result[10],  # fallback to organization
            "manufacturer": result[4],
            "model": result[5],
            "aircraft_type": result[6],
            "serial_number": result[7],
            "year_built": result[8],
            "country_of_registration": result[9],
            "organization": result[10],
            "livery_type": result[11],
            "images": image_paths,
        }
        return {"success": True, "static_info": static_info}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# HTML shells — pure `render_template`, login-gated. The client-side JS
# fetches from the JSON endpoints above.
# ---------------------------------------------------------------------------


@router.get("/aircraft/{registration}", name="aircraft_detail")
async def aircraft_detail(
    request: Request,
    registration: str,
    _user: dict[str, Any] = Depends(require_login),
):
    """Aircraft detail page shell. Same as ``web_app.py:1520``."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "aircraft_detail.html", {"registration": registration}
    )


@router.get("/aircraft-type/{type_code}", name="aircraft_type_detail")
async def aircraft_type_detail(
    request: Request,
    type_code: str,
    _user: dict[str, Any] = Depends(require_login),
):
    """Aircraft-type detail page shell. Same as ``web_app.py:1527``."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "aircraft_type_detail.html", {"type_code": type_code}
    )
