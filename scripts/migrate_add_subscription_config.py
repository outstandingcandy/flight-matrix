#!/usr/bin/env python3
"""
Migration script to add custom report configuration columns to subscriptions table.

This script adds the following columns:
- custom_cooldown_hours: Custom cooldown hours (NULL means use tier default)
- custom_daily_report_limit: Custom daily report limit
- custom_monthly_report_limit: Custom monthly report limit

Run this script once to update existing databases.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from src.utils.yaml_config import YAMLConfig
from src.utils.database import DatabaseManager


def migrate(config_path: str | None = None) -> None:
    """Add custom report configuration columns to subscriptions table."""
    config = YAMLConfig(config_path)
    database_url = config.get('database', {}).get('url', 'sqlite:///aircraft_data.db')
    print(f"Connecting to database: {database_url[:50]}...")
    db = DatabaseManager(database_url)
    session = db.get_session()

    # List of columns to add
    columns_to_add = [
        ("custom_cooldown_hours", "NUMERIC(6, 2)"),
        ("custom_daily_report_limit", "INTEGER"),
        ("custom_monthly_report_limit", "INTEGER"),
    ]

    try:
        for column_name, column_type in columns_to_add:
            # Check if column already exists
            try:
                session.execute(text(f"SELECT {column_name} FROM subscriptions LIMIT 1"))
                print(f"Column '{column_name}' already exists, skipping...")
            except Exception:
                # Column doesn't exist, add it
                session.rollback()
                print(f"Adding column '{column_name}' ({column_type})...")
                session.execute(
                    text(f"ALTER TABLE subscriptions ADD COLUMN {column_name} {column_type}")
                )
                session.commit()
                print(f"Successfully added column '{column_name}'")

        print("\nMigration completed successfully!")

    except Exception as e:
        session.rollback()
        print(f"Migration failed: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Add custom report config columns to subscriptions table')
    parser.add_argument('--config', '-c', help='Path to config.yaml file')
    args = parser.parse_args()
    migrate(args.config)
