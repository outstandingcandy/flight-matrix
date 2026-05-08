#!/usr/bin/env python3
"""
Migration script to rename subscription columns and add max_filters.

This script:
1. Renames custom_cooldown_hours -> cooldown_hours
2. Renames custom_daily_report_limit -> daily_report_limit
3. Renames custom_monthly_report_limit -> monthly_report_limit
4. Adds max_filters column

For existing records:
- If custom_* values are NULL, sets new columns to default values
- If custom_* values exist, preserves them in the new columns

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

# Default values for new columns
DEFAULTS = {
    'cooldown_hours': 12.0,
    'daily_report_limit': -1,
    'monthly_report_limit': -1,
    'max_filters': -1,
}


def column_exists(session, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    try:
        session.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
        session.rollback()
        return True
    except Exception:
        session.rollback()
        return False


def migrate(config_path: str | None = None) -> None:
    """Rename columns and add max_filters to subscriptions table."""
    config = YAMLConfig(config_path)
    database_url = config.get('database', {}).get('url', 'sqlite:///aircraft_data.db')
    print(f"Connecting to database: {database_url[:50]}...")
    db = DatabaseManager(database_url)
    session = db.get_session()

    try:
        # Step 1: Add new columns if they don't exist
        new_columns = [
            ("cooldown_hours", f"NUMERIC(6, 2) DEFAULT {DEFAULTS['cooldown_hours']}"),
            ("daily_report_limit", f"INTEGER DEFAULT {DEFAULTS['daily_report_limit']}"),
            ("monthly_report_limit", f"INTEGER DEFAULT {DEFAULTS['monthly_report_limit']}"),
            ("max_filters", f"INTEGER DEFAULT {DEFAULTS['max_filters']}"),
        ]

        for column_name, column_def in new_columns:
            if column_exists(session, 'subscriptions', column_name):
                print(f"Column '{column_name}' already exists, skipping...")
            else:
                print(f"Adding column '{column_name}'...")
                session.execute(
                    text(f"ALTER TABLE subscriptions ADD COLUMN {column_name} {column_def}")
                )
                session.commit()
                print(f"Successfully added column '{column_name}'")

        # Step 2: Migrate data from old custom_* columns to new columns
        column_mappings = [
            ("custom_cooldown_hours", "cooldown_hours", DEFAULTS['cooldown_hours']),
            ("custom_daily_report_limit", "daily_report_limit", DEFAULTS['daily_report_limit']),
            ("custom_monthly_report_limit", "monthly_report_limit", DEFAULTS['monthly_report_limit']),
        ]

        for old_col, new_col, default in column_mappings:
            if column_exists(session, 'subscriptions', old_col):
                print(f"Migrating data from '{old_col}' to '{new_col}'...")
                # Copy non-null values from old column to new column
                session.execute(text(f"""
                    UPDATE subscriptions
                    SET {new_col} = COALESCE({old_col}, {default})
                    WHERE {new_col} IS NULL OR {new_col} = {default}
                """))
                session.commit()
                print(f"Data migrated from '{old_col}' to '{new_col}'")

        # Step 3: Set default values for records that have NULL in new columns
        print("Setting default values for NULL records...")
        for col, default in [
            ('cooldown_hours', DEFAULTS['cooldown_hours']),
            ('daily_report_limit', DEFAULTS['daily_report_limit']),
            ('monthly_report_limit', DEFAULTS['monthly_report_limit']),
            ('max_filters', DEFAULTS['max_filters']),
        ]:
            session.execute(text(f"""
                UPDATE subscriptions
                SET {col} = {default}
                WHERE {col} IS NULL
            """))
        session.commit()

        # Step 4: Verify migration
        print("\nVerifying migration...")
        result = session.execute(text("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN cooldown_hours IS NOT NULL THEN 1 ELSE 0 END) as has_cooldown,
                   SUM(CASE WHEN daily_report_limit IS NOT NULL THEN 1 ELSE 0 END) as has_daily,
                   SUM(CASE WHEN monthly_report_limit IS NOT NULL THEN 1 ELSE 0 END) as has_monthly,
                   SUM(CASE WHEN max_filters IS NOT NULL THEN 1 ELSE 0 END) as has_max_filters
            FROM subscriptions
        """)).fetchone()

        print(f"Total subscriptions: {result[0]}")
        print(f"  With cooldown_hours: {result[1]}")
        print(f"  With daily_report_limit: {result[2]}")
        print(f"  With monthly_report_limit: {result[3]}")
        print(f"  With max_filters: {result[4]}")

        print("\nMigration completed successfully!")
        print("\nNote: Old custom_* columns are preserved for backward compatibility.")
        print("They can be dropped manually after verifying the migration.")

    except Exception as e:
        session.rollback()
        print(f"Migration failed: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description='Migrate subscription columns to remove tier defaults'
    )
    parser.add_argument('--config', '-c', help='Path to config.yaml file')
    args = parser.parse_args()
    migrate(args.config)
