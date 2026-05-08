"""
Unified geographic calculation utilities.

This module provides common geospatial functions used across the application,
eliminating duplicate implementations in various services.
"""

import math

# Earth's radius in kilometers
EARTH_RADIUS_KM = 6371.0


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points using Haversine formula.

    Args:
        lat1: Latitude of first point in degrees
        lon1: Longitude of first point in degrees
        lat2: Latitude of second point in degrees
        lon2: Longitude of second point in degrees

    Returns:
        Distance in kilometers
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return EARTH_RADIUS_KM * c


def is_valid_coordinate(lat: int | float | None, lon: int | float | None) -> bool:
    """Validate if coordinates are within valid geographic ranges.

    Args:
        lat: Latitude value (-90 to 90)
        lon: Longitude value (-180 to 180)

    Returns:
        True if coordinates are valid, False otherwise
    """
    if lat is None or lon is None:
        return False

    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False

    # Check for NaN or infinite values
    if math.isnan(lat) or math.isnan(lon) or math.isinf(lat) or math.isinf(lon):
        return False

    # Check valid ranges
    if not (-90 <= lat <= 90):
        return False

    if not (-180 <= lon <= 180):
        return False

    # Check for coordinates very close to (0, 0) which are often GPS default/error values
    # Using a small threshold (0.0001 degrees ~ 11 meters) to filter out GPS errors
    # while allowing real flights near the equator/prime meridian intersection
    if abs(lat) < 0.0001 and abs(lon) < 0.0001:
        return False

    return True
