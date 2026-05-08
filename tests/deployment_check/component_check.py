"""Application component health checks.

This module verifies that core application components can be
imported and initialized correctly.
"""

import logging
import time

from tests.deployment_check.base import BaseHealthCheck, CheckResult

logger = logging.getLogger("deployment_check.component")


class ComponentHealthCheck(BaseHealthCheck):
    """Health checks for application components.

    Checks performed:
    - Core module imports
    - Flask application initialization
    - DatabaseManager instantiation
    - TrackService instantiation
    - ReportService instantiation
    """

    category = "Application Components"

    async def run(self) -> list[CheckResult]:
        """Execute all component checks.

        Returns:
            List of CheckResult objects for each component check.
        """
        results: list[CheckResult] = []

        # Check module imports
        results.extend(self._check_module_imports())

        # Check Flask app initialization
        results.append(self._check_flask_app_init())

        # Check DatabaseManager
        results.append(self._check_database_manager_init())

        # Check TrackService
        results.append(self._check_track_service_init())

        # Check ReportService
        results.append(self._check_report_service_init())

        return results

    def _check_module_imports(self) -> list[CheckResult]:
        """Check that core modules can be imported.

        Returns:
            List of CheckResult objects for each module import.
        """
        results: list[CheckResult] = []

        core_modules = [
            ("src.core.exceptions", "Core exceptions"),
            ("src.utils.yaml_config", "YAML configuration"),
            ("src.utils.database", "Database utilities"),
            ("src.data.models", "Data models"),
            ("src.services.track_service", "Track service"),
            ("src.services.report_service", "Report service"),
            ("src.notifications.factory", "Notification factory"),
            ("src.analysis", "Analysis module"),
        ]

        for module_path, display_name in core_modules:
            start_time = time.perf_counter()

            try:
                __import__(module_path)
                results.append(
                    self._pass(
                        f"Import: {display_name}",
                        f"Module loaded: {module_path}",
                        start_time,
                    )
                )
            except ImportError as e:
                results.append(
                    self._fail(
                        f"Import: {display_name}",
                        f"Import failed: {e}",
                        start_time,
                    )
                )
            except Exception as e:
                results.append(
                    self._fail(
                        f"Import: {display_name}",
                        f"Error during import: {e}",
                        start_time,
                    )
                )

        return results

    def _check_flask_app_init(self) -> CheckResult:
        """Check Flask application can be initialized.

        Returns:
            CheckResult indicating if Flask app initializes.
        """
        start_time = time.perf_counter()

        try:
            # Check if web app module exists and can create app
            from src.web.app import create_app

            # Create app with test config
            app = create_app()

            if app:
                return self._pass(
                    "Flask app initialization",
                    "Application created successfully",
                    start_time,
                    {"app_name": app.name},
                )
            else:
                return self._fail(
                    "Flask app initialization",
                    "create_app() returned None",
                    start_time,
                )

        except ImportError as e:
            return self._skip(
                "Flask app initialization",
                f"Web module not found: {e}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "Flask app initialization",
                f"Initialization failed: {e}",
                start_time,
            )

    def _check_database_manager_init(self) -> CheckResult:
        """Check DatabaseManager can be instantiated.

        Returns:
            CheckResult indicating if DatabaseManager initializes.
        """
        start_time = time.perf_counter()

        if not self.config:
            return self._skip(
                "DatabaseManager initialization",
                "Config not loaded",
                start_time,
            )

        try:
            from src.utils.database import DatabaseManager

            db_config = self.config.get_database_config()
            db_url = db_config.get("url")

            if not db_url:
                return self._skip(
                    "DatabaseManager initialization",
                    "Database URL not configured",
                    start_time,
                )

            # Create manager instance
            db_manager = DatabaseManager(db_url)

            dialect = "unknown"
            if db_manager.is_postgres:
                dialect = "PostgreSQL"
            elif db_manager.is_sqlite:
                dialect = "SQLite"
            elif db_manager.is_mysql:
                dialect = "MySQL"

            return self._pass(
                "DatabaseManager initialization",
                f"Created ({dialect})",
                start_time,
                {"dialect": dialect},
            )

        except Exception as e:
            return self._fail(
                "DatabaseManager initialization",
                f"Failed: {e}",
                start_time,
            )

    def _check_track_service_init(self) -> CheckResult:
        """Check TrackService can be instantiated.

        Returns:
            CheckResult indicating if TrackService initializes.
        """
        start_time = time.perf_counter()

        try:
            from src.services.track_service import TrackService

            # TrackService takes config_file parameter
            _ = TrackService(config_file="config/config.yaml")

            return self._pass(
                "TrackService initialization",
                "Service created successfully",
                start_time,
            )

        except ImportError as e:
            return self._fail(
                "TrackService initialization",
                f"Import failed: {e}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "TrackService initialization",
                f"Failed: {e}",
                start_time,
            )

    def _check_report_service_init(self) -> CheckResult:
        """Check ReportService can be instantiated.

        Returns:
            CheckResult indicating if ReportService initializes.
        """
        start_time = time.perf_counter()

        try:
            from src.services.report_service import ReportService

            # ReportService takes config_file parameter
            _ = ReportService(config_file="config/config.yaml")

            return self._pass(
                "ReportService initialization",
                "Service created successfully",
                start_time,
            )

        except ImportError as e:
            return self._fail(
                "ReportService initialization",
                f"Import failed: {e}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "ReportService initialization",
                f"Failed: {e}",
                start_time,
            )
