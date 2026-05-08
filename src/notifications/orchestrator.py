"""
Notification orchestrator.

This module coordinates the notification workflow:
1. Run AI analysis (separate from email)
2. Generate maps and get aircraft images
3. Build email content
4. Send email

This ensures clean separation between analysis, content building, and sending.
"""

import logging

from .base import BaseEmailNotifier
from .content import NotificationContentBuilder

logger = logging.getLogger("notifications.orchestrator")


class NotificationOrchestrator:
    """Orchestrates the complete notification workflow.

    This class coordinates:
    - Running AI analysis (via FlightAnalysisService)
    - Generating maps and getting images (via MediaService)
    - Building email content (via NotificationContentBuilder)
    - Sending email (via BaseEmailNotifier)

    Each component is called separately, ensuring clean separation of concerns.
    """

    def __init__(self, notifier: BaseEmailNotifier, analysis_service=None, media_service=None):
        """Initialize the notification orchestrator.

        Args:
            notifier: Email notifier for sending
            analysis_service: Optional FlightAnalysisService for AI analysis
            media_service: Optional MediaService for maps/images
        """
        self.notifier = notifier
        self.analysis_service = analysis_service
        self.media_service = media_service
        self.content_builder = NotificationContentBuilder()

        logger.info(
            f"Notification orchestrator initialized "
            f"(analysis: {'enabled' if analysis_service else 'disabled'}, "
            f"media: {'enabled' if media_service else 'disabled'})"
        )

    def send_notification(
        self,
        recipients: list[str],
        subject: str,
        aircraft_data: dict,
        include_map: bool = True,
        include_analysis: bool = False,
        include_aircraft_images: bool = True,
    ) -> bool:
        """Send a complete aircraft notification.

        This method orchestrates the full workflow:
        1. Run AI analysis (if enabled and requested)
        2. Generate maps (if enabled and requested)
        3. Get aircraft images (if enabled and requested)
        4. Build email content
        5. Send email

        Args:
            recipients: List of email recipients
            subject: Email subject line
            aircraft_data: Aircraft data dictionary
            include_map: Whether to include maps
            include_analysis: Whether to include AI analysis
            include_aircraft_images: Whether to include aircraft images

        Returns:
            True if sent successfully, False otherwise
        """
        if not recipients:
            logger.warning("No recipients specified")
            return False

        try:
            # Extract identifiers
            registration = (aircraft_data.get("r") or "").strip() or None
            icao = (aircraft_data.get("hex") or "").strip().upper() or None
            identifier = registration or icao or "unknown"

            logger.info(f"Preparing notification for aircraft {identifier}")

            # Step 1: Run AI analysis (separate from email)
            analysis_html = None
            if include_analysis and self.analysis_service:
                logger.info("Running AI analysis...")
                analysis_html = self.analysis_service.analyze_aircraft(aircraft_data)
                if analysis_html:
                    logger.info("AI analysis completed")

            # Step 2: Generate maps and get images
            map_paths = []
            aircraft_image_paths = []
            static_info = None
            flight_endpoints = None

            if self.media_service:
                if include_map:
                    logger.info("Generating maps...")
                    map_paths = self.media_service.generate_maps(aircraft_data, registration, icao)

                if registration:
                    if include_aircraft_images:
                        logger.info("Getting aircraft images...")
                        aircraft_image_paths = self.media_service.get_aircraft_images(registration)

                    logger.info("Getting static info...")
                    static_info = self.media_service.get_static_info(registration, icao)

                    logger.info("Getting flight endpoints...")
                    flight_endpoints = self.media_service.get_flight_endpoints(registration)

            # Step 3: Build email content
            logger.info("Building email content...")
            content = self.content_builder.build_content(
                subject=subject,
                aircraft_data=aircraft_data,
                aircraft_image_paths=aircraft_image_paths if aircraft_image_paths else None,
                map_image_paths=map_paths if map_paths else None,
                analysis_html=analysis_html,
                static_info=static_info,
                flight_endpoints=flight_endpoints,
            )

            # Step 4: Send email
            logger.info(f"Sending email to {len(recipients)} recipients...")
            success = self.notifier.send(recipients, content)

            if success:
                logger.info(f"Notification sent successfully for {identifier}")
            else:
                logger.error(f"Failed to send notification for {identifier}")

            return success

        except Exception as e:
            logger.error(f"Error in notification workflow: {e}")
            return False

    def test_connection(self) -> bool:
        """Test email service connection.

        Returns:
            True if connection successful
        """
        return self.notifier.test_connection()


class NotificationOrchestratorFactory:
    """Factory for creating notification orchestrators from configuration."""

    @staticmethod
    def create(yaml_config, database_manager=None) -> NotificationOrchestrator:
        """Create notification orchestrator from YAML configuration.

        Args:
            yaml_config: YAMLConfig instance
            database_manager: Optional database manager

        Returns:
            Configured NotificationOrchestrator
        """
        from src.analysis.service import FlightAnalysisService
        from src.media.service import MediaService

        from .factory import EmailNotifierFactory

        # Get email configuration
        email_config = yaml_config.get_email_config()
        features = email_config.get("features", {})
        enable_maps = features.get("enable_maps", True)
        enable_aircraft_images = features.get("enable_aircraft_images", True)
        # Enable flight analysis for AI-powered reports
        enable_flight_analysis = features.get("enable_flight_analysis", False)

        # Get LLM configuration
        llm_config = yaml_config.get_llm_config() if hasattr(yaml_config, "get_llm_config") else {}

        # Get reporting configuration
        reporting_config = (
            yaml_config.get_reporting_config()
            if hasattr(yaml_config, "get_reporting_config")
            else {}
        )
        recent_tracks_count = reporting_config.get("recent_tracks_count", 10)

        # Create email notifier
        notifier = EmailNotifierFactory.create(yaml_config)

        # Create analysis service
        analysis_service = None
        if enable_flight_analysis:
            try:
                analysis_service = FlightAnalysisService(
                    enable_web_search=True,
                    provider_config=llm_config,
                    database_manager=database_manager,
                    recent_tracks_count=recent_tracks_count,
                )
            except Exception as e:
                logger.warning(f"Failed to create analysis service: {e}")

        # Create media service
        media_service = None
        if enable_maps or enable_aircraft_images:
            try:
                media_service = MediaService(
                    enable_maps=enable_maps,
                    enable_aircraft_images=enable_aircraft_images,
                    database_manager=database_manager,
                    recent_tracks_count=recent_tracks_count,
                )
            except Exception as e:
                logger.warning(f"Failed to create media service: {e}")

        return NotificationOrchestrator(
            notifier=notifier, analysis_service=analysis_service, media_service=media_service
        )
