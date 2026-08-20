#!/usr/bin/env python3
"""Fill `aircraft_images.airport_icao` from the location text already stored.

The column was empty for every row ever scraped -- 0 of 1.36 M -- because the
extractor took the code from the `/airport/<slug>` href and required
`^[A-Z]{4}$`, which a slug never matches. The code was in the link text all
along and landed in `location` as "Beijing Capital - ZBAA". The extractor is
fixed (see `icao_from_airport_name` in the resilient-scraper submodule); this
script applies the same parse to the rows already there.

Parsing happens in Python, using that same function, so the backfill and the
scraper cannot drift -- and so this runs on SQLite as well as PostgreSQL, which a
`substring(location from '...')` statement would not.

Rows whose location carries no code keep an empty `airport_icao`: "Inflight",
museum entries and strips like "Breighton Airfield - EG10" have no ICAO, and
guessing one would be worse than leaving it out. Safe to re-run -- it only
considers rows that are still empty.

Usage:
    python scripts/migrate_backfill_image_airport_icao.py --dry-run
    python scripts/migrate_backfill_image_airport_icao.py
    python scripts/migrate_backfill_image_airport_icao.py --batch-size 20000
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from resilient_scraper.scrapers.aviation.jetphotos.extractor import (  # noqa: E402
    icao_from_airport_name,
)
from sqlalchemy import create_engine, inspect, text  # noqa: E402 - after sys.path setup
from sqlalchemy.engine import Connection  # noqa: E402 - after sys.path setup

from src.utils.yaml_config import YAMLConfig  # noqa: E402 - after sys.path setup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TABLE = "aircraft_images"
DEFAULT_BATCH_SIZE = 50_000

# Keyset pagination, not OFFSET: the rows being read are the rows being written,
# so an offset would skip over rows as the candidate set shrinks under it.
_SELECT_BATCH = text(
    f"""
    SELECT id, location
    FROM {TABLE}
    WHERE id > :after
      AND location IS NOT NULL
      AND location <> ''
      AND (airport_icao IS NULL OR airport_icao = '')
    ORDER BY id
    LIMIT :limit
    """
)

_UPDATE_ONE = text(f"UPDATE {TABLE} SET airport_icao = :icao WHERE id = :id")

_CREATE_INDEX = text(
    f"CREATE INDEX IF NOT EXISTS idx_image_airport_reg ON {TABLE} (airport_icao, registration)"
)


def backfill(conn: Connection, batch_size: int, dry_run: bool) -> Counter[str]:
    """Walk the table by id, writing a code for every location that carries one.

    Args:
        conn: Open connection to the target database.
        batch_size: Rows read per round, committed as one transaction.
        dry_run: Parse and count without writing.

    Returns:
        Counter with `scanned`, `updated` and `no_icao` totals.
    """
    totals: Counter[str] = Counter()
    after = 0

    while True:
        rows = conn.execute(_SELECT_BATCH, {"after": after, "limit": batch_size}).fetchall()
        if not rows:
            break

        after = rows[-1][0]
        updates = []
        for row_id, location in rows:
            icao = icao_from_airport_name(location)
            if icao:
                updates.append({"id": row_id, "icao": icao})

        totals["scanned"] += len(rows)
        totals["updated"] += len(updates)
        totals["no_icao"] += len(rows) - len(updates)

        if updates and not dry_run:
            conn.execute(_UPDATE_ONE, updates)
            conn.commit()

        logger.info(
            f"…{totals['scanned']:,} scanned, {totals['updated']:,} with a code "
            f"(through id {after:,})"
        )

    return totals


def sample_by_airport(conn: Connection, limit: int = 10) -> list[tuple[str, int]]:
    """Return the busiest airports as now recorded, for a sanity check.

    Args:
        conn: Open connection to the target database.
        limit: How many airports to report.

    Returns:
        `(icao, photo_count)` pairs, most photographed first.
    """
    result = conn.execute(
        text(
            f"""
            SELECT airport_icao, COUNT(*) AS photos
            FROM {TABLE}
            WHERE airport_icao IS NOT NULL AND airport_icao <> ''
            GROUP BY airport_icao
            ORDER BY photos DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [(row[0], row[1]) for row in result]


def run_migration(database_url: str, batch_size: int, dry_run: bool) -> bool:
    """Backfill the column and add the composite index the photo query needs.

    Args:
        database_url: SQLAlchemy database URL.
        batch_size: Rows per batch.
        dry_run: Report what would change without writing.

    Returns:
        True on success, False if the table is missing.
    """
    engine = create_engine(database_url)

    if TABLE not in inspect(engine).get_table_names():
        logger.error(f"Table '{TABLE}' does not exist — nothing to backfill.")
        return False

    with engine.connect() as conn:
        totals = backfill(conn, batch_size, dry_run)

        logger.info(
            f"{totals['scanned']:,} rows had a location and no code: "
            f"{totals['updated']:,} parsed, {totals['no_icao']:,} carry no ICAO"
        )

        if dry_run:
            logger.info("Would also run: " + _CREATE_INDEX.text)
            return True

        # After the data, so the index is built once over final values rather than
        # maintained across every batch.
        logger.info("Creating idx_image_airport_reg (airport_icao, registration)")
        conn.execute(_CREATE_INDEX)
        conn.commit()

        for icao, photos in sample_by_airport(conn):
            logger.info(f"  {icao}: {photos:,} photos")

    return True


def main() -> None:
    """Parse arguments, resolve the database URL and run the backfill."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the root config file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per committed batch (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would be filled without writing anything",
    )
    args = parser.parse_args()

    config = YAMLConfig(args.config)
    # A single dotted path, not config.get("database", {}).get("url", "") —
    # YAMLConfig.get() only resolves ${VAR} when the path leads directly to the
    # leaf string; fetching the parent dict and indexing into it in plain
    # Python bypasses interpolation and returns the literal "${DATABASE_URL}".
    database_url = config.get("database.url", "")
    if not database_url:
        logger.error(f"No database.url in {args.config}")
        sys.exit(1)

    from src.data.db_manager import mask_database_url

    logger.info(f"Target database: {mask_database_url(database_url)}")

    if not run_migration(database_url, max(1, args.batch_size), args.dry_run):
        sys.exit(1)

    logger.info("Dry run complete — nothing changed." if args.dry_run else "Backfill complete.")


if __name__ == "__main__":
    main()
