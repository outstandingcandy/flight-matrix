"""Database health checks.

This module verifies database connectivity, schema integrity, and
basic CRUD operations for the flight-matrix system.
"""

import logging
import time
import uuid
from typing import Any

from tests.deployment_check.base import BaseHealthCheck, CheckResult, CheckStatus

logger = logging.getLogger("deployment_check.database")

# Required tables in the flight-matrix schema
REQUIRED_TABLES = [
    "aircraft_snapshots",
    "aircraft_static_info",
    "airports",
    "report_cooldowns",
]

# Tables required for multi-user mode
MULTI_USER_TABLES = [
    "users",
    "subscriptions",
    "user_filters",
    "user_cooldowns",
    "user_usage",
]

# Optional tables that may exist
OPTIONAL_TABLES = [
    "scraper_tasks",
    "geographic_regions",
    "flight_schedules",
]


class DatabaseHealthCheck(BaseHealthCheck):
    """Health checks for database connectivity and schema.

    Checks performed:
    - PostgreSQL/SQLite/MySQL connection
    - Required tables exist
    - Multi-user tables exist (if multi-user mode enabled)
    - Basic SELECT query execution
    - Write/Read/Delete cycle test
    """

    category = "Database"

    def __init__(self, config: Any | None = None) -> None:
        """Initialize database health check.

        Args:
            config: YAMLConfig instance for accessing database configuration.
        """
        super().__init__(config)
        self._db_manager: Any | None = None

    async def run(self) -> list[CheckResult]:
        """Execute all database checks.

        Returns:
            List of CheckResult objects for each database check.
        """
        results: list[CheckResult] = []

        # Check database connection
        conn_result = self._check_connection()
        results.append(conn_result)

        if conn_result.status == CheckStatus.FAIL:
            return results

        # Check required tables
        results.extend(self._check_tables(REQUIRED_TABLES, required=True))

        # Check optional tables
        results.extend(self._check_tables(OPTIONAL_TABLES, required=False))

        # Check multi-user tables if multi-user mode is enabled
        if self.config:
            multi_user_config = self.config.get_multi_user_config()
            if multi_user_config.get("enabled", False):
                results.extend(self._check_tables(MULTI_USER_TABLES, required=True))

        # Check basic query execution
        results.append(self._check_basic_query())

        # Check write/read/delete cycle
        results.append(self._check_write_read_delete())

        return results

    def _check_connection(self) -> CheckResult:
        """Check database connection.

        Returns:
            CheckResult indicating if database connection succeeds.
        """
        start_time = time.perf_counter()

        if not self.config:
            return self._fail(
                "Database connection",
                "Config not loaded",
                start_time,
            )

        try:
            from src.utils.database import DatabaseManager

            db_config = self.config.get_database_config()
            db_url = db_config.get("url")

            if not db_url:
                return self._fail(
                    "Database connection",
                    "Database URL not configured",
                    start_time,
                )

            self._db_manager = DatabaseManager(db_url)

            # Test connection by getting a session
            from sqlalchemy import text

            with self._db_manager.get_session() as session:
                # Execute simple query to verify connection
                session.execute(text("SELECT 1"))

            dialect = "unknown"
            if self._db_manager.is_postgres:
                dialect = "PostgreSQL"
            elif self._db_manager.is_sqlite:
                dialect = "SQLite"
            elif self._db_manager.is_mysql:
                dialect = "MySQL"

            return self._pass(
                "Database connection",
                f"Connected ({dialect})",
                start_time,
                {"dialect": dialect},
            )

        except ImportError as e:
            return self._fail(
                "Database connection",
                f"Import error: {e}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "Database connection",
                f"Connection failed: {e}",
                start_time,
            )

    def _check_tables(self, tables: list[str], required: bool) -> list[CheckResult]:
        """Check that specified tables exist.

        Args:
            tables: List of table names to check.
            required: Whether missing tables should be FAIL or SKIP.

        Returns:
            List of CheckResult objects for each table.
        """
        results: list[CheckResult] = []

        if not self._db_manager:
            return results

        for table_name in tables:
            start_time = time.perf_counter()

            try:
                with self._db_manager.get_session() as session:
                    # Check if table exists using database-agnostic approach
                    if self._db_manager.is_postgres:
                        query = """
                            SELECT EXISTS (
                                SELECT FROM information_schema.tables
                                WHERE table_name = :table_name
                            )
                        """
                    elif self._db_manager.is_sqlite:
                        query = """
                            SELECT EXISTS (
                                SELECT 1 FROM sqlite_master
                                WHERE type='table' AND name = :table_name
                            )
                        """
                    else:  # MySQL
                        query = """
                            SELECT EXISTS (
                                SELECT 1 FROM information_schema.tables
                                WHERE table_name = :table_name
                            )
                        """

                    from sqlalchemy import text

                    result = session.execute(text(query), {"table_name": table_name}).scalar()

                    if result:
                        results.append(
                            self._pass(
                                f"Table: {table_name}",
                                "Exists",
                                start_time,
                            )
                        )
                    elif required:
                        results.append(
                            self._fail(
                                f"Table: {table_name}",
                                "Table does not exist",
                                start_time,
                            )
                        )
                    else:
                        results.append(
                            self._skip(
                                f"Table: {table_name}",
                                "Optional table not found",
                                start_time,
                            )
                        )

            except Exception as e:
                results.append(
                    self._fail(
                        f"Table: {table_name}",
                        f"Error checking: {e}",
                        start_time,
                    )
                )

        return results

    def _check_basic_query(self) -> CheckResult:
        """Check that basic SELECT query works.

        Returns:
            CheckResult indicating if basic query execution succeeds.
        """
        start_time = time.perf_counter()

        if not self._db_manager:
            return self._fail(
                "Basic query execution",
                "Database manager not initialized",
                start_time,
            )

        try:
            with self._db_manager.get_session() as session:
                from sqlalchemy import text

                # Try to count rows in aircraft_snapshots
                result = session.execute(text("SELECT COUNT(*) FROM aircraft_snapshots")).scalar()

                return self._pass(
                    "Basic query execution",
                    f"SELECT succeeded ({result} snapshots)",
                    start_time,
                    {"snapshot_count": result},
                )

        except Exception as e:
            return self._fail(
                "Basic query execution",
                f"Query failed: {e}",
                start_time,
            )

    def _check_write_read_delete(self) -> CheckResult:
        """Check write/read/delete cycle on report_cooldowns table.

        This uses the report_cooldowns table for testing since it's
        designed for ephemeral data and won't affect core functionality.

        Returns:
            CheckResult indicating if CRUD operations succeed.
        """
        start_time = time.perf_counter()

        if not self._db_manager:
            return self._fail(
                "Write/Read/Delete test",
                "Database manager not initialized",
                start_time,
            )

        # aircraft_hex is varchar(6), so use short test value
        test_hex = uuid.uuid4().hex[:6].upper()

        try:
            from sqlalchemy import text

            with self._db_manager.get_session() as session:
                # Write
                if self._db_manager.is_postgres:
                    insert_query = text("""
                        INSERT INTO report_cooldowns (aircraft_hex, last_report_time, last_latitude, last_longitude)
                        VALUES (:hex, NOW(), 0.0, 0.0)
                    """)
                else:
                    insert_query = text("""
                        INSERT INTO report_cooldowns (aircraft_hex, last_report_time, last_latitude, last_longitude)
                        VALUES (:hex, CURRENT_TIMESTAMP, 0.0, 0.0)
                    """)

                session.execute(insert_query, {"hex": test_hex})
                session.commit()

                # Read
                select_query = text(
                    "SELECT aircraft_hex FROM report_cooldowns WHERE aircraft_hex = :hex"
                )
                result = session.execute(select_query, {"hex": test_hex}).fetchone()

                if not result:
                    return self._fail(
                        "Write/Read/Delete test",
                        "Read after write failed",
                        start_time,
                    )

                # Delete
                delete_query = text("DELETE FROM report_cooldowns WHERE aircraft_hex = :hex")
                session.execute(delete_query, {"hex": test_hex})
                session.commit()

                # Verify deletion
                result = session.execute(select_query, {"hex": test_hex}).fetchone()
                if result:
                    return self._fail(
                        "Write/Read/Delete test",
                        "Delete verification failed",
                        start_time,
                    )

                return self._pass(
                    "Write/Read/Delete test",
                    "CRUD cycle completed",
                    start_time,
                )

        except Exception as e:
            # Cleanup attempt
            try:
                with self._db_manager.get_session() as session:
                    from sqlalchemy import text

                    session.execute(
                        text("DELETE FROM report_cooldowns WHERE aircraft_hex = :hex"),
                        {"hex": test_hex},
                    )
                    session.commit()
            except Exception:
                pass

            return self._fail(
                "Write/Read/Delete test",
                f"CRUD test failed: {e}",
                start_time,
            )
