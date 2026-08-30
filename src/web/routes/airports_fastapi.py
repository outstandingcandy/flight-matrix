"""FastAPI port of the ``/api/airports/*`` and ``/api/statistics`` routes.

Stage 0 lift-and-shift — same delegation pattern as
:mod:`aircraft_fastapi`. Handlers only own the framework layer;
``AirportService``, ``get_image_url``, ``_to_iso``, ``convert_utc_to_beijing``,
``latest_rows``, ``minutes_ago``, ``minutes_from_now`` and the module-level
``api_cache`` all continue to live on ``web_app``.

``/api/statistics`` lives here rather than in a one-endpoint module of
its own; it's five lines calling ``db_manager.get_statistics()`` and
adding a file per handler would be more noise than signal.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import bindparam, text

from src.web.time_helpers import naive_utc_now

logger = logging.getLogger("web.airports")

router = APIRouter(prefix="/api/v1", tags=["airports"])


MAX_TYPE_PHOTO_TYPES = 40
MAX_TYPE_PHOTOS_PER_TYPE = 12
DEFAULT_TYPE_PHOTOS_PER_TYPE = 8
TYPE_PHOTOS_CACHE_TTL = 1800


@router.get("/statistics", name="get_statistics")
async def get_statistics() -> dict[str, Any]:
    """Same as ``web_app.py:1098``. ``DatabaseManager.get_statistics()``
    already returns the shape the frontend expects."""
    from src.web.runtime import db_manager

    return {"success": True, "statistics": db_manager.get_statistics()}


@router.get("/airports/search", name="search_airports")
async def search_airports(
    q: str = Query("", description="Search text (name / IATA / ICAO / city)"),
    limit: int = Query(20, ge=1, le=100),
    type: str | None = Query(
        None, description="Airport type filter: large_airport / medium_airport / …"
    ),
) -> dict[str, Any]:
    """Same as ``web_app.py:1726``. 400 when ``q`` is empty (Flask matched)."""
    from src.services.airport_service import AirportService
    from src.web.runtime import config, db_manager

    query = q.strip()
    if not query:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": "Search query is required"}
        )

    session = db_manager.get_session()
    try:
        airport_service = AirportService(session, config.config if config else {})
        airport_types = [type] if type else None
        airports = airport_service.search_airports(query, limit, airport_types)
        return {"success": True, "airports": airports, "count": len(airports)}
    finally:
        session.close()


@router.get("/airports/popular", name="get_popular_airports")
async def get_popular_airports(
    country: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Same as ``web_app.py:2184``.

    Registered before the ``{airport_code}`` catch-all so ``/popular``
    doesn't get swallowed.
    """
    from src.services.airport_service import AirportService
    from src.web.runtime import config, db_manager

    session = db_manager.get_session()
    try:
        airport_service = AirportService(session, config.config if config else {})
        airports = airport_service.get_popular_airports(country, limit)
        return {"success": True, "airports": airports, "count": len(airports)}
    finally:
        session.close()


@router.get("/airports/{airport_code}", name="get_airport")
async def get_airport(airport_code: str) -> dict[str, Any]:
    """Same as ``web_app.py:1751``. 404 when no matching row."""
    from src.services.airport_service import AirportService
    from src.web.runtime import config, db_manager

    session = db_manager.get_session()
    try:
        airport_service = AirportService(session, config.config if config else {})
        airport = airport_service.get_airport_by_code(airport_code)
        if not airport:
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": f"Airport not found: {airport_code}"},
            )
        return {"success": True, "airport": airport}
    finally:
        session.close()


@router.get("/airports/{airport_code}/nearby", name="get_aircraft_near_airport")
async def get_aircraft_near_airport(
    airport_code: str,
    radius_km: float = Query(1000, gt=0, le=10000),
    hours_back: float = Query(0.5, gt=0, le=48),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """Same as ``web_app.py:1773``. Delegates to
    ``AirportService.get_aircraft_near_airport`` which returns a dict
    that already includes the extra top-level keys."""
    from src.services.airport_service import AirportService
    from src.web.runtime import config, db_manager

    session = db_manager.get_session()
    try:
        airport_service = AirportService(session, config.config if config else {})
        result = airport_service.get_aircraft_near_airport(
            airport_code, radius_km, hours_back, limit
        )
        if "error" in result and not result.get("aircraft"):
            raise HTTPException(
                status_code=404, detail={"success": False, "error": result["error"]}
            )
        return {"success": True, **result}
    finally:
        session.close()


@router.get(
    "/airports/{airport_code}/realtime-aircraft",
    name="get_realtime_aircraft_near_airport",
)
async def get_realtime_aircraft_near_airport(
    airport_code: str,
    radius_km: float | None = Query(None, gt=0, le=10000),
    minutes_back: float = Query(10, gt=0, le=1440),
    limit: int = Query(500, ge=1, le=5000),
    aircraft_type: str = Query("", description="Aircraft-type substring filter"),
    has_livery: str = Query("", description="Currently unused; Flask signature kept"),
    flight_numbers: str = Query("", description="Comma-separated whitelist of flight numbers"),
) -> dict[str, Any]:
    """FR24 realtime positions near the airport, matched with today's
    flight_schedules where available.

    Direct port of ``web_app.py:1799``. Long — but the body is exactly
    what Flask ran; the only changes are typed query params, dict return,
    and ``HTTPException`` for the 404 branch.
    """
    from src.data.dialect import latest_rows, minutes_ago, minutes_from_now
    from src.services.airport_service import AirportService
    from src.web.runtime import config, db_manager
    from src.web.time_helpers import _to_iso, convert_utc_to_beijing

    flight_numbers_filter = (
        [fn.strip().upper() for fn in flight_numbers.split(",") if fn.strip()]
        if flight_numbers
        else []
    )

    db_session = db_manager.get_session()
    try:
        airport_service = AirportService(db_session, config.config if config else {})
        airport = airport_service.get_airport_by_code(airport_code)
        if not airport:
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": f"Airport not found: {airport_code}"},
            )

        airport_lat = airport["latitude"]
        airport_lon = airport["longitude"]
        minutes_back_int = int(minutes_back)
        query_params: dict[str, Any] = {}

        geo_filter_clause = ""
        if radius_km:
            lat_delta = radius_km / 111.0
            lon_delta = radius_km / (111.0 * max(0.1, math.cos(math.radians(airport_lat))))
            query_params.update(
                {
                    "min_lat": airport_lat - lat_delta,
                    "max_lat": airport_lat + lat_delta,
                    "min_lon": airport_lon - lon_delta,
                    "max_lon": airport_lon + lon_delta,
                }
            )
            geo_filter_clause = (
                "AND latitude BETWEEN :min_lat AND :max_lat "
                "AND longitude BETWEEN :min_lon AND :max_lon"
            )

        flight_numbers_clause = ""
        if flight_numbers_filter:
            flight_numbers_clause = "WHERE UPPER(flight_number) IN :flight_numbers"
            query_params["flight_numbers"] = flight_numbers_filter

        latest_positions = latest_rows(
            columns="""fr24_id, flight_number, callsign, registration, aircraft_type,
                    latitude, longitude, altitude, ground_speed, heading,
                    vertical_speed, squawk, origin_iata, destination_iata,
                    on_ground, fr24_timestamp, scraped_at""",
            source="aircraft_realtime_positions",
            partition_by="fr24_id",
            order_by="scraped_at DESC",
            where=(
                f"scraped_at >= {minutes_ago(minutes_back_int, is_postgres=db_manager.is_postgres)} "
                f"{geo_filter_clause}"
            ),
            is_postgres=db_manager.is_postgres,
        )

        query = f"""
            WITH latest_positions AS (
                {latest_positions}
            )
            SELECT * FROM latest_positions
            {flight_numbers_clause}
        """
        statement = text(query)
        if flight_numbers_filter:
            statement = statement.bindparams(bindparam("flight_numbers", expanding=True))
        result = db_session.execute(statement, query_params)

        aircraft_list: list[dict[str, Any]] = []
        for row in result:
            if row.latitude is None or row.longitude is None:
                continue
            lat = float(row.latitude)
            lon = float(row.longitude)

            R = 6371
            lat1, lon1 = math.radians(airport_lat), math.radians(airport_lon)
            lat2, lon2 = math.radians(lat), math.radians(lon)
            dlat, dlon = lat2 - lat1, lon2 - lon1
            a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            distance_km = 2 * R * math.asin(math.sqrt(a))
            if radius_km and distance_km > radius_km:
                continue

            if aircraft_type and row.aircraft_type:
                if aircraft_type.upper() not in row.aircraft_type.upper():
                    continue

            altitude = row.altitude or 0
            vertical_rate = row.vertical_speed or 0
            if altitude < 500 or row.on_ground:
                status = "ground"
            elif distance_km < 50 and altitude < 10000:
                if vertical_rate < -300:
                    status = "approaching"
                elif vertical_rate > 300:
                    status = "departing"
                else:
                    status = "approaching" if altitude < 5000 else "cruising"
            else:
                status = "cruising"

            aircraft_list.append(
                {
                    "fr24_id": row.fr24_id,
                    "registration": row.registration,
                    "flight_number": row.flight_number,
                    "callsign": row.callsign,
                    "aircraft_type": row.aircraft_type,
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": altitude,
                    "altitude_baro": altitude,
                    "ground_speed": row.ground_speed,
                    "heading": row.heading,
                    "track": row.heading,
                    "vertical_speed": row.vertical_speed,
                    "squawk": row.squawk,
                    "origin_iata": row.origin_iata,
                    "destination_iata": row.destination_iata,
                    "on_ground": row.on_ground,
                    "distance_km": round(distance_km, 2),
                    "flight_status": status,
                    "scraped_at": _to_iso(row.scraped_at),
                    "is_military": False,
                    "is_widebody": False,
                    "is_cargo": False,
                }
            )

        aircraft_list.sort(key=lambda x: x["distance_km"])
        aircraft_list = aircraft_list[:limit]

        # Match aircraft with today's flight schedules.
        airport_iata = airport.get("iata_code", "").upper()
        if airport_iata:
            is_postgres = db_manager.is_postgres
            schedule_query = f"""
                SELECT
                    fs.id as schedule_id,
                    fs.flight_number,
                    fs.aircraft_registration,
                    fs.scheduled_time,
                    fs.status,
                    fs.flight_type
                FROM flight_schedules fs
                WHERE fs.airport_iata = :airport_iata
                  AND fs.scheduled_time BETWEEN
                      {minutes_from_now(-2 * 60, is_postgres=is_postgres)}
                      AND {minutes_from_now(4 * 60, is_postgres=is_postgres)}
            """
            schedule_result = db_session.execute(
                text(schedule_query), {"airport_iata": airport_iata}
            )
            schedules = [dict(row._mapping) for row in schedule_result]

            schedule_by_flight_number: dict[str, dict[str, Any]] = {}
            schedule_by_registration: dict[str, dict[str, Any]] = {}
            for sched in schedules:
                fn = (sched.get("flight_number") or "").upper()
                reg = (sched.get("aircraft_registration") or "").upper()
                if fn and fn not in schedule_by_flight_number:
                    schedule_by_flight_number[fn] = sched
                if reg and reg not in schedule_by_registration:
                    schedule_by_registration[reg] = sched

            for ac in aircraft_list:
                ac_fn = (ac.get("flight_number") or "").upper()
                ac_reg = (ac.get("registration") or "").upper()
                matched_schedule = None
                if ac_fn and ac_fn in schedule_by_flight_number:
                    matched_schedule = schedule_by_flight_number[ac_fn]
                elif ac_reg and ac_reg in schedule_by_registration:
                    matched_schedule = schedule_by_registration[ac_reg]

                if matched_schedule:
                    ac["schedule_id"] = matched_schedule.get("schedule_id")
                    ac["has_schedule"] = True
                    sched_time = matched_schedule.get("scheduled_time")
                    ac["scheduled_time"] = (
                        convert_utc_to_beijing(_to_iso(sched_time)) if sched_time else None
                    )
                    ac["schedule_status"] = matched_schedule.get("status")
                    ac["schedule_flight_type"] = matched_schedule.get("flight_type")
                else:
                    ac["schedule_id"] = None
                    ac["has_schedule"] = False
                    ac["scheduled_time"] = None
                    ac["schedule_status"] = None
                    ac["schedule_flight_type"] = None

        approaching = [a for a in aircraft_list if a["flight_status"] == "approaching"]
        departing = [a for a in aircraft_list if a["flight_status"] == "departing"]
        cruising = [a for a in aircraft_list if a["flight_status"] == "cruising"]
        ground = [a for a in aircraft_list if a["flight_status"] == "ground"]

        return {
            "success": True,
            "airport": airport,
            "radius_km": radius_km,
            "query_time": naive_utc_now().isoformat(),
            "total_count": len(aircraft_list),
            "approaching_count": len(approaching),
            "departing_count": len(departing),
            "cruising_count": len(cruising),
            "ground_count": len(ground),
            "aircraft": aircraft_list,
        }
    finally:
        db_session.close()


@router.get(
    "/airports/{airport_code}/type-photos",
    name="get_airport_type_photos",
)
async def get_airport_type_photos(
    airport_code: str,
    types: str = Query("", description="Comma-separated ICAO type codes"),
    limit: int = Query(DEFAULT_TYPE_PHOTOS_PER_TYPE, ge=1, le=MAX_TYPE_PHOTOS_PER_TYPE),
) -> dict[str, Any]:
    """Best-N same-type-here photos per requested type. Same as
    ``web_app.py:2065``. Uses the shared ``api_cache`` on ``web_app``."""
    from src.services.airport_service import AirportService
    from src.web.image_helpers import get_image_url
    from src.web.runtime import api_cache, config, db_manager
    from src.web.time_helpers import _to_iso

    requested_types = sorted({t.strip().upper() for t in types.split(",") if t.strip()})[
        :MAX_TYPE_PHOTO_TYPES
    ]
    if not requested_types:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": "types parameter is required"}
        )

    db_session = db_manager.get_session()
    try:
        airport_service = AirportService(db_session, config.config if config else {})
        airport = airport_service.get_airport_by_code(airport_code)
        if not airport:
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": f"Airport not found: {airport_code}"},
            )

        airport_icao = (airport.get("icao_code") or "").upper()
        if not airport_icao:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "error": f"Airport {airport_code} has no ICAO code",
                },
            )

        cache_key = f"type_photos:{airport_icao}:{','.join(requested_types)}:{limit}"
        cached_data, hit = api_cache.get(cache_key)
        if hit:
            return cached_data

        statement = text(
            """
            SELECT aircraft_type, image_path, registration, photographer, photo_date, likes
            FROM (
                SELECT
                    asi.aircraft_type AS aircraft_type,
                    ai.image_path AS image_path,
                    ai.registration AS registration,
                    ai.photographer AS photographer,
                    ai.photo_date AS photo_date,
                    ai.likes AS likes,
                    ROW_NUMBER() OVER (
                        PARTITION BY asi.aircraft_type
                        ORDER BY COALESCE(ai.likes, 0) DESC,
                                 (ai.photo_date IS NULL), ai.photo_date DESC,
                                 ai.id DESC
                    ) AS rn
                FROM aircraft_images ai
                JOIN aircraft_static_info asi ON asi.registration = ai.registration
                WHERE ai.airport_icao = :airport_icao
                  AND ai.image_path IS NOT NULL
                  AND ai.image_path <> ''
                  AND asi.aircraft_type IN :types
            ) ranked
            WHERE rn <= :limit
            ORDER BY aircraft_type, rn
            """
        ).bindparams(bindparam("types", expanding=True))

        rows = db_session.execute(
            statement,
            {"airport_icao": airport_icao, "types": requested_types, "limit": limit},
        )

        photos_by_type: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            photos_by_type.setdefault(row.aircraft_type, []).append(
                {
                    "image_url": get_image_url(row.image_path),
                    "registration": row.registration,
                    "photographer": row.photographer,
                    "photo_date": _to_iso(row.photo_date),
                    "likes": row.likes,
                }
            )

        result_data = {
            "success": True,
            "airport_icao": airport_icao,
            "types": photos_by_type,
        }
        api_cache.set(cache_key, result_data, ttl_seconds=TYPE_PHOTOS_CACHE_TTL)
        return result_data
    finally:
        db_session.close()
