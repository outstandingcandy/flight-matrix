"""FastAPI port of ``/api/admin/reports*`` and ``/api/admin/scraped-data/*``.

Stage 0 lift-and-shift. Ten handlers grouped in one router file
because their scope is small and the code they share (Depends(require_admin),
session lifecycle, _to_iso / convert_utc_to_beijing / get_image_url
delegation) doesn't need to be split.

Reports (3 handlers) — cover both single- and multi-user cooldown
tables and are driven by ``config.is_multi_user_enabled()``:

- GET /api/admin/reports
- GET /api/admin/reports/stats
- GET /api/admin/reports/{aircraft_hex}/detail

Scraped-data (7 handlers) — three data sources (xhs / fr24 /
jetphotos), each with a ``/stats`` overview and a filtered list:

- GET /api/admin/scraped-data/xiaohongshu/stats
- GET /api/admin/scraped-data/xiaohongshu/notes
- GET /api/admin/scraped-data/xiaohongshu/notes/{note_id}
- GET /api/admin/scraped-data/fr24/stats
- GET /api/admin/scraped-data/fr24/flights
- GET /api/admin/scraped-data/jetphotos/stats
- GET /api/admin/scraped-data/jetphotos/images
"""

from __future__ import annotations

import json as _json_mod
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from src.auth.dependencies import require_admin

logger = logging.getLogger("web.admin.reports_scraped")

router = APIRouter(tags=["admin-reports-scraped"], dependencies=[Depends(require_admin)])


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@router.get("/api/admin/reports", name="api_admin_list_reports")
async def api_admin_list_reports(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=500),
    search: str = Query(""),
    user_id: str | None = Query(None, description="Multi-user mode: filter by user id"),
) -> dict[str, Any]:
    """Report history joined with latest snapshot data. Same as
    ``web_app.py:3907``. Backend table depends on
    ``config.is_multi_user_enabled()`` — ``user_cooldowns`` when on,
    ``report_cooldowns`` otherwise."""
    from src.data.dialect import latest_rows
    from web_app import (
        _to_iso,
        config,
        convert_utc_to_beijing,
        db_manager,
        get_image_url,
    )

    search = search.strip()
    offset = (page - 1) * limit
    multi_user_enabled = config.is_multi_user_enabled() if config else False

    session = db_manager.get_session()
    try:
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        latest_snapshot = latest_rows(
            columns=(
                "hex, registration, aircraft_type, flight_number, is_military, current_country"
            ),
            source="aircraft_snapshots",
            partition_by="hex",
            order_by="snapshot_time DESC",
            is_postgres=db_manager.is_postgres,
        )

        if multi_user_enabled:
            base_query = f"""
                SELECT
                    uc.id,
                    uc.aircraft_hex,
                    uc.last_report_time,
                    uc.last_latitude,
                    uc.last_longitude,
                    uc.report_count,
                    uc.last_report_time as updated_at,
                    s.registration,
                    s.aircraft_type,
                    s.flight_number,
                    s.is_military,
                    s.current_country,
                    (SELECT ai.image_path FROM aircraft_images ai
                     WHERE ai.registration = s.registration
                     ORDER BY ai.display_order LIMIT 1) as image_path,
                    uc.user_id,
                    u.email as user_email
                FROM user_cooldowns uc
                LEFT JOIN users u ON uc.user_id = u.id
                LEFT JOIN (
                    {latest_snapshot}
                ) s ON uc.aircraft_hex = s.hex
            """

            where_clauses: list[str] = []
            if user_id:
                where_clauses.append("uc.user_id = :user_id")
                params["user_id"] = int(user_id)
            if search:
                where_clauses.append(
                    "(LOWER(uc.aircraft_hex) LIKE LOWER(:search)"
                    " OR LOWER(s.registration) LIKE LOWER(:search)"
                    " OR LOWER(s.flight_number) LIKE LOWER(:search)"
                    " OR LOWER(u.email) LIKE LOWER(:search))"
                )
                params["search"] = f"%{search}%"

            if where_clauses:
                base_query += " WHERE " + " AND ".join(where_clauses)
            base_query += """
                ORDER BY uc.last_report_time DESC
                LIMIT :limit OFFSET :offset
            """

            result = session.execute(text(base_query), params).fetchall()
            reports = [
                {
                    "id": row[0],
                    "aircraft_hex": row[1],
                    "last_report_time": _to_iso(row[2]),
                    "last_report_time_beijing": (
                        convert_utc_to_beijing(_to_iso(row[2])) if row[2] else None
                    ),
                    "last_latitude": float(row[3]) if row[3] else None,
                    "last_longitude": float(row[4]) if row[4] else None,
                    "report_count": row[5],
                    "updated_at": _to_iso(row[6]),
                    "registration": row[7],
                    "aircraft_type": row[8],
                    "flight_number": row[9],
                    "is_military": row[10],
                    "current_country": row[11],
                    "image_url": get_image_url(row[12]) if row[12] else None,
                    "user_id": row[13],
                    "user_email": row[14],
                }
                for row in result
            ]

            count_params: dict[str, Any] = {}
            count_where: list[str] = []
            if user_id:
                count_where.append("uc.user_id = :user_id")
                count_params["user_id"] = int(user_id)

            if search:
                count_query = """
                    SELECT COUNT(DISTINCT uc.id) FROM user_cooldowns uc
                    LEFT JOIN users u ON uc.user_id = u.id
                    LEFT JOIN aircraft_snapshots s ON uc.aircraft_hex = s.hex
                """
                count_where.append(
                    "(LOWER(uc.aircraft_hex) LIKE LOWER(:search)"
                    " OR LOWER(s.registration) LIKE LOWER(:search)"
                    " OR LOWER(u.email) LIKE LOWER(:search))"
                )
                count_params["search"] = f"%{search}%"
            else:
                count_query = "SELECT COUNT(*) FROM user_cooldowns uc"

            if count_where:
                count_query += " WHERE " + " AND ".join(count_where)
            total = session.execute(text(count_query), count_params).scalar() or 0
        else:
            base_query = f"""
                SELECT
                    rc.id, rc.aircraft_hex, rc.last_report_time,
                    rc.last_latitude, rc.last_longitude, rc.report_count,
                    rc.updated_at,
                    s.registration, s.aircraft_type, s.flight_number,
                    s.is_military, s.current_country,
                    (SELECT ai.image_path FROM aircraft_images ai
                     WHERE ai.registration = s.registration
                     ORDER BY ai.display_order LIMIT 1) as image_path
                FROM report_cooldowns rc
                LEFT JOIN (
                    {latest_snapshot}
                ) s ON rc.aircraft_hex = s.hex
            """
            if search:
                base_query += """
                    WHERE LOWER(rc.aircraft_hex) LIKE LOWER(:search)
                    OR LOWER(s.registration) LIKE LOWER(:search)
                    OR LOWER(s.flight_number) LIKE LOWER(:search)
                """
                params["search"] = f"%{search}%"

            base_query += """
                ORDER BY rc.last_report_time DESC
                LIMIT :limit OFFSET :offset
            """
            result = session.execute(text(base_query), params).fetchall()
            reports = [
                {
                    "id": row[0],
                    "aircraft_hex": row[1],
                    "last_report_time": _to_iso(row[2]),
                    "last_report_time_beijing": (
                        convert_utc_to_beijing(_to_iso(row[2])) if row[2] else None
                    ),
                    "last_latitude": float(row[3]) if row[3] else None,
                    "last_longitude": float(row[4]) if row[4] else None,
                    "report_count": row[5],
                    "updated_at": _to_iso(row[6]),
                    "registration": row[7],
                    "aircraft_type": row[8],
                    "flight_number": row[9],
                    "is_military": row[10],
                    "current_country": row[11],
                    "image_url": get_image_url(row[12]) if row[12] else None,
                }
                for row in result
            ]

            count_query = "SELECT COUNT(*) FROM report_cooldowns"
            if search:
                count_query = """
                    SELECT COUNT(*) FROM report_cooldowns rc
                    LEFT JOIN aircraft_snapshots s ON rc.aircraft_hex = s.hex
                    WHERE LOWER(rc.aircraft_hex) LIKE LOWER(:search)
                    OR LOWER(s.registration) LIKE LOWER(:search)
                """
            total = (
                session.execute(
                    text(count_query), {"search": f"%{search}%"} if search else {}
                ).scalar()
                or 0
            )

        pages = (total + limit - 1) // limit

        return {
            "success": True,
            "reports": reports,
            "total": total,
            "page": page,
            "pages": pages,
            "multi_user_mode": multi_user_enabled,
        }
    finally:
        session.close()


@router.get("/api/admin/reports/stats", name="api_admin_report_stats")
async def api_admin_report_stats() -> dict[str, Any]:
    """Report table rollup. Same as ``web_app.py:4141``.

    Which cooldown table it reads is driven by
    ``config.is_multi_user_enabled()``."""
    from src.data.dialect import day_of
    from web_app import config, db_manager

    multi_user_enabled = config.is_multi_user_enabled() if config else False
    table_name = "user_cooldowns" if multi_user_enabled else "report_cooldowns"

    session = db_manager.get_session()
    try:
        total = session.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0

        is_postgres = db_manager.is_postgres
        today_query = f"""
            SELECT COUNT(*) FROM {table_name}
            WHERE {day_of("last_report_time", is_postgres=is_postgres)}
                  = {day_of("CURRENT_TIMESTAMP", is_postgres=is_postgres)}
        """
        today = session.execute(text(today_query)).scalar() or 0

        total_sent = (
            session.execute(
                text(f"SELECT COALESCE(SUM(report_count), 0) FROM {table_name}")
            ).scalar()
            or 0
        )
        unique_aircraft = (
            session.execute(text(f"SELECT COUNT(DISTINCT aircraft_hex) FROM {table_name}")).scalar()
            or 0
        )

        return {
            "success": True,
            "stats": {
                "total_tracked": total,
                "reports_today": today,
                "total_sent": total_sent,
                "unique_aircraft": unique_aircraft,
            },
            "multi_user_mode": multi_user_enabled,
        }
    finally:
        session.close()


@router.get(
    "/api/admin/reports/{aircraft_hex}/detail",
    name="api_admin_report_detail",
)
async def api_admin_report_detail(
    aircraft_hex: str,
    user_id: str | None = Query(None, description="Multi-user mode: narrow to one user"),
) -> dict[str, Any]:
    """Per-aircraft cooldown detail + last 10 snapshots. Same as
    ``web_app.py:4205``. 404 when the aircraft has no cooldown row."""
    from web_app import _to_iso, config, convert_utc_to_beijing, db_manager, get_image_url

    multi_user_enabled = config.is_multi_user_enabled() if config else False

    session = db_manager.get_session()
    try:
        if multi_user_enabled:
            if user_id:
                cooldown = session.execute(
                    text(
                        """
                        SELECT uc.id, uc.aircraft_hex, uc.last_report_time,
                               uc.last_latitude, uc.last_longitude,
                               uc.report_count, uc.last_report_time as updated_at,
                               uc.user_id, u.email
                        FROM user_cooldowns uc
                        LEFT JOIN users u ON uc.user_id = u.id
                        WHERE uc.aircraft_hex = :hex AND uc.user_id = :user_id
                        """
                    ),
                    {"hex": aircraft_hex, "user_id": int(user_id)},
                ).fetchone()
            else:
                cooldown = session.execute(
                    text(
                        """
                        SELECT uc.id, uc.aircraft_hex, uc.last_report_time,
                               uc.last_latitude, uc.last_longitude,
                               uc.report_count, uc.last_report_time as updated_at,
                               uc.user_id, u.email
                        FROM user_cooldowns uc
                        LEFT JOIN users u ON uc.user_id = u.id
                        WHERE uc.aircraft_hex = :hex
                        ORDER BY uc.last_report_time DESC
                        LIMIT 1
                        """
                    ),
                    {"hex": aircraft_hex},
                ).fetchone()
        else:
            cooldown = session.execute(
                text(
                    """
                    SELECT id, aircraft_hex, last_report_time, last_latitude,
                           last_longitude, report_count, updated_at
                    FROM report_cooldowns
                    WHERE aircraft_hex = :hex
                    """
                ),
                {"hex": aircraft_hex},
            ).fetchone()

        if not cooldown:
            raise HTTPException(
                status_code=404, detail={"success": False, "error": "Report not found"}
            )

        snapshots = session.execute(
            text(
                """
                SELECT id, snapshot_time, hex, registration, aircraft_type, flight_number,
                       latitude, longitude, altitude_baro, ground_speed, track,
                       is_military, current_country
                FROM aircraft_snapshots
                WHERE hex = :hex
                ORDER BY snapshot_time DESC
                LIMIT 10
                """
            ),
            {"hex": aircraft_hex},
        ).fetchall()

        registration = snapshots[0][3] if snapshots and snapshots[0][3] else None
        images: list[str] = []
        if registration:
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
            images = [row[0] for row in images_result if row[0]]

        cooldown_data: dict[str, Any] = {
            "id": cooldown[0],
            "aircraft_hex": cooldown[1],
            "last_report_time": _to_iso(cooldown[2]),
            "last_report_time_beijing": convert_utc_to_beijing(_to_iso(cooldown[2])),
            "last_latitude": float(cooldown[3]) if cooldown[3] else None,
            "last_longitude": float(cooldown[4]) if cooldown[4] else None,
            "report_count": cooldown[5],
            "updated_at": _to_iso(cooldown[6]),
        }
        if multi_user_enabled and len(cooldown) > 7:
            cooldown_data["user_id"] = cooldown[7]
            cooldown_data["user_email"] = cooldown[8]

        report_detail = {
            "cooldown": cooldown_data,
            "recent_snapshots": [
                {
                    "id": s[0],
                    "snapshot_time": _to_iso(s[1]),
                    "snapshot_time_beijing": convert_utc_to_beijing(_to_iso(s[1])),
                    "hex": s[2],
                    "registration": s[3],
                    "aircraft_type": s[4],
                    "flight_number": s[5],
                    "latitude": float(s[6]) if s[6] else None,
                    "longitude": float(s[7]) if s[7] else None,
                    "altitude": s[8],
                    "ground_speed": s[9],
                    "track": s[10],
                    "is_military": s[11],
                    "current_country": s[12],
                    "image_url_1": get_image_url(images[0]) if len(images) > 0 else None,
                    "image_url_2": get_image_url(images[1]) if len(images) > 1 else None,
                    "image_url_3": get_image_url(images[2]) if len(images) > 2 else None,
                }
                for s in snapshots
            ],
            "multi_user_mode": multi_user_enabled,
        }

        return {"success": True, "detail": report_detail}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Scraped-data / Xiaohongshu
# ---------------------------------------------------------------------------


def _count_json_list(raw: Any) -> int:
    """Length of a JSON-array-shaped value that Postgres returns as
    ``list`` (JSONB) and SQLite returns as ``str``. Non-array yields 0."""
    if raw is None:
        return 0
    if isinstance(raw, list):
        return len(raw)
    if isinstance(raw, str):
        try:
            parsed = _json_mod.loads(raw)
        except (ValueError, TypeError):
            return 0
        return len(parsed) if isinstance(parsed, list) else 0
    return 0


@router.get("/api/admin/scraped-data/xiaohongshu/stats", name="api_admin_xhs_stats")
async def api_admin_xhs_stats() -> dict[str, Any]:
    """XHS scraped-data rollup. Same as ``web_app.py:4352``."""
    from web_app import _table_exists, db_manager

    session = db_manager.get_session()
    try:
        if not _table_exists(session, "xiaohongshu_notes"):
            return {
                "success": True,
                "notes_count": 0,
                "authors_count": 0,
                "images_count": 0,
                "latest_scrape": None,
            }

        notes_count = session.execute(text("SELECT COUNT(*) FROM xiaohongshu_notes")).scalar() or 0

        authors_count = 0
        if _table_exists(session, "xiaohongshu_authors"):
            authors_count = (
                session.execute(text("SELECT COUNT(*) FROM xiaohongshu_authors")).scalar() or 0
            )

        # Image count: sum of image_paths lengths in Python. Portable
        # across Postgres (JSONB) and SQLite (TEXT).
        image_paths_rows = session.execute(
            text("SELECT image_paths FROM xiaohongshu_notes")
        ).fetchall()
        images_count = sum(_count_json_list(raw) for (raw,) in image_paths_rows)

        latest = session.execute(text("SELECT MAX(scraped_at) FROM xiaohongshu_notes")).scalar()

        return {
            "success": True,
            "notes_count": notes_count,
            "authors_count": authors_count,
            "images_count": images_count,
            "latest_scrape": latest.strftime("%m-%d %H:%M") if latest else None,
        }
    finally:
        session.close()


@router.get("/api/admin/scraped-data/xiaohongshu/notes", name="api_admin_xhs_notes")
async def api_admin_xhs_notes(
    author: str = Query(""),
    title: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """XHS notes list. Same as ``web_app.py:4427``."""
    from web_app import _table_exists, _to_iso, db_manager

    session = db_manager.get_session()
    try:
        if not _table_exists(session, "xiaohongshu_notes"):
            return {"success": True, "notes": []}

        where_clause = " WHERE 1=1"
        filter_params: dict[str, Any] = {}

        if author:
            where_clause += " AND LOWER(author_name) LIKE LOWER(:author)"
            filter_params["author"] = f"%{author}%"
        if title:
            where_clause += " AND LOWER(title) LIKE LOWER(:title)"
            filter_params["title"] = f"%{title}%"

        total = (
            session.execute(
                text(f"SELECT COUNT(*) FROM xiaohongshu_notes{where_clause}"),
                filter_params,
            ).scalar()
            or 0
        )

        query = f"""
            SELECT note_id, source_url, title, author_name, author_id,
                   image_paths,
                   like_count, collect_count, comment_count, share_count,
                   location, scraped_at, updated_at, content, tags
            FROM xiaohongshu_notes{where_clause}
            ORDER BY COALESCE(updated_at, scraped_at) DESC
            LIMIT :limit OFFSET :offset
        """
        params = {**filter_params, "limit": limit, "offset": offset}

        result = session.execute(text(query), params)
        notes: list[dict[str, Any]] = []
        for row in result:
            notes.append(
                {
                    "note_id": row.note_id,
                    "source_url": row.source_url,
                    "title": row.title,
                    "author_name": row.author_name,
                    "author_id": row.author_id,
                    "image_count": _count_json_list(row.image_paths),
                    "like_count": row.like_count,
                    "collect_count": row.collect_count,
                    "comment_count": row.comment_count,
                    "share_count": row.share_count,
                    "location": row.location,
                    "scraped_at": _to_iso(row.scraped_at),
                    "updated_at": _to_iso(row.updated_at),
                    "content": (
                        row.content[:200] + "..."
                        if row.content and len(row.content) > 200
                        else row.content
                    ),
                    "tags": row.tags,
                }
            )

        return {
            "success": True,
            "notes": notes,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        session.close()


@router.get(
    "/api/admin/scraped-data/xiaohongshu/notes/{note_id}",
    name="api_admin_xhs_note_detail",
)
async def api_admin_xhs_note_detail(note_id: str) -> dict[str, Any]:
    """Full XHS note detail. Same as ``web_app.py:4524``. 404 when the
    note doesn't exist or the table isn't there."""
    from src.storage import ObjectStorage
    from web_app import _table_exists, _to_iso, db_manager, get_image_url

    session = db_manager.get_session()
    try:
        if not _table_exists(session, "xiaohongshu_notes"):
            raise HTTPException(
                status_code=404, detail={"success": False, "error": "Note not found"}
            )

        row = session.execute(
            text(
                """
                SELECT note_id, source_url, title, content, tags, location,
                       author_id, author_name, image_urls, image_paths,
                       video_url, like_count, collect_count, comment_count, share_count,
                       comments, note_created_at, scraped_at
                FROM xiaohongshu_notes
                WHERE note_id = :note_id
                """
            ),
            {"note_id": note_id},
        ).fetchone()

        if not row:
            raise HTTPException(
                status_code=404, detail={"success": False, "error": "Note not found"}
            )

        display_images: list[str] = []
        if row.image_paths:
            paths = row.image_paths if isinstance(row.image_paths, list) else []
            for p in paths:
                if isinstance(p, str):
                    normalized = os.path.normpath(ObjectStorage.strip_public_prefix(p))
                    url = get_image_url(normalized)
                    if url:
                        display_images.append(url)

        note = {
            "note_id": row.note_id,
            "source_url": row.source_url,
            "title": row.title,
            "content": row.content,
            "tags": row.tags,
            "location": row.location,
            "author_id": row.author_id,
            "author_name": row.author_name,
            "image_urls": display_images,
            "video_url": row.video_url,
            "like_count": row.like_count,
            "collect_count": row.collect_count,
            "comment_count": row.comment_count,
            "share_count": row.share_count,
            "comments": row.comments,
            "note_created_at": _to_iso(row.note_created_at),
            "scraped_at": _to_iso(row.scraped_at),
        }

        return {"success": True, "note": note}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Scraped-data / FR24
# ---------------------------------------------------------------------------


@router.get("/api/admin/scraped-data/fr24/stats", name="api_admin_fr24_stats")
async def api_admin_fr24_stats() -> dict[str, Any]:
    """FR24 flight-schedules rollup. Same as ``web_app.py:4591``."""
    from web_app import db_manager

    session = db_manager.get_session()
    try:
        flights_count = session.execute(text("SELECT COUNT(*) FROM flight_schedules")).scalar() or 0

        airports_count = (
            session.execute(
                text(
                    "SELECT COUNT(DISTINCT airport_iata) FROM flight_schedules"
                    " WHERE airport_iata IS NOT NULL"
                )
            ).scalar()
            or 0
        )
        today_count = (
            session.execute(
                text(
                    "SELECT COUNT(*) FROM flight_schedules"
                    " WHERE DATE(scheduled_time) = CURRENT_DATE"
                )
            ).scalar()
            or 0
        )
        latest = session.execute(text("SELECT MAX(scraped_at) FROM flight_schedules")).scalar()

        return {
            "success": True,
            "flights_count": flights_count,
            "airports_count": airports_count,
            "today_count": today_count,
            "latest_scrape": latest.strftime("%m-%d %H:%M") if latest else None,
        }
    finally:
        session.close()


@router.get("/api/admin/scraped-data/fr24/flights", name="api_admin_fr24_flights")
async def api_admin_fr24_flights(
    airport: str = Query(""),
    registration: str = Query(""),
    flight_type: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """FR24 flights list. Same as ``web_app.py:4640``."""
    from web_app import _to_iso, db_manager

    session = db_manager.get_session()
    try:
        query = """
            SELECT fr24_flight_id, flight_type, airport_iata, airport_icao,
                   flight_number, callsign, airline_name,
                   remote_airport_iata, remote_airport_name,
                   aircraft_type, aircraft_registration,
                   scheduled_time, estimated_time, actual_time, status,
                   terminal, gate, scraped_at
            FROM flight_schedules
            WHERE 1=1
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if airport:
            query += (
                " AND (LOWER(airport_iata) LIKE LOWER(:airport)"
                " OR LOWER(airport_icao) LIKE LOWER(:airport))"
            )
            params["airport"] = f"{airport}%"
        if registration:
            query += " AND LOWER(aircraft_registration) LIKE LOWER(:registration)"
            params["registration"] = f"%{registration}%"
        if flight_type:
            query += " AND flight_type = :flight_type"
            params["flight_type"] = flight_type

        query += " ORDER BY scraped_at DESC LIMIT :limit OFFSET :offset"

        result = session.execute(text(query), params)
        flights = [
            {
                "fr24_flight_id": row.fr24_flight_id,
                "flight_type": row.flight_type,
                "airport_iata": row.airport_iata,
                "airport_icao": row.airport_icao,
                "flight_number": row.flight_number,
                "callsign": row.callsign,
                "airline_name": row.airline_name,
                "remote_airport_iata": row.remote_airport_iata,
                "remote_airport_name": row.remote_airport_name,
                "aircraft_type": row.aircraft_type,
                "aircraft_registration": row.aircraft_registration,
                "scheduled_time": _to_iso(row.scheduled_time),
                "status": row.status,
                "scraped_at": _to_iso(row.scraped_at),
            }
            for row in result
        ]

        return {"success": True, "flights": flights}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Scraped-data / JetPhotos
# ---------------------------------------------------------------------------


@router.get("/api/admin/scraped-data/jetphotos/stats", name="api_admin_jetphotos_stats")
async def api_admin_jetphotos_stats() -> dict[str, Any]:
    """JetPhotos scraped-data rollup. Same as ``web_app.py:4708``."""
    from web_app import db_manager

    session = db_manager.get_session()
    try:
        images_count = (
            session.execute(
                text("SELECT COUNT(*) FROM aircraft_images WHERE source = 'jetphotos'")
            ).scalar()
            or 0
        )
        aircraft_count = (
            session.execute(
                text(
                    "SELECT COUNT(DISTINCT registration) FROM aircraft_images"
                    " WHERE source = 'jetphotos'"
                )
            ).scalar()
            or 0
        )
        photographers_count = (
            session.execute(
                text(
                    "SELECT COUNT(DISTINCT photographer) FROM aircraft_images"
                    " WHERE source = 'jetphotos' AND photographer IS NOT NULL"
                )
            ).scalar()
            or 0
        )
        latest = session.execute(
            text("SELECT MAX(created_at) FROM aircraft_images WHERE source = 'jetphotos'")
        ).scalar()

        return {
            "success": True,
            "images_count": images_count,
            "aircraft_count": aircraft_count,
            "photographers_count": photographers_count,
            "latest_scrape": latest.strftime("%m-%d %H:%M") if latest else None,
        }
    finally:
        session.close()


@router.get("/api/admin/scraped-data/jetphotos/images", name="api_admin_jetphotos_images")
async def api_admin_jetphotos_images(
    registration: str = Query(""),
    photographer: str = Query(""),
    airport: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """JetPhotos images list. Same as ``web_app.py:4766``."""
    from web_app import _to_iso, db_manager, get_image_url

    session = db_manager.get_session()
    try:
        query = """
            SELECT id, registration, image_path, source_url, jetphotos_id,
                   photographer, photo_date, location, airport_icao,
                   width, height, file_size_bytes, notes, created_at
            FROM aircraft_images
            WHERE source = 'jetphotos'
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}

        if registration:
            query += " AND LOWER(registration) LIKE LOWER(:registration)"
            params["registration"] = f"%{registration}%"
        if photographer:
            query += " AND LOWER(photographer) LIKE LOWER(:photographer)"
            params["photographer"] = f"%{photographer}%"
        if airport:
            query += " AND LOWER(airport_icao) LIKE LOWER(:airport)"
            params["airport"] = f"{airport}%"

        query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

        result = session.execute(text(query), params)
        images = [
            {
                "id": row.id,
                "registration": row.registration,
                "image_url": get_image_url(row.image_path),
                "source_url": row.source_url,
                "jetphotos_id": row.jetphotos_id,
                "photographer": row.photographer,
                "photo_date": _to_iso(row.photo_date),
                "location": row.location,
                "airport_icao": row.airport_icao,
                "width": row.width,
                "height": row.height,
                "file_size_bytes": row.file_size_bytes,
                "notes": row.notes,
                "created_at": _to_iso(row.created_at),
            }
            for row in result
        ]

        return {"success": True, "images": images}
    finally:
        session.close()
