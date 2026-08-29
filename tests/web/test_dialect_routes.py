"""Route tests for the four handlers whose SQL used to be PostgreSQL-only.

`test_route_smoke.py` probes these paths but proves less than it looks: every
one of them returns early on an empty database — 404 for an unknown airport,
an empty result set for the rest — so the interesting SQL never runs. These
tests seed just enough rows to force each converted query to execute, and then
assert the *result*, not only the status code, because the two constructs being
replaced (`DISTINCT ON` and `x::date`) are deduplication and grouping: a query
that runs but groups wrongly returns a 200 with the wrong rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text


def _seed(client: Any) -> None:
    """Insert one airport, one aircraft and a handful of duplicated rows."""
    db_manager = client.application_module.db_manager
    now = datetime.now(UTC).replace(tzinfo=None)

    session = db_manager.get_session()
    try:
        session.execute(
            text("""
            INSERT INTO airports (iata_code, icao_code, name, latitude, longitude)
            VALUES ('JFK', 'KJFK', 'John F Kennedy Intl', 40.6413, -73.7781)
            """)
        )

        # Two positions for the same aircraft: only the newer one may come back.
        for minutes, altitude in ((1, 30000), (5, 10000)):
            session.execute(
                text("""
                INSERT INTO aircraft_realtime_positions
                    (fr24_id, flight_number, registration, aircraft_type,
                     latitude, longitude, altitude, scraped_at)
                VALUES ('fr1', 'AA100', 'N12345', 'B738',
                        40.65, -73.78, :altitude, :scraped_at)
                """),
                {"altitude": altitude, "scraped_at": now - timedelta(minutes=minutes)},
            )

        # Two schedule rows for one flight on one day: the row carrying a
        # registration wins, and they must collapse to a single result.
        # SQLite's ORM-created tables have no AUTOINCREMENT, so `id` is explicit.
        for row_id, offset_minutes, registration in ((1, 30, "N12345"), (2, 35, None)):
            session.execute(
                text("""
                INSERT INTO flight_schedules
                    (id, fr24_flight_id, airport_iata, flight_type, flight_number,
                     aircraft_type, aircraft_registration, scheduled_time, status)
                VALUES (:row_id, :fr24_id, 'JFK', 'arrival', 'AA100',
                        'B738', :registration, :scheduled_time, 'scheduled')
                """),
                {
                    "row_id": row_id,
                    "fr24_id": f"fr24-{row_id}",
                    "registration": registration,
                    "scheduled_time": now + timedelta(minutes=offset_minutes),
                },
            )
        session.execute(
            text("""
            INSERT INTO flight_schedules
                (id, fr24_flight_id, airport_iata, flight_type, flight_number,
                 aircraft_type, aircraft_registration, scheduled_time, status)
            VALUES (3, 'fr24-3', 'JFK', 'departure', 'AA200',
                    'A320', 'N54321', :scheduled_time, 'scheduled')
            """),
            {"scheduled_time": now + timedelta(minutes=90)},
        )

        session.execute(
            text("""
            INSERT INTO aircraft_static_info (id, registration, aircraft_type, livery_type)
            VALUES (1, 'N12345', 'B738', 'special livery')
            """)
        )

        # Report rows: two snapshots for one hex, newest registration wins.
        for row_id, minutes, registration in ((1, 1, "N12345"), (2, 60, "N-STALE")):
            session.execute(
                text("""
                INSERT INTO aircraft_snapshots
                    (id, hex, registration, aircraft_type, flight_number, snapshot_time)
                VALUES (:row_id, 'abc123', :registration, 'B738', 'AA100', :snapshot_time)
                """),
                {
                    "row_id": row_id,
                    "registration": registration,
                    "snapshot_time": now - timedelta(minutes=minutes),
                },
            )
        session.execute(
            text("""
            INSERT INTO report_cooldowns
                (id, aircraft_hex, last_report_time, last_latitude, last_longitude, report_count)
            VALUES (1, 'abc123', :last_report_time, 40.65, -73.78, 2)
            """),
            {"last_report_time": now},
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def seeded_client(app_client: Any) -> Any:
    _seed(app_client)
    return app_client


def _payload(response: Any, path: str) -> dict[str, Any]:
    assert response.status_code == 200, (
        f"{path} → {response.status_code}: {response.text[:400]}"
    )
    body = response.json()
    assert body["success"] is True, body
    return body


class TestRealtimeAircraft:
    """`/api/airports/<code>/realtime-aircraft` — `DISTINCT ON`, `= ANY()`, `NOW() - INTERVAL`."""

    def test_returns_only_the_newest_position_per_aircraft(self, seeded_client: Any) -> None:
        path = "/api/airports/JFK/realtime-aircraft"
        body = _payload(seeded_client.get(path), path)

        aircraft = body["aircraft"]
        assert len(aircraft) == 1, "two snapshots of one aircraft must collapse to one row"
        assert aircraft[0]["altitude"] == 30000, "the newer of the two positions must win"

    def test_flight_number_filter_uses_an_expanding_bind(self, seeded_client: Any) -> None:
        """The filter replaced PostgreSQL's `= ANY(:param)`, which SQLite rejects."""
        path = "/api/airports/JFK/realtime-aircraft?flight_numbers=AA100"
        body = _payload(seeded_client.get(path), path)
        assert [a["flight_number"] for a in body["aircraft"]] == ["AA100"]

        path = "/api/airports/JFK/realtime-aircraft?flight_numbers=ZZ999"
        body = _payload(seeded_client.get(path), path)
        assert body["aircraft"] == []


class TestFlightSchedules:
    """`/api/flight-schedules` — `DISTINCT ON` plus four `scheduled_time::date`."""

    def test_deduplicates_one_flight_per_day(self, seeded_client: Any) -> None:
        path = "/api/flight-schedules?airport=JFK"
        body = _payload(seeded_client.get(path), path)

        flights = [s["flight_number"] for s in body["schedules"]]
        assert sorted(flights) == ["AA100", "AA200"]
        arrival = next(s for s in body["schedules"] if s["flight_number"] == "AA100")
        assert arrival["aircraft_registration"] == "N12345", (
            "of two rows for the same flight and day, the one with a registration wins"
        )

    def test_icao_code_is_normalised_to_iata(self, seeded_client: Any) -> None:
        path = "/api/flight-schedules?airport=KJFK"
        body = _payload(seeded_client.get(path), path)
        assert len(body["schedules"]) == 2

    def test_flight_type_filter_runs_the_separate_count_query(self, seeded_client: Any) -> None:
        """`flight_type` is the only way into the second `DISTINCT ON` query."""
        path = "/api/flight-schedules?airport=JFK&flight_type=arrival"
        body = _payload(seeded_client.get(path), path)

        assert [s["flight_number"] for s in body["schedules"]] == ["AA100"]
        # Counts come from the unfiltered dedup query, so both types appear.
        assert body["arrival_count"] == 1
        assert body["departure_count"] == 1

    def test_has_livery_filter_runs(self, seeded_client: Any) -> None:
        """Guards the predicate that replaced the nonexistent `has_special_livery`."""
        path = "/api/flight-schedules?airport=JFK&has_livery=true"
        body = _payload(seeded_client.get(path), path)
        assert [s["flight_number"] for s in body["schedules"]] == ["AA100"]

    def test_livery_filter_runs(self, seeded_client: Any) -> None:
        path = "/api/flight-schedules?airport=JFK&livery=special+livery&flight_type=arrival"
        body = _payload(seeded_client.get(path), path)
        assert [s["flight_number"] for s in body["schedules"]] == ["AA100"]


def _seed_photo_priority(client: Any) -> None:
    """Two airports, one aircraft with photos at each, one with none."""
    db_manager = client.application_module.db_manager
    now = datetime.now(UTC).replace(tzinfo=None)

    session = db_manager.get_session()
    try:
        session.execute(
            text("""
            INSERT INTO airports (iata_code, icao_code, name, latitude, longitude)
            VALUES ('PEK', 'ZBAA', 'Beijing Capital', 40.08, 116.58)
            """)
        )
        session.execute(
            text("""
            INSERT INTO airports (iata_code, icao_code, name, latitude, longitude)
            VALUES ('PVG', 'ZSPD', 'Shanghai Pudong', 31.14, 121.80)
            """)
        )

        aircraft = [
            ("B-1111", "CA100"),  # photo at both PEK and PVG
            ("B-2222", "CA200"),  # photo at PVG only
            ("B-3333", "CA300"),  # no photo at all
        ]
        for row_id, (registration, flight_number) in enumerate(aircraft, start=1):
            session.execute(
                text("""
                INSERT INTO aircraft_static_info (id, registration, aircraft_type)
                VALUES (:id, :registration, 'B738')
                """),
                {"id": row_id, "registration": registration},
            )
            session.execute(
                text("""
                INSERT INTO flight_schedules
                    (id, fr24_flight_id, airport_iata, flight_type, flight_number,
                     aircraft_type, aircraft_registration, scheduled_time, status)
                VALUES (:row_id, :fr24_id, 'PEK', 'arrival', :flight_number,
                        'B738', :registration, :scheduled_time, 'scheduled')
                """),
                {
                    "row_id": row_id,
                    "fr24_id": f"priority-{row_id}",
                    "flight_number": flight_number,
                    "registration": registration,
                    "scheduled_time": now + timedelta(minutes=row_id),
                },
            )

        images = [
            ("B-1111", "own_at_pvg.jpg", "ZSPD", 1),
            ("B-1111", "own_at_pek.jpg", "ZBAA", 2),
            ("B-2222", "own_elsewhere.jpg", "ZSPD", 1),
        ]
        for registration, image_path, airport_icao, display_order in images:
            session.execute(
                text("""
                INSERT INTO aircraft_images (registration, image_path, airport_icao, display_order)
                VALUES (:registration, :image_path, :airport_icao, :display_order)
                """),
                {
                    "registration": registration,
                    "image_path": image_path,
                    "airport_icao": airport_icao,
                    "display_order": display_order,
                },
            )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def photo_priority_client(app_client: Any) -> Any:
    _seed_photo_priority(app_client)
    return app_client


class TestOwnPhotoPriority:
    """image_url/image_source on /api/flight-schedules: this airframe at this
    airport beats this airframe anywhere, which beats having nothing at all."""

    def test_a_photo_at_this_airport_wins_over_one_taken_elsewhere(
        self, photo_priority_client: Any
    ) -> None:
        path = "/api/flight-schedules?airport=PEK"
        body = _payload(photo_priority_client.get(path), path)

        flight = next(s for s in body["schedules"] if s["flight_number"] == "CA100")
        assert flight["image_url"].endswith("own_at_pek.jpg")
        assert flight["image_source"] == "own_here"

    def test_a_photo_from_elsewhere_is_used_when_none_exists_here(
        self, photo_priority_client: Any
    ) -> None:
        path = "/api/flight-schedules?airport=PEK"
        body = _payload(photo_priority_client.get(path), path)

        flight = next(s for s in body["schedules"] if s["flight_number"] == "CA200")
        assert flight["image_url"].endswith("own_elsewhere.jpg")
        assert flight["image_source"] == "own_elsewhere"

    def test_no_own_photo_leaves_both_fields_empty(self, photo_priority_client: Any) -> None:
        """The frontend's same-type fallback only kicks in when both are empty."""
        path = "/api/flight-schedules?airport=PEK"
        body = _payload(photo_priority_client.get(path), path)

        flight = next(s for s in body["schedules"] if s["flight_number"] == "CA300")
        assert flight["image_url"] is None
        assert flight["image_source"] is None

    def test_the_icao_code_resolves_the_same_way(self, photo_priority_client: Any) -> None:
        path = "/api/flight-schedules?airport=ZBAA"
        body = _payload(photo_priority_client.get(path), path)

        flight = next(s for s in body["schedules"] if s["flight_number"] == "CA100")
        assert flight["image_source"] == "own_here"


class TestFilterOptions:
    """`/api/flight-schedules/filter-options` — `::text`, `TO_CHAR`, `AT TIME ZONE`."""

    def test_returns_types_liveries_and_beijing_dates(self, seeded_client: Any) -> None:
        path = "/api/flight-schedules/filter-options?airport=JFK"
        body = _payload(seeded_client.get(path), path)

        assert {t["code"] for t in body["aircraft_types"]} == {"B738", "A320"}
        assert [lv["name"] for lv in body["liveries"]] == ["special livery"]

        now_beijing = datetime.now(UTC) + timedelta(hours=8)
        expected_today = now_beijing.strftime("%Y-%m-%d")
        expected_tomorrow = (now_beijing + timedelta(days=1)).strftime("%Y-%m-%d")
        # The seeded flights are up to 90 minutes out. On a normal run that
        # spans today; on a run near Beijing midnight (UTC 16:00) every
        # seeded flight may already sit on tomorrow. Accept either — the
        # point of the assertion is "we got a real Beijing date back and it
        # matches when the seeded rows should land", not "we got today's".
        assert body["available_dates"], "grouping by Beijing date returned nothing"
        for value in body["available_dates"]:
            datetime.strptime(value, "%Y-%m-%d")
        assert set(body["available_dates"]) & {expected_today, expected_tomorrow}, (
            f"expected today ({expected_today}) or tomorrow ({expected_tomorrow}) "
            f"in dates but got {body['available_dates']}"
        )


class TestAdminReportsMultiUser:
    """The default `user_cooldowns` branch of `/api/admin/reports`.

    Multi-user mode is on by default, so this is the branch that actually runs.
    It used to carry its own `INNER JOIN (SELECT MAX(snapshot_time) GROUP BY hex)`
    subquery, which returns *both* rows when two snapshots tie on the newest
    timestamp; it now shares `latest_rows` with the single-user branch.
    """

    @pytest.fixture(autouse=True)
    def seed_user_cooldown(self, seeded_client: Any) -> None:
        db_manager = seeded_client.application_module.db_manager
        session = db_manager.get_session()
        try:
            session.execute(text("INSERT INTO users (id, email) VALUES (1, 'test@example.com')"))
            session.execute(
                text("""
                INSERT INTO user_cooldowns
                    (id, user_id, aircraft_hex, last_report_time, report_count)
                VALUES (1, 1, 'abc123', :ts, 2)
                """),
                {"ts": datetime.now(UTC).replace(tzinfo=None)},
            )
            # A snapshot tied with the newest one: the old subquery returned two
            # rows here, duplicating the report in the response.
            session.execute(
                text("""
                INSERT INTO aircraft_snapshots
                    (id, hex, registration, aircraft_type, snapshot_time)
                SELECT 3, 'abc123', 'N-TIED', 'B738', snapshot_time
                FROM aircraft_snapshots WHERE id = 1
                """)
            )
            session.commit()
        finally:
            session.close()

    def test_returns_one_row_per_cooldown_despite_tied_snapshots(self, seeded_client: Any) -> None:
        path = "/api/admin/reports"
        body = _payload(seeded_client.get(path), path)

        assert body["multi_user_mode"] is True
        assert len(body["reports"]) == 1, "tied snapshots must not duplicate the report"
        assert body["reports"][0]["user_email"] == "test@example.com"
        assert body["total"] == 1


class TestAdminReports:
    """`/api/admin/reports` and `/stats` — `DISTINCT ON` and the date comparison.

    The default config enables multi-user mode, which reads `user_cooldowns`.
    These tests cover the single-user branch over `report_cooldowns`, so they
    turn multi-user off; the shared latest-snapshot subquery is the same either
    way.
    """

    @pytest.fixture(autouse=True)
    def single_user_mode(self, seeded_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            seeded_client.application_module.config, "is_multi_user_enabled", lambda: False
        )

    def test_joins_the_newest_snapshot_per_hex(self, seeded_client: Any) -> None:
        path = "/api/admin/reports"
        body = _payload(seeded_client.get(path), path)

        assert len(body["reports"]) == 1
        report = body["reports"][0]
        assert report["aircraft_hex"] == "abc123"
        assert report["registration"] == "N12345", "the stale snapshot must not win"

    def test_stats_counts_today(self, seeded_client: Any) -> None:
        path = "/api/admin/reports/stats"
        body = _payload(seeded_client.get(path), path)

        assert body["stats"]["total_tracked"] == 1
        assert body["stats"]["reports_today"] == 1

    def test_stats_ignores_a_future_timestamp(self, seeded_client: Any) -> None:
        """`>= CURRENT_DATE` counted rows dated tomorrow; comparing dates does not."""
        db_manager = seeded_client.application_module.db_manager
        session = db_manager.get_session()
        try:
            session.execute(
                text("""
                INSERT INTO report_cooldowns
                    (id, aircraft_hex, last_report_time, report_count)
                VALUES (2, 'future1', :ts, 1)
                """),
                {"ts": datetime.now(UTC).replace(tzinfo=None) + timedelta(days=3)},
            )
            session.commit()
        finally:
            session.close()

        path = "/api/admin/reports/stats"
        body = _payload(seeded_client.get(path), path)

        assert body["stats"]["total_tracked"] == 2
        assert body["stats"]["reports_today"] == 1
