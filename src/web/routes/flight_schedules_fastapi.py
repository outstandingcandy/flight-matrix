"""FastAPI port of the ``/api/flight-schedules*`` routes.

Stage 0 lift-and-shift. The main endpoint here (``/api/flight-schedules``)
is the single heaviest handler in the app: it does the airport-board
query that the frontend calls every 30 seconds, with a three-tier photo
fallback (this airframe at this airport → this airframe anywhere → the
same-type-at-this-airport fallback the frontend fetches separately),
livery / has_livery filters applied *after* dedup for speed, and a
second window-function pass to keep the arrival / departure tab counts
honest when a ``flight_type`` filter is active.

None of that logic changes here. The only differences from
``web_app.py`` are the framework primitives — typed query params, dict
return, ``HTTPException`` for the 400 branch. SQL, window functions,
``latest_rows``, ``day_of``, ``beijing_date`` dialect helpers, and the
``HAS_LIVERY_SQL`` constant are all reused verbatim.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

logger = logging.getLogger("web.flight_schedules")

router = APIRouter(tags=["flight-schedules"])


@router.get("/api/flight-schedules", name="get_flight_schedules")
async def get_flight_schedules(
    airport: str = Query("", description="Airport IATA (3) or ICAO (4) — required"),
    flight_type: str = Query("", description="'arrival' | 'departure' | ''"),
    aircraft_type: str = Query("", description="Substring match on aircraft_type"),
    livery: str = Query("", description="Exact livery_type"),
    has_livery: str = Query("", description="'true' filters to rows with a livery"),
    date: str = Query("", description="YYYY-MM-DD (Beijing) or 'recent' / empty"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Airport arrivals/departures board with a three-tier photo fallback.

    Direct port of ``web_app.py:5187``. Long — but the body is the same
    query the Flask version ran; the header is where all the migration
    changes live.
    """
    from src.data.dialect import day_of, latest_rows
    from web_app import (
        BEIJING_TZ,
        HAS_LIVERY_SQL,
        _to_iso,
        convert_utc_to_beijing,
        db_manager,
        extract_livery_indicator,
        get_image_url,
    )

    airport = airport.strip().upper()
    flight_type = flight_type.strip().lower()
    aircraft_type = aircraft_type.strip().upper()
    livery = livery.strip()
    has_livery_bool = has_livery.strip().lower() == "true"

    if not airport:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": "Airport code is required"}
        )

    if date and date != "recent":
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            target_date = datetime.now()
        beijing_start = BEIJING_TZ.localize(
            datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
        )
        beijing_end = BEIJING_TZ.localize(
            datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
        )
        utc_start = beijing_start.astimezone(UTC)
        utc_end = beijing_end.astimezone(UTC)
    else:
        now_utc = datetime.now(UTC)
        utc_start = now_utc - timedelta(hours=1)
        utc_end = now_utc + timedelta(days=7)

    session = db_manager.get_session()
    try:
        # Normalize airport code: ICAO -> IATA for efficient index usage; and
        # resolve the other side so the "own photo here" subquery can match.
        airport_iata = airport
        airport_icao: str | None = airport if len(airport) == 4 else None
        if len(airport) == 4:
            icao_result = session.execute(
                text("SELECT iata_code FROM airports WHERE icao_code = :icao"),
                {"icao": airport},
            ).fetchone()
            if icao_result and icao_result[0]:
                airport_iata = icao_result[0]
        else:
            iata_result = session.execute(
                text("SELECT icao_code FROM airports WHERE iata_code = :iata"),
                {"iata": airport_iata},
            ).fetchone()
            if iata_result and iata_result[0]:
                airport_icao = iata_result[0]

        params: dict[str, Any] = {
            "airport_iata": airport_iata,
            "start_time": utc_start,
            "end_time": utc_end,
            "limit": limit,
            "offset": offset,
            "airport_icao": airport_icao or "",
        }

        where_conditions = [
            "fs.airport_iata = :airport_iata",
            "fs.scheduled_time >= :start_time",
            "fs.scheduled_time <= :end_time",
        ]

        if flight_type in ("arrival", "departure"):
            where_conditions.append("fs.flight_type = :flight_type")
            params["flight_type"] = flight_type

        if aircraft_type:
            where_conditions.append("LOWER(fs.aircraft_type) LIKE LOWER(:aircraft_type_pattern)")
            params["aircraft_type_pattern"] = f"%{aircraft_type}%"

        where_clause = " AND ".join(where_conditions)

        outer_where_conditions: list[str] = []
        if livery:
            outer_where_conditions.append("asi.livery_type = :livery_type")
            params["livery_type"] = livery
        if has_livery_bool:
            outer_where_conditions.append(HAS_LIVERY_SQL)
        outer_where_clause = (
            " AND ".join(outer_where_conditions) if outer_where_conditions else "1=1"
        )

        is_postgres = db_manager.is_postgres
        base_data = latest_rows(
            columns="""fs.id,
                    fs.flight_type,
                    fs.flight_number,
                    fs.callsign,
                    fs.airline_name,
                    fs.airline_iata,
                    fs.remote_airport_iata,
                    fs.remote_airport_name,
                    fs.aircraft_type,
                    fs.aircraft_registration,
                    fs.scheduled_time,
                    fs.estimated_time,
                    fs.actual_time,
                    fs.status,
                    fs.terminal,
                    fs.gate""",
            source="flight_schedules fs",
            partition_by=(
                "COALESCE(fs.flight_number, fs.callsign), "
                f"{day_of('fs.scheduled_time', is_postgres=is_postgres)}"
            ),
            order_by=(
                "CASE WHEN fs.aircraft_registration IS NOT NULL THEN 0 ELSE 1 END, "
                "fs.scheduled_time DESC"
            ),
            where=where_clause,
            is_postgres=is_postgres,
        )

        query = f"""
            WITH base_data AS (
                {base_data}
            ),
            filtered_data AS (
                SELECT bd.*
                FROM base_data bd
                LEFT JOIN aircraft_static_info asi ON bd.aircraft_registration = asi.registration
                WHERE {outer_where_clause}
            ),
            counted_data AS (
                SELECT
                    fd.*,
                    COUNT(*) OVER() as total_count,
                    COUNT(*) FILTER (WHERE fd.flight_type = 'arrival') OVER() as arrival_count,
                    COUNT(*) FILTER (WHERE fd.flight_type = 'departure') OVER() as departure_count
                FROM filtered_data fd
            )
            SELECT
                cd.id,
                cd.flight_type,
                cd.flight_number,
                cd.callsign,
                cd.airline_name,
                cd.airline_iata,
                cd.remote_airport_iata,
                cd.remote_airport_name,
                cd.aircraft_type,
                cd.aircraft_registration,
                cd.scheduled_time,
                cd.estimated_time,
                cd.actual_time,
                cd.status,
                cd.terminal,
                cd.gate,
                CASE WHEN asi.registration IS NOT NULL THEN true ELSE false END as has_static_info,
                CASE WHEN asi.images_downloaded = true THEN true ELSE false END as has_images,
                asi.livery_type,
                (SELECT ai.image_path FROM aircraft_images ai
                 WHERE ai.registration = cd.aircraft_registration
                   AND ai.airport_icao = :airport_icao
                   AND ai.image_path IS NOT NULL AND ai.image_path <> ''
                 ORDER BY ai.display_order LIMIT 1) as image_path_here,
                (SELECT ai.image_path FROM aircraft_images ai
                 WHERE ai.registration = cd.aircraft_registration
                   AND ai.image_path IS NOT NULL AND ai.image_path <> ''
                 ORDER BY ai.display_order LIMIT 1) as image_path_any,
                cd.total_count,
                cd.arrival_count,
                cd.departure_count
            FROM counted_data cd
            LEFT JOIN aircraft_static_info asi ON cd.aircraft_registration = asi.registration
            ORDER BY cd.scheduled_time ASC
            LIMIT :limit OFFSET :offset
        """

        result = session.execute(text(query), params).fetchall()

        if result:
            total_count = result[0][21]
            arrival_count = result[0][22]
            departure_count = result[0][23]
        else:
            total_count = 0
            arrival_count = 0
            departure_count = 0

        # When a flight_type filter is on, the window-function arrival /
        # departure counts only see the filtered rows. Take a second pass
        # without the flight_type filter so the UI tabs stay honest.
        if flight_type in ("arrival", "departure"):
            base_where_conditions = [
                "fs.airport_iata = :airport_iata",
                "fs.scheduled_time >= :start_time",
                "fs.scheduled_time <= :end_time",
            ]
            type_count_params: dict[str, Any] = {
                "airport_iata": airport_iata,
                "start_time": utc_start,
                "end_time": utc_end,
            }
            if aircraft_type:
                base_where_conditions.append(
                    "LOWER(fs.aircraft_type) LIKE LOWER(:aircraft_type_pattern)"
                )
                type_count_params["aircraft_type_pattern"] = f"%{aircraft_type}%"

            base_where_clause = " AND ".join(base_where_conditions)

            outer_livery_conditions: list[str] = []
            if livery:
                outer_livery_conditions.append("asi.livery_type = :livery_type")
                type_count_params["livery_type"] = livery
            if has_livery_bool:
                outer_livery_conditions.append(HAS_LIVERY_SQL)
            outer_livery_clause = (
                " AND ".join(outer_livery_conditions) if outer_livery_conditions else "1=1"
            )

            deduped_types = latest_rows(
                columns="fs.flight_type, fs.aircraft_registration",
                source="flight_schedules fs",
                partition_by=(
                    "COALESCE(fs.flight_number, fs.callsign), "
                    f"{day_of('fs.scheduled_time', is_postgres=is_postgres)}, "
                    "fs.flight_type"
                ),
                order_by=(
                    "CASE WHEN fs.aircraft_registration IS NOT NULL THEN 0 ELSE 1 END, "
                    "fs.scheduled_time DESC"
                ),
                where=base_where_clause,
                is_postgres=is_postgres,
            )
            type_count_query = f"""
                SELECT
                    COUNT(*) FILTER (WHERE flight_type = 'arrival') as arrivals,
                    COUNT(*) FILTER (WHERE flight_type = 'departure') as departures
                FROM (
                    {deduped_types}
                ) deduped
                LEFT JOIN aircraft_static_info asi ON deduped.aircraft_registration = asi.registration
                WHERE {outer_livery_clause}
            """
            type_result = session.execute(text(type_count_query), type_count_params).fetchone()
            if type_result:
                arrival_count = type_result[0] or 0
                departure_count = type_result[1] or 0

        schedules: list[dict[str, Any]] = []
        for row in result:
            scheduled_time = row[10]
            estimated_time = row[11]
            actual_time = row[12]

            schedule: dict[str, Any] = {
                "id": row[0],
                "flight_type": row[1],
                "flight_number": row[2],
                "callsign": row[3],
                "airline_name": row[4],
                "airline_iata": row[5],
                "remote_airport_iata": row[6],
                "remote_airport_name": row[7],
                "aircraft_type": row[8],
                "aircraft_registration": row[9],
                "scheduled_time": (
                    convert_utc_to_beijing(_to_iso(scheduled_time)) if scheduled_time else None
                ),
                "estimated_time": (
                    convert_utc_to_beijing(_to_iso(estimated_time)) if estimated_time else None
                ),
                "actual_time": (
                    convert_utc_to_beijing(_to_iso(actual_time)) if actual_time else None
                ),
                "status": row[13],
                "terminal": row[14],
                "gate": row[15],
                "has_static_info": row[16],
                "has_images": row[17],
                "livery_indicator": extract_livery_indicator(row[4]),
                "livery_type": row[18],
            }
            image_path_here, image_path_any = row[19], row[20]
            if image_path_here:
                schedule["image_url"] = get_image_url(image_path_here)
                schedule["image_source"] = "own_here"
            elif image_path_any:
                schedule["image_url"] = get_image_url(image_path_any)
                schedule["image_source"] = "own_elsewhere"
            else:
                schedule["image_url"] = None
                schedule["image_source"] = None
            schedules.append(schedule)

        return {
            "success": True,
            "schedules": schedules,
            "total_count": total_count,
            "arrival_count": arrival_count,
            "departure_count": departure_count,
            "limit": limit,
            "offset": offset,
        }
    finally:
        session.close()


@router.get("/api/flight-schedules/filter-options", name="get_flight_schedule_filter_options")
async def get_flight_schedule_filter_options(
    airport: str = Query("", description="Airport IATA / ICAO — narrows types+liveries+dates"),
    date: str = Query("", description="YYYY-MM-DD (Beijing)"),
    search: str = Query("", description="Airport name / code substring"),
) -> dict[str, Any]:
    """Available airports, aircraft types, liveries, dates for the
    frontend's filter dropdowns.

    Direct port of ``web_app.py:5551``. Same three-in-one UNION query
    for types/liveries/dates when an airport is picked, same
    "no search → empty airports list" fast path.
    """
    from src.data.dialect import beijing_date
    from web_app import BEIJING_TZ, HAS_LIVERY_SQL, db_manager

    airport = airport.strip().upper()

    session = db_manager.get_session()
    try:
        search_params: dict[str, Any] = {}
        if search:
            search_upper = search.upper()
            search_params["search_pattern"] = f"%{search_upper}%"
            search_params["search_exact"] = search_upper
            search_params["search_starts"] = f"{search_upper}%"

        if search:
            airports_query = """
                SELECT iata_code as airport_iata, icao_code as airport_icao, name
                FROM airports
                WHERE (
                    UPPER(iata_code) LIKE :search_pattern
                    OR UPPER(icao_code) LIKE :search_pattern
                    OR UPPER(COALESCE(name, '')) LIKE :search_pattern
                )
                ORDER BY
                    CASE
                        WHEN UPPER(iata_code) = :search_exact THEN 0
                        WHEN UPPER(icao_code) = :search_exact THEN 1
                        WHEN UPPER(iata_code) LIKE :search_starts THEN 2
                        WHEN UPPER(icao_code) LIKE :search_starts THEN 3
                        ELSE 4
                    END,
                    iata_code
                LIMIT 50
            """
            airports_result = session.execute(text(airports_query), search_params).fetchall()
        else:
            airports_result = []
        airports = [
            {"iata": row[0], "icao": row[1], "name": row[2] or f"{row[0]}/{row[1]}"}
            for row in airports_result
        ]

        aircraft_types: list[dict[str, Any]] = []
        liveries: list[dict[str, Any]] = []
        available_dates: list[str] = []

        if airport:
            params: dict[str, Any] = {"airport_iata": airport, "airport_icao": airport}

            if date:
                try:
                    target_date = datetime.strptime(date, "%Y-%m-%d")
                    beijing_start = BEIJING_TZ.localize(
                        datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
                    )
                    beijing_end = BEIJING_TZ.localize(
                        datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
                    )
                    params["start_time"] = beijing_start.astimezone(UTC)
                    params["end_time"] = beijing_end.astimezone(UTC)
                except ValueError:
                    now_utc = datetime.now(UTC)
                    params["start_time"] = now_utc - timedelta(days=30)
                    params["end_time"] = now_utc + timedelta(days=7)
            else:
                now_utc = datetime.now(UTC)
                params["start_time"] = now_utc - timedelta(days=1)
                params["end_time"] = now_utc + timedelta(days=3)

            date_filter = "AND fs.scheduled_time >= :start_time AND fs.scheduled_time <= :end_time"

            local_day = beijing_date("fs.scheduled_time", is_postgres=db_manager.is_postgres)
            combined_query = f"""
                -- Query 1: Aircraft types
                SELECT 'type' as query_type, fs.aircraft_type as value, CAST(COUNT(DISTINCT fs.aircraft_registration) AS TEXT) as count
                FROM flight_schedules fs
                WHERE fs.airport_iata = :airport_iata
                  AND fs.aircraft_type IS NOT NULL
                  AND fs.aircraft_type != ''
                  {date_filter}
                GROUP BY fs.aircraft_type

                UNION ALL

                -- Query 2: Liveries
                SELECT 'livery' as query_type, asi.livery_type as value, CAST(COUNT(DISTINCT fs.aircraft_registration) AS TEXT) as count
                FROM flight_schedules fs
                JOIN aircraft_static_info asi ON fs.aircraft_registration = asi.registration
                WHERE fs.airport_iata = :airport_iata
                  AND {HAS_LIVERY_SQL}
                  {date_filter}
                GROUP BY asi.livery_type

                UNION ALL

                -- Query 3: Dates
                SELECT 'date' as query_type,
                       {local_day} as value,
                       '0' as count
                FROM flight_schedules fs
                WHERE fs.airport_iata = :airport_iata
                  {date_filter}
                GROUP BY {local_day}
            """
            combined_result = session.execute(
                text(combined_query), {"airport_iata": airport, **params}
            ).fetchall()

            for row in combined_result:
                query_type, value, count = row[0], row[1], row[2]
                if query_type == "type":
                    aircraft_types.append({"code": value, "count": int(count)})
                elif query_type == "livery":
                    liveries.append({"name": value, "count": int(count)})
                elif query_type == "date":
                    available_dates.append(value)

            aircraft_types.sort(key=lambda x: x["count"], reverse=True)
            liveries.sort(key=lambda x: x["count"], reverse=True)
            available_dates.sort(reverse=True)

            aircraft_types = aircraft_types[:50]
            liveries = liveries[:50]
            available_dates = available_dates[:30]

        return {
            "success": True,
            "airports": airports,
            "aircraft_types": aircraft_types,
            "liveries": liveries,
            "available_dates": available_dates if airport else [],
        }
    finally:
        session.close()
