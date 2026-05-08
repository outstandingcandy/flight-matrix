#!/usr/bin/env python3
"""
Migration script for multi-user subscription system.

This script:
1. Creates the multi-user database tables if they don't exist
2. Optionally migrates existing email recipients to users
3. Creates default filters from existing config.yaml SQL filters

Usage:
    python scripts/migrate_to_multi_user.py --config config.yaml
    python scripts/migrate_to_multi_user.py --config config.yaml --migrate-recipients
    python scripts/migrate_to_multi_user.py --config config.yaml --migrate-filters
    python scripts/migrate_to_multi_user.py --config config.yaml --migrate-all
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.yaml_config import YAMLConfig
from src.utils.database import DatabaseManager
from src.services.user_service import UserService
from src.services.subscription_service import SubscriptionService
from src.services.filter_service import FilterService

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('migrate_to_multi_user')


def create_tables(db: DatabaseManager) -> bool:
    """Create multi-user tables if they don't exist.

    Args:
        db: Database manager instance

    Returns:
        True if successful
    """
    logger.info("Creating multi-user tables...")
    try:
        db.ensure_multi_user_tables_exist()
        logger.info("Multi-user tables created/verified successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to create tables: {e}")
        return False


def migrate_recipients(
    config: YAMLConfig,
    user_service: UserService,
    default_tier: str = 'basic'
) -> int:
    """Migrate existing email recipients to users.

    Args:
        config: YAML configuration instance
        user_service: User service instance
        default_tier: Default subscription tier for migrated users

    Returns:
        Number of users created
    """
    logger.info("Migrating email recipients to users...")

    email_config = config.get_email_config()
    recipients = email_config.get('recipients', [])

    if not recipients:
        logger.warning("No recipients found in config")
        return 0

    created = 0
    for email in recipients:
        if not email or not isinstance(email, str):
            continue

        email = email.strip()
        if not email:
            continue

        # Check if user already exists
        existing = user_service.get_user_by_email(email)
        if existing:
            logger.info(f"User {email} already exists, skipping")
            continue

        # Create user
        user = user_service.create_user(
            email=email,
            name=None,
            tier=default_tier,
            generate_api_key=True
        )

        if user:
            logger.info(f"Created user: {email} (tier: {default_tier})")
            created += 1
        else:
            logger.warning(f"Failed to create user: {email}")

    logger.info(f"Migration complete: {created} users created")
    return created


def migrate_filters(
    config: YAMLConfig,
    user_service: UserService,
    filter_service: FilterService
) -> int:
    """Migrate existing SQL filter to all users.

    Args:
        config: YAML configuration instance
        user_service: User service instance
        filter_service: Filter service instance

    Returns:
        Number of filters created
    """
    logger.info("Migrating global SQL filter to user filters...")

    filter_config = config.get_filter_config()
    custom_sql = filter_config.get('custom_sql', '').strip()

    if not custom_sql:
        logger.warning("No custom SQL filter found in config")
        return 0

    # Get all active users
    users = user_service.list_users(status='active', limit=10000)

    if not users:
        logger.warning("No active users found")
        return 0

    created = 0
    for user_data in users:
        user_id = user_data['id']
        user_email = user_data['email']

        # Check if user already has filters
        existing_filters = filter_service.get_user_filters(user_id, active_only=False)
        if existing_filters:
            logger.info(f"User {user_email} already has filters, skipping")
            continue

        # Create filter
        user_filter = filter_service.create_filter(
            user_id=user_id,
            name="Default Filter (migrated)",
            filter_sql=custom_sql,
            description="Migrated from global config.yaml filter",
            priority=100
        )

        if user_filter:
            logger.info(f"Created filter for user: {user_email}")
            created += 1
        else:
            logger.warning(f"Failed to create filter for user: {user_email}")

    logger.info(f"Filter migration complete: {created} filters created")
    return created


def migrate_cooldowns(
    db: DatabaseManager,
    user_service: UserService
) -> int:
    """Migrate existing global cooldowns to user-specific cooldowns.

    This copies the global report_cooldowns to user_cooldowns for all users.

    Args:
        db: Database manager instance
        user_service: User service instance

    Returns:
        Number of cooldown records migrated
    """
    logger.info("Migrating global cooldowns to user cooldowns...")

    from sqlalchemy import text

    session = db.get_session()
    try:
        # Check if report_cooldowns table exists and has data
        try:
            result = session.execute(text("SELECT COUNT(*) FROM report_cooldowns")).scalar()
            if result == 0:
                logger.info("No global cooldowns to migrate")
                return 0
        except Exception:
            logger.info("No report_cooldowns table found, skipping cooldown migration")
            return 0

        # Get all active users
        users = user_service.list_users(status='active', limit=10000)
        if not users:
            logger.warning("No active users found")
            return 0

        # Get global cooldowns
        global_cooldowns = session.execute(text('''
            SELECT aircraft_hex, last_report_time, last_latitude, last_longitude, report_count
            FROM report_cooldowns
        ''')).fetchall()

        if not global_cooldowns:
            logger.info("No global cooldowns to migrate")
            return 0

        migrated = 0
        for user_data in users:
            user_id = user_data['id']

            for cd in global_cooldowns:
                # Check if already exists
                existing = session.execute(text('''
                    SELECT 1 FROM user_cooldowns
                    WHERE user_id = :user_id AND aircraft_hex = :hex
                '''), {'user_id': user_id, 'hex': cd[0]}).fetchone()

                if existing:
                    continue

                # Insert
                session.execute(text('''
                    INSERT INTO user_cooldowns (user_id, aircraft_hex, last_report_time, last_latitude, last_longitude, report_count)
                    VALUES (:user_id, :hex, :time, :lat, :lon, :count)
                '''), {
                    'user_id': user_id,
                    'hex': cd[0],
                    'time': cd[1],
                    'lat': cd[2],
                    'lon': cd[3],
                    'count': cd[4]
                })
                migrated += 1

        session.commit()
        logger.info(f"Cooldown migration complete: {migrated} records migrated")
        return migrated

    except Exception as e:
        session.rollback()
        logger.error(f"Error migrating cooldowns: {e}")
        return 0
    finally:
        session.close()


def print_summary(db: DatabaseManager):
    """Print summary of multi-user system status.

    Args:
        db: Database manager instance
    """
    from sqlalchemy import text

    session = db.get_session()
    try:
        users = session.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
        active_users = session.execute(text("SELECT COUNT(*) FROM users WHERE status = 'active'")).scalar() or 0
        subscriptions = session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE status = 'active'")).scalar() or 0
        filters = session.execute(text("SELECT COUNT(*) FROM user_filters")).scalar() or 0
        cooldowns = session.execute(text("SELECT COUNT(*) FROM user_cooldowns")).scalar() or 0

        print("\n" + "=" * 50)
        print("Multi-User System Summary")
        print("=" * 50)
        print(f"Total Users:          {users}")
        print(f"Active Users:         {active_users}")
        print(f"Active Subscriptions: {subscriptions}")
        print(f"Total Filters:        {filters}")
        print(f"Cooldown Records:     {cooldowns}")
        print("=" * 50 + "\n")

    except Exception as e:
        logger.error(f"Error printing summary: {e}")
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(
        description="Migrate to multi-user subscription system"
    )
    parser.add_argument(
        '--config', '-c',
        default='config/config.yaml',
        help='Path to config file'
    )
    parser.add_argument(
        '--migrate-recipients',
        action='store_true',
        help='Migrate email recipients to users'
    )
    parser.add_argument(
        '--migrate-filters',
        action='store_true',
        help='Migrate global SQL filter to user filters'
    )
    parser.add_argument(
        '--migrate-cooldowns',
        action='store_true',
        help='Migrate global cooldowns to user cooldowns'
    )
    parser.add_argument(
        '--migrate-all',
        action='store_true',
        help='Run all migrations'
    )
    parser.add_argument(
        '--tier',
        default='basic',
        choices=['basic', 'premium', 'enterprise'],
        help='Default tier for migrated users'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )

    args = parser.parse_args()

    # Load configuration
    logger.info(f"Loading configuration from {args.config}")
    config = YAMLConfig(args.config)

    # Initialize database
    db_config = config.get_database_config()
    db = DatabaseManager(db_config['url'])

    # Create tables first
    if not create_tables(db):
        logger.error("Failed to create tables, aborting")
        sys.exit(1)

    # Initialize services
    user_service = UserService(db)
    subscription_service = SubscriptionService(db, config)
    filter_service = FilterService(db)

    # Run migrations
    if args.migrate_all or args.migrate_recipients:
        if args.dry_run:
            email_config = config.get_email_config()
            recipients = email_config.get('recipients', [])
            logger.info(f"[DRY RUN] Would migrate {len(recipients)} recipients")
        else:
            migrate_recipients(config, user_service, args.tier)

    if args.migrate_all or args.migrate_filters:
        if args.dry_run:
            filter_config = config.get_filter_config()
            custom_sql = filter_config.get('custom_sql', '').strip()
            logger.info(f"[DRY RUN] Would migrate SQL filter: {custom_sql[:100]}...")
        else:
            migrate_filters(config, user_service, filter_service)

    if args.migrate_all or args.migrate_cooldowns:
        if args.dry_run:
            logger.info("[DRY RUN] Would migrate global cooldowns")
        else:
            migrate_cooldowns(db, user_service)

    # Print summary
    print_summary(db)

    # Reminder
    if not args.dry_run:
        print("Next steps:")
        print("1. Set 'multi_user.enabled: true' in config.yaml to enable multi-user mode")
        print("2. Access the admin panel at /admin/users")
        print("3. Users can manage their filters at /user/<email>/filters")


if __name__ == '__main__':
    main()
