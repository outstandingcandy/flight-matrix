"""Sink for the ADS-B Exchange map scraper.

The table is injected rather than hard-coded because the same rows serve two
retention policies. ``adsbx_military_positions`` is the original military-only
table; ``adsbx_positions`` holds the full fleet once ``military_only`` is off
(see ``scraper.scrapers.adsbx_map.target`` in ``config/scraper/fr24.yaml``).
Column set and upsert are identical — only the volume and the cleanup horizon
differ, so parameterising the name keeps one copy of the schema.

Deliberately *not* the same path as :class:`ADSBxSnapshotsSink`: that one writes
``aircraft_snapshots``, which stores the whole source dict again in a
``raw_data`` JSON column and bootstraps an ``aircraft_static_info`` row per
unseen registration. Both are affordable for ~150 military rows per region and
neither is at full-fleet volume.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    # Annotation-only, so `prune_positions` below is importable by the task
    # scheduler — a process that has no other reason to load the submodule.
    from resilient_scraper.models import ScraperTask
    from resilient_scraper.scrapers.aviation.adsbx_map.models import ADSBxMapResult

logger = logging.getLogger("scraper.sinks.adsbx_map")

# The military-only table this sink originally owned, kept as the default so
# existing deployments and callers behave exactly as before.
MILITARY_TABLE = "adsbx_military_positions"

# The full-fleet table, written when the scraper's military filter is off.
POSITIONS_TABLE = "adsbx_positions"

# Columns written for every row, in one place so the CREATE, the INSERT column
# list, the VALUES placeholders and the DO UPDATE assignments cannot drift.
_COLUMNS: tuple[str, ...] = (
    "hex",
    "flight",
    "registration",
    "aircraft_type",
    "type_description",
    "latitude",
    "longitude",
    "altitude_baro",
    "altitude_geom",
    "ground_speed",
    "track",
    "heading",
    "vertical_rate",
    "squawk",
    "category",
    "emergency",
    "db_flags",
    "mil",
    "on_ground",
    "country",
    "messages",
    "rssi",
    "seen_pos",
    "feed_timestamp",
    "scraped_at",
    "scrape_task_key",
)

# `hex` and `feed_timestamp` are the conflict target and `scrape_task_key`
# records which region won the race, so neither is overwritten on conflict.
_UPDATE_COLUMNS: tuple[str, ...] = tuple(
    c for c in _COLUMNS if c not in ("hex", "feed_timestamp", "scrape_task_key")
)


class ADSBxMapSink:
    """Persists ``ADSBxMapResult.aircraft`` to an ADSBx positions table.

    Args:
        database_url: SQLAlchemy URL. An empty value disables the sink.
        table: Destination table. Defaults to the military-only table so the
            original behaviour is unchanged; pass :data:`POSITIONS_TABLE` for
            full-fleet capture.
    """

    def __init__(self, database_url: str, table: str = MILITARY_TABLE) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            # The name is interpolated into DDL and DML, so it can never come
            # from anywhere but a constant in this module.
            raise ValueError(f"Invalid table name: {table!r}")
        self.table = table
        self.db_engine: Any | None = None
        if database_url:
            try:
                self.db_engine = create_engine(database_url, echo=False, pool_pre_ping=True)
                self._ensure_table_exists()
            except Exception as e:
                logger.error(f"Failed to initialize DB engine: {e}")

    def _ensure_table_exists(self) -> None:
        if not self.db_engine:
            return

        is_postgres = self.db_engine.dialect.name == "postgresql"
        if is_postgres:
            pk = "SERIAL PRIMARY KEY"
            ts = "TIMESTAMP WITH TIME ZONE"
            ts_default = "TIMESTAMP WITH TIME ZONE DEFAULT NOW()"
            dbl = "DOUBLE PRECISION"
        else:
            pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
            ts = "DATETIME"
            ts_default = "DATETIME DEFAULT CURRENT_TIMESTAMP"
            dbl = "REAL"

        # Index names are per-table: two tables share this schema and Postgres
        # index names are database-wide.
        ix = "adsbx_mil" if self.table == MILITARY_TABLE else self.table

        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.table} (
            id {pk},
            hex VARCHAR(10),
            flight VARCHAR(16),
            registration VARCHAR(16),
            aircraft_type VARCHAR(8),
            type_description VARCHAR(128),
            latitude {dbl},
            longitude {dbl},
            altitude_baro INTEGER,
            altitude_geom INTEGER,
            ground_speed {dbl},
            track {dbl},
            heading {dbl},
            vertical_rate INTEGER,
            squawk VARCHAR(8),
            category VARCHAR(4),
            emergency VARCHAR(16),
            db_flags INTEGER,
            mil BOOLEAN DEFAULT FALSE,
            on_ground BOOLEAN DEFAULT FALSE,
            country VARCHAR(64),
            messages INTEGER,
            rssi {dbl},
            seen_pos {dbl},
            feed_timestamp {ts},
            scraped_at {ts_default},
            scrape_task_key VARCHAR(64),
            UNIQUE (hex, feed_timestamp)
        );

        CREATE INDEX IF NOT EXISTS idx_{ix}_hex
            ON {self.table}(hex);
        CREATE INDEX IF NOT EXISTS idx_{ix}_scraped_at
            ON {self.table}(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_{ix}_mil_scraped
            ON {self.table}(mil, scraped_at);
        CREATE INDEX IF NOT EXISTS idx_{ix}_registration
            ON {self.table}(registration);
        """

        try:
            with self.db_engine.connect() as conn:
                for statement in create_sql.strip().split(";"):
                    if statement.strip():
                        conn.execute(text(statement))
                conn.commit()
        except SQLAlchemyError as e:
            logger.error(f"Failed to create {self.table}: {e}")
            return

        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Add columns introduced after a deployment first created the table.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op against a table that already
        exists, so a new column never reaches an existing deployment and every
        INSERT then fails on the unknown name. ``seen_pos`` is the first such
        column; adding it is additive and nullable, so it is safe to attempt on
        every startup.
        """
        if not self.db_engine:
            return

        existing = self._existing_columns()
        if not existing:
            # Could not read the schema; assume the CREATE above is authoritative
            # rather than issuing ALTERs blindly.
            return

        for column, ddl_type in (("seen_pos", "DOUBLE PRECISION"),):
            if column in existing:
                continue
            if self.db_engine.dialect.name != "postgresql":
                ddl_type = "REAL"
            try:
                with self.db_engine.connect() as conn:
                    conn.execute(text(f"ALTER TABLE {self.table} ADD COLUMN {column} {ddl_type}"))
                    conn.commit()
                logger.info(f"Added {self.table}.{column}")
            except SQLAlchemyError as e:
                logger.error(f"Failed to add {self.table}.{column}: {e}")

    def _existing_columns(self) -> set[str]:
        """Return the destination table's current column names, or an empty set."""
        if not self.db_engine:
            return set()
        try:
            from sqlalchemy import inspect as sa_inspect

            return {c["name"] for c in sa_inspect(self.db_engine).get_columns(self.table)}
        except SQLAlchemyError as e:
            logger.warning(f"Could not inspect {self.table}: {e}")
            return set()

    def on_success(self, task: ScraperTask, result: ADSBxMapResult) -> None:
        if not self.db_engine or not result.aircraft:
            return

        now = datetime.now(UTC)
        batch: list[dict[str, Any]] = []
        for ac in result.aircraft:
            if not ac.hex:
                continue
            batch.append(
                {
                    "hex": ac.hex,
                    "flight": ac.flight,
                    "registration": ac.registration,
                    "aircraft_type": ac.aircraft_type,
                    "type_description": ac.type_description,
                    "latitude": ac.latitude,
                    "longitude": ac.longitude,
                    "altitude_baro": ac.altitude_baro,
                    "altitude_geom": ac.altitude_geom,
                    "ground_speed": ac.ground_speed,
                    "track": ac.track,
                    "heading": ac.heading,
                    "vertical_rate": ac.vertical_rate,
                    "squawk": ac.squawk,
                    "category": ac.category,
                    "emergency": ac.emergency,
                    "db_flags": ac.db_flags,
                    "mil": ac.mil,
                    "on_ground": ac.on_ground,
                    "country": ac.country,
                    "messages": ac.messages,
                    "rssi": ac.rssi,
                    "seen_pos": ac.seen_pos,
                    "feed_timestamp": ac.timestamp or now,
                    "scraped_at": now,
                    "scrape_task_key": result.task_key,
                }
            )

        if not batch:
            return

        try:
            with self.db_engine.connect() as conn:
                conn.execute(text(self._upsert_sql()), batch)
                conn.commit()
            logger.info(
                f"[{task.task_key}] Saved {len(batch)}/{len(result.aircraft)} "
                f"ADSBx positions to {self.table}"
            )
        except SQLAlchemyError as e:
            logger.error(f"[{task.task_key}] Failed to save positions to {self.table}: {e}")

    def _upsert_sql(self) -> str:
        """Build the upsert from :data:`_COLUMNS` so the three lists cannot drift."""
        columns = ", ".join(_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        assignments = ",\n                    ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATE_COLUMNS)
        return f"""
            INSERT INTO {self.table} ({columns})
            VALUES ({placeholders})
            ON CONFLICT (hex, feed_timestamp)
            DO UPDATE SET
                    {assignments}
        """

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        # No DB actions on failure — the worker handles retries/status.
        pass


# Rows deleted per statement, and the most statements one prune call will issue.
# A full-fleet pass writes ~38,000 rows, so 500,000 per call keeps the table
# shrinking faster than it grows while leaving the scheduler's cycle responsive
# and, on Postgres, keeping each DELETE's lock footprint and WAL burst bounded.
_PRUNE_BATCH = 10_000
_PRUNE_MAX_BATCHES = 50


def prune_positions(
    engine: Any,
    table: str,
    *,
    civil_hours: int,
    military_hours: int,
) -> int:
    """Delete rows from an ADSBx positions table past their retention horizon.

    Military and civil rows expire on different clocks: military is roughly 3% of
    the volume and the reason the pipeline exists, so it is kept far longer than
    the civil bulk. Passing ``0`` or a negative value for either horizon disables
    that half.

    Deletion is batched by primary key rather than issued as one statement. A
    single ``DELETE`` covering a day of full-fleet rows would hold locks and
    generate WAL for the whole scan; ``id IN (SELECT id ... LIMIT n)`` is the one
    bounded form both Postgres and SQLite accept.

    Args:
        engine: SQLAlchemy engine for the database holding ``table``.
        table: Destination table, which must be one of this module's constants.
        civil_hours: Hours to keep rows whose ``mil`` flag is false or null.
        military_hours: Hours to keep rows whose ``mil`` flag is true.

    Returns:
        Total rows deleted. Zero if nothing had expired, or if the table does not
        exist yet.

    Raises:
        ValueError: If ``table`` is not a plain identifier.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(f"Invalid table name: {table!r}")

    now = datetime.now(UTC)
    deleted = 0
    for hours, is_mil, label in (
        (civil_hours, False, "civil"),
        (military_hours, True, "military"),
    ):
        if hours <= 0:
            continue
        cutoff = now - timedelta(hours=hours)
        # `mil` is nullable, and a null flag is not a military aircraft — without
        # the IS NULL arm those rows would never expire under either clock.
        predicate = "mil = :is_mil" if is_mil else "(mil IS NULL OR mil = :is_mil)"
        sql = text(
            f"DELETE FROM {table} WHERE id IN ("
            f"  SELECT id FROM {table} WHERE scraped_at < :cutoff AND {predicate}"
            f"  LIMIT {_PRUNE_BATCH})"
        )
        removed = 0
        try:
            for _ in range(_PRUNE_MAX_BATCHES):
                with engine.connect() as conn:
                    result = conn.execute(sql, {"cutoff": cutoff, "is_mil": is_mil})
                    conn.commit()
                if not result.rowcount:
                    break
                removed += result.rowcount
                if result.rowcount < _PRUNE_BATCH:
                    break
            else:
                logger.warning(
                    f"Hit the {_PRUNE_MAX_BATCHES}-batch cap pruning {label} rows from "
                    f"{table}; {removed} deleted, more remain for the next cycle"
                )
        except SQLAlchemyError as e:
            logger.error(f"Failed to prune {label} rows from {table}: {e}")
            continue
        if removed:
            logger.info(f"Pruned {removed} {label} rows older than {hours}h from {table}")
        deleted += removed

    return deleted
