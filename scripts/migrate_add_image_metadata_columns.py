#!/usr/bin/env python3
"""Add the five JetPhotos metadata columns that were missing from the model.

`src/scraper/sinks/jetphotos_sink.py` has always written `camera`, `views`,
`likes`, `badges` and `html_s3_path` into `aircraft_images`, but the
`AircraftImage` model did not declare them. Databases created by
`Base.metadata.create_all()` -- which is what `scripts/gcp/deploy-app.sh` runs on
a fresh host -- therefore had no such columns, and every JetPhotos insert failed
on them. The model now declares all five, so new databases are correct; this
script brings existing ones into line.

Safe to re-run: each column is added only if the table does not already have it,
which is also why this uses `inspect()` rather than `ADD COLUMN IF NOT EXISTS`
(SQLite does not support that form).

Usage:
    python scripts/migrate_add_image_metadata_columns.py
    python scripts/migrate_add_image_metadata_columns.py --dry-run
    python scripts/migrate_add_image_metadata_columns.py --config config/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, inspect, text  # noqa: E402 - after sys.path setup

from src.utils.yaml_config import YAMLConfig  # noqa: E402 - after sys.path setup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TABLE = "aircraft_images"

# Column name -> DDL type. Deliberately spelled with types both PostgreSQL and
# SQLite accept, and kept in step with `AircraftImage` in src/data/models.py.
NEW_COLUMNS: dict[str, str] = {
    "camera": "VARCHAR(200)",
    "views": "INTEGER",
    "likes": "INTEGER",
    "badges": "TEXT",
    "html_s3_path": "VARCHAR(500)",
}


def run_migration(database_url: str, dry_run: bool = False) -> bool:
    """Add any missing metadata columns to `aircraft_images`.

    Args:
        database_url: SQLAlchemy database URL.
        dry_run: Report what would change without altering the schema.

    Returns:
        True on success, False if the table is missing or a statement failed.
    """
    engine = create_engine(database_url)
    inspector = inspect(engine)

    if TABLE not in inspector.get_table_names():
        logger.error(
            f"Table '{TABLE}' does not exist. Run scripts/migrate_create_aircraft_images.py first."
        )
        return False

    existing = {column["name"] for column in inspector.get_columns(TABLE)}
    missing = {name: ddl for name, ddl in NEW_COLUMNS.items() if name not in existing}

    present = sorted(set(NEW_COLUMNS) & existing)
    if present:
        logger.info(f"Already present, leaving alone: {', '.join(present)}")

    if not missing:
        logger.info("Nothing to do — all five columns are already there.")
        return True

    if dry_run:
        for name, ddl in missing.items():
            logger.info(f"Would add: ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}")
        return True

    with engine.connect() as conn:
        for name, ddl in missing.items():
            logger.info(f"Adding column {name} {ddl}")
            conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}"))
        conn.commit()

    # Re-inspect rather than trusting the statements: a dialect that silently
    # ignores part of the DDL would otherwise be reported as a success.
    still_missing = sorted(
        set(NEW_COLUMNS) - {c["name"] for c in inspect(engine).get_columns(TABLE)}
    )
    if still_missing:
        logger.error(f"These columns are still absent after the migration: {still_missing}")
        return False

    logger.info(f"Added {len(missing)} column(s): {', '.join(sorted(missing))}")
    return True


def main() -> None:
    """Parse arguments, resolve the database URL and run the migration."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the root config file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the columns that would be added without changing anything",
    )
    args = parser.parse_args()

    config = YAMLConfig(args.config)
    database_url = config.get("database", {}).get("url", "")
    if not database_url:
        logger.error(f"No database.url in {args.config}")
        sys.exit(1)

    from src.data.db_manager import mask_database_url

    logger.info(f"Target database: {mask_database_url(database_url)}")

    if not run_migration(database_url, dry_run=args.dry_run):
        sys.exit(1)

    logger.info("Dry run complete — nothing changed." if args.dry_run else "Migration complete.")


if __name__ == "__main__":
    main()
