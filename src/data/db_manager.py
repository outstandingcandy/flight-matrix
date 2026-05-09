"""Connection + session lifecycle for Flight Matrix.

`DatabaseManager` owns the SQLAlchemy engine and the session factory. All
snapshot and cooldown logic lives in dedicated repositories
(`SnapshotRepository`, `CooldownRepository`); this class is a thin facade
that exposes them on the legacy API so existing callers do not break.

Public callers should, over time, access repositories directly:

    from src.data.snapshot_repo import SnapshotRepository

But until every caller is migrated, the facade keeps them working.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.data.cooldown_repo import CooldownRepository
from src.data.schema import (
    create_core_tables,
    create_sqlite_tables,
    ensure_multi_user_tables,
    ensure_report_cooldowns_table,
)
from src.data.snapshot_repo import SnapshotRepository

logger = logging.getLogger("database")


class DatabaseManager:
    """Engine + session factory + delegating facade over repositories."""

    def __init__(self, database_url: str = "sqlite:///aircraft_data.db") -> None:
        self.database_url = self._normalize_url(database_url)
        self.is_postgres = self.database_url.startswith("postgresql")
        self.is_mysql = self.database_url.startswith("mysql")
        self.is_sqlite = self.database_url.startswith("sqlite")

        # Ensure SQLite parent dir exists.
        if self.database_url.startswith("sqlite:///") and not self.database_url.endswith(
            ":memory:"
        ):
            db_path = self.database_url.replace("sqlite:///", "")
            db_dir = Path(db_path).parent
            if not db_dir.exists():
                db_dir.mkdir(parents=True, exist_ok=True)

        self.engine = create_engine(self.database_url, echo=False)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        self._snapshots = SnapshotRepository(self.SessionLocal, is_postgres=self.is_postgres)
        self._cooldowns = CooldownRepository(self.SessionLocal, is_postgres=self.is_postgres)

        logger.info("Ensuring all database tables exist...")
        self._bootstrap_schema()
        logger.info(f"SQL database initialized: {self.database_url}")

    @staticmethod
    def _normalize_url(database_url: str) -> str:
        if database_url.startswith(("sqlite:", "mysql:", "postgresql:", "postgresql+")):
            return database_url
        if database_url == ":memory:":
            return "sqlite:///:memory:"
        return f"sqlite:///{database_url}"

    def _bootstrap_schema(self) -> None:
        """Create every table this manager owns, idempotently.

        Two-step on SQLite:

        1. Raw CREATE TABLE for `aircraft_snapshots` when it's missing, because
           SQLAlchemy's `create_all` does not emit the AUTOINCREMENT clause
           that the ingest path depends on.
        2. `Base.metadata.create_all()` to bring up every other ORM-declared
           table (`aircraft_static_info`, `airports`, `flight_schedules`,
           multi-user tables, etc.). It is idempotent.

        On Postgres/MySQL only step 2 is needed — there's no AUTOINCREMENT
        quirk to work around.
        """
        if self.is_sqlite:
            session = self.SessionLocal()
            try:
                session.execute(text("SELECT COUNT(*) FROM aircraft_snapshots LIMIT 1"))
                logger.info("SQLite core tables already exist")
            except Exception:
                create_sqlite_tables(session)
            finally:
                session.close()

        # Bring up every other ORM-declared table (idempotent on every dialect).
        create_core_tables(self.engine)
        logger.info("All tables ensured (including models.py)")

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def get_session(self):
        """Return a new SQLAlchemy session. Caller is responsible for close()."""
        return self.SessionLocal()

    def close(self) -> None:
        """Dispose the engine. Safe to call multiple times."""
        if hasattr(self, "engine"):
            self.engine.dispose()

    # ------------------------------------------------------------------
    # Schema helpers — delegated
    # ------------------------------------------------------------------

    def ensure_report_tables_exist(self) -> None:
        """Create `report_cooldowns` on demand (for pre-existing databases)."""
        session = self.SessionLocal()
        try:
            ensure_report_cooldowns_table(session)
        finally:
            session.close()

    def ensure_multi_user_tables_exist(self) -> None:
        """Create the users/subscriptions/user_filters/… tables on demand."""
        session = self.SessionLocal()
        try:
            ensure_multi_user_tables(session, is_postgres=self.is_postgres)
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Snapshot repository — delegated (preserves the old API shape)
    # ------------------------------------------------------------------

    def batch_insert_aircraft(self, aircraft_data_list: list[dict]) -> int:
        return self._snapshots.batch_insert(aircraft_data_list)

    def execute_filter_query(self, where_clause: str, limit: int = 1000) -> list[dict]:
        return self._snapshots.execute_filter_query(where_clause, limit)

    def cleanup_old_data(self, hours_to_keep: int = 24) -> None:
        self._snapshots.cleanup_old_data(hours_to_keep)

    def get_flight_tracks_by_registration(
        self,
        registration: str,
        limit: int = 500,
        start_time: int | None = None,
    ) -> list[dict]:
        return self._snapshots.get_flight_tracks_by_registration(registration, limit, start_time)

    def get_statistics(self) -> dict:
        return self._snapshots.get_statistics(self.database_url)

    def has_active_flight_track(self, aircraft_hex: str, minutes: int = 10) -> bool:
        return self._snapshots.has_active_flight_track(aircraft_hex, minutes)

    # ------------------------------------------------------------------
    # Cooldown repository — delegated
    # ------------------------------------------------------------------

    def should_generate_report_db(
        self,
        aircraft_hex: str,
        lat: float | None,
        lon: float | None,
        cooldown_hours: float,
        min_move_km: float,
        key_suffix: str = "",
    ) -> bool:
        return self._cooldowns.should_generate_report(
            aircraft_hex, lat, lon, cooldown_hours, min_move_km, key_suffix
        )

    def update_report_cooldown(
        self,
        aircraft_hex: str,
        latitude: float | None,
        longitude: float | None,
        key_suffix: str = "",
    ) -> None:
        self._cooldowns.update(aircraft_hex, latitude, longitude, key_suffix)

    def get_cooldown_status(
        self,
        aircraft_hex: str,
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict:
        return self._cooldowns.get_status(aircraft_hex, lat, lon)

    def cleanup_old_cooldowns(self, max_age_hours: float = 24.0) -> None:
        self._cooldowns.cleanup_old(max_age_hours)

    # ------------------------------------------------------------------
    # Direct repository access (preferred for new code)
    # ------------------------------------------------------------------

    @property
    def snapshots(self) -> SnapshotRepository:
        """Direct access to the snapshot repository (preferred for new code)."""
        return self._snapshots

    @property
    def cooldowns(self) -> CooldownRepository:
        """Direct access to the cooldown repository (preferred for new code)."""
        return self._cooldowns
