#!/usr/bin/env python3
"""Migrate the Aurora/Postgres queue schema to the submodule-unified layout.

The submodule Worker relies on two pieces that the older flight-matrix
schema did not have:

  1. A partial unique index on ``scraper_tasks(task_type, task_key)`` limited
     to active rows (pending/claimed/processing/login_required). The submodule
     uses ``INSERT … ON CONFLICT DO NOTHING`` against this index to dedupe
     tasks at the DB layer; without it, add_task() either can't execute or
     races with concurrent workers.

  2. Two auxiliary tables — ``scraper_screenshots`` and
     ``scraper_user_inputs`` — used by login-aware scrapers (Xiaohongshu) to
     persist QR-code screenshots and relay SMS verification codes through
     the Feishu bot.

The migration is **additive and non-destructive**: it never drops existing
rows, columns, or indexes. Run it before deploying the step-6 worker
rewrite; the old worker keeps working against a post-migration schema too.

Usage:
    python scripts/migrate_scraper_unified.py [--config config/config.yaml]
                                                [--dry-run]

Environment:
    DATABASE_URL — overrides the config-file URL if set.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure project root is importable when running this script directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("migrate_scraper_unified")


# ---------------------------------------------------------------------------
# Migration DDL
# ---------------------------------------------------------------------------

# Partial unique index for DB-level deduping. Postgres only; SQLite does not
# support partial UNIQUE indexes with the same semantics (and flight-matrix
# local dev doesn't need the optimisation either).
UNIQUE_INDEX_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_scraper_tasks_type_key_active'
    ) THEN
        CREATE UNIQUE INDEX idx_scraper_tasks_type_key_active
            ON scraper_tasks (task_type, task_key)
            WHERE status IN ('pending', 'claimed', 'processing', 'login_required');
    END IF;
END $$;
"""

SCREENSHOTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scraper_screenshots (
    id             BIGSERIAL PRIMARY KEY,
    task_id        BIGINT NOT NULL REFERENCES scraper_tasks(id) ON DELETE CASCADE,
    data           BYTEA,
    screenshot_url VARCHAR(1000),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

SCREENSHOTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_scraper_screenshots_task
    ON scraper_screenshots (task_id);
"""

USER_INPUTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scraper_user_inputs (
    id         BIGSERIAL PRIMARY KEY,
    task_id    BIGINT NOT NULL REFERENCES scraper_tasks(id) ON DELETE CASCADE,
    value      TEXT NOT NULL,
    consumed   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

USER_INPUTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_scraper_user_inputs_task
    ON scraper_user_inputs (task_id, consumed);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_database_url(config_path: str | None) -> str:
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    if config_path is None:
        raise RuntimeError(
            "DATABASE_URL not set and no --config provided"
        )
    from src.utils.yaml_config import YAMLConfig

    cfg = YAMLConfig(config_path)
    return cfg.get_database_config().get("url", "")


def check_existing(engine) -> dict[str, bool]:
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    result = {
        "scraper_tasks": "scraper_tasks" in existing_tables,
        "scraper_screenshots": "scraper_screenshots" in existing_tables,
        "scraper_user_inputs": "scraper_user_inputs" in existing_tables,
        "idx_scraper_tasks_type_key_active": False,
    }
    if result["scraper_tasks"]:
        for idx in insp.get_indexes("scraper_tasks"):
            if idx.get("name") == "idx_scraper_tasks_type_key_active":
                result["idx_scraper_tasks_type_key_active"] = True
                break
    return result


def run_migration(engine, dry_run: bool = False) -> None:
    is_postgres = engine.dialect.name == "postgresql"
    if not is_postgres:
        logger.warning(
            "Non-Postgres dialect detected (%s). The partial unique index "
            "is Postgres-only; skipping it. scraper_screenshots / "
            "scraper_user_inputs tables will still be created.",
            engine.dialect.name,
        )

    existing = check_existing(engine)
    logger.info("Pre-migration state:")
    for key, present in existing.items():
        logger.info("  %s: %s", key, "present" if present else "missing")

    if not existing["scraper_tasks"]:
        raise RuntimeError(
            "scraper_tasks is missing — run the base scraper migration first "
            "(e.g. `python scripts/migrate_scraper_tables.py`) before this one."
        )

    statements: list[tuple[str, str]] = []

    if not existing["scraper_screenshots"]:
        statements.append(("scraper_screenshots table", SCREENSHOTS_TABLE_SQL))
        statements.append(("scraper_screenshots index", SCREENSHOTS_INDEX_SQL))
    if not existing["scraper_user_inputs"]:
        statements.append(("scraper_user_inputs table", USER_INPUTS_TABLE_SQL))
        statements.append(("scraper_user_inputs index", USER_INPUTS_INDEX_SQL))
    if is_postgres and not existing["idx_scraper_tasks_type_key_active"]:
        statements.append(("scraper_tasks partial unique index", UNIQUE_INDEX_SQL))

    if not statements:
        logger.info("Nothing to do — schema already up to date.")
        return

    for label, sql in statements:
        logger.info(
            "%s %s",
            "[DRY-RUN] would run:" if dry_run else "Running:",
            label,
        )
        if not dry_run:
            with engine.begin() as conn:
                for stmt in sql.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        conn.execute(text(stmt))
            logger.info("  done")

    if dry_run:
        logger.info("Dry run complete; no changes applied.")
    else:
        logger.info("Migration complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        "-c",
        default="config/config.yaml",
        help="Config YAML (used to look up DATABASE_URL if the env var isn't set)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the SQL that would run without executing it.",
    )
    args = parser.parse_args()

    database_url = get_database_url(args.config)
    if not database_url:
        logger.error("Database URL not found. Set DATABASE_URL or supply --config.")
        return 1

    logger.info(
        "Connecting to %s",
        # Mask credentials when echoing the URL
        database_url.split("@")[-1] if "@" in database_url else database_url,
    )
    engine = create_engine(database_url, echo=False, pool_pre_ping=True)
    try:
        run_migration(engine, dry_run=args.dry_run)
    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
