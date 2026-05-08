#!/usr/bin/env python3
"""
Data Migration Script: Aircraft Images to Static Info Table

Migrates image paths from aircraft_snapshots to aircraft_static_info table.
This makes aircraft_static_info the single source of truth for aircraft images.

Usage:
    python scripts/migrate_images_to_static_info.py --dry-run  # Preview changes
    python scripts/migrate_images_to_static_info.py            # Execute migration
    python scripts/migrate_images_to_static_info.py --batch-size 500  # Custom batch size

Requirements:
    - Database must be accessible via config.yaml settings
    - aircraft_static_info table must exist with image columns
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.utils.database import DatabaseManager
from src.utils.yaml_config import YAMLConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_images_from_snapshots(
    db: DatabaseManager, batch_size: int = 1000, offset: int = 0
) -> List[Dict]:
    """Query distinct registrations with images from aircraft_snapshots.

    Args:
        db: DatabaseManager instance
        batch_size: Number of records per batch
        offset: Offset for pagination

    Returns:
        List of dictionaries with registration and image paths
    """
    session = db.get_session()
    try:
        result = session.execute(text('''
            SELECT
                registration,
                MAX(image_path_1) as image_path_1,
                MAX(image_path_2) as image_path_2,
                MAX(image_path_3) as image_path_3,
                MAX(hex) as hex_code
            FROM aircraft_snapshots
            WHERE registration IS NOT NULL
            AND registration != ''
            AND images_downloaded = true
            AND (image_path_1 IS NOT NULL OR image_path_2 IS NOT NULL OR image_path_3 IS NOT NULL)
            GROUP BY registration
            ORDER BY registration
            LIMIT :limit OFFSET :offset
        '''), {'limit': batch_size, 'offset': offset}).fetchall()

        records = []
        for row in result:
            records.append({
                'registration': row[0],
                'image_path_1': row[1],
                'image_path_2': row[2],
                'image_path_3': row[3],
                'hex_code': row[4]
            })
        return records
    finally:
        session.close()


def count_images_in_snapshots(db: DatabaseManager) -> int:
    """Count total distinct registrations with images in aircraft_snapshots.

    Args:
        db: DatabaseManager instance

    Returns:
        Count of distinct registrations with images
    """
    session = db.get_session()
    try:
        result = session.execute(text('''
            SELECT COUNT(DISTINCT registration)
            FROM aircraft_snapshots
            WHERE registration IS NOT NULL
            AND registration != ''
            AND images_downloaded = true
            AND (image_path_1 IS NOT NULL OR image_path_2 IS NOT NULL OR image_path_3 IS NOT NULL)
        ''')).scalar()
        return result or 0
    finally:
        session.close()


def upsert_to_static_info(
    db: DatabaseManager, records: List[Dict], dry_run: bool = False
) -> Tuple[int, int]:
    """UPSERT image data to aircraft_static_info table.

    Args:
        db: DatabaseManager instance
        records: List of records to upsert
        dry_run: If True, don't actually execute the upsert

    Returns:
        Tuple of (inserted_count, updated_count)
    """
    if dry_run:
        return len(records), 0

    session = db.get_session()
    inserted = 0
    updated = 0

    try:
        for record in records:
            # Check if registration already exists
            existing = session.execute(text('''
                SELECT images_downloaded FROM aircraft_static_info
                WHERE registration = :reg
            '''), {'reg': record['registration']}).fetchone()

            if existing:
                # Update existing record (only if not already downloaded from static_info)
                if not existing[0]:
                    session.execute(text('''
                        UPDATE aircraft_static_info
                        SET image_path_1 = :p1,
                            image_path_2 = :p2,
                            image_path_3 = :p3,
                            images_downloaded = true,
                            images_updated_at = CURRENT_TIMESTAMP,
                            last_updated = CURRENT_TIMESTAMP
                        WHERE registration = :reg
                        AND (images_downloaded IS NULL OR images_downloaded = false)
                    '''), {
                        'p1': record['image_path_1'],
                        'p2': record['image_path_2'],
                        'p3': record['image_path_3'],
                        'reg': record['registration']
                    })
                    updated += 1
            else:
                # Insert new record
                session.execute(text('''
                    INSERT INTO aircraft_static_info
                    (registration, hex_code, image_path_1, image_path_2, image_path_3,
                     images_downloaded, images_updated_at, last_updated)
                    VALUES
                    (:reg, :hex, :p1, :p2, :p3, true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                '''), {
                    'reg': record['registration'],
                    'hex': record['hex_code'],
                    'p1': record['image_path_1'],
                    'p2': record['image_path_2'],
                    'p3': record['image_path_3']
                })
                inserted += 1

        session.commit()
        return inserted, updated

    except Exception as e:
        session.rollback()
        logger.error(f"Error during upsert: {e}")
        raise
    finally:
        session.close()


def verify_migration(db: DatabaseManager) -> Dict:
    """Verify migration was successful.

    Args:
        db: DatabaseManager instance

    Returns:
        Dictionary with verification statistics
    """
    session = db.get_session()
    try:
        # Count records in aircraft_static_info with images
        static_count = session.execute(text('''
            SELECT COUNT(*) FROM aircraft_static_info
            WHERE images_downloaded = true
            AND (image_path_1 IS NOT NULL OR image_path_2 IS NOT NULL OR image_path_3 IS NOT NULL)
        ''')).scalar() or 0

        # Count records in aircraft_snapshots with images
        snapshots_count = session.execute(text('''
            SELECT COUNT(DISTINCT registration) FROM aircraft_snapshots
            WHERE images_downloaded = true
            AND (image_path_1 IS NOT NULL OR image_path_2 IS NOT NULL OR image_path_3 IS NOT NULL)
        ''')).scalar() or 0

        # Sample check - get a few registrations and verify
        sample = session.execute(text('''
            SELECT registration FROM aircraft_static_info
            WHERE images_downloaded = true
            LIMIT 5
        ''')).fetchall()

        return {
            'static_info_count': static_count,
            'snapshots_count': snapshots_count,
            'sample_registrations': [r[0] for r in sample],
            'migration_complete': static_count >= snapshots_count * 0.95  # 95% threshold
        }
    finally:
        session.close()


def main():
    """Main entry point for migration script."""
    parser = argparse.ArgumentParser(
        description='Migrate aircraft images from snapshots to static_info table'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview changes without actually migrating'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=500,
        help='Number of records to process per batch (default: 500)'
    )
    parser.add_argument(
        '--config',
        type=str,
        default='config/config.yaml',
        help='Path to configuration file (default: config/config.yaml)'
    )
    parser.add_argument(
        '--verify-only',
        action='store_true',
        help='Only verify the migration status without migrating'
    )

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Aircraft Image Migration: snapshots -> static_info")
    logger.info("=" * 60)

    if args.dry_run:
        logger.info("DRY RUN MODE - No changes will be made")

    # Initialize database
    try:
        config = YAMLConfig(args.config)
        db_config = config.get_database_config()
        db = DatabaseManager(db_config['url'])
        logger.info(f"Connected to database")
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        sys.exit(1)

    # Verify only mode
    if args.verify_only:
        logger.info("Verifying migration status...")
        stats = verify_migration(db)
        logger.info(f"Static info records with images: {stats['static_info_count']}")
        logger.info(f"Snapshots distinct registrations with images: {stats['snapshots_count']}")
        logger.info(f"Sample registrations: {stats['sample_registrations']}")
        logger.info(f"Migration complete: {stats['migration_complete']}")
        sys.exit(0)

    # Count total records to migrate
    total_count = count_images_in_snapshots(db)
    logger.info(f"Total registrations with images in snapshots: {total_count}")

    if total_count == 0:
        logger.info("No records to migrate. Exiting.")
        sys.exit(0)

    # Process in batches
    total_inserted = 0
    total_updated = 0
    offset = 0
    batch_num = 0

    start_time = datetime.now()

    while True:
        batch_num += 1
        records = get_images_from_snapshots(db, args.batch_size, offset)

        if not records:
            break

        logger.info(f"Processing batch {batch_num}: {len(records)} records (offset: {offset})")

        if args.dry_run:
            for record in records[:3]:  # Show first 3 in dry run
                logger.info(f"  Would migrate: {record['registration']} "
                           f"(images: {bool(record['image_path_1'])}, "
                           f"{bool(record['image_path_2'])}, {bool(record['image_path_3'])})")
            if len(records) > 3:
                logger.info(f"  ... and {len(records) - 3} more")
            total_inserted += len(records)
        else:
            inserted, updated = upsert_to_static_info(db, records, dry_run=args.dry_run)
            total_inserted += inserted
            total_updated += updated
            logger.info(f"  Batch complete: {inserted} inserted, {updated} updated")

        offset += args.batch_size

        # Progress report every 10 batches
        if batch_num % 10 == 0:
            elapsed = (datetime.now() - start_time).total_seconds()
            progress = min(offset / total_count * 100, 100)
            logger.info(f"Progress: {progress:.1f}% ({offset}/{total_count}), "
                       f"elapsed: {elapsed:.1f}s")

    # Summary
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info("Migration Summary")
    logger.info("=" * 60)
    logger.info(f"Total batches processed: {batch_num}")
    logger.info(f"Total records inserted: {total_inserted}")
    logger.info(f"Total records updated: {total_updated}")
    logger.info(f"Total time: {elapsed:.1f} seconds")

    if not args.dry_run:
        # Verify migration
        logger.info("\nVerifying migration...")
        stats = verify_migration(db)
        logger.info(f"Static info records with images: {stats['static_info_count']}")
        logger.info(f"Migration complete: {stats['migration_complete']}")

    if args.dry_run:
        logger.info("\nTo execute the migration, run without --dry-run flag")

    logger.info("Done!")


if __name__ == '__main__':
    main()
