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
        # A failed statement puts a Postgres transaction into the aborted state,
        # where every following command -- including the CREATE TABLE below --
        # raises InFailedSqlTransaction. The probe failing is the normal path on
        # a fresh database, so it has to be rolled back rather than swallowed.
        # SQLite does not need this, which is why it went unnoticed.
        session.rollback()

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


def ensure_scraper_tables(session: Session, *, is_postgres: bool) -> None:
    """Create the tables the scraper writes and the web app reads.

    Covers scraper_tasks, scraper_workers, scraper_results and
    aircraft_realtime_positions.

    The scraper framework creates these at worker startup —
    `TaskQueue.ensure_tables_exist()` for the first three,
    `FR24MapSink._ensure_table()` for the last — using Postgres-specific DDL.
    The web app reads them too and fails on a fresh SQLite DB without them, so
    this helper builds them early with dialect-appropriate types.

    Idempotent: returns early if the tables already exist.
    """
    if is_postgres:
        pk = "BIGSERIAL PRIMARY KEY"
        ts = "TIMESTAMPTZ"
        ts_default_now = "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
        json_col = "JSONB"
        json_default = "JSONB NOT NULL DEFAULT '{}'"
        numeric = "NUMERIC(10, 3)"
        dbl = "DOUBLE PRECISION"
    else:
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
        ts = "DATETIME"
        ts_default_now = "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
        json_col = "TEXT"
        json_default = "TEXT NOT NULL DEFAULT '{}'"
        numeric = "REAL"
        dbl = "REAL"

    _ensure_realtime_positions_table(session, pk=pk, ts=ts, ts_default_now=ts_default_now, dbl=dbl)

    try:
        session.execute(text("SELECT 1 FROM scraper_tasks LIMIT 1"))
        logger.debug("Scraper tables already exist")
        return
    except Exception:
        # Roll back the aborted transaction; see ensure_report_cooldowns_table.
        session.rollback()

    try:
        session.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS scraper_tasks (
                    id {pk},
                    task_type VARCHAR(50) NOT NULL,
                    task_key VARCHAR(500) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    payload {json_default},
                    claimed_by VARCHAR(100),
                    claimed_at {ts},
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT,
                    result {json_col},
                    scheduled_for {ts_default_now},
                    created_at {ts_default_now},
                    completed_at {ts},
                    heartbeat_at {ts}
                )
            """)
        )
        session.execute(
            text("CREATE INDEX IF NOT EXISTS idx_scraper_tasks_status ON scraper_tasks(status)")
        )
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_type_key "
                "ON scraper_tasks(task_type, task_key)"
            )
        )
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_scraper_tasks_scheduled "
                "ON scraper_tasks(scheduled_for)"
            )
        )

        session.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS scraper_workers (
                    id {pk},
                    worker_id VARCHAR(100) UNIQUE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    last_heartbeat {ts_default_now},
                    tasks_completed INTEGER NOT NULL DEFAULT 0,
                    current_task_id INTEGER,
                    started_at {ts} DEFAULT CURRENT_TIMESTAMP,
                    metadata {json_default}
                )
            """)
        )

        session.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS scraper_results (
                    id {pk},
                    task_id INTEGER REFERENCES scraper_tasks(id),
                    worker_id VARCHAR(100) NOT NULL,
                    success BOOLEAN NOT NULL,
                    duration_seconds {numeric},
                    result {json_col},
                    error TEXT,
                    created_at {ts_default_now}
                )
            """)
        )
        session.execute(
            text("CREATE INDEX IF NOT EXISTS idx_scraper_results_task ON scraper_results(task_id)")
        )

        session.commit()
        logger.info("Scraper tables created successfully")
    except Exception as e:
        session.rollback()
        logger.error(f"Error creating scraper tables: {e}")
        raise


def _ensure_realtime_positions_table(
    session: Session, *, pk: str, ts: str, ts_default_now: str, dbl: str
) -> None:
    """Create aircraft_realtime_positions if missing.

    Mirrors `FR24MapSink._ensure_table`, which is the writer. Kept separate from
    the scraper_tasks block because that block short-circuits on scraper_tasks
    alone, and this table was added later — a database bootstrapped before it
    existed has the task tables but not this one.

    Args:
        session: Session to execute DDL on.
        pk: Dialect-appropriate primary-key clause.
        ts: Dialect-appropriate timestamp type.
        ts_default_now: Timestamp type with a now() default.
        dbl: Dialect-appropriate double-precision type.
    """
    try:
        session.execute(text("SELECT 1 FROM aircraft_realtime_positions LIMIT 1"))
        return
    except Exception:
        session.rollback()

    try:
        session.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS aircraft_realtime_positions (
                    id {pk},
                    fr24_id VARCHAR(32),
                    flight_number VARCHAR(16),
                    callsign VARCHAR(16),
                    registration VARCHAR(16),
                    aircraft_type VARCHAR(8),
                    latitude {dbl},
                    longitude {dbl},
                    altitude INTEGER,
                    ground_speed INTEGER,
                    heading INTEGER,
                    vertical_speed INTEGER,
                    squawk VARCHAR(8),
                    origin_iata VARCHAR(4),
                    destination_iata VARCHAR(4),
                    on_ground BOOLEAN DEFAULT FALSE,
                    fr24_timestamp {ts},
                    scraped_at {ts_default_now},
                    scrape_task_key VARCHAR(64),
                    UNIQUE (fr24_id, fr24_timestamp)
                )
            """)
        )
        for column in ("fr24_id", "registration", "scraped_at", "flight_number"):
            session.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS idx_aircraft_realtime_{column} "
                    f"ON aircraft_realtime_positions({column})"
                )
            )
        session.commit()
        logger.info("aircraft_realtime_positions table created successfully")
    except Exception as e:
        session.rollback()
        logger.error(f"Error creating aircraft_realtime_positions table: {e}")
        raise


def ensure_xiaohongshu_tables(session: Session, *, is_postgres: bool) -> None:
    """Create xiaohongshu_notes / xiaohongshu_comments tables if missing.

    Mirror the production schema these admin routes query. Dialect-aware.
    """
    try:
        session.execute(text("SELECT 1 FROM xiaohongshu_notes LIMIT 1"))
        return
    except Exception:
        # Roll back the aborted transaction; see ensure_report_cooldowns_table.
        session.rollback()

    if is_postgres:
        pk = "BIGSERIAL PRIMARY KEY"
        ts = "TIMESTAMPTZ"
        ts_default_now = "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
    else:
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
        ts = "DATETIME"
        ts_default_now = "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"

    # Columns chosen to match what web_app.py's admin queries reference.
    # Production Postgres has these as JSONB; on SQLite we store TEXT.
    json_col = "JSONB" if is_postgres else "TEXT"
    try:
        session.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS xiaohongshu_notes (
                    id {pk},
                    note_id VARCHAR(100) UNIQUE NOT NULL,
                    title TEXT,
                    content TEXT,
                    author_id VARCHAR(100),
                    author_name VARCHAR(200),
                    like_count INTEGER DEFAULT 0,
                    comment_count INTEGER DEFAULT 0,
                    collect_count INTEGER DEFAULT 0,
                    share_count INTEGER DEFAULT 0,
                    publish_time {ts},
                    scraped_at {ts_default_now},
                    updated_at {ts},
                    note_created_at {ts},
                    source_url TEXT,
                    image_paths {json_col},
                    image_urls {json_col},
                    video_url TEXT,
                    comments {json_col},
                    tags {json_col},
                    location VARCHAR(255)
                )
            """)
        )
        session.execute(
            text(f"""
                CREATE TABLE IF NOT EXISTS xiaohongshu_comments (
                    id {pk},
                    note_id VARCHAR(100) NOT NULL,
                    comment_id VARCHAR(100),
                    content TEXT,
                    author_id VARCHAR(100),
                    author_name VARCHAR(200),
                    like_count INTEGER DEFAULT 0,
                    publish_time {ts},
                    scraped_at {ts_default_now}
                )
            """)
        )
        session.commit()
        logger.info("Xiaohongshu tables created successfully")
    except Exception as e:
        session.rollback()
        logger.error(f"Error creating xiaohongshu tables: {e}")
        raise


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
        # Roll back the aborted transaction; see ensure_report_cooldowns_table.
        session.rollback()

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
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            google_sub VARCHAR(255),
            apple_sub VARCHAR(255),
            wechat_unionid VARCHAR(64),
            wechat_openid VARCHAR(64),
            wechat_platform VARCHAR(16)
        )
    """)
    )
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_email ON users(email)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_status ON users(status)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_api_key ON users(api_key)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_google_sub ON users(google_sub)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_apple_sub ON users(apple_sub)"))
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_user_wechat_unionid ON users(wechat_unionid)")
    )
    # Partial UNIQUE so pre-existing rows with NULL wechat_openid don't collide.
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_wechat_openid_platform "
            "ON users (wechat_openid, wechat_platform) "
            "WHERE wechat_openid IS NOT NULL"
        )
    )

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
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            google_sub VARCHAR(255),
            apple_sub VARCHAR(255),
            wechat_unionid VARCHAR(64),
            wechat_openid VARCHAR(64),
            wechat_platform VARCHAR(16)
        )
    """)
    )
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_email ON users(email)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_status ON users(status)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_api_key ON users(api_key)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_google_sub ON users(google_sub)"))
    session.execute(text("CREATE INDEX IF NOT EXISTS idx_user_apple_sub ON users(apple_sub)"))
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_user_wechat_unionid ON users(wechat_unionid)")
    )
    # SQLite treats each NULL as distinct in a UNIQUE index by default, so a
    # plain UNIQUE works here — no partial-index syntax needed.
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_wechat_openid_platform "
            "ON users (wechat_openid, wechat_platform)"
        )
    )

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
