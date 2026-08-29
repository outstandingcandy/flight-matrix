"""FastAPI port of the ``/api/search/*`` routes.

Stage 0 lift-and-shift — same delegation pattern as
:mod:`aircraft_fastapi`. Handler bodies keep the exact SQL and response
shape they had in Flask; only the framework layer changes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

logger = logging.getLogger("web.search")

router = APIRouter(tags=["search"])


@router.get("/api/search/unified", name="unified_search")
async def unified_search(
    q: str = Query("", description="Search text"),
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    """Cross-category search: airports + aircraft (by registration prefix)
    + aircraft types (by type-code prefix). Same as ``web_app.py:1542``.

    400 when ``q`` is shorter than 2 characters, matching Flask.
    """
    from src.services.airport_service import AirportService
    from web_app import config, db_manager, get_aircraft_type_name

    query = q.strip()
    if not query or len(query) < 2:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "Search query must be at least 2 characters"},
        )

    results: dict[str, list[Any]] = {"airports": [], "aircraft": [], "aircraft_types": []}

    db_session = db_manager.get_session()
    try:
        airport_service = AirportService(db_session, config.config if config else {})
        results["airports"] = airport_service.search_airports(query, limit)

        query_upper = query.upper()

        aircraft_result = db_session.execute(
            text(
                """
                SELECT registration, aircraft_type, owner, operator, hex_code
                FROM aircraft_static_info
                WHERE LOWER(registration) LIKE LOWER(:pattern)
                ORDER BY registration
                LIMIT :limit
                """
            ),
            {"pattern": f"{query_upper}%", "limit": limit},
        )
        for row in aircraft_result:
            results["aircraft"].append(
                {
                    "registration": row.registration,
                    "aircraft_type": row.aircraft_type,
                    "owner": row.owner,
                    "operator": row.operator,
                    "hex_code": row.hex_code,
                }
            )

        type_result = db_session.execute(
            text(
                """
                SELECT aircraft_type, COUNT(*) as aircraft_count
                FROM aircraft_static_info
                WHERE aircraft_type IS NOT NULL AND aircraft_type != ''
                  AND LOWER(aircraft_type) LIKE LOWER(:pattern)
                GROUP BY aircraft_type
                ORDER BY aircraft_count DESC
                LIMIT :limit
                """
            ),
            {"pattern": f"{query_upper}%", "limit": limit},
        )
        for row in type_result:
            results["aircraft_types"].append(
                {
                    "type_code": row.aircraft_type,
                    "aircraft_count": row.aircraft_count,
                    "name": get_aircraft_type_name(row.aircraft_type),
                }
            )

        return {"success": True, "results": results}
    finally:
        db_session.close()


@router.get("/api/search/suggestions", name="search_suggestions")
async def search_suggestions() -> dict[str, Any]:
    """Curated popular items for the home page, sourced from
    ``config.home_popular``. Cached for 1 h on the shared ``api_cache``
    on ``web_app``.

    Same as ``web_app.py:1622``. Deliberately does NOT fall back to slow
    DB queries when the config lists come up empty — matches Flask,
    which "skip[s] slow database fallback queries" too.
    """
    from web_app import api_cache, config, db_manager

    cache_key = "search_suggestions"
    cached_data, hit = api_cache.get(cache_key)
    if hit:
        return cached_data

    db_session = db_manager.get_session()
    try:
        home_config = config.get("home_popular", {}) if config else {}
        config_airports = home_config.get("airports", [])
        config_aircraft = home_config.get("aircraft", [])

        config_airport_details: list[dict[str, Any]] = []
        if config_airports:
            placeholders = ", ".join([f":code{i}" for i in range(len(config_airports))])
            params = {f"code{i}": code.upper() for i, code in enumerate(config_airports)}
            config_airports_result = db_session.execute(
                text(
                    f"""
                    SELECT a.iata_code, a.icao_code, a.name, a.city, a.country, a.country_code
                    FROM airports a
                    WHERE a.iata_code IN ({placeholders}) OR a.icao_code IN ({placeholders})
                    """
                ),
                params,
            )
            for row in config_airports_result:
                config_airport_details.append(
                    {
                        "iata_code": row.iata_code,
                        "icao_code": row.icao_code,
                        "name": row.name or row.iata_code,
                        "city": row.city,
                        "country": row.country,
                        "country_code": row.country_code,
                        "from_config": True,
                    }
                )

        config_aircraft_details: list[dict[str, Any]] = []
        if config_aircraft:
            placeholders = ", ".join([f":reg{i}" for i in range(len(config_aircraft))])
            params = {f"reg{i}": reg.upper() for i, reg in enumerate(config_aircraft)}
            config_aircraft_result = db_session.execute(
                text(
                    f"""
                    SELECT asi.registration, asi.aircraft_type, asi.owner, asi.operator
                    FROM aircraft_static_info asi
                    WHERE asi.registration IN ({placeholders})
                    """
                ),
                params,
            )
            for row in config_aircraft_result:
                config_aircraft_details.append(
                    {
                        "registration": row.registration,
                        "aircraft_type": row.aircraft_type,
                        "owner": row.owner,
                        "operator": row.operator,
                        "from_config": True,
                    }
                )

        result_data = {
            "success": True,
            "popular_airports": config_airport_details[:5],
            "recent_aircraft": config_aircraft_details[:5],
        }
        api_cache.set(cache_key, result_data, ttl_seconds=3600)
        return result_data
    finally:
        db_session.close()


@router.get("/api/search/aircraft", name="super_search_aircraft")
async def super_search_aircraft(
    registration: str = Query("", description="Registration filter"),
    flight_number: str = Query("", description="Flight number filter"),
    type_series: str = Query("", description="Aircraft type series filter"),
    operator: str = Query("", description="Operator filter"),
    is_military: str | None = Query(None),
    is_widebody: str | None = Query(None),
    is_cargo: str | None = Query(None),
    hours_back: float | None = Query(None),
    limit: int = Query(100, ge=1, le=5000),
) -> dict[str, Any]:
    """Composite search delegated to ``AircraftService.search_aircraft``.

    Same as ``web_app.py:2204``. The bool query params come in as strings
    (Flask semantics: any ``is_military=true`` case-insensitive is True,
    everything else — including empty — is False) instead of typed bools;
    keeping the string parse so a client that passes "" gets the "not
    filtered" branch, not the "false" branch.
    """
    from src.services.aircraft_service import AircraftService
    from web_app import config, convert_utc_to_beijing, db_manager, transform_image_paths

    def _tri_bool(raw: str | None) -> bool | None:
        if raw is None:
            return None
        return raw.lower() == "true"

    session = db_manager.get_session()
    try:
        aircraft_service = AircraftService(session, config.config if config else {})
        results = aircraft_service.search_aircraft(
            registration=registration.strip() or None,
            flight_number=flight_number.strip() or None,
            type_series=type_series.strip() or None,
            operator=operator.strip() or None,
            is_military=_tri_bool(is_military),
            is_widebody=_tri_bool(is_widebody),
            is_cargo=_tri_bool(is_cargo),
            hours_back=hours_back,
            limit=limit,
        )

        for result in results:
            if result.get("snapshot_time"):
                result["snapshot_time"] = convert_utc_to_beijing(result["snapshot_time"])
            result["timezone"] = "Asia/Shanghai"
            transform_image_paths(result)

        return {"success": True, "aircraft": results, "count": len(results)}
    finally:
        session.close()
