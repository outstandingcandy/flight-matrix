"""
Services package for flight-matrix application.

This package contains the main services:
- TrackService: Aircraft data collection from API
- ReportService: Report filtering and generation

Note: Image downloading has been moved to the scraper framework.
Use src.scraper_main with --local for single-machine image downloading.
"""

# Services are imported directly by entry points, not through __init__.py
# to avoid circular import issues.
#
# Usage:
#   from services.track_service import TrackService
#   from services.report_service import ReportService
