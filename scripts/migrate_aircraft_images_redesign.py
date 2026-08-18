#!/usr/bin/env python3
"""
Migration script for aircraft_images table redesign.

This migration:
1. Adds new columns to aircraft_images: aircraft_id (FK), display_order, is_primary, notes
2. Creates new indexes for efficient queries
3. Backfills aircraft_id from aircraft_static_info
4. Sets display_order and is_primary values based on existing data

Usage:
    python scripts/migrate_aircraft_images_redesign.py [--config config/config.yaml]
    python scripts/migrate_aircraft_images_redesign.py --dry-run  # Preview changes
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


ADD_COLUMNS_SQL = [
    # Add aircraft_id column (FK to aircraft_static_info)
    """
    ALTER TABLE aircraft_images
    ADD COLUMN IF NOT EXISTS aircraft_id INTEGER;
    """,
    # Add display_order column for ordering images
    """
    ALTER TABLE aircraft_images
    ADD COLUMN IF NOT EXISTS display_order INTEGER DEFAULT 1;
    """,
    # Add is_primary column to mark the main image
    """
    ALTER TABLE aircraft_images
    ADD COLUMN IF NOT EXISTS is_primary BOOLEAN DEFAULT false;
    """,
    # Add notes column if it doesn't exist (should exist from original migration)
    """
    ALTER TABLE aircraft_images
    ADD COLUMN IF NOT EXISTS notes TEXT;
    """,
]

CREATE_INDEXES_SQL = [
    # Index for FK lookups
    "CREATE INDEX IF NOT EXISTS idx_images_aircraft_id ON aircraft_images(aircraft_id);",
    # Composite index for registration + display_order queries
    "CREATE INDEX IF NOT EXISTS idx_images_reg_order ON aircraft_images(registration, display_order);",
    # Index for primary image lookup
    "CREATE INDEX IF NOT EXISTS idx_images_primary ON aircraft_images(registration, is_primary) WHERE is_primary = true;",
]

ADD_FOREIGN_KEY_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_aircraft_images_aircraft_id'
        AND table_name = 'aircraft_images'
    ) THEN
        ALTER TABLE aircraft_images
        ADD CONSTRAINT fk_aircraft_images_aircraft_id
        FOREIGN KEY (aircraft_id)
        REFERENCES aircraft_static_info(id)
        ON DELETE SET NULL;
    END IF;
END $$;
"""

BACKFILL_AIRCRAFT_ID_SQL = """
UPDATE aircraft_images ai
SET aircraft_id = asi.id
FROM aircraft_static_info asi
WHERE ai.registration = asi.registration
AND ai.aircraft_id IS NULL;
"""

SET_DISPLAY_ORDER_SQL = """
WITH ordered_images AS (
    SELECT
        id,
        registration,
        ROW_NUMBER() OVER (
            PARTITION BY registration
            ORDER BY
                CASE WHEN is_primary THEN 0 ELSE 1 END,
                created_at ASC,
                id ASC
        ) as new_order
    FROM aircraft_images
)
UPDATE aircraft_images ai
SET display_order = oi.new_order
FROM ordered_images oi
WHERE ai.id = oi.id
AND (ai.display_order IS NULL OR ai.display_order != oi.new_order);
"""

SET_PRIMARY_IMAGE_SQL = """
WITH first_images AS (
    SELECT DISTINCT ON (registration) id
    FROM aircraft_images
    ORDER BY registration, display_order ASC, created_at ASC, id ASC
)
UPDATE aircraft_images
SET is_primary = (id IN (SELECT id FROM first_images));
"""


def check_column_exists(conn, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    result = conn.execute(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = :table AND column_name = :column
            );
        """),
        {"table": table, "column": column},
    )
    return result.scalar()


def run_migration(database_url: str, dry_run: bool = False) -> bool:
    """Run the migration to add new columns and indexes.

    Args:
        database_url: Database connection URL.
        dry_run: If True, only show what would be done.

    Returns:
        True if migration successful, False otherwise.
    """
    try:
        engine = create_engine(database_url)

        with engine.connect() as conn:
            # Check if aircraft_images table exists
            result = conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = 'aircraft_images'
                    );
                """)
            )
            if not result.scalar():
                logger.error(
                    "Table 'aircraft_images' does not exist. Run migrate_create_aircraft_images.py first."
                )
                return False

            # Get current state
            count_result = conn.execute(text("SELECT COUNT(*) FROM aircraft_images"))
            total_images = count_result.scalar()
            logger.info(f"Found {total_images} images in aircraft_images table")

            # Check existing columns
            has_aircraft_id = check_column_exists(conn, "aircraft_images", "aircraft_id")
            has_display_order = check_column_exists(conn, "aircraft_images", "display_order")
            has_is_primary = check_column_exists(conn, "aircraft_images", "is_primary")
            has_notes = check_column_exists(conn, "aircraft_images", "notes")

            logger.info(
                f"Column status: aircraft_id={has_aircraft_id}, display_order={has_display_order}, "
                f"is_primary={has_is_primary}, notes={has_notes}"
            )

            if dry_run:
                logger.info("=== DRY RUN MODE - No changes will be made ===")
                if not has_aircraft_id:
                    logger.info("Would add column: aircraft_id INTEGER")
                if not has_display_order:
                    logger.info("Would add column: display_order INTEGER DEFAULT 1")
                if not has_is_primary:
                    logger.info("Would add column: is_primary BOOLEAN DEFAULT false")
                if not has_notes:
                    logger.info("Would add column: notes TEXT")
                logger.info(
                    "Would create indexes: idx_images_aircraft_id, idx_images_reg_order, idx_images_primary"
                )
                logger.info("Would add foreign key: fk_aircraft_images_aircraft_id")
                logger.info("Would backfill aircraft_id from aircraft_static_info")
                logger.info("Would set display_order based on creation time")
                logger.info("Would set is_primary for first image of each registration")
                return True

            # Step 1: Add columns
            logger.info("Step 1: Adding new columns...")
            for sql in ADD_COLUMNS_SQL:
                conn.execute(text(sql))
            conn.commit()
            logger.info("Columns added successfully")

            # Step 2: Create indexes
            logger.info("Step 2: Creating indexes...")
            for sql in CREATE_INDEXES_SQL:
                try:
                    conn.execute(text(sql))
                except Exception as e:
                    logger.debug(f"Index may already exist: {e}")
            conn.commit()
            logger.info("Indexes created successfully")

            # Step 3: Add foreign key constraint
            logger.info("Step 3: Adding foreign key constraint...")
            try:
                conn.execute(text(ADD_FOREIGN_KEY_SQL))
                conn.commit()
                logger.info("Foreign key constraint added")
            except Exception as e:
                logger.warning(f"Foreign key may already exist or failed: {e}")

            # Step 4: Backfill aircraft_id
            logger.info("Step 4: Backfilling aircraft_id from aircraft_static_info...")
            result = conn.execute(text(BACKFILL_AIRCRAFT_ID_SQL))
            conn.commit()
            logger.info(f"Backfilled aircraft_id for {result.rowcount} images")

            # Step 5: Set display_order
            logger.info("Step 5: Setting display_order for all images...")
            result = conn.execute(text(SET_DISPLAY_ORDER_SQL))
            conn.commit()
            logger.info(f"Updated display_order for {result.rowcount} images")

            # Step 6: Set is_primary
            logger.info("Step 6: Setting is_primary flag...")
            result = conn.execute(text(SET_PRIMARY_IMAGE_SQL))
            conn.commit()
            logger.info("Updated is_primary flags")

            # Verify results
            logger.info("=== Migration Complete - Verification ===")

            # Count images with aircraft_id set
            result = conn.execute(
                text("SELECT COUNT(*) FROM aircraft_images WHERE aircraft_id IS NOT NULL")
            )
            linked_count = result.scalar()
            logger.info(f"Images linked to aircraft_static_info: {linked_count}/{total_images}")

            # Count primary images
            result = conn.execute(
                text("SELECT COUNT(*) FROM aircraft_images WHERE is_primary = true")
            )
            primary_count = result.scalar()
            logger.info(f"Primary images marked: {primary_count}")

            # Count unique registrations
            result = conn.execute(text("SELECT COUNT(DISTINCT registration) FROM aircraft_images"))
            reg_count = result.scalar()
            logger.info(f"Unique registrations with images: {reg_count}")

            # Sample output
            result = conn.execute(
                text("""
                    SELECT registration, aircraft_id, display_order, is_primary,
                           LEFT(image_path, 50) as image_path_short
                    FROM aircraft_images
                    ORDER BY registration, display_order
                    LIMIT 10
                """)
            )
            logger.info("Sample data:")
            for row in result:
                logger.info(f"  {row[0]}: order={row[2]}, primary={row[3]}, aircraft_id={row[1]}")

            return True

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate aircraft_images table with new columns for redesign"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config/config.yaml",
        help="Path to config file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without making them",
    )
    args = parser.parse_args()

    logger.info("Starting aircraft_images table redesign migration...")

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    database_url = config.get("database", {}).get("url", "")

    if not database_url:
        logger.error("Database URL not found in config")
        sys.exit(1)

    success = run_migration(database_url, dry_run=args.dry_run)

    if success:
        if args.dry_run:
            logger.info("Dry run complete - no changes made")
        else:
            logger.info("Migration completed successfully!")
    else:
        logger.error("Migration failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
