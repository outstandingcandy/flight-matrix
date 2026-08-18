"""Tests for src.data.db_manager.DatabaseManager.

Exercise the facade's normalisation, schema bootstrap, and delegation to
the repositories. Database is always in-memory SQLite.
"""

from __future__ import annotations

import logging

import pytest

from src.data.cooldown_repo import CooldownRepository
from src.data.db_manager import DatabaseManager, mask_database_url
from src.data.snapshot_repo import SnapshotRepository


class TestPasswordMasking:
    """`mask_database_url` is the only thing standing between a production
    DSN and CloudWatch, so it gets tested on the shapes that actually break
    naive string-splitting."""

    def test_password_is_replaced_but_host_and_db_survive(self) -> None:
        masked = mask_database_url(
            "postgresql+psycopg2://flight:s3cret@db.abc.rds.amazonaws.com:5432/flight"
        )
        assert "s3cret" not in masked
        assert masked == "postgresql+psycopg2://flight:***@db.abc.rds.amazonaws.com:5432/flight"

    @pytest.mark.parametrize("password", ["p@ss", "pa:ss", "p@ss:word", "@:@"])
    def test_punctuation_in_password_still_masked(self, password: str) -> None:
        """`split("@")` gets these wrong; parsing gets them right."""
        masked = mask_database_url(f"postgresql://user:{password}@host:5432/db")
        assert password not in masked
        assert masked == "postgresql://user:***@host:5432/db"

    def test_sqlite_url_is_unchanged(self) -> None:
        assert mask_database_url("sqlite:///aircraft_data.db") == "sqlite:///aircraft_data.db"
        assert mask_database_url("sqlite:///:memory:") == "sqlite:///:memory:"

    def test_url_without_credentials_is_unchanged(self) -> None:
        assert mask_database_url("postgresql://host/db") == "postgresql://host/db"

    def test_empty_string(self) -> None:
        assert mask_database_url("") == ""

    def test_unparseable_url_is_redacted_not_echoed(self) -> None:
        """Losing the detail beats leaking it."""
        assert mask_database_url("postgresql://u:p@[bad") == "postgresql://***"

    def test_init_does_not_log_the_password(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: `__init__` logged the raw DSN at INFO, so every process
        that touched the database wrote the production password to its log.

        A real Postgres URL is used because SQLite has no password to leak.
        `create_engine` is lazy, so nothing connects; only the schema bootstrap
        would, and it's stubbed out.
        """
        monkeypatch.setattr(DatabaseManager, "_bootstrap_schema", lambda self: None)

        with caplog.at_level(logging.INFO):
            DatabaseManager("postgresql+psycopg2://flight:s3cret@db.example.com:5432/flight")

        assert "SQL database initialized" in caplog.text
        assert "s3cret" not in caplog.text
        assert "db.example.com" in caplog.text


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

    def test_orm_tables_bootstrapped_on_sqlite(self, db_manager: DatabaseManager) -> None:
        """Regression: before the fix, _bootstrap_schema on SQLite only created
        the three tables from `create_sqlite_tables` (aircraft_snapshots,
        geographic_regions, report_cooldowns) and skipped
        `Base.metadata.create_all`, leaving aircraft_static_info, airports,
        flight_schedules, aircraft_images, etc. missing. That broke
        `batch_insert_aircraft` (it tries to auto-populate aircraft_static_info
        and swallowed the OperationalError silently).
        """
        from sqlalchemy import text

        expected_orm_tables = [
            "aircraft_snapshots",  # raw-SQL path
            "geographic_regions",  # raw-SQL path
            "report_cooldowns",  # raw-SQL path
            "aircraft_static_info",  # ORM-only — was missing pre-fix
            "aircraft_images",  # ORM-only
            "airports",  # ORM-only
            "flight_schedules",  # ORM-only
            "note_aircraft_analysis",  # ORM-only
            "aircraft_attention_aggregate",  # ORM-only
        ]

        with db_manager.get_session() as session:
            for table in expected_orm_tables:
                # Raises OperationalError if the table is missing.
                session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()

    def test_batch_insert_does_not_log_missing_table_errors(
        self, db_manager: DatabaseManager
    ) -> None:
        """Regression: batch_insert_aircraft should not hit
        'no such table: aircraft_static_info' during _auto_create_static_info.
        """
        sample = [
            {
                "hex": "abc123",
                "flight": "VIP123",
                "r": "N-TEST1",
                "t": "A380",
                "lat": 37.5,
                "lon": -122.3,
                "alt_baro": 35000,
                "gs": 480.5,
            }
        ]
        inserted = db_manager.batch_insert_aircraft(sample)
        assert inserted == 1

        # aircraft_static_info row should have been created by _auto_create_static_info.
        from sqlalchemy import text

        with db_manager.get_session() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM aircraft_static_info WHERE registration = 'N-TEST1'")
            ).scalar()
            assert count == 1, "auto_create_static_info should have inserted a row"


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
