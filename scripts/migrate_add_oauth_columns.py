#!/usr/bin/env python3
"""Add native-login subject / openid columns to the ``users`` table.

Introduces the columns that ``UserService.find_or_create_by_wechat`` /
``find_or_create_by_google_sub`` / ``find_or_create_by_apple_sub`` write
into. All columns are nullable — pre-existing rows (admin-created users,
cookie-session Google users) never populated these fields and don't need
back-fill.

Columns added:

+---------------------+---------------+----------------------------------+
| name                | type          | rationale                        |
+=====================+===============+==================================+
| google_sub          | VARCHAR(255)  | Google OIDC ``sub`` claim.       |
| apple_sub           | VARCHAR(255)  | Apple ``sub`` claim — stable     |
|                     |               | even when the email is relayed.  |
| wechat_unionid      | VARCHAR(64)   | Cross-app stable id under one    |
|                     |               | Weixin Open Platform account.    |
| wechat_openid       | VARCHAR(64)   | Per-(AppID, user) id.            |
| wechat_platform     | VARCHAR(16)   | ``mp`` (mini-program) or         |
|                     |               | ``app`` (native mobile).         |
+---------------------+---------------+----------------------------------+

Indexes added:

- Regular btree on each of ``google_sub``, ``apple_sub``,
  ``wechat_unionid`` for the sub-first lookup path.
- Partial unique index on ``(wechat_openid, wechat_platform)`` WHERE
  ``wechat_openid IS NOT NULL`` — many pre-migration rows share NULL
  so a plain UNIQUE would collide; the partial form is Postgres-specific
  syntax and is emitted only for postgres.

Idempotent: safe to run twice. Each column and index is checked before
creation.

Usage:
    python scripts/migrate_add_oauth_columns.py --config config/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.yaml_config import YAMLConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


COLUMNS = [
    ("google_sub", "VARCHAR(255)"),
    ("apple_sub", "VARCHAR(255)"),
    ("wechat_unionid", "VARCHAR(64)"),
    ("wechat_openid", "VARCHAR(64)"),
    ("wechat_platform", "VARCHAR(16)"),
]

BTREE_INDEXES = [
    ("idx_user_google_sub", "google_sub"),
    ("idx_user_apple_sub", "apple_sub"),
    ("idx_user_wechat_unionid", "wechat_unionid"),
]


def _is_postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


def _column_exists(conn, table: str, column: str) -> bool:
    """Cross-dialect column existence check."""
    if conn.engine.dialect.name == "postgresql":
        result = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = :t AND column_name = :c
                """
            ),
            {"t": table, "c": column},
        ).fetchone()
    else:
        # SQLite
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(row[1] == column for row in rows)
    return result is not None


def _index_exists(conn, index_name: str) -> bool:
    """Cross-dialect index existence check."""
    if conn.engine.dialect.name == "postgresql":
        result = conn.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
            {"n": index_name},
        ).fetchone()
    else:
        result = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='index' AND name = :n"),
            {"n": index_name},
        ).fetchone()
    return result is not None


def migrate(engine: Engine) -> None:
    """Idempotently add the OAuth columns + their indexes."""
    is_pg = _is_postgres(engine)

    # Columns come first — indexes reference them.
    with engine.connect() as conn:
        for name, sql_type in COLUMNS:
            if _column_exists(conn, "users", name):
                logger.info("Column users.%s already present", name)
                continue
            try:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {sql_type}"))
                conn.commit()
                logger.info("Added column users.%s (%s)", name, sql_type)
            except SQLAlchemyError as e:
                conn.rollback()
                logger.error("Failed to add users.%s: %s", name, e)
                raise

    # Non-unique btree indexes on each sub column.
    with engine.connect() as conn:
        for index_name, column in BTREE_INDEXES:
            if _index_exists(conn, index_name):
                logger.info("Index %s already present", index_name)
                continue
            try:
                conn.execute(text(f"CREATE INDEX {index_name} ON users ({column})"))
                conn.commit()
                logger.info("Created index %s on users(%s)", index_name, column)
            except SQLAlchemyError as e:
                conn.rollback()
                logger.error("Failed to create %s: %s", index_name, e)
                raise

    # Partial UNIQUE (wechat_openid, wechat_platform).
    # Postgres: partial index. SQLite: plain UNIQUE index — SQLite treats
    # NULL as distinct in a UNIQUE index by default, so multiple NULL rows
    # don't collide there either.
    partial_index = "idx_user_wechat_openid_platform"
    with engine.connect() as conn:
        if _index_exists(conn, partial_index):
            logger.info("Index %s already present", partial_index)
        else:
            try:
                if is_pg:
                    conn.execute(
                        text(
                            f"CREATE UNIQUE INDEX {partial_index} ON users "
                            "(wechat_openid, wechat_platform) "
                            "WHERE wechat_openid IS NOT NULL"
                        )
                    )
                else:
                    conn.execute(
                        text(
                            f"CREATE UNIQUE INDEX {partial_index} ON users "
                            "(wechat_openid, wechat_platform)"
                        )
                    )
                conn.commit()
                logger.info("Created unique index %s", partial_index)
            except SQLAlchemyError as e:
                conn.rollback()
                logger.error("Failed to create %s: %s", partial_index, e)
                raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the YAML config (default: config/config.yaml)",
    )
    parser.add_argument(
        "--database-url",
        help="Override the DATABASE_URL (default: read from config / env)",
    )
    args = parser.parse_args()

    if args.database_url:
        db_url = args.database_url
    else:
        cfg = YAMLConfig(args.config)
        db_url = cfg.get_database_config()["url"]

    logger.info("Running OAuth column migration against %s", db_url.split("@")[-1])
    engine = create_engine(db_url)
    migrate(engine)
    logger.info("OAuth column migration complete")


if __name__ == "__main__":
    main()
