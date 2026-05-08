#!/usr/bin/env python3
"""
Database migration script for scraper tables.

Creates the scraper_tasks, scraper_workers, and scraper_results tables
if they don't already exist.

Usage:
    python scripts/migrate_scraper_tables.py [--config config.yaml]
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


SCRAPER_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS scraper_tasks (
    id BIGSERIAL PRIMARY KEY,
    task_type VARCHAR(50) NOT NULL,
    task_key VARCHAR(255) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    payload JSONB DEFAULT '{}',
    claimed_by VARCHAR(100),
    claimed_at TIMESTAMP,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    last_error TEXT,
    result JSONB,
    scheduled_for TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
)
"""

SCRAPER_WORKERS_SQL = """
CREATE TABLE IF NOT EXISTS scraper_workers (
    id BIGSERIAL PRIMARY KEY,
    worker_id VARCHAR(100) UNIQUE NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tasks_completed INTEGER DEFAULT 0,
    current_task_id BIGINT,
    metadata JSONB DEFAULT '{}'
)
"""

SCRAPER_RESULTS_SQL = """
CREATE TABLE IF NOT EXISTS scraper_results (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT REFERENCES scraper_tasks(id),
    worker_id VARCHAR(100),
    success BOOLEAN NOT NULL,
    duration_seconds NUMERIC(10, 3),
    result JSONB,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_status ON scraper_tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_type_key ON scraper_tasks(task_type, task_key)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_scheduled ON scraper_tasks(scheduled_for)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_priority ON scraper_tasks(priority DESC, scheduled_for)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_results_task ON scraper_results(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_workers_status ON scraper_workers(status)",
]


def get_database_url(config_path: str | None = None) -> str:
    """Get database URL from config file or environment.

    Args:
        config_path: Path to config.yaml file.

    Returns:
        Database URL string.
    """
    if config_path:
        from src.utils.yaml_config import YAMLConfig

        config = YAMLConfig(config_path)
        db_config = config.get_database_config()
        return db_config.get("url", "")

    # Try environment variable
    import os

    return os.environ.get(
        "DATABASE_URL",
        "postgresql://localhost:5432/aircraft_data",
    )


def check_tables_exist(engine) -> dict[str, bool]:
    """Check which tables already exist.

    Args:
        engine: SQLAlchemy engine.

    Returns:
        Dictionary mapping table names to existence status.
    """
    tables = {
        "scraper_tasks": False,
        "scraper_workers": False,
        "scraper_results": False,
    }

    with engine.connect() as conn:
        for table in tables:
            try:
                conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
                tables[table] = True
            except Exception:
                pass

    return tables


def run_migration(database_url: str, dry_run: bool = False) -> None:
    """Run the migration.

    Args:
        database_url: PostgreSQL connection URL.
        dry_run: If True, only print what would be done.
    """
    logger.info(f"Connecting to database...")
    engine = create_engine(database_url, echo=False)

    # Check existing tables
    existing = check_tables_exist(engine)
    logger.info(f"Existing tables: {existing}")

    if dry_run:
        logger.info("DRY RUN - No changes will be made")

    with engine.connect() as conn:
        # Create scraper_tasks
        if not existing["scraper_tasks"]:
            logger.info("Creating scraper_tasks table...")
            if not dry_run:
                conn.execute(text(SCRAPER_TASKS_SQL))
                conn.commit()
        else:
            logger.info("scraper_tasks already exists, skipping")

        # Create scraper_workers
        if not existing["scraper_workers"]:
            logger.info("Creating scraper_workers table...")
            if not dry_run:
                conn.execute(text(SCRAPER_WORKERS_SQL))
                conn.commit()
        else:
            logger.info("scraper_workers already exists, skipping")

        # Create scraper_results
        if not existing["scraper_results"]:
            logger.info("Creating scraper_results table...")
            if not dry_run:
                conn.execute(text(SCRAPER_RESULTS_SQL))
                conn.commit()
        else:
            logger.info("scraper_results already exists, skipping")

        # Create indexes
        logger.info("Creating indexes...")
        for index_sql in INDEXES_SQL:
            if not dry_run:
                try:
                    conn.execute(text(index_sql))
                    conn.commit()
                except Exception as e:
                    logger.warning(f"Index creation warning: {e}")

    logger.info("Migration complete!")

    # Verify
    final_state = check_tables_exist(engine)
    logger.info(f"Final state: {final_state}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate database for scraper tables",
    )
    parser.add_argument(
        "--config",
        "-c",
        default="config/config.yaml",
        help="Path to config file",
    )
    parser.add_argument(
        "--database-url",
        "-d",
        help="Database URL (overrides config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what would be done",
    )

    args = parser.parse_args()

    # Get database URL
    if args.database_url:
        database_url = args.database_url
    else:
        database_url = get_database_url(args.config)

    if not database_url:
        logger.error("No database URL provided")
        sys.exit(1)

    logger.info(f"Using database: {database_url.split('@')[-1]}")

    try:
        run_migration(database_url, dry_run=args.dry_run)
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
