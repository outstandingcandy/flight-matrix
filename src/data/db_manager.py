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
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.data.cooldown_repo import CooldownRepository
from src.data.schema import (
    create_core_tables,
    create_sqlite_tables,
    ensure_multi_user_tables,
    ensure_report_cooldowns_table,
    ensure_scraper_tables,
    ensure_xiaohongshu_tables,
)
from src.data.snapshot_repo import SnapshotRepository

logger = logging.getLogger("database")


_PROD_HOST_MARKERS = (
    ".rds.amazonaws.com",  # AWS RDS / Aurora
    ".redshift.amazonaws.com",  # Redshift, just in case
)


def _looks_like_prod_host(database_url: str) -> bool:
    """Heuristic: does this URL point at a managed AWS database?"""
    return any(marker in database_url for marker in _PROD_HOST_MARKERS)


def mask_database_url(database_url: str) -> str:
    """Return `database_url` with the password replaced by ``***``.

    Every DSN that reaches a log line must go through this first. A URL like
    ``postgresql+psycopg2://user:s3cret@db.example.com:5432/flight`` becomes
    ``postgresql+psycopg2://user:***@db.example.com:5432/flight``, so the host
    and database stay readable — which is the part that's useful when
    diagnosing "why did it connect *there*".

    Parsing rather than string-splitting matters: ``url.split("@")[0]`` keeps
    the password and throws away the host, and ``split("@")[-1]`` silently
    drops the whole userinfo when the password itself contains an ``@``.

    Args:
        database_url: A SQLAlchemy URL, or any string that looks like one.

    Returns:
        The URL with its password masked. SQLite URLs carry no credentials and
        are returned unchanged. A URL that cannot be parsed is reduced to just
        its scheme, because leaking it would be worse than losing the detail.
    """
    if not database_url:
        return database_url

    try:
        parts = urlsplit(database_url)
    except ValueError:
        scheme, sep, _ = database_url.partition("://")
        return f"{scheme}{sep}***" if sep else "***"

    if parts.password is None:
        return database_url

    userinfo = f"{parts.username or ''}:***"
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{userinfo}@{host}"))


class DatabaseManager:
    """Engine + session factory + delegating facade over repositories."""

    def __init__(self, database_url: str = "sqlite:///aircraft_data.db") -> None:
        self.database_url = self._normalize_url(database_url)
        self.is_postgres = self.database_url.startswith("postgresql")
        self.is_mysql = self.database_url.startswith("mysql")
        self.is_sqlite = self.database_url.startswith("sqlite")

        # --- Stage safety guard -------------------------------------------
        # When STAGE=local, refuse to connect to a production-looking DB.
        # This catches the very easy mistake of a single shared .env with a
        # prod DATABASE_URL that leaks into dev runs. Set
        # ALLOW_PROD_DB_FROM_LOCAL=1 to override intentionally.
        stage = os.environ.get("STAGE", "").lower()
        override = os.environ.get("ALLOW_PROD_DB_FROM_LOCAL", "").lower() in {
            "1",
            "true",
            "yes",
        }
        if stage == "local" and _looks_like_prod_host(self.database_url) and not override:
            raise RuntimeError(
                f"Refusing to connect to what looks like a production database "
                f"while STAGE=local.\n"
                f"  URL host contains one of: {', '.join(_PROD_HOST_MARKERS)}\n"
                f"  Either unset/change STAGE, or move the prod credentials out of\n"
                f"  the local env file (use `.env.prod` instead of `.env`).\n"
                f"  To override deliberately, set ALLOW_PROD_DB_FROM_LOCAL=1."
            )

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
        logger.info(f"SQL database initialized: {mask_database_url(self.database_url)}")

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

        # Scraper and xiaohongshu tables are not ORM-declared; they are raw
        # DDL that historically only ran via `TaskQueue.ensure_tables_exist`
        # (scraper worker startup) or production migrations. Admin pages
        # read from them, so bootstrap them here too.
        session = self.SessionLocal()
        try:
            ensure_scraper_tables(session, is_postgres=self.is_postgres)
            ensure_xiaohongshu_tables(session, is_postgres=self.is_postgres)
        finally:
            session.close()

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

    def execute_filter_query(
        self,
        where_clause: str,
        limit: int = 1000,
        params: dict[str, Any] | None = None,
    ) -> list[dict]:
        return self._snapshots.execute_filter_query(where_clause, limit, params)

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
