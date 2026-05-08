"""
Report Generation Service

Independent service that filters aircraft from database and generates reports.
This service runs separately from the tracking service and handles:
- Filtering aircraft using custom SQL from config.yaml
- Cooldown management to avoid duplicate reports
- AI analysis generation
- Map and image generation
- Email sending

Supports both single-user mode (backward compatible) and multi-user mode
with per-user filters, cooldowns, and quotas.
"""

import asyncio
import logging
from datetime import datetime

from src.notifications import NotificationOrchestratorFactory
from src.reporting.registration_match_filter import RegistrationMatchFilterEngine
from src.reporting.schedule_filter import ScheduleFilterEngine
from src.reporting.sql_filter import SQLFilterEngine
from src.reporting.subject_generator import ReportSubjectGenerator
from src.utils.database import DatabaseManager
from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("report_service")


class ReportService:
    """Independent service for generating and sending aircraft reports.

    This service filters aircraft from the database using custom SQL
    and generates reports for aircraft that pass the filter and cooldown checks.

    Supports two modes:
    - Single-user mode (default): Uses global config for all recipients
    - Multi-user mode: Per-user filters, cooldowns, and quotas
    """

    def __init__(self, config_file: str = "config.yaml"):
        """Initialize the report service.

        Args:
            config_file: Path to YAML configuration file
        """
        self.config_file = config_file
        self.yaml_config = YAMLConfig(config_file)
        self.db = self._init_database()
        self.sql_filter = SQLFilterEngine(self.db, self.yaml_config)
        self.schedule_filter = ScheduleFilterEngine(self.db, self.yaml_config)
        self.registration_match_filter = RegistrationMatchFilterEngine(self.db, self.yaml_config)
        self.notifier = self._init_notifier()
        self.subject_generator = ReportSubjectGenerator()

        # Check if multi-user mode is enabled
        self.multi_user_enabled = self.yaml_config.is_multi_user_enabled()

        # Initialize multi-user services if enabled
        self.user_service = None
        self.subscription_service = None
        self.filter_service = None

        if self.multi_user_enabled:
            self._init_multi_user_services()

        # Configuration
        report_service_config = self.yaml_config.config.get("report_service", {})
        self.poll_interval = report_service_config.get("poll_interval", 30)
        self.batch_size = report_service_config.get("batch_size", 10)
        self.max_retries = report_service_config.get("max_retries", 3)
        self.processing_timeout = report_service_config.get("processing_timeout", 300)

        # Cooldown configuration (used in single-user mode)
        reporting_config = self.yaml_config.get_reporting_config()
        self.cooldown_hours = reporting_config.get("cooldown_hours", 1.0)
        self.min_move_distance_km = reporting_config.get("min_move_distance_km", 1.0)

        # Runtime state
        self.is_running = False
        self._processed_count = 0
        self._failed_count = 0
        self._start_time = None

        mode = "multi-user" if self.multi_user_enabled else "single-user"
        logger.info(
            f"Report Service initialized in {mode} mode "
            f"(poll_interval={self.poll_interval}s, batch_size={self.batch_size})"
        )

    def _init_multi_user_services(self):
        """Initialize services for multi-user mode."""
        try:
            from src.services.filter_service import FilterService
            from src.services.subscription_service import SubscriptionService
            from src.services.user_service import UserService

            # Ensure multi-user tables exist
            self.db.ensure_multi_user_tables_exist()

            self.user_service = UserService(self.db)
            self.subscription_service = SubscriptionService(self.db, self.yaml_config)
            self.filter_service = FilterService(self.db)

            logger.info("Multi-user services initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize multi-user services: {e}")
            self.multi_user_enabled = False

    def _init_database(self) -> DatabaseManager:
        """Initialize database connection."""
        db_config = self.yaml_config.get_database_config()
        return DatabaseManager(db_config["url"])

    def _init_notifier(self):
        """Initialize notification orchestrator."""
        return NotificationOrchestratorFactory.create(self.yaml_config, self.db)

    async def run_forever(self):
        """Run the report service continuously.

        Filters aircraft from database and processes reports in batches.
        """
        self.is_running = True
        self._start_time = datetime.now()
        logger.info(f"Starting report service with {self.poll_interval}s poll interval")

        while self.is_running:
            try:
                await self._process_filtered_aircraft()
                await asyncio.sleep(self.poll_interval)
            except KeyboardInterrupt:
                logger.info("Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"Error in report service loop: {e}")
                await asyncio.sleep(60)  # Wait before retrying

        logger.info("Report service stopped")

    async def run_once(self):
        """Process one batch of filtered aircraft and return.

        Useful for testing or one-time processing.
        """
        self._start_time = datetime.now()
        await self._process_filtered_aircraft()

    async def _process_filtered_aircraft(self):
        """Filter aircraft from database and process reports.

        Routes to single-user or multi-user processing based on configuration.
        Each iteration fetches fresh user configurations from the database.
        """
        logger.debug("Starting new processing cycle - fetching latest configurations from database")

        if self.multi_user_enabled:
            await self._process_multi_user()
        else:
            await self._process_single_user()

    async def _process_single_user(self):
        """Process reports in single-user mode with dual-mode filtering.

        Executes both snapshot-based (Mode A) and schedule-based (Mode B) filters,
        merges results with deduplication, and processes reports with independent cooldowns.
        """
        # Get filter mode configurations
        filter_modes = self.yaml_config.get("reporting.filter_modes", {})
        snapshot_config = filter_modes.get("snapshot_based", {"enabled": True})
        schedule_config = filter_modes.get("schedule_based", {"enabled": False})

        all_aircraft = []
        aircraft_sources = {}  # Track which mode found each aircraft

        # Mode A: Snapshot-based filtering (existing logic)
        if snapshot_config.get("enabled", True):
            snapshot_aircraft = self._execute_snapshot_filter(snapshot_config)
            for aircraft in snapshot_aircraft:
                hex_code = aircraft.get("hex")
                if hex_code:
                    aircraft_sources.setdefault(hex_code, []).append("snapshot")
                    all_aircraft.append(aircraft)
            logger.info(f"Mode A (snapshot): found {len(snapshot_aircraft)} aircraft")

        # Mode B: Schedule-based filtering (new)
        if schedule_config.get("enabled", False):
            schedule_aircraft = self._execute_schedule_filter(schedule_config)
            for aircraft in schedule_aircraft:
                hex_code = aircraft.get("hex")
                if hex_code:
                    aircraft_sources.setdefault(hex_code, []).append("schedule")
                    # Only add if not already found by snapshot filter
                    if "snapshot" not in aircraft_sources.get(hex_code, []):
                        all_aircraft.append(aircraft)
            logger.info(f"Mode B (schedule): found {len(schedule_aircraft)} aircraft")

        # Mode C: Registration match-based filtering
        regmatch_config = filter_modes.get("registration_match_based", {"enabled": False})
        if regmatch_config.get("enabled", False):
            regmatch_aircraft = self.registration_match_filter.execute_filter()
            seen_hex = {a.get("hex") for a in all_aircraft if a.get("hex")}
            for aircraft in regmatch_aircraft:
                hex_code = aircraft.get("hex")
                if hex_code:
                    aircraft_sources.setdefault(hex_code, []).append("regmatch")
                    # Only add if not already found by other filters
                    if hex_code not in seen_hex:
                        all_aircraft.append(aircraft)
                        seen_hex.add(hex_code)
            logger.info(f"Mode C (regmatch): found {len(regmatch_aircraft)} aircraft")

        # Deduplicate by hex
        seen_hex = set()
        unique_aircraft = []
        for aircraft in all_aircraft:
            hex_code = aircraft.get("hex")
            if hex_code and hex_code not in seen_hex:
                seen_hex.add(hex_code)
                unique_aircraft.append(aircraft)

        if not unique_aircraft:
            logger.debug("No aircraft matched any filter criteria")
            return

        logger.info(f"Total unique aircraft from all modes: {len(unique_aircraft)}")

        recipients = self._get_recipients()
        if not recipients:
            logger.warning("No recipients configured, skipping report processing")
            return

        # Apply cooldowns and process reports
        for aircraft in unique_aircraft:
            hex_code = aircraft.get("hex", "")
            sources = aircraft_sources.get(hex_code, [])

            # Check cooldowns for each source that found this aircraft
            should_report = False
            active_source = None

            for source in sources:
                if source == "snapshot":
                    config = snapshot_config
                elif source == "schedule":
                    config = schedule_config
                elif source == "regmatch":
                    config = regmatch_config
                else:
                    continue

                # For ADS-B based sources (snapshot/regmatch), check for active flight track
                if source in ("snapshot", "regmatch"):
                    require_track = config.get("require_active_track", True)
                    track_minutes = config.get("track_check_minutes", 10)
                    if require_track and not self.db.has_active_flight_track(
                        hex_code, track_minutes
                    ):
                        identifier = self._get_identifier(aircraft)
                        logger.info(
                            f"Skipping {identifier} ({hex_code}) - "
                            f"no active flight track in last {track_minutes} minutes"
                        )
                        continue  # Try next source

                cooldown_hours = config.get("cooldown_hours", self.cooldown_hours)
                key_suffix = config.get("cooldown_key_suffix", "")
                min_move_km = config.get("min_move_distance_km", self.min_move_distance_km)

                if self.db.should_generate_report_db(
                    aircraft_hex=hex_code,
                    lat=aircraft.get("lat"),
                    lon=aircraft.get("lon"),
                    cooldown_hours=cooldown_hours,
                    min_move_km=min_move_km,
                    key_suffix=key_suffix,
                ):
                    should_report = True
                    active_source = source
                    break

            if should_report and active_source:
                await self._process_single_aircraft_with_source(
                    aircraft, recipients, active_source, filter_modes
                )

    def _execute_snapshot_filter(self, config: dict) -> list[dict]:
        """Execute snapshot-based (Mode A) filter.

        Args:
            config: Snapshot filter configuration

        Returns:
            List of aircraft matching snapshot criteria
        """
        # Get time filter from config (default: 1 hour)
        max_age_hours = config.get("snapshot_max_age_hours", 1.0)

        # Execute filter with time restriction
        return self.sql_filter.execute_filter_with_time_limit(max_age_hours)

    def _execute_schedule_filter(self, config: dict) -> list[dict]:
        """Execute schedule-based (Mode B) filter.

        Args:
            config: Schedule filter configuration

        Returns:
            List of aircraft with scheduled flights to target airports
        """
        return self.schedule_filter.execute_filter()

    async def _process_single_aircraft_with_source(
        self, aircraft: dict, recipients: list[str], source: str, filter_modes: dict
    ):
        """Process a single aircraft and send report with source-specific cooldown.

        Args:
            aircraft: Aircraft data dict
            recipients: List of email recipients
            source: Filter mode that triggered the report ('snapshot' or 'schedule')
            filter_modes: Filter modes configuration
        """
        aircraft_hex = aircraft.get("hex", "")
        identifier = self._get_identifier(aircraft)

        # Get source-specific configuration
        source_config = filter_modes.get(f"{source}_based", {})
        key_suffix = source_config.get("cooldown_key_suffix", "")

        try:
            logger.info(f"Processing report for aircraft {identifier} (source: {source})")

            # Generate subject
            subject = self.subject_generator.generate(aircraft)

            # Send notification via orchestrator
            success = self.notifier.send_notification(
                recipients, subject, aircraft, include_map=True
            )

            if success:
                # Update cooldown with source-specific key suffix
                self.db.update_report_cooldown(
                    aircraft_hex=aircraft_hex,
                    latitude=aircraft.get("lat"),
                    longitude=aircraft.get("lon"),
                    key_suffix=key_suffix,
                )
                self._processed_count += 1
                logger.info(f"Report sent successfully for {identifier} (source: {source})")
            else:
                self._failed_count += 1
                logger.error(f"Failed to send report for {identifier}")

        except Exception as e:
            self._failed_count += 1
            logger.error(f"Error processing report for {identifier}: {e}")

    async def _process_multi_user(self):
        """Process reports in multi-user mode with dual-mode filtering.

        For each active subscriber:
        1. Fetch latest user configurations from database
        2. Check their quota
        3. Execute both filter modes (snapshot + schedule)
        4. Apply per-user cooldowns with mode-specific key suffixes
        5. Send reports with user-specific features
        """
        # Fetch fresh user data from database (not cached)
        logger.debug("Fetching latest active subscribers from database")
        subscribers = self.user_service.get_active_subscribers()

        if not subscribers:
            logger.debug("No active subscribers in multi-user mode")
            return

        logger.info(
            f"Processing reports for {len(subscribers)} active subscribers (fresh from database)"
        )

        # Get filter mode configurations (shared across all users)
        filter_modes = self.yaml_config.get("reporting.filter_modes", {})

        for user_data in subscribers:
            await self._process_user_reports(user_data, filter_modes)

    async def _process_user_reports(self, user_data: dict, filter_modes: dict | None = None):
        """Process reports for a single user with dual-mode filtering.

        Args:
            user_data: User dictionary with subscription info
            filter_modes: Filter modes configuration (snapshot_based, schedule_based)
        """
        user_id = user_data["id"]
        user_email = user_data["email"]

        if filter_modes is None:
            filter_modes = self.yaml_config.get("reporting.filter_modes", {})

        snapshot_config = filter_modes.get("snapshot_based", {"enabled": True})
        schedule_config = filter_modes.get("schedule_based", {"enabled": False})

        try:
            # Check user quota
            if not self.subscription_service.check_daily_quota(user_id):
                logger.debug(f"User {user_email} has exceeded daily quota")
                return

            if not self.subscription_service.check_monthly_quota(user_id):
                logger.debug(f"User {user_email} has exceeded monthly quota")
                return

            # Get user features (fresh from database)
            features = self.subscription_service.get_user_features(user_id)
            default_cooldown_hours = features.get("cooldown_hours", 24.0)

            all_aircraft = []
            aircraft_sources = {}  # Track which mode found each aircraft

            # Mode A: User's custom filters (snapshot-based)
            if snapshot_config.get("enabled", True):
                logger.debug(f"Fetching latest filters for user {user_email} from database")
                max_age_hours = snapshot_config.get("snapshot_max_age_hours", 1.0)
                snapshot_aircraft = self.filter_service.execute_user_filters(
                    user_id, max_age_hours=max_age_hours
                )
                for aircraft in snapshot_aircraft:
                    hex_code = aircraft.get("hex")
                    if hex_code:
                        aircraft_sources.setdefault(hex_code, []).append("snapshot")
                        all_aircraft.append(aircraft)
                logger.debug(
                    f"Mode A (user filters): found {len(snapshot_aircraft)} aircraft for {user_email}"
                )

            # Mode B: Schedule-based filter (shared across users)
            if schedule_config.get("enabled", False):
                schedule_aircraft = self.schedule_filter.execute_filter()
                for aircraft in schedule_aircraft:
                    hex_code = aircraft.get("hex")
                    if hex_code:
                        aircraft_sources.setdefault(hex_code, []).append("schedule")
                        # Only add if not already found by user filter
                        if "snapshot" not in aircraft_sources.get(hex_code, []):
                            all_aircraft.append(aircraft)
                logger.debug(
                    f"Mode B (schedule): found {len(schedule_aircraft)} aircraft for {user_email}"
                )

            # Mode C: Registration match-based filter (shared across users)
            regmatch_config = filter_modes.get("registration_match_based", {"enabled": False})
            if regmatch_config.get("enabled", False):
                regmatch_aircraft = self.registration_match_filter.execute_filter()
                seen_hex_before = {a.get("hex") for a in all_aircraft if a.get("hex")}
                for aircraft in regmatch_aircraft:
                    hex_code = aircraft.get("hex")
                    if hex_code:
                        aircraft_sources.setdefault(hex_code, []).append("regmatch")
                        # Only add if not already found by other filters
                        if hex_code not in seen_hex_before:
                            all_aircraft.append(aircraft)
                            seen_hex_before.add(hex_code)
                logger.debug(
                    f"Mode C (regmatch): found {len(regmatch_aircraft)} aircraft for {user_email}"
                )

            # Deduplicate
            seen_hex = set()
            unique_aircraft = []
            for aircraft in all_aircraft:
                hex_code = aircraft.get("hex")
                if hex_code and hex_code not in seen_hex:
                    seen_hex.add(hex_code)
                    unique_aircraft.append(aircraft)

            if not unique_aircraft:
                logger.debug(f"No aircraft matched filters for user {user_email}")
                return

            logger.debug(f"Total unique aircraft for {user_email}: {len(unique_aircraft)}")

            # Apply cooldowns and process reports
            for aircraft in unique_aircraft:
                hex_code = aircraft.get("hex", "")
                sources = aircraft_sources.get(hex_code, [])
                lat = aircraft.get("lat")
                lon = aircraft.get("lon")

                # Check cooldowns for each source
                should_report = False
                active_source = None

                for source in sources:
                    if source == "snapshot":
                        config = snapshot_config
                        cooldown_hours = config.get("cooldown_hours", default_cooldown_hours)
                        key_suffix = config.get("cooldown_key_suffix", "")
                    elif source == "schedule":
                        config = schedule_config
                        cooldown_hours = config.get("cooldown_hours", 24.0)
                        key_suffix = config.get("cooldown_key_suffix", ":schedule")
                    elif source == "regmatch":
                        config = regmatch_config
                        cooldown_hours = config.get("cooldown_hours", 12.0)
                        key_suffix = config.get("cooldown_key_suffix", ":regmatch")
                    else:
                        continue

                    # For ADS-B based sources (snapshot/regmatch), check for active flight track
                    if source in ("snapshot", "regmatch"):
                        require_track = config.get("require_active_track", True)
                        track_minutes = config.get("track_check_minutes", 10)
                        if require_track and not self.db.has_active_flight_track(
                            hex_code, track_minutes
                        ):
                            identifier = self._get_identifier(aircraft)
                            logger.info(
                                f"Skipping {identifier} ({hex_code}) for {user_email} - "
                                f"no active flight track in last {track_minutes} minutes"
                            )
                            continue  # Try next source

                    # Use FilterService for per-user cooldowns with source-specific suffix
                    if self.filter_service.should_report_for_user(
                        user_id,
                        f"{hex_code}{key_suffix}",
                        lat,
                        lon,
                        cooldown_hours,
                        self.min_move_distance_km,
                    ):
                        should_report = True
                        active_source = source
                        break

                if should_report and active_source:
                    await self._process_single_aircraft_for_user_with_source(
                        aircraft, user_data, features, active_source, filter_modes
                    )

        except Exception as e:
            logger.error(f"Error processing reports for user {user_email}: {e}")

    async def _process_single_aircraft_for_user(
        self, aircraft: dict, user_data: dict, features: dict, key_suffix: str = ""
    ):
        """Process a single aircraft report for a specific user.

        Args:
            aircraft: Aircraft data dict
            user_data: User dictionary
            features: User feature configuration
            key_suffix: Suffix for cooldown key
        """
        user_id = user_data["id"]
        user_email = user_data["email"]
        aircraft_hex = aircraft.get("hex", "")
        identifier = self._get_identifier(aircraft)

        try:
            logger.info(f"Processing report for {identifier} -> {user_email}")

            # Generate subject
            subject = self.subject_generator.generate(aircraft)

            # Send notification with user-specific features
            success = self.notifier.send_notification(
                recipients=[user_email],
                subject=subject,
                aircraft_data=aircraft,
                include_map=features.get("enable_maps", True),
                include_analysis=True,  # Always include LLM analysis (BRIEF mode)
                include_aircraft_images=features.get("enable_aircraft_images", True),
            )

            if success:
                # Update per-user cooldown with key suffix
                self.filter_service.update_user_cooldown(
                    user_id=user_id,
                    aircraft_hex=f"{aircraft_hex}{key_suffix}",
                    latitude=aircraft.get("lat"),
                    longitude=aircraft.get("lon"),
                )

                # Increment usage
                self.subscription_service.increment_usage(user_id, "reports")
                self.subscription_service.increment_usage(user_id, "emails")

                self._processed_count += 1
                logger.info(f"Report sent to {user_email} for {identifier}")
            else:
                self._failed_count += 1
                logger.error(f"Failed to send report to {user_email} for {identifier}")

        except Exception as e:
            self._failed_count += 1
            logger.error(f"Error processing report for {identifier} -> {user_email}: {e}")

    async def _process_single_aircraft_for_user_with_source(
        self, aircraft: dict, user_data: dict, features: dict, source: str, filter_modes: dict
    ):
        """Process a single aircraft report for a specific user with source-specific cooldown.

        Args:
            aircraft: Aircraft data dict
            user_data: User dictionary
            features: User feature configuration
            source: Filter mode that triggered the report ('snapshot' or 'schedule')
            filter_modes: Filter modes configuration
        """
        source_config = filter_modes.get(f"{source}_based", {})
        key_suffix = source_config.get("cooldown_key_suffix", "")

        user_email = user_data["email"]
        identifier = self._get_identifier(aircraft)
        logger.info(f"Processing report for {identifier} -> {user_email} (source: {source})")

        await self._process_single_aircraft_for_user(
            aircraft, user_data, features, key_suffix=key_suffix
        )

    def _apply_cooldown_filter(
        self,
        aircraft_list: list[dict],
        cooldown_hours: float | None = None,
        min_move_km: float | None = None,
        key_suffix: str = "",
    ) -> list[dict]:
        """Filter aircraft based on cooldown rules.

        Args:
            aircraft_list: List of aircraft that matched SQL filter
            cooldown_hours: Override cooldown hours (default: use self.cooldown_hours)
            min_move_km: Override min move distance (default: use self.min_move_distance_km)
            key_suffix: Suffix for cooldown key to enable independent cooldowns

        Returns:
            List of aircraft that should receive reports
        """
        if cooldown_hours is None:
            cooldown_hours = self.cooldown_hours
        if min_move_km is None:
            min_move_km = self.min_move_distance_km

        result = []
        for aircraft in aircraft_list:
            aircraft_hex = aircraft.get("hex", "")
            lat = aircraft.get("lat")
            lon = aircraft.get("lon")

            # Check cooldown using database with key suffix
            should_report = self.db.should_generate_report_db(
                aircraft_hex=aircraft_hex,
                lat=lat,
                lon=lon,
                cooldown_hours=cooldown_hours,
                min_move_km=min_move_km,
                key_suffix=key_suffix,
            )

            if should_report:
                result.append(aircraft)
            else:
                identifier = self._get_identifier(aircraft)
                logger.debug(f"Skipping {identifier} - cooldown active or not moved enough")

        return result

    async def _process_single_aircraft(
        self, aircraft: dict, recipients: list[str], key_suffix: str = ""
    ):
        """Process a single aircraft and send report.

        Args:
            aircraft: Aircraft data dict
            recipients: List of email recipients
            key_suffix: Suffix for cooldown key to enable independent cooldowns
        """
        aircraft_hex = aircraft.get("hex", "")
        identifier = self._get_identifier(aircraft)

        try:
            logger.info(f"Processing report for aircraft {identifier}")

            # Generate subject
            subject = self.subject_generator.generate(aircraft)

            # Send notification via orchestrator
            success = self.notifier.send_notification(
                recipients, subject, aircraft, include_map=True
            )

            if success:
                # Update cooldown after successful report
                self.db.update_report_cooldown(
                    aircraft_hex=aircraft_hex,
                    latitude=aircraft.get("lat"),
                    longitude=aircraft.get("lon"),
                    key_suffix=key_suffix,
                )
                self._processed_count += 1
                logger.info(f"Report sent successfully for {identifier}")
            else:
                self._failed_count += 1
                logger.error(f"Failed to send report for {identifier}")

        except Exception as e:
            self._failed_count += 1
            logger.error(f"Error processing report for {identifier}: {e}")

    def _get_recipients(self) -> list[str]:
        """Get email recipients from configuration."""
        email_config = self.yaml_config.get_email_config()
        return email_config.get("recipients", [])

    def _get_identifier(self, aircraft: dict) -> str:
        """Get best identifier for an aircraft."""
        flight = (aircraft.get("flight") or "").strip()
        registration = (aircraft.get("r") or "").strip()
        return flight or registration or aircraft.get("hex", "unknown")

    def stop(self):
        """Stop the service."""
        self.is_running = False
        logger.info("Stopping report service...")

    def get_service_status(self) -> dict:
        """Get service status and statistics.

        Returns:
            Dict with service status information
        """
        uptime_seconds = 0
        if self._start_time:
            uptime_seconds = (datetime.now() - self._start_time).total_seconds()

        return {
            "is_running": self.is_running,
            "uptime_seconds": uptime_seconds,
            "processed_this_session": self._processed_count,
            "failed_this_session": self._failed_count,
            "poll_interval": self.poll_interval,
            "batch_size": self.batch_size,
            "cooldown_hours": self.cooldown_hours,
            "min_move_distance_km": self.min_move_distance_km,
        }

    def cleanup_cooldowns(self, max_age_hours: float = 24.0):
        """Clean up old cooldown records.

        Args:
            max_age_hours: Remove records older than this many hours
        """
        self.db.cleanup_old_cooldowns(max_age_hours)
        logger.info(f"Cooldown cleanup completed (removed records older than {max_age_hours}h)")
