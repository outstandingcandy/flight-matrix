#!/usr/bin/env python3
"""
Migration script to create static_info records for all tracked aircraft.

This script finds all aircraft in aircraft_snapshots that don't have
a corresponding record in aircraft_static_info and creates basic records.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from src.utils.yaml_config import YAMLConfig
from src.utils.database import DatabaseManager


def migrate(config_path: str = 'config.yaml', batch_size: int = 1000) -> None:
    """Create static_info records for all aircraft missing them."""
    config = YAMLConfig(config_path)
    database_url = config.get('database', {}).get('url', 'sqlite:///aircraft_data.db')
    print(f"Connecting to database: {database_url[:50]}...")
    db = DatabaseManager(database_url)
    session = db.get_session()

    try:
        # Count aircraft without static info
        count_result = session.execute(text('''
            SELECT COUNT(DISTINCT s.registration)
            FROM aircraft_snapshots s
            LEFT JOIN aircraft_static_info i ON s.registration = i.registration
            WHERE s.registration IS NOT NULL
              AND s.registration != ''
              AND i.registration IS NULL
        ''')).scalar()

        print(f"Found {count_result} aircraft without static info records")

        if count_result == 0:
            print("No migration needed.")
            return

        # Process in batches
        total_created = 0
        offset = 0

        while True:
            # Get batch of aircraft without static info
            result = session.execute(text('''
                SELECT DISTINCT s.registration, s.hex, s.aircraft_type
                FROM aircraft_snapshots s
                LEFT JOIN aircraft_static_info i ON s.registration = i.registration
                WHERE s.registration IS NOT NULL
                  AND s.registration != ''
                  AND i.registration IS NULL
                ORDER BY s.registration
                LIMIT :limit OFFSET :offset
            '''), {'limit': batch_size, 'offset': offset}).fetchall()

            if not result:
                break

            # Insert static info records
            batch_created = 0
            for row in result:
                reg = row[0]
                hex_code = row[1]
                aircraft_type = row[2]

                try:
                    session.execute(text('''
                        INSERT INTO aircraft_static_info (registration, hex_code, aircraft_type, last_updated)
                        VALUES (:reg, :hex, :type, CURRENT_TIMESTAMP)
                        ON CONFLICT (registration) DO NOTHING
                    '''), {'reg': reg, 'hex': hex_code, 'type': aircraft_type})
                    batch_created += 1
                except Exception as e:
                    print(f"  Failed to create record for {reg}: {e}")

            session.commit()
            total_created += batch_created
            offset += batch_size
            print(f"  Created {batch_created} records (total: {total_created}/{count_result})")

        print(f"\nMigration completed. Created {total_created} static info records.")

    except Exception as e:
        session.rollback()
        print(f"Migration failed: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description='Create static_info records for all tracked aircraft'
    )
    parser.add_argument('--config', '-c', default='config/config.yaml', help='Path to config file')
    parser.add_argument('--batch-size', '-b', type=int, default=1000, help='Batch size')
    args = parser.parse_args()
    migrate(args.config, args.batch_size)
