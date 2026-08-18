#!/usr/bin/env python3
"""
Migration script to create the aircraft_images table.

This table stores detailed metadata for each aircraft image,
including photographer info, capture location, and dates.

Usage:
    python scripts/migrate_create_aircraft_images.py [--config config/config.yaml]
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS aircraft_images (
    id BIGSERIAL PRIMARY KEY,
    registration VARCHAR(20) NOT NULL,

    -- Image storage info
    image_path VARCHAR(500) NOT NULL,
    source_url VARCHAR(500),
    source VARCHAR(50) DEFAULT 'jetphotos',

    -- Photo metadata (extracted from JetPhotos)
    photographer VARCHAR(200),
    photo_date DATE,
    upload_date DATE,
    location VARCHAR(200),
    airport_icao VARCHAR(4),
    airport_name VARCHAR(200),

    -- Image properties
    width INTEGER,
    height INTEGER,
    file_size_bytes INTEGER,

    -- Source-specific IDs (for deduplication)
    jetphotos_id VARCHAR(20) UNIQUE,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_image_registration ON aircraft_images(registration);",
    "CREATE INDEX IF NOT EXISTS idx_image_source ON aircraft_images(source);",
    "CREATE INDEX IF NOT EXISTS idx_image_photographer ON aircraft_images(photographer);",
    "CREATE INDEX IF NOT EXISTS idx_image_photo_date ON aircraft_images(photo_date);",
    "CREATE INDEX IF NOT EXISTS idx_image_airport ON aircraft_images(airport_icao);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_image_jetphotos_id ON aircraft_images(jetphotos_id);",
]


def run_migration(database_url: str) -> bool:
    """Run the migration to create aircraft_images table.

    Args:
        database_url: Database connection URL.

    Returns:
        True if migration successful, False otherwise.
    """
    try:
        engine = create_engine(database_url)

        with engine.connect() as conn:
            # Check if table already exists
            result = conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = 'aircraft_images'
                    );
                """)
            )
            table_exists = result.scalar()

            if table_exists:
                logger.info("Table 'aircraft_images' already exists")

                # Check and add any missing indexes
                for index_sql in CREATE_INDEXES_SQL:
                    try:
                        conn.execute(text(index_sql))
                    except Exception as e:
                        logger.debug(f"Index may already exist: {e}")

                conn.commit()
                return True

            # Create table
            logger.info("Creating 'aircraft_images' table...")
            conn.execute(text(CREATE_TABLE_SQL))

            # Create indexes
            logger.info("Creating indexes...")
            for index_sql in CREATE_INDEXES_SQL:
                conn.execute(text(index_sql))

            conn.commit()
            logger.info("Migration completed successfully!")
            return True

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Create aircraft_images table migration")
    parser.add_argument(
        "--config",
        type=str,
        default="config/config.yaml",
        help="Path to config file (default: config/config.yaml)",
    )
    args = parser.parse_args()

    logger.info("Starting aircraft_images table migration...")

    # Load configuration
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

    success = run_migration(database_url)

    if success:
        logger.info("Migration completed!")

        # Show table info
        engine = create_engine(database_url)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM aircraft_images;"))
            count = result.scalar()
            logger.info(f"Table 'aircraft_images' now has {count} records")
    else:
        logger.error("Migration failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
