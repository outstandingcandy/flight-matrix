"""DDL helpers for Flight Matrix's database schema.

`DatabaseManager` orchestrates lifecycle; this module owns the raw CREATE TABLE
statements. Kept separate so the schema can be audited in one place, and so
the manager file stays small.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.data.models import Base

logger = logging.getLogger("database.schema")


def create_core_tables(engine: Engine) -> None:
    """Create every ORM-declared table defined in src.data.models.

    Idempotent: SQLAlchemy's `create_all` skips tables that already exist.
    """
    Base.metadata.create_all(bind=engine)


def create_sqlite_tables(session: Session) -> None:
    """Create aircraft_snapshots and related tables with SQLite AUTOINCREMENT.

    SQLAlchemy's default `create_all` does not emit AUTOINCREMENT for SQLite
    integer primary keys, which the ingest path depends on. This helper does
    the raw CREATE TABLE so IDs grow monotonically.
    """
    try:
        session.execute(
            text("""
        CREATE TABLE aircraft_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            hex VARCHAR(6) NOT NULL,
            flight_number VARCHAR(10),
            registration VARCHAR(20),
            aircraft_type VARCHAR(10),
            latitude NUMERIC(10, 7),
            longitude NUMERIC(11, 7),
            altitude_baro INTEGER,
            altitude_geom INTEGER,
            ground_speed NUMERIC(6, 2),
            track NUMERIC(5, 2),
            vertical_rate INTEGER,
            squawk VARCHAR(4),
            emergency VARCHAR(20),
            category VARCHAR(2),
            country_of_registration VARCHAR(50),
            current_country VARCHAR(50),
            is_military BOOLEAN DEFAULT 0,
            is_interesting BOOLEAN DEFAULT 0,
            raw_data JSON
        )
        """)
        )
        session.execute(text("CREATE INDEX idx_hex ON aircraft_snapshots(hex)"))
        session.execute(text("CREATE INDEX idx_snapshot_time ON aircraft_snapshots(snapshot_time)"))
        session.execute(
            text("CREATE INDEX idx_location ON aircraft_snapshots(latitude, longitude)")
        )
        session.execute(
            text(
                "CREATE INDEX idx_recent_military ON aircraft_snapshots(snapshot_time, is_military)"
            )
        )
        session.execute(
            text(
                "CREATE INDEX idx_recent_interesting ON aircraft_snapshots(snapshot_time, is_interesting)"
            )
        )
        session.execute(text("CREATE INDEX idx_hex_time ON aircraft_snapshots(hex, snapshot_time)"))

        session.execute(
            text("""
        CREATE TABLE geographic_regions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            region_type VARCHAR(20) NOT NULL,
            geometry_type VARCHAR(20),
            center_lat NUMERIC(10, 7),
            center_lon NUMERIC(11, 7),
            radius_km NUMERIC(8, 3),
            boundary_points JSON,
            country_code VARCHAR(3),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        )

        session.execute(
            text("""
        CREATE TABLE IF NOT EXISTS report_cooldowns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            aircraft_hex VARCHAR(6) NOT NULL UNIQUE,
            last_report_time DATETIME NOT NULL,
            last_latitude NUMERIC(10, 7),
            last_longitude NUMERIC(11, 7),
            report_count INTEGER DEFAULT 1,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        )
        session.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cooldowns_hex ON report_cooldowns(aircraft_hex)")
        )
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_cooldowns_time ON report_cooldowns(last_report_time)"
            )
        )

        session.commit()
        logger.info("SQLite tables created successfully with AUTOINCREMENT")

    except Exception as e:
        session.rollback()
        logger.error(f"Error creating SQLite tables: {e}")
        raise


def ensure_report_cooldowns_table(session: Session) -> None:
    """Create the report_cooldowns table on demand in existing databases."""
    try:
        session.execute(text("SELECT 1 FROM report_cooldowns LIMIT 1"))
        return
    except Exception:
        pass

    try:
        session.execute(
            text("""
        CREATE TABLE IF NOT EXISTS report_cooldowns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            aircraft_hex VARCHAR(6) NOT NULL UNIQUE,
            last_report_time DATETIME NOT NULL,
            last_latitude NUMERIC(10, 7),
            last_longitude NUMERIC(11, 7),
            report_count INTEGER DEFAULT 1,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        )
        session.execute(
            text("CREATE INDEX IF NOT EXISTS idx_cooldowns_hex ON report_cooldowns(aircraft_hex)")
        )
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_cooldowns_time ON report_cooldowns(last_report_time)"
            )
        )
        session.commit()
        logger.info("Report cooldowns table created successfully")
    except Exception as e:
        session.rollback()
        logger.error(f"Error creating report cooldowns table: {e}")


def ensure_multi_user_tables(session: Session, *, is_postgres: bool) -> None:
    """Create users, subscriptions, user_filters, user_cooldowns, user_usage.

    Idempotent: returns early if the `users` table already exists. Uses
    dialect-specific DDL because of Postgres BIGSERIAL vs. SQLite
    AUTOINCREMENT differences.
    """
    try:
        session.execute(text("SELECT 1 FROM users LIMIT 1"))
        logger.debug("Multi-user tables already exist")
        return
    except Exception:
        pass

    try:
        if is_postgres:
            _create_multi_user_tables_postgres(session)
        else:
            _create_multi_user_tables_sqlite(session)
        session.commit()
        logger.info("Multi-user tables created successfully")
    except Exception as e:
        session.rollback()
        logger.error(f"Error creating multi-user tables: {e}")
        raise


def _create_multi_user_tables_postgres(session: Session) -> None:
    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            name VARCHAR(100),
            status VARCHAR(20) DEFAULT 'active',
            api_key VARCHAR(64) UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    )
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_email ON users(email)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_status ON users(status)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_api_key ON users(api_key)"))

    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tier VARCHAR(20) DEFAULT 'basic',
            status VARCHAR(20) DEFAULT 'active',
            enable_maps BOOLEAN DEFAULT true,
            enable_aircraft_images BOOLEAN DEFAULT true,
            starts_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    )
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_subscription_user ON subscriptions(user_id)")
    )
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_subscription_status ON subscriptions(status)")
    )

    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS user_filters (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            description TEXT,
            filter_sql TEXT NOT NULL,
            is_active BOOLEAN DEFAULT true,
            priority INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    )
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_user_filter_user ON user_filters(user_id)")
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_user_filter_active ON user_filters(user_id, is_active)"
        )
    )

    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS user_cooldowns (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            aircraft_hex VARCHAR(6) NOT NULL,
            last_report_time TIMESTAMP NOT NULL,
            last_latitude NUMERIC(10, 7),
            last_longitude NUMERIC(11, 7),
            report_count INTEGER DEFAULT 1,
            UNIQUE(user_id, aircraft_hex)
        )
    """)
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_user_cooldown_user_hex ON user_cooldowns(user_id, aircraft_hex)"
        )
    )

    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS user_usage (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            period_start DATE NOT NULL,
            period_type VARCHAR(10) DEFAULT 'monthly',
            reports_sent INTEGER DEFAULT 0,
            emails_sent INTEGER DEFAULT 0,
            UNIQUE(user_id, period_start, period_type)
        )
    """)
    )
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_usage_user ON user_usage(user_id)"))


def _create_multi_user_tables_sqlite(session: Session) -> None:
    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email VARCHAR(255) UNIQUE NOT NULL,
            name VARCHAR(100),
            status VARCHAR(20) DEFAULT 'active',
            api_key VARCHAR(64) UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    )
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_email ON users(email)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_status ON users(status)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_api_key ON users(api_key)"))

    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tier VARCHAR(20) DEFAULT 'basic',
            status VARCHAR(20) DEFAULT 'active',
            enable_maps BOOLEAN DEFAULT 1,
            enable_aircraft_images BOOLEAN DEFAULT 1,
            starts_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    )
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_subscription_user ON subscriptions(user_id)")
    )
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_subscription_status ON subscriptions(status)")
    )

    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS user_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            description TEXT,
            filter_sql TEXT NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            priority INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    )
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_user_filter_user ON user_filters(user_id)")
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_user_filter_active ON user_filters(user_id, is_active)"
        )
    )

    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS user_cooldowns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            aircraft_hex VARCHAR(6) NOT NULL,
            last_report_time DATETIME NOT NULL,
            last_latitude NUMERIC(10, 7),
            last_longitude NUMERIC(11, 7),
            report_count INTEGER DEFAULT 1,
            UNIQUE(user_id, aircraft_hex)
        )
    """)
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_user_cooldown_user_hex ON user_cooldowns(user_id, aircraft_hex)"
        )
    )

    session.execute(
        text("""
        CREATE TABLE IF NOT EXISTS user_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            period_start DATE NOT NULL,
            period_type VARCHAR(10) DEFAULT 'monthly',
            reports_sent INTEGER DEFAULT 0,
            emails_sent INTEGER DEFAULT 0,
            UNIQUE(user_id, period_start, period_type)
        )
    """)
    )
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_usage_user ON user_usage(user_id)"))
