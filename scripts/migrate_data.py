#!/usr/bin/env python3
"""
Migrate data from SQLite database to PostgreSQL Aurora Serverless
Handles 404MB database with batching and progress tracking
"""

import os
import sys
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
import logging
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("data_migration")

# Batch size for migration
BATCH_SIZE = 1000  # Process 1000 rows at a time


def migrate_table_data(source_engine, target_engine, table_name):
    """
    Migrate data for a single table with batching and progress tracking

    Args:
        source_engine: SQLAlchemy engine for source database (SQLite)
        target_engine: SQLAlchemy engine for target database (PostgreSQL)
        table_name: Name of table to migrate
    """
    logger.info(f"Migrating table: {table_name}")
    start_time = datetime.now()

    try:
        # Get total row count
        with source_engine.connect() as conn:
            count_result = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
            count = count_result.scalar()
            logger.info(f"  Total rows to migrate: {count:,}")

        if count == 0:
            logger.info(f"  Table {table_name} is empty, skipping")
            return

        # Get column names
        inspector = inspect(source_engine)
        columns = [col["name"] for col in inspector.get_columns(table_name)]
        columns_str = ", ".join(columns)
        placeholders = ", ".join([f":{col}" for col in columns])

        # Migrate in batches
        offset = 0
        migrated = 0

        while offset < count:
            # Read batch from source
            with source_engine.connect() as source_conn:
                query = text(
                    f"SELECT {columns_str} FROM {table_name} LIMIT {BATCH_SIZE} OFFSET {offset}"
                )
                batch = source_conn.execute(query).fetchall()

            if not batch:
                break

            # Write batch to target
            with target_engine.begin() as target_conn:
                for row in batch:
                    try:
                        # Convert row to dict
                        row_dict = dict(row._mapping)

                        # Build INSERT statement
                        insert_sql = (
                            f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders})"
                        )

                        target_conn.execute(text(insert_sql), row_dict)
                    except Exception as e:
                        logger.error(f"  Error inserting row: {e}")
                        logger.error(f"  Row data: {row_dict}")
                        # Continue with next row
                        continue

            migrated += len(batch)
            offset += BATCH_SIZE

            # Progress update every batch
            progress = (migrated / count) * 100
            elapsed = (datetime.now() - start_time).total_seconds()
            rate = migrated / elapsed if elapsed > 0 else 0
            eta = (count - migrated) / rate if rate > 0 else 0

            logger.info(
                f"  Progress: {migrated:,}/{count:,} ({progress:.1f}%) | "
                f"Rate: {rate:.0f} rows/sec | ETA: {eta:.0f}s"
            )

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"  ✓ Completed migrating {table_name}: {migrated:,} rows in {elapsed:.1f}s")

    except Exception as e:
        logger.error(f"  ✗ Error migrating {table_name}: {e}")
        raise


def migrate_all_data():
    """
    Migrate all tables from SQLite to PostgreSQL

    Environment variables required:
        SOURCE_DATABASE_URL: SQLite database URL (default: sqlite:///aircraft_data.db)
        TARGET_DATABASE_URL: PostgreSQL database URL
    """
    logger.info("=" * 60)
    logger.info("Starting Database Migration: SQLite → PostgreSQL")
    logger.info("=" * 60)

    # Get database URLs from environment
    source_url = os.environ.get("SOURCE_DATABASE_URL", "sqlite:///aircraft_data.db")
    target_url = os.environ.get("TARGET_DATABASE_URL")

    if not target_url:
        logger.error("ERROR: TARGET_DATABASE_URL environment variable is required")
        logger.error(
            "Example: export TARGET_DATABASE_URL='postgresql+psycopg2://user:pass@host:5432/dbname'"
        )
        sys.exit(1)

    logger.info(f"Source: {source_url}")
    logger.info(f"Target: {target_url.split('@')[0] if '@' in target_url else target_url}@...")
    logger.info("")

    # Create engines
    try:
        logger.info("Connecting to source database...")
        source_engine = create_engine(source_url)

        logger.info("Connecting to target database...")
        target_engine = create_engine(target_url)

        logger.info("✓ Database connections established")
        logger.info("")
    except Exception as e:
        logger.error(f"Failed to connect to databases: {e}")
        sys.exit(1)

    # Get list of tables from source database
    inspector = inspect(source_engine)
    tables = inspector.get_table_names()

    logger.info(f"Found {len(tables)} tables to migrate:")
    for table in tables:
        logger.info(f"  - {table}")
    logger.info("")

    # Migrate each table
    total_start = datetime.now()
    successful = 0
    failed = 0

    for table_name in tables:
        try:
            migrate_table_data(source_engine, target_engine, table_name)
            successful += 1
        except Exception as e:
            logger.error(f"Failed to migrate {table_name}: {e}")
            failed += 1
            # Continue with next table
            continue

        logger.info("")  # Blank line between tables

    # Summary
    total_elapsed = (datetime.now() - total_start).total_seconds()
    logger.info("=" * 60)
    logger.info("Migration Complete!")
    logger.info("=" * 60)
    logger.info(f"Total time: {total_elapsed:.1f}s ({total_elapsed / 60:.1f} minutes)")
    logger.info(f"Tables migrated successfully: {successful}/{len(tables)}")
    if failed > 0:
        logger.warning(f"Tables with errors: {failed}")
    logger.info("=" * 60)

    # Verification
    logger.info("")
    logger.info("Verifying migration...")
    for table_name in tables:
        try:
            with source_engine.connect() as source_conn:
                source_count = source_conn.execute(
                    text(f"SELECT COUNT(*) FROM {table_name}")
                ).scalar()

            with target_engine.connect() as target_conn:
                target_count = target_conn.execute(
                    text(f"SELECT COUNT(*) FROM {table_name}")
                ).scalar()

            if source_count == target_count:
                logger.info(f"  ✓ {table_name}: {source_count:,} rows (match)")
            else:
                logger.warning(
                    f"  ⚠ {table_name}: source={source_count:,}, target={target_count:,} (MISMATCH!)"
                )
        except Exception as e:
            logger.error(f"  ✗ {table_name}: verification failed - {e}")

    logger.info("")
    logger.info("Migration script completed.")


if __name__ == "__main__":
    try:
        migrate_all_data()
    except KeyboardInterrupt:
        logger.info("\nMigration interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)
