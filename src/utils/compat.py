"""
Backward compatibility module.

This module re-exports classes and functions from their new locations
to maintain backward compatibility with existing code that imports
from src.utils.

As modules are moved to new packages, add re-exports here to ensure
old import paths continue to work.

Example:
    # Old import (still works):
    from src.utils.database import DatabaseManager

    # New import (preferred):
    from src.data.database import DatabaseManager
"""

import warnings

# This module will be populated as we move modules to their new locations.
# Re-exports will be added incrementally during the refactoring process.
# Exceptions (from src.core.exceptions)

# Search base classes (from src.search.base)

# Retry utilities (from src.utils.retry)


def _deprecation_warning(old_path: str, new_path: str):
    """Issue a deprecation warning for old import paths."""
    warnings.warn(
        f"Importing from '{old_path}' is deprecated. Please use '{new_path}' instead.",
        DeprecationWarning,
        stacklevel=3,
    )


# Placeholder for future re-exports as modules are moved:
#
# Phase 2 - Data Layer:
# from src.data.database import DatabaseManager
# from src.data.models import AircraftSnapshot, GeographicRegion
# from src.data.cache import AircraftCacheManager
# from src.data.filters import SQLFilterEngine
#
# Phase 3 - Core System:
# from src.core.config import YAMLConfig
#
# Phase 4 - Aircraft Domain:
# from src.aircraft.classification import AircraftClassification
# from src.aircraft.enricher import AircraftInfoEnricher
# from src.aircraft.data_models import FlightData, TokenUsage
#
# Phase 5 - Search & Notifications:
# from src.search.tavily import TavilySearchClient
# from src.search.duckduckgo import DuckDuckGoSearchClient
# from src.notifications.base import BaseEmailNotifier
# from src.notifications.smtp import EmailNotifier
# from src.notifications.ses import AWSEmailNotifier
# from src.notifications.factory import EmailNotifierFactory
#
# Phase 6 - Media & Utilities:
# from src.geo.locator import GeoLocator
# from src.media.maps import MapGenerator
# from src.reporting.manager import ReportManager
