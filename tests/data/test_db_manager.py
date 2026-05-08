"""Tests for src.data.db_manager.DatabaseManager.

Exercise the facade's normalisation, schema bootstrap, and delegation to
the repositories. Database is always in-memory SQLite.
"""

from __future__ import annotations

import pytest

from src.data.cooldown_repo import CooldownRepository
from src.data.db_manager import DatabaseManager
from src.data.snapshot_repo import SnapshotRepository


class TestURLNormalisation:
    def test_memory_shortcut(self) -> None:
        assert DatabaseManager._normalize_url(":memory:") == "sqlite:///:memory:"

    def test_bare_filename_becomes_sqlite(self) -> None:
        assert DatabaseManager._normalize_url("foo.db") == "sqlite:///foo.db"

    def test_existing_sqlite_url_passes_through(self) -> None:
        url = "sqlite:///aircraft_data.db"
        assert DatabaseManager._normalize_url(url) == url

    def test_postgresql_url_passes_through(self) -> None:
        url = "postgresql+psycopg2://u:p@h/db"
        assert DatabaseManager._normalize_url(url) == url


class TestDialectDetection:
    def test_sqlite_memory(self, db_manager: DatabaseManager) -> None:
        assert db_manager.is_sqlite
        assert not db_manager.is_postgres
        assert not db_manager.is_mysql

    def test_database_url_preserved(self, db_manager: DatabaseManager) -> None:
        assert db_manager.database_url == "sqlite:///:memory:"


class TestSchemaBootstrap:
    def test_aircraft_snapshots_table_exists(self, db_manager: DatabaseManager) -> None:
        from sqlalchemy import text

        with db_manager.get_session() as session:
            result = session.execute(text("SELECT COUNT(*) FROM aircraft_snapshots"))
            assert result.scalar() == 0

    def test_ensure_report_tables_is_idempotent(self, db_manager: DatabaseManager) -> None:
        # Core bootstrap already created report_cooldowns; calling again is a no-op.
        db_manager.ensure_report_tables_exist()
        db_manager.ensure_report_tables_exist()

    def test_ensure_multi_user_tables_is_idempotent(self, db_manager: DatabaseManager) -> None:
        db_manager.ensure_multi_user_tables_exist()
        db_manager.ensure_multi_user_tables_exist()

        from sqlalchemy import text

        with db_manager.get_session() as session:
            session.execute(text("SELECT COUNT(*) FROM users"))


class TestRepositoryProperties:
    def test_snapshots_returns_repo(self, db_manager: DatabaseManager) -> None:
        assert isinstance(db_manager.snapshots, SnapshotRepository)

    def test_cooldowns_returns_repo(self, db_manager: DatabaseManager) -> None:
        assert isinstance(db_manager.cooldowns, CooldownRepository)

    def test_repositories_are_stable(self, db_manager: DatabaseManager) -> None:
        # Same instance returned on repeat access (not a new repo per call).
        assert db_manager.snapshots is db_manager.snapshots
        assert db_manager.cooldowns is db_manager.cooldowns


class TestFacadeDelegation:
    def test_batch_insert_empty_returns_zero(self, db_manager: DatabaseManager) -> None:
        assert db_manager.batch_insert_aircraft([]) == 0

    def test_get_statistics_shape(self, db_manager: DatabaseManager) -> None:
        stats = db_manager.get_statistics()
        assert stats["total_snapshots"] == 0
        assert stats["recent_snapshots_1h"] == 0
        assert stats["unique_aircraft_total"] == 0
        assert stats["military_aircraft_total"] == 0
        assert stats["database_url"] == "sqlite:///:memory:"

    def test_get_flight_tracks_empty(self, db_manager: DatabaseManager) -> None:
        assert db_manager.get_flight_tracks_by_registration("B-1234") == []

    def test_has_active_flight_track_fail_open(self, db_manager: DatabaseManager) -> None:
        # No data and an unknown hex should return False (no activity).
        assert db_manager.has_active_flight_track("aaaaaa") is False


class TestClose:
    def test_close_is_safe_to_call_twice(self) -> None:
        dm = DatabaseManager(":memory:")
        dm.close()
        dm.close()  # should not raise
