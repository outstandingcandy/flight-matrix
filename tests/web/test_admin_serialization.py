"""Route tests for the twelve handlers that serialize raw-SQL timestamps.

`test_route_smoke.py` probes every one of these paths and proves less than it
looks: they all return an empty result set on an empty database, so the loop
that renders each row — and therefore every `.isoformat()` call in it — never
runs. The routes are reported as green while the only interesting code in them
is unreachable.

What breaks once a row exists: a `text()` query carries no type information, so
the driver decides what a timestamp column becomes. psycopg2 builds a
`datetime`; SQLite hands back the string it stored. `value.isoformat()` is
therefore an `AttributeError` on SQLite for SQL that works fine against Aurora,
and the whole request 500s — the local database and the entire test suite run on
SQLite, so these routes are unusable in development.

These tests seed one row into every table these handlers touch, so every branch
that renders a timestamp executes, and assert the rendered value parses back as
a timestamp rather than merely asserting the status code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

# Naive UTC, which is what every writer in this project stores.
NOW = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
REGISTRATION = "N12345"
HEX = "abc123"


def _seed(client: Any) -> None:
    """Insert one row into every table the twelve handlers read.

    Every timestamp column is populated: a NULL one takes the `else None`
    branch and skips the conversion under test.
    """
    db_manager = client.application_module.db_manager
    session = db_manager.get_session()
    try:
        session.execute(
            text("""
            INSERT INTO aircraft_static_info
                (id, registration, hex_code, aircraft_type, manufacturer, model,
                 images_downloaded, images_updated_at, last_updated,
                 ps_first_flight, ps_delivery_date)
            VALUES (1, :reg, :hex, 'B738', 'Boeing', '737-800',
                    1, :ts, :ts, :ts, :ts)
            """),
            {"reg": REGISTRATION, "hex": HEX, "ts": NOW},
        )

        session.execute(
            text("""
            INSERT INTO aircraft_snapshots
                (id, snapshot_time, hex, flight_number, registration, aircraft_type,
                 latitude, longitude, altitude_baro, ground_speed, track,
                 is_military, current_country)
            VALUES (1, :ts, :hex, 'AA100', :reg, 'B738',
                    40.65, -73.78, 30000, 450, 90, 0, 'United States')
            """),
            {"ts": NOW, "hex": HEX, "reg": REGISTRATION},
        )

        session.execute(
            text("""
            INSERT INTO aircraft_realtime_positions
                (fr24_id, flight_number, callsign, registration, aircraft_type,
                 latitude, longitude, altitude, ground_speed, heading,
                 origin_iata, destination_iata, on_ground, fr24_timestamp, scraped_at)
            VALUES ('fr1', 'AA100', 'AAL100', :reg, 'B738',
                    40.65, -73.78, 30000, 450, 90,
                    'JFK', 'LAX', 0, :ts, :ts)
            """),
            {"reg": REGISTRATION, "ts": NOW},
        )

        session.execute(
            text("""
            INSERT INTO aircraft_images
                (id, registration, image_path, source, photographer,
                 photo_date, upload_date, display_order, is_primary, created_at)
            VALUES (1, :reg, 'data/jetphotos_images/N12345_001.jpg', 'jetphotos',
                    'A Photographer', :day, :day, 0, 1, :ts)
            """),
            {"reg": REGISTRATION, "day": date(2026, 1, 2), "ts": NOW},
        )

        session.execute(
            text("""
            INSERT INTO flight_schedules
                (id, flight_type, airport_iata, airport_icao, flight_number, callsign,
                 fr24_flight_id, airline_name, airline_iata, remote_airport_iata,
                 remote_airport_name, aircraft_type, aircraft_registration,
                 scheduled_time, estimated_time, actual_time, status, scraped_at)
            VALUES (1, 'arrival', 'JFK', 'KJFK', 'AA100', 'AAL100',
                    'fr24-1', 'American Airlines', 'AA', 'LAX',
                    'Los Angeles Intl', 'B738', :reg,
                    :ts, :ts, :ts, 'landed', :ts)
            """),
            {"reg": REGISTRATION, "ts": NOW},
        )

        # The social-mentions query is a join, so both halves need a row. On
        # SQLite the JSON columns are TEXT, which is what the `LIKE` over the
        # serialized registrations array relies on.
        session.execute(
            text("""
            INSERT INTO xiaohongshu_notes
                (note_id, title, content, author_id, author_name,
                 like_count, comment_count, collect_count, share_count,
                 scraped_at, updated_at, note_created_at,
                 image_paths, image_urls, tags, location)
            VALUES ('note1', 'A title', 'Some content', 'author1', 'An Author',
                    10, 2, 3, 1, :ts, :ts, :ts, NULL, NULL, NULL, 'Beijing')
            """),
            {"ts": NOW},
        )
        session.execute(
            text("""
            INSERT INTO note_aircraft_analysis
                (id, note_id, source_type, registrations, attention_index,
                 attention_level, content_type, sentiment, analyzed_at)
            VALUES (1, 'note1', 'xiaohongshu', :registrations, 80,
                    'high', 'photo', 'positive', :ts)
            """),
            {"registrations": f'["{REGISTRATION}"]', "ts": NOW},
        )

        session.execute(
            text("""
            INSERT INTO aircraft_attention_aggregate
                (id, registration, total_mentions, avg_attention_index,
                 max_attention_index, mentions_7d, mentions_30d,
                 first_seen, last_seen, trending_score, updated_at)
            VALUES (1, :reg, 5, 72.5, 90, 2, 5, :ts, :ts, 12.5, :ts)
            """),
            {"reg": REGISTRATION, "ts": NOW},
        )

        session.execute(
            text("""
            INSERT INTO users (id, email) VALUES (1, 'test@example.com')
            """)
        )
        session.execute(
            text("""
            INSERT INTO user_cooldowns
                (id, user_id, aircraft_hex, last_report_time, last_latitude,
                 last_longitude, report_count)
            VALUES (1, 1, :hex, :ts, 40.65, -73.78, 2)
            """),
            {"hex": HEX, "ts": NOW},
        )
        session.execute(
            text("""
            INSERT INTO report_cooldowns
                (id, aircraft_hex, last_report_time, last_latitude, last_longitude,
                 report_count, updated_at)
            VALUES (1, :hex, :ts, 40.65, -73.78, 2, :ts)
            """),
            {"hex": HEX, "ts": NOW},
        )

        session.execute(
            text("""
            INSERT INTO scraper_workers
                (worker_id, status, last_heartbeat, tasks_completed, current_task_id)
            VALUES ('worker-1', 'active', :ts, 7, NULL)
            """),
            {"ts": NOW},
        )
        session.execute(
            text("""
            INSERT INTO scraper_tasks
                (id, task_type, task_key, status, priority, attempts, max_attempts,
                 scheduled_for, created_at, completed_at)
            VALUES (1, 'jetphotos', :reg, 'completed', 0, 1, 3, :ts, :ts, :ts)
            """),
            {"reg": REGISTRATION, "ts": NOW},
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def seeded_client(app_client: Any) -> Any:
    _seed(app_client)
    return app_client


def _payload(response: Any, path: str) -> dict[str, Any]:
    assert response.status_code == 200, f"{path} → {response.status_code}: {response.text[:400]}"
    body = response.json()
    assert body["success"] is True, body
    return body


def _assert_timestamp(value: Any, field: str) -> None:
    """Assert `value` is a rendered timestamp, not None and not a datetime."""
    assert isinstance(value, str), f"{field} should be a string, got {value!r}"
    # `fromisoformat` accepts both the "T" and the " " separator, so this covers
    # psycopg2's `datetime.isoformat()` and SQLite's stored form alike.
    parsed = datetime.fromisoformat(value)
    assert abs(parsed.replace(tzinfo=None) - NOW) < timedelta(days=1), (
        f"{field} parsed to {parsed}, which is not the seeded timestamp"
    )


class TestAircraftQuery:
    """`/api/v1/admin/aircraft-query/<registration>` — 18 timestamp conversions."""

    @pytest.fixture
    def body(self, seeded_client: Any) -> dict[str, Any]:
        path = f"/api/v1/admin/aircraft-query/{REGISTRATION}"
        return _payload(seeded_client.get(path), path)

    def test_static_info_timestamps(self, body: dict[str, Any]) -> None:
        static_info = body["static_info"]
        assert static_info is not None, "the seeded aircraft_static_info row was not returned"
        for field in ("images_updated_at", "last_updated", "ps_first_flight", "ps_delivery_date"):
            _assert_timestamp(static_info[field], field)

    def test_snapshot_timestamps(self, body: dict[str, Any]) -> None:
        snapshots = body["snapshots"]["recent"]
        assert len(snapshots) == 1
        _assert_timestamp(snapshots[0]["snapshot_time"], "snapshot_time")

    def test_realtime_position_timestamps(self, body: dict[str, Any]) -> None:
        positions = body["realtime_positions"]["recent"]
        assert len(positions) == 1
        _assert_timestamp(positions[0]["fr24_timestamp"], "fr24_timestamp")
        _assert_timestamp(positions[0]["scraped_at"], "scraped_at")

    def test_image_dates(self, body: dict[str, Any]) -> None:
        images = body["images"]["items"]
        assert len(images) == 1
        assert images[0]["photo_date"] == "2026-01-02"
        assert images[0]["upload_date"] == "2026-01-02"

    def test_flight_schedule_timestamps(self, body: dict[str, Any]) -> None:
        schedules = body["flight_schedules"]["items"]
        assert len(schedules) == 1
        for field in ("scheduled_time", "estimated_time", "actual_time", "scraped_at"):
            _assert_timestamp(schedules[0][field], field)

    def test_social_mention_timestamps(self, body: dict[str, Any]) -> None:
        mentions = body["social_mentions"]["items"]
        assert len(mentions) == 1, "the registrations LIKE match did not find the seeded note"
        _assert_timestamp(mentions[0]["analyzed_at"], "analyzed_at")
        _assert_timestamp(mentions[0]["note_scraped_at"], "note_scraped_at")

    def test_attention_metric_timestamps(self, body: dict[str, Any]) -> None:
        metrics = body["attention_metrics"]
        assert metrics is not None
        for field in ("first_seen", "last_seen", "updated_at"):
            _assert_timestamp(metrics[field], field)


class TestReportDetail:
    """`/api/v1/admin/reports/<hex>/detail` — timestamps also feed a Beijing conversion.

    `convert_utc_to_beijing(value.isoformat())` fails twice over on a string:
    the attribute lookup raises first, and the conversion needs a string anyway.
    """

    def test_multi_user_mode(self, seeded_client: Any) -> None:
        path = f"/api/v1/admin/reports/{HEX}/detail"
        body = _payload(seeded_client.get(path), path)

        cooldown = body["detail"]["cooldown"]
        _assert_timestamp(cooldown["last_report_time"], "last_report_time")
        assert cooldown["last_report_time_beijing"], "the Beijing rendering is empty"

        snapshot = body["detail"]["recent_snapshots"][0]
        _assert_timestamp(snapshot["snapshot_time"], "snapshot_time")
        assert snapshot["snapshot_time_beijing"], "the Beijing rendering is empty"

    def test_single_user_mode(self, seeded_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """The `report_cooldowns` branch renders an `updated_at` the other lacks."""
        monkeypatch.setattr(
            seeded_client.application_module.config, "is_multi_user_enabled", lambda: False
        )
        path = f"/api/v1/admin/reports/{HEX}/detail"
        body = _payload(seeded_client.get(path), path)

        cooldown = body["detail"]["cooldown"]
        _assert_timestamp(cooldown["last_report_time"], "last_report_time")
        _assert_timestamp(cooldown["updated_at"], "updated_at")


class TestFR24Flights:
    """`/api/v1/admin/scraped-data/fr24/flights` — two conversions in the row loop."""

    def test_flight_timestamps(self, seeded_client: Any) -> None:
        path = "/api/v1/admin/scraped-data/fr24/flights"
        body = _payload(seeded_client.get(path), path)

        assert len(body["flights"]) == 1
        _assert_timestamp(body["flights"][0]["scheduled_time"], "scheduled_time")
        _assert_timestamp(body["flights"][0]["scraped_at"], "scraped_at")


class TestScraperWorkers:
    """`/api/v1/admin/scraper/workers` — already string-safe; guards it staying so."""

    def test_heartbeat_timestamp(self, seeded_client: Any) -> None:
        path = "/api/v1/admin/scraper/workers"
        body = _payload(seeded_client.get(path), path)

        assert len(body["workers"]) == 1
        worker = body["workers"][0]
        _assert_timestamp(worker["last_heartbeat"], "last_heartbeat")
        assert worker["seconds_since_heartbeat"] is not None, (
            "the heartbeat age is computed from the same value and must survive too"
        )


class TestTimestampArithmetic:
    """The two routes that subtract a raw-SQL timestamp instead of rendering it.

    `datetime.now() - value` is a `TypeError` on a string, so rendering the field
    correctly is not enough on its own — these two need the value parsed back.
    """

    def test_user_cooldowns_report_an_age(self, seeded_client: Any) -> None:
        path = "/api/v1/user/test@example.com/cooldowns"
        body = _payload(seeded_client.get(path), path)

        assert len(body["cooldowns"]) == 1
        cooldown = body["cooldowns"][0]
        _assert_timestamp(cooldown["last_report_time"], "last_report_time")
        assert cooldown["hours_since_last_report"] is not None
        assert cooldown["hours_since_last_report"] < 24


class TestRemainingRawSQLRoutes:
    """The eight other handlers that serialize a `text()` row's timestamps.

    Same defect, same fix; grouped because each one has only a field or two
    worth asserting.
    """

    def test_aircraft_recent_flights(self, seeded_client: Any) -> None:
        path = f"/api/v1/aircraft/{REGISTRATION}/recent-flights"
        body = _payload(seeded_client.get(path), path)

        assert len(body["flights"]) == 1
        _assert_timestamp(body["flights"][0]["scheduled_time"], "scheduled_time")

    def test_aircraft_images(self, seeded_client: Any) -> None:
        path = f"/api/v1/aircraft/{REGISTRATION}/images"
        body = _payload(seeded_client.get(path), path)

        images = body["images_with_metadata"]
        assert len(images) == 1
        assert images[0]["photo_date"] == "2026-01-02"
        assert images[0]["upload_date"] == "2026-01-02"

    def test_admin_aircraft_list(self, seeded_client: Any) -> None:
        path = "/api/v1/admin/aircraft"
        body = _payload(seeded_client.get(path), path)

        assert len(body["aircraft"]) == 1
        _assert_timestamp(body["aircraft"][0]["last_updated"], "last_updated")

    def test_admin_xhs_notes(self, seeded_client: Any) -> None:
        path = "/api/v1/admin/scraped-data/xiaohongshu/notes"
        body = _payload(seeded_client.get(path), path)

        assert len(body["notes"]) == 1
        _assert_timestamp(body["notes"][0]["scraped_at"], "scraped_at")
        _assert_timestamp(body["notes"][0]["updated_at"], "updated_at")

    def test_admin_xhs_note_detail(self, seeded_client: Any) -> None:
        path = "/api/v1/admin/scraped-data/xiaohongshu/notes/note1"
        body = _payload(seeded_client.get(path), path)

        _assert_timestamp(body["note"]["scraped_at"], "scraped_at")
        _assert_timestamp(body["note"]["note_created_at"], "note_created_at")

    def test_admin_jetphotos_images(self, seeded_client: Any) -> None:
        path = "/api/v1/admin/scraped-data/jetphotos/images"
        body = _payload(seeded_client.get(path), path)

        assert len(body["images"]) == 1
        assert body["images"][0]["photo_date"] == "2026-01-02"
        _assert_timestamp(body["images"][0]["created_at"], "created_at")

    def test_admin_scraper_recent_tasks(self, seeded_client: Any) -> None:
        path = "/api/v1/admin/scraper/recent-tasks"
        body = _payload(seeded_client.get(path), path)

        assert len(body["tasks"]) == 1
        _assert_timestamp(body["tasks"][0]["created_at"], "created_at")
        _assert_timestamp(body["tasks"][0]["completed_at"], "completed_at")
