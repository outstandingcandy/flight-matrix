#!/usr/bin/env python3
"""
Database migration script to add image_path_4 through image_path_10 columns.

This script adds the new image path columns to aircraft_static_info table.

Usage:
    python scripts/migrate_add_image_columns.py --config config.yaml
"""

import argparse
import logging
from sqlalchemy import create_engine, text

from src.utils.yaml_config import YAMLConfig

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def migrate_table(engine, table_name: str) -> None:
    """Add image_path_4 through image_path_10 columns to a table.

    Args:
        engine: SQLAlchemy engine
        table_name: Name of the table to migrate
    """
    columns_to_add = [
        f"image_path_{i}" for i in range(4, 11)
    ]

    with engine.connect() as conn:
        for column_name in columns_to_add:
            try:
                # Check if column exists
                check_sql = text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = :table_name
                    AND column_name = :column_name
                """)
                result = conn.execute(check_sql, {
                    "table_name": table_name,
                    "column_name": column_name
                }).fetchone()

                if result:
                    logger.info(f"Column {column_name} already exists in {table_name}")
                    continue

                # Add column
                alter_sql = text(f"""
                    ALTER TABLE {table_name}
                    ADD COLUMN {column_name} VARCHAR(500)
                """)
                conn.execute(alter_sql)
                conn.commit()
                logger.info(f"Added column {column_name} to {table_name}")

            except Exception as e:
                logger.error(f"Error adding {column_name} to {table_name}: {e}")
                conn.rollback()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Add image_path_4-10 columns to database tables'
    )
    parser.add_argument(
        '--config', '-c',
        default='config/config.yaml',
        help='Config file path'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )

    args = parser.parse_args()

    # Load config
    config = YAMLConfig(args.config)
    db_url = config.get_database_config()['url']

    if args.dry_run:
        logger.info("DRY RUN - No changes will be made")
        logger.info("Would migrate table: aircraft_static_info")
        logger.info("Would add columns: image_path_4, image_path_5, ..., image_path_10")
        return

    # Create engine
    engine = create_engine(db_url, echo=False)

    logger.info("Starting database migration...")

    # Migrate aircraft_static_info
    logger.info("Migrating aircraft_static_info table...")
    migrate_table(engine, "aircraft_static_info")

    logger.info("Migration complete!")


if __name__ == "__main__":
    main()
