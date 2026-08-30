"""FastAPI port of the ``/api/admin/aircraft*`` routes.

Stage 0 lift-and-shift. Six handlers:

- GET /api/admin/aircraft                       — paginated list w/ OpenSearch backend
- GET /api/admin/aircraft/stats                 — total / active / special counts
- GET /api/admin/aircraft/types                 — distinct types (autocomplete)
- GET /api/admin/aircraft/liveries              — distinct liveries (autocomplete)
- GET /api/admin/aircraft/registrations         — registration autocomplete
- GET /api/admin/aircraft-query/{registration}  — cross-table admin drill-down

All gated with ``require_admin`` at the router level; the Flask
originals had per-route ``@admin_required``. Every SQL query, every
OpenSearch-vs-SQL fallback, every response field lines up 1:1 with
the Flask handlers — the only differences are typed query params,
dict returns, and ``HTTPException`` for the 400 branch.
"""

from __future__ import annotations

import logging
import time as _time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import bindparam, text

from src.auth.dependencies import require_admin

logger = logging.getLogger("web.admin.aircraft")

router = APIRouter(prefix="/api/v1", tags=["admin-aircraft"], dependencies=[Depends(require_admin)])


@router.get("/admin/aircraft", name="api_admin_list_aircraft")
async def api_admin_list_aircraft(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=500),
    search: str = Query(""),
    aircraft_type: str = Query(""),
    livery: str = Query(""),
    category: str = Query(""),
    sort: str = Query(""),
    order: str = Query(""),
) -> dict[str, Any]:
    """Same as ``web_app.py:3392``.

    OpenSearch answers the query whole (text search, filters, sort,
    page, total) when the cluster is reachable and the requested window
    stays within ``from + size``. SQL is the fallback with matching
    filters and order; only the collation of the registration tie-break
    differs. ``search_backend`` in the response reports which one
    answered.
    """
    from src.search.aircraft_index import MAX_WINDOW
    from src.web.helpers import SPECIAL_ATTENTION_LEVELS
    from src.web.image_helpers import get_image_url
    from src.web.runtime import db_manager
    from src.web.search_index import with_aircraft_index
    from src.web.time_helpers import _to_iso

    search = search.strip()
    aircraft_type = aircraft_type.strip()
    livery = livery.strip()
    category = category.strip()
    sort = sort.strip()
    order = order.strip().lower()
    offset = (page - 1) * limit

    sort_expressions = {
        "last_updated": ("asi.last_updated", "desc"),
        "photographers": ("COALESCE(pc.photographer_count, 0)", "desc"),
        "registration": ("asi.registration", "asc"),
    }
    if sort not in sort_expressions:
        sort = "last_updated"
    sort_expr, default_order = sort_expressions[sort]
    if order not in ("asc", "desc"):
        order = default_order
    direction = "ASC" if order == "asc" else "DESC"

    listing = (
        with_aircraft_index(
            lambda index: index.query_page(
                text=search,
                aircraft_type=aircraft_type,
                livery=livery,
                attention_levels=(SPECIAL_ATTENTION_LEVELS if category == "special" else ()),
                sort=sort,
                order=order,
                offset=offset,
                limit=limit,
            ),
            "Aircraft list",
        )
        if offset + limit <= MAX_WINDOW
        else None
    )
    search_backend = "opensearch" if listing is not None else "sql"

    session = db_manager.get_session()
    try:
        params: dict[str, Any] = {}
        where_clauses: list[str] = []
        page_sql = ""

        if listing is not None:
            where_clauses.append("asi.registration IN :registrations")
            params["registrations"] = listing.registrations
        else:
            if search:
                where_clauses.append(
                    "(LOWER(asi.registration) LIKE LOWER(:search)"
                    " OR LOWER(asi.hex_code) LIKE LOWER(:search))"
                )
                params["search"] = f"%{search}%"
            if aircraft_type:
                where_clauses.append("asi.aircraft_type = :aircraft_type")
                params["aircraft_type"] = aircraft_type
            if livery:
                where_clauses.append("asi.livery_name = :livery")
                params["livery"] = livery
            if category == "special":
                where_clauses.append("asi.attention_level IN :attention_levels")
                params["attention_levels"] = list(SPECIAL_ATTENTION_LEVELS)

            order_sql = f"{sort_expr} {direction} NULLS LAST"
            if sort != "registration":
                order_sql += ", asi.registration"
            page_sql = f"ORDER BY {order_sql} LIMIT :limit OFFSET :offset"
            params["limit"] = limit
            params["offset"] = offset

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        query = f"""
            SELECT
                asi.id, asi.registration, asi.hex_code, asi.aircraft_type,
                asi.manufacturer, asi.model, asi.operator, asi.owner,
                asi.country_of_registration, asi.year_built,
                (SELECT ai.image_path FROM aircraft_images ai
                 WHERE ai.registration = asi.registration
                 ORDER BY ai.display_order LIMIT 1) as image_path,
                asi.images_downloaded, asi.last_updated, asi.livery_name,
                asi.attention_level,
                COALESCE(pc.photographer_count, 0) as photographer_count
            FROM aircraft_static_info asi
            LEFT JOIN (
                SELECT registration, COUNT(DISTINCT photographer) as photographer_count
                FROM aircraft_images
                WHERE source = 'jetphotos'
                  AND photographer IS NOT NULL
                  AND photographer <> ''
                GROUP BY registration
            ) pc ON pc.registration = asi.registration
            {where_sql}
            {page_sql}
        """

        statement = text(query)
        for name in ("registrations", "attention_levels"):
            if name in params:
                statement = statement.bindparams(bindparam(name, expanding=True))

        result = (
            []
            if listing is not None and not listing.registrations
            else session.execute(statement, params).fetchall()
        )

        aircraft_list: list[dict[str, Any]] = []
        for row in result:
            is_special = row[14] in SPECIAL_ATTENTION_LEVELS if row[14] else False
            aircraft_list.append(
                {
                    "id": row[0],
                    "registration": row[1],
                    "hex_code": row[2],
                    "aircraft_type": row[3],
                    "manufacturer": row[4],
                    "model": row[5],
                    "operator": row[6],
                    "owner": row[7],
                    "country_of_registration": row[8],
                    "year_built": row[9],
                    "image_url": get_image_url(row[10]) if row[10] else None,
                    "images_downloaded": row[11],
                    "last_updated": _to_iso(row[12]),
                    "livery_name": row[13],
                    "aircraft_type_code": row[3],
                    "aircraft_type_full": None,
                    "is_widebody": False,
                    "is_cargo": False,
                    "is_passenger": False,
                    "is_military": False,
                    "is_special": is_special,
                    "photographer_count": row[15],
                }
            )

        if listing is not None:
            hydrated = {aircraft["registration"]: aircraft for aircraft in aircraft_list}
            aircraft_list = [
                hydrated[registration]
                for registration in listing.registrations
                if registration in hydrated
            ]
            total = listing.total
        else:
            count_query = f"SELECT COUNT(*) FROM aircraft_static_info asi {where_sql}"
            count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
            count_statement = text(count_query)
            if "attention_levels" in count_params:
                count_statement = count_statement.bindparams(
                    bindparam("attention_levels", expanding=True)
                )
            total = session.execute(count_statement, count_params).scalar() or 0

        pages = (total + limit - 1) // limit if total else 0

        return {
            "success": True,
            "aircraft": aircraft_list,
            "total": total,
            "page": page,
            "pages": pages,
            "sort": sort,
            "order": order,
            "search_backend": search_backend,
        }
    finally:
        session.close()


@router.get("/admin/aircraft/stats", name="api_admin_aircraft_stats")
async def api_admin_aircraft_stats() -> dict[str, Any]:
    """Aircraft list header counts. Same as ``web_app.py:3638``."""
    from src.web.helpers import SPECIAL_ATTENTION_LEVELS
    from src.web.runtime import db_manager
    from src.web.search_index import with_aircraft_index

    empty_categories = {"widebody": 0, "cargo": 0, "military": 0}

    counts = with_aircraft_index(
        lambda index: index.summary_counts(SPECIAL_ATTENTION_LEVELS), "Aircraft stats"
    )
    if counts is not None:
        return {"success": True, "stats": {**counts, **empty_categories}}

    session = db_manager.get_session()
    try:
        stats: dict[str, Any] = dict(empty_categories)
        stats["total"] = (
            session.execute(text("SELECT COUNT(*) FROM aircraft_static_info")).scalar() or 0
        )
        stats["with_images"] = (
            session.execute(
                text("SELECT COUNT(*) FROM aircraft_static_info WHERE images_downloaded = true")
            ).scalar()
            or 0
        )
        stats["special"] = (
            session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM aircraft_static_info
                    WHERE attention_level IN ('高', '极高', 'high', 'very high')
                    """
                )
            ).scalar()
            or 0
        )
        return {"success": True, "stats": stats}
    finally:
        session.close()


@router.get("/admin/aircraft/types", name="api_admin_aircraft_types")
async def api_admin_aircraft_types(search: str = Query("")) -> dict[str, Any]:
    """Distinct aircraft types (autocomplete). Same as ``web_app.py:3695``."""
    from src.web.runtime import db_manager
    from src.web.search_index import with_aircraft_index

    search = search.strip()
    narrowed = search if len(search) >= 2 else ""
    values = with_aircraft_index(
        lambda index: index.field_counts(
            "aircraft_type", contains=narrowed, limit=20 if narrowed else 200
        ),
        "Aircraft type list",
    )
    if values is not None:
        return {
            "success": True,
            "types": [
                {"code": value, "full_name": value, "count": count} for value, count in values
            ],
        }

    session = db_manager.get_session()
    try:
        if search and len(search) >= 2:
            result = session.execute(
                text(
                    """
                    SELECT aircraft_type, COUNT(*) as count
                    FROM aircraft_static_info
                    WHERE aircraft_type IS NOT NULL AND aircraft_type != ''
                      AND LOWER(aircraft_type) LIKE LOWER(:search)
                    GROUP BY aircraft_type
                    ORDER BY count DESC
                    LIMIT 20
                    """
                ),
                {"search": f"%{search}%"},
            ).fetchall()
        else:
            result = session.execute(
                text(
                    """
                    SELECT aircraft_type, COUNT(*) as count
                    FROM aircraft_static_info
                    WHERE aircraft_type IS NOT NULL AND aircraft_type != ''
                    GROUP BY aircraft_type
                    ORDER BY count DESC
                    LIMIT 200
                    """
                )
            ).fetchall()
        types = [{"code": row[0], "full_name": row[0], "count": row[1]} for row in result]
        return {"success": True, "types": types}
    finally:
        session.close()


@router.get("/admin/aircraft/liveries", name="api_admin_aircraft_liveries")
async def api_admin_aircraft_liveries(search: str = Query("")) -> dict[str, Any]:
    """Distinct liveries (autocomplete). Same as ``web_app.py:3764``."""
    from src.web.runtime import db_manager
    from src.web.search_index import with_aircraft_index

    search = search.strip()
    narrowed = search if len(search) >= 2 else ""
    values = with_aircraft_index(
        lambda index: index.field_counts(
            "livery_name", contains=narrowed, limit=20 if narrowed else 200
        ),
        "Aircraft livery list",
    )
    if values is not None:
        return {
            "success": True,
            "liveries": [{"name": value, "count": count} for value, count in values],
        }

    session = db_manager.get_session()
    try:
        if search and len(search) >= 2:
            result = session.execute(
                text(
                    """
                    SELECT livery_name, COUNT(*) as count
                    FROM aircraft_static_info
                    WHERE livery_name IS NOT NULL AND livery_name != ''
                      AND LOWER(livery_name) LIKE LOWER(:search)
                    GROUP BY livery_name
                    ORDER BY count DESC
                    LIMIT 20
                    """
                ),
                {"search": f"%{search}%"},
            ).fetchall()
        else:
            result = session.execute(
                text(
                    """
                    SELECT livery_name, COUNT(*) as count
                    FROM aircraft_static_info
                    WHERE livery_name IS NOT NULL AND livery_name != ''
                    GROUP BY livery_name
                    ORDER BY count DESC
                    LIMIT 200
                    """
                )
            ).fetchall()
        liveries = [{"name": row[0], "count": row[1]} for row in result]
        return {"success": True, "liveries": liveries}
    finally:
        session.close()


@router.get("/admin/aircraft/registrations", name="api_admin_aircraft_registrations")
async def api_admin_aircraft_registrations(search: str = Query("")) -> dict[str, Any]:
    """Registration autocomplete. Same as ``web_app.py:3830``."""
    from src.web.runtime import db_manager
    from src.web.search_index import with_aircraft_index

    search = search.strip()
    if len(search) < 2:
        return {"success": True, "registrations": []}

    suggested = with_aircraft_index(
        lambda index: index.suggest_registrations(search), "Registration autocomplete"
    )

    session = db_manager.get_session()
    try:
        if suggested is not None:
            if not suggested:
                return {"success": True, "registrations": []}
            statement = text(
                """
                SELECT registration, hex_code, aircraft_type
                FROM aircraft_static_info
                WHERE registration IN :registrations
                """
            ).bindparams(bindparam("registrations", expanding=True))
            hydrated = {
                row[0]: {"registration": row[0], "hex_code": row[1], "aircraft_type": row[2]}
                for row in session.execute(statement, {"registrations": suggested}).fetchall()
            }
            return {
                "success": True,
                "registrations": [
                    hydrated[registration] for registration in suggested if registration in hydrated
                ],
            }

        result = session.execute(
            text(
                """
                SELECT registration, hex_code, aircraft_type
                FROM aircraft_static_info
                WHERE registration IS NOT NULL AND registration != ''
                  AND (LOWER(registration) LIKE LOWER(:prefix)
                       OR LOWER(registration) LIKE LOWER(:contains)
                       OR LOWER(hex_code) LIKE LOWER(:contains))
                ORDER BY
                    CASE WHEN LOWER(registration) LIKE LOWER(:prefix) THEN 0 ELSE 1 END,
                    registration
                LIMIT 15
                """
            ),
            {"prefix": f"{search}%", "contains": f"%{search}%"},
        ).fetchall()

        registrations = [
            {"registration": row[0], "hex_code": row[1], "aircraft_type": row[2]} for row in result
        ]
        return {"success": True, "registrations": registrations}
    finally:
        session.close()


@router.get(
    "/admin/aircraft-query/{registration}",
    name="api_admin_aircraft_query",
)
async def api_admin_aircraft_query(registration: str) -> dict[str, Any]:
    """Comprehensive per-aircraft admin drill-down.

    Same as ``web_app.py:2761``. Queries 7 tables in sequence:
    aircraft_static_info, aircraft_snapshots, aircraft_realtime_positions,
    aircraft_images, flight_schedules, note_aircraft_analysis (JSONB
    search), aircraft_attention_aggregate. Each optional table is
    guarded by ``_table_exists``. ``query_times_ms`` in the response
    reports where the time went.
    """
    from src.storage import ObjectStorage
    from src.web.helpers import table_exists as _table_exists
    from src.web.image_helpers import get_image_url
    from src.web.runtime import db_manager
    from src.web.time_helpers import _to_iso

    query_times: dict[str, float] = {}
    total_start = _time.time()

    reg_upper = registration.upper().strip()
    if not reg_upper:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": "Registration is required"}
        )

    session = db_manager.get_session()
    try:
        result_data: dict[str, Any] = {
            "success": True,
            "registration": reg_upper,
            "static_info": None,
            "snapshots": {"count": 0, "recent": []},
            "realtime_positions": {"count": 0, "recent": []},
            "images": {"count": 0, "items": []},
            "flight_schedules": {"total": 0, "items": []},
            "social_mentions": {"count": 0, "items": []},
            "attention_metrics": None,
        }

        # 1. aircraft_static_info
        t0 = _time.time()
        static_result = session.execute(
            text(
                """
                SELECT id, registration, hex_code, aircraft_type, owner, operator,
                       manufacturer, model, serial_number, year_built,
                       country_of_registration, ai_analysis, images_downloaded,
                       images_updated_at, last_updated, data_source,
                       ad_status, ad_owner, ad_engines, ad_seats, ad_location, ad_delivery_date,
                       ps_status, ps_airline, ps_first_flight, ps_delivery_date,
                       jp_airline, jp_cn
                FROM aircraft_static_info
                WHERE UPPER(registration) = :reg
                """
            ),
            {"reg": reg_upper},
        ).fetchone()
        if static_result:
            result_data["static_info"] = {
                "id": static_result.id,
                "registration": static_result.registration,
                "hex_code": static_result.hex_code,
                "aircraft_type": static_result.aircraft_type,
                "owner": static_result.owner,
                "operator": static_result.operator,
                "manufacturer": static_result.manufacturer,
                "model": static_result.model,
                "serial_number": static_result.serial_number,
                "year_built": static_result.year_built,
                "country_of_registration": static_result.country_of_registration,
                "ai_analysis": static_result.ai_analysis,
                "images_downloaded": static_result.images_downloaded,
                "images_updated_at": _to_iso(static_result.images_updated_at),
                "last_updated": _to_iso(static_result.last_updated),
                "data_source": static_result.data_source,
                "ad_status": static_result.ad_status,
                "ad_owner": static_result.ad_owner,
                "ad_engines": static_result.ad_engines,
                "ad_seats": static_result.ad_seats,
                "ad_location": static_result.ad_location,
                "ad_delivery_date": static_result.ad_delivery_date,
                "ps_status": static_result.ps_status,
                "ps_airline": static_result.ps_airline,
                "ps_first_flight": _to_iso(static_result.ps_first_flight),
                "ps_delivery_date": _to_iso(static_result.ps_delivery_date),
                "jp_airline": static_result.jp_airline,
                "jp_cn": static_result.jp_cn,
            }
        query_times["static_info"] = _time.time() - t0

        # 2. aircraft_snapshots (ADS-B)
        t0 = _time.time()
        snapshots_result = session.execute(
            text(
                """
                SELECT id, snapshot_time, hex, flight_number, registration, aircraft_type,
                       latitude, longitude, altitude_baro, altitude_geom, ground_speed,
                       track, vertical_rate, squawk, emergency, is_military, is_interesting
                FROM aircraft_snapshots
                WHERE registration = :reg OR registration = :reg_orig
                ORDER BY snapshot_time DESC
                LIMIT 20
                """
            ),
            {"reg": reg_upper, "reg_orig": registration.strip()},
        )
        snapshots_list: list[dict[str, Any]] = []
        for row in snapshots_result:
            snapshots_list.append(
                {
                    "id": row.id,
                    "snapshot_time": _to_iso(row.snapshot_time),
                    "hex": row.hex,
                    "flight_number": row.flight_number,
                    "aircraft_type": row.aircraft_type,
                    "latitude": float(row.latitude) if row.latitude else None,
                    "longitude": float(row.longitude) if row.longitude else None,
                    "altitude_baro": row.altitude_baro,
                    "altitude_geom": row.altitude_geom,
                    "ground_speed": float(row.ground_speed) if row.ground_speed else None,
                    "track": float(row.track) if row.track else None,
                    "vertical_rate": row.vertical_rate,
                    "squawk": row.squawk,
                    "emergency": row.emergency,
                    "is_military": row.is_military,
                    "is_interesting": row.is_interesting,
                }
            )
        result_data["snapshots"] = {"count": len(snapshots_list), "recent": snapshots_list}
        query_times["snapshots"] = _time.time() - t0

        # 3. aircraft_realtime_positions (FR24)
        t0 = _time.time()
        if _table_exists(session, "aircraft_realtime_positions"):
            realtime_result = session.execute(
                text(
                    """
                    SELECT * FROM (
                        SELECT id, fr24_id, flight_number, callsign, registration, aircraft_type,
                               latitude, longitude, altitude, ground_speed, heading,
                               vertical_speed, squawk, origin_iata, destination_iata,
                               on_ground, fr24_timestamp, scraped_at
                        FROM aircraft_realtime_positions
                        WHERE registration = :reg
                        LIMIT 1000
                    ) sub
                    ORDER BY scraped_at DESC
                    LIMIT 20
                    """
                ),
                {"reg": reg_upper},
            )
            realtime_list: list[dict[str, Any]] = []
            for row in realtime_result:
                realtime_list.append(
                    {
                        "id": row.id,
                        "callsign": row.callsign,
                        "aircraft_type": row.aircraft_type,
                        "latitude": float(row.latitude) if row.latitude else None,
                        "longitude": float(row.longitude) if row.longitude else None,
                        "altitude": row.altitude,
                        "ground_speed": row.ground_speed,
                        "heading": row.heading,
                        "vertical_speed": row.vertical_speed,
                        "origin_iata": row.origin_iata,
                        "destination_iata": row.destination_iata,
                        "flight_number": row.flight_number,
                        "fr24_id": row.fr24_id,
                        "on_ground": row.on_ground,
                        "fr24_timestamp": _to_iso(row.fr24_timestamp),
                        "scraped_at": _to_iso(row.scraped_at),
                    }
                )
            result_data["realtime_positions"] = {
                "count": len(realtime_list),
                "recent": realtime_list,
            }
        query_times["realtime_positions"] = _time.time() - t0

        # 4. aircraft_images
        t0 = _time.time()
        images_result = session.execute(
            text(
                """
                SELECT id, registration, image_path, source_url, source,
                       photographer, photo_date, upload_date, location,
                       airport_icao, airport_name, notes, display_order,
                       is_primary, width, height, jetphotos_id, created_at
                FROM aircraft_images
                WHERE UPPER(registration) = :reg
                  AND image_path IS NOT NULL
                  AND image_path != ''
                ORDER BY display_order ASC, created_at DESC
                LIMIT 20
                """
            ),
            {"reg": reg_upper},
        )
        images_list: list[dict[str, Any]] = []
        for row in images_result:
            image_url = get_image_url(row.image_path)
            if not image_url:
                continue
            images_list.append(
                {
                    "id": row.id,
                    "registration": row.registration,
                    "image_url": image_url,
                    "source_url": row.source_url,
                    "source": row.source,
                    "photographer": row.photographer,
                    "photo_date": _to_iso(row.photo_date),
                    "upload_date": _to_iso(row.upload_date),
                    "location": row.location,
                    "airport_icao": row.airport_icao,
                    "airport_name": row.airport_name,
                    "notes": row.notes,
                    "display_order": row.display_order,
                    "is_primary": row.is_primary,
                    "width": row.width,
                    "height": row.height,
                    "jetphotos_id": row.jetphotos_id,
                }
            )
        result_data["images"] = {"count": len(images_list), "items": images_list}
        query_times["images"] = _time.time() - t0

        # 5. flight_schedules
        t0 = _time.time()
        schedules_result = session.execute(
            text(
                """
                SELECT id, flight_type, airport_iata, airport_icao, flight_number,
                       callsign, fr24_flight_id, airline_name, airline_iata,
                       remote_airport_iata, remote_airport_name, aircraft_type,
                       aircraft_registration, scheduled_time, estimated_time,
                       actual_time, status, terminal, gate, scraped_at
                FROM flight_schedules
                WHERE aircraft_registration = :reg OR aircraft_registration = :reg_orig
                ORDER BY scheduled_time DESC
                LIMIT 30
                """
            ),
            {"reg": reg_upper, "reg_orig": registration.strip()},
        )
        schedules_list: list[dict[str, Any]] = []
        for row in schedules_result:
            schedules_list.append(
                {
                    "id": row.id,
                    "flight_type": row.flight_type,
                    "airport_iata": row.airport_iata,
                    "airport_icao": row.airport_icao,
                    "flight_number": row.flight_number,
                    "callsign": row.callsign,
                    "fr24_flight_id": row.fr24_flight_id,
                    "airline_name": row.airline_name,
                    "airline_iata": row.airline_iata,
                    "remote_airport_iata": row.remote_airport_iata,
                    "remote_airport_name": row.remote_airport_name,
                    "aircraft_type": row.aircraft_type,
                    "scheduled_time": _to_iso(row.scheduled_time),
                    "estimated_time": _to_iso(row.estimated_time),
                    "actual_time": _to_iso(row.actual_time),
                    "status": row.status,
                    "terminal": row.terminal,
                    "gate": row.gate,
                    "scraped_at": _to_iso(row.scraped_at),
                }
            )
        result_data["flight_schedules"] = {
            "total": len(schedules_list),
            "items": schedules_list,
        }
        query_times["flight_schedules"] = _time.time() - t0

        # 6. note_aircraft_analysis (social mentions via JSONB)
        t0 = _time.time()
        if _table_exists(session, "note_aircraft_analysis"):
            mentions_result = session.execute(
                text(
                    """
                    SELECT naa.id, naa.note_id, naa.source_type, naa.registrations,
                           naa.attention_index, naa.attention_level, naa.attention_reason,
                           naa.content_type, naa.sentiment, naa.topics, naa.analyzed_at,
                           xn.title, xn.author_name, xn.author_id, xn.content,
                           xn.like_count, xn.collect_count, xn.comment_count, xn.share_count,
                           xn.location, xn.tags,
                           xn.image_urls as original_image_urls,
                           xn.image_paths,
                           xn.scraped_at as note_scraped_at
                    FROM note_aircraft_analysis naa
                    LEFT JOIN xiaohongshu_notes xn ON naa.note_id = xn.note_id
                    -- Postgres stores registrations as JSONB; SQLite as TEXT.
                    -- LIKE over the serialized JSON works for both as long as
                    -- the reg is quoted (which it is in the JSON array).
                    WHERE CAST(naa.registrations AS TEXT) LIKE :reg_pattern
                    ORDER BY naa.analyzed_at DESC
                    LIMIT 20
                    """
                ),
                {"reg_pattern": f'%"{reg_upper}"%'},
            )
            mentions_list: list[dict[str, Any]] = []
            for row in mentions_result:
                image_urls: list[str] = []
                if row.image_paths:
                    paths = row.image_paths if isinstance(row.image_paths, list) else []
                    for p in paths[:5]:
                        if not p:
                            continue
                        image_urls.append(get_image_url(ObjectStorage.strip_public_prefix(p)))
                if not image_urls and row.original_image_urls:
                    urls = (
                        row.original_image_urls if isinstance(row.original_image_urls, list) else []
                    )
                    image_urls = [url for url in urls if url][:5]
                mentions_list.append(
                    {
                        "id": row.id,
                        "note_id": row.note_id,
                        "source_type": row.source_type,
                        "registrations": row.registrations,
                        "attention_index": row.attention_index,
                        "attention_level": row.attention_level,
                        "attention_reason": row.attention_reason,
                        "content_type": row.content_type,
                        "sentiment": row.sentiment,
                        "topics": row.topics,
                        "analyzed_at": _to_iso(row.analyzed_at),
                        "title": row.title,
                        "author_name": row.author_name,
                        "author_id": row.author_id,
                        "content": row.content,
                        "like_count": row.like_count,
                        "collect_count": row.collect_count,
                        "comment_count": row.comment_count,
                        "share_count": row.share_count,
                        "location": row.location,
                        "tags": row.tags,
                        "image_urls": image_urls,
                        "note_scraped_at": _to_iso(row.note_scraped_at),
                    }
                )
            result_data["social_mentions"] = {
                "count": len(mentions_list),
                "items": mentions_list,
            }
        query_times["social_mentions"] = _time.time() - t0

        # 7. aircraft_attention_aggregate
        t0 = _time.time()
        if _table_exists(session, "aircraft_attention_aggregate"):
            attention_result = session.execute(
                text(
                    """
                    SELECT registration, total_mentions, avg_attention_index,
                           max_attention_index, mentions_7d, mentions_30d,
                           first_seen, last_seen, top_topics, sentiment_distribution,
                           source_distribution, content_type_distribution,
                           trending_score, updated_at
                    FROM aircraft_attention_aggregate
                    WHERE UPPER(registration) = :reg
                    """
                ),
                {"reg": reg_upper},
            ).fetchone()
            if attention_result:
                result_data["attention_metrics"] = {
                    "registration": attention_result.registration,
                    "total_mentions": attention_result.total_mentions,
                    "avg_attention_index": (
                        float(attention_result.avg_attention_index)
                        if attention_result.avg_attention_index
                        else None
                    ),
                    "max_attention_index": attention_result.max_attention_index,
                    "mentions_7d": attention_result.mentions_7d,
                    "mentions_30d": attention_result.mentions_30d,
                    "first_seen": _to_iso(attention_result.first_seen),
                    "last_seen": _to_iso(attention_result.last_seen),
                    "top_topics": attention_result.top_topics,
                    "sentiment_distribution": attention_result.sentiment_distribution,
                    "source_distribution": attention_result.source_distribution,
                    "content_type_distribution": attention_result.content_type_distribution,
                    "trending_score": (
                        float(attention_result.trending_score)
                        if attention_result.trending_score
                        else None
                    ),
                    "updated_at": _to_iso(attention_result.updated_at),
                }
        query_times["attention_metrics"] = _time.time() - t0

        query_times["total"] = _time.time() - total_start
        result_data["query_times_ms"] = {k: round(v * 1000, 2) for k, v in query_times.items()}

        return result_data
    finally:
        session.close()
