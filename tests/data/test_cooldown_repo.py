"""Tests for CooldownRepository — time-window + move-distance policy."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from src.data.cooldown_repo import CooldownRepository


class TestShouldGenerateReport:
    def test_first_sighting_allowed(self, cooldown_repo: CooldownRepository) -> None:
        assert (
            cooldown_repo.should_generate_report(
                aircraft_hex="abc123",
                lat=1.0,
                lon=2.0,
                cooldown_hours=1.0,
                min_move_km=100.0,
            )
            is True
        )

    def test_within_cooldown_not_allowed(
        self, db_manager, cooldown_repo: CooldownRepository
    ) -> None:
        # Manually insert a recent report row.
        with db_manager.get_session() as session:
            session.execute(
                text(
                    "INSERT INTO report_cooldowns "
                    "(aircraft_hex, last_report_time, last_latitude, last_longitude, report_count) "
                    "VALUES ('xyz', CURRENT_TIMESTAMP, 10.0, 20.0, 1)"
                )
            )
            session.commit()

        assert (
            cooldown_repo.should_generate_report(
                aircraft_hex="xyz",
                lat=10.0,
                lon=20.0,
                cooldown_hours=1.0,
                min_move_km=100.0,
            )
            is False
        )

    def test_expired_cooldown_with_movement_allowed(
        self, db_manager, cooldown_repo: CooldownRepository
    ) -> None:
        # Insert a row with last_report_time 2 hours ago, small delta,
        # then ask whether we should report at a location far away.
        stale = datetime.now() - timedelta(hours=2)
        with db_manager.get_session() as session:
            session.execute(
                text(
                    "INSERT INTO report_cooldowns "
                    "(aircraft_hex, last_report_time, last_latitude, last_longitude, report_count) "
                    "VALUES ('mover', :t, 0.0, 0.0, 1)"
                ),
                {"t": stale.isoformat(sep=" ", timespec="seconds")},
            )
            session.commit()

        # ~111 km per degree latitude at equator; 2° ≈ 222 km > 100 km threshold.
        assert (
            cooldown_repo.should_generate_report(
                aircraft_hex="mover",
                lat=2.0,
                lon=0.0,
                cooldown_hours=1.0,
                min_move_km=100.0,
            )
            is True
        )

    def test_expired_cooldown_without_movement_blocked(
        self, db_manager, cooldown_repo: CooldownRepository
    ) -> None:
        stale = datetime.now() - timedelta(hours=2)
        with db_manager.get_session() as session:
            session.execute(
                text(
                    "INSERT INTO report_cooldowns "
                    "(aircraft_hex, last_report_time, last_latitude, last_longitude, report_count) "
                    "VALUES ('stayer', :t, 10.0, 20.0, 1)"
                ),
                {"t": stale.isoformat(sep=" ", timespec="seconds")},
            )
            session.commit()

        # ~100 m away, well under the 100 km threshold.
        assert (
            cooldown_repo.should_generate_report(
                aircraft_hex="stayer",
                lat=10.001,
                lon=20.001,
                cooldown_hours=1.0,
                min_move_km=100.0,
            )
            is False
        )

    def test_key_suffix_creates_independent_cooldown(
        self, cooldown_repo: CooldownRepository
    ) -> None:
        # Register a cooldown for hex 'foo:schedule'; hex 'foo:regmatch' must
        # still be treated as unseen.
        cooldown_repo.update("foo", 0.0, 0.0, key_suffix=":schedule")
        assert (
            cooldown_repo.should_generate_report(
                aircraft_hex="foo",
                lat=0.0,
                lon=0.0,
                cooldown_hours=1.0,
                min_move_km=0.0,
                key_suffix=":regmatch",
            )
            is True
        )


class TestUpdateThenStatus:
    def test_update_and_read(self, cooldown_repo: CooldownRepository) -> None:
        cooldown_repo.update("abc", 10.0, 20.0)
        status = cooldown_repo.get_status("abc", lat=10.0, lon=20.0)
        assert status["has_previous_report"] is True
        assert status["report_count"] == 1

    def test_update_is_idempotent_but_increments_count(
        self, cooldown_repo: CooldownRepository
    ) -> None:
        cooldown_repo.update("abc", 10.0, 20.0)
        cooldown_repo.update("abc", 11.0, 21.0)
        cooldown_repo.update("abc", 12.0, 22.0)
        status = cooldown_repo.get_status("abc")
        assert status["report_count"] == 3

    def test_status_for_unknown_hex(self, cooldown_repo: CooldownRepository) -> None:
        assert cooldown_repo.get_status("never-seen") == {"has_previous_report": False}


class TestCleanupOld:
    def test_cleanup_removes_stale(self, db_manager, cooldown_repo: CooldownRepository) -> None:
        stale = (datetime.now() - timedelta(hours=48)).isoformat(sep=" ", timespec="seconds")
        fresh = datetime.now().isoformat(sep=" ", timespec="seconds")
        with db_manager.get_session() as session:
            session.execute(
                text(
                    "INSERT INTO report_cooldowns "
                    "(aircraft_hex, last_report_time, report_count) "
                    "VALUES ('old', :t1, 1), ('new', :t2, 1)"
                ),
                {"t1": stale, "t2": fresh},
            )
            session.commit()

        cooldown_repo.cleanup_old(max_age_hours=24.0)

        with db_manager.get_session() as session:
            remaining = {
                row[0]
                for row in session.execute(
                    text("SELECT aircraft_hex FROM report_cooldowns")
                ).fetchall()
            }
        assert "old" not in remaining
        assert "new" in remaining
