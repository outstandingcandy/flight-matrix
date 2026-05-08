"""
Flight analysis service.

This module provides AI-powered flight analysis functionality.
It is responsible ONLY for running analysis, not for email sending or content building.

Separation of concerns:
- This module: Runs AI analysis and returns results
- Content module: Builds HTML/text email content
- Email module: Sends the prepared content
"""

import logging
from typing import Any

logger = logging.getLogger("analysis.service")


class FlightAnalysisService:
    """Service for running AI-powered flight analysis.

    This class is responsible for:
    - Initializing the AI analysis agent
    - Running analysis on aircraft data
    - Returning analysis results (as HTML)

    This class is NOT responsible for:
    - Building email content
    - Sending emails
    - Database operations
    """

    def __init__(
        self,
        enable_web_search: bool = True,
        provider_config: dict[str, Any] | None = None,
        database_manager: Any | None = None,
        recent_tracks_count: int = 10,
    ) -> None:
        """Initialize the flight analysis service.

        Args:
            enable_web_search: Whether to enable web search in analysis
            provider_config: LLM provider configuration
            database_manager: Database manager for historical data
            recent_tracks_count: Number of recent track points to include in summary
        """
        self.database_manager = database_manager
        self.provider_config = provider_config or {}
        self.agent: Any | None = None
        self.enricher: Any | None = None
        self._enabled = False

        try:
            from src.analysis.flight_agent import FlightAnalysisAgent

            self.agent = FlightAnalysisAgent(
                enable_web_search=enable_web_search, provider_config=self.provider_config
            )

            from src.aircraft.enricher import AircraftInfoEnricher

            self.enricher = AircraftInfoEnricher(
                database_manager, recent_tracks_count=recent_tracks_count
            )

            self._enabled = True
            logger.info("Flight analysis service initialized successfully")

        except Exception as e:
            logger.warning(f"Failed to initialize flight analysis service: {e}")
            self._enabled = False

    @property
    def enabled(self) -> bool:
        """Check if the service is enabled and ready."""
        return self._enabled and self.agent is not None

    def analyze_aircraft(self, aircraft_data: dict[str, Any]) -> str | None:
        """Analyze aircraft and return HTML report.

        Args:
            aircraft_data: Aircraft data dictionary from API

        Returns:
            HTML string containing the analysis report, or None if analysis fails
        """
        if not self.enabled:
            logger.debug("Flight analysis service is disabled")
            return None

        try:
            # Get enhanced historical data
            if self.enricher:
                enhanced_data = self.enricher.get_historical_aircraft_data(aircraft_data)
            else:
                enhanced_data = aircraft_data

            # Run analysis
            logger.info("Running flight analysis...")
            analysis_report = self.agent.analyze_aircraft(enhanced_data)

            # Format as HTML
            from src.media.markdown_converter import format_analysis_report_html

            html_report = format_analysis_report_html(analysis_report)

            logger.info("Flight analysis completed successfully")
            return html_report

        except Exception as e:
            logger.error(f"Flight analysis failed: {e}")
            from src.media.markdown_converter import create_error_analysis_html

            return create_error_analysis_html(str(e))

    def get_token_usage(self) -> dict[str, Any] | None:
        """Get token usage statistics from the analysis agent.

        Returns:
            Dictionary with token usage stats, or None if not available
        """
        if not self.enabled or not self.agent:
            return None

        try:
            return self.agent.get_token_usage()
        except Exception as e:
            logger.warning(f"Failed to get token usage: {e}")
            return None
