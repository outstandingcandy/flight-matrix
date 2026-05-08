import logging
import os
import time

from src.geo.geo import haversine_distance, is_valid_coordinate

# For map generation
try:
    import numpy as np
    import plotly.graph_objects as go
    import plotly.io as pio

    PLOTLY_AVAILABLE = True
except ImportError:
    logging.warning(
        "Plotly packages not installed. Map generation will be disabled. Install with: pip install plotly kaleido numpy"
    )
    PLOTLY_AVAILABLE = False

    # Create dummy classes to prevent import errors
    class DummyPlotly:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    go = DummyPlotly()
    pio = DummyPlotly()
    np = DummyPlotly()

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("map_generator")

# Suppress verbose Kaleido/Choreographer logs (Chromium is launched when generating map images)
logging.getLogger("kaleido").setLevel(logging.WARNING)
logging.getLogger("choreographer").setLevel(logging.WARNING)


class MapGenerator:
    """
    A class for generating maps with flight tracks.
    """

    def __init__(self, maps_dir: str = "data/maps", enable_maps: bool = True):
        """
        Initialize the map generator.

        Args:
            maps_dir: Directory to store generated map images
            enable_maps: Whether to enable map generation (default: True)
        """
        self.maps_dir = maps_dir
        self.enable_maps = enable_maps

        if self.enable_maps and PLOTLY_AVAILABLE:
            # Create maps directory if it doesn't exist
            os.makedirs(maps_dir, exist_ok=True)
            logger.info(f"Map generator initialized, maps will be saved to {maps_dir}")
        elif self.enable_maps and not PLOTLY_AVAILABLE:
            logger.warning(
                "Map generation requested but Plotly is not available. Maps will be disabled."
            )
            self.enable_maps = False
        else:
            logger.info("Map generator initialized with maps disabled")

    def generate_flight_track_map(
        self,
        track_points: list[dict],
        aircraft_id: str,
        title: str = None,
        area_center: tuple[float, float] = None,
        area_radius_km: float = None,
        map_mode: str = "detail",
    ) -> str | None:
        """
        Generate a map with flight track points.

        Args:
            track_points: List of track point dictionaries from the database
            aircraft_id: Identifier for the aircraft (hex code, registration, or flight number)
            title: Optional title for the map
            area_center: Optional tuple of (latitude, longitude) for an area to highlight
            area_radius_km: Optional radius in kilometers for the area
            map_mode: Map display mode - "detail" for detailed local view, "globe" for global view

        Returns:
            Path to the generated map image, or None if generation failed
        """
        if not self.enable_maps:
            logger.debug("Map generation is disabled")
            return None

        try:
            if not track_points:
                logger.warning(f"No track points provided for {aircraft_id}, cannot generate map")
                return None

            # Sort track points by timestamp (ascending) to ensure correct start/current order
            track_points = sorted(track_points, key=lambda p: p.get("timestamp", 0))

            # Extract coordinates and timestamps from track points
            lats = []
            lons = []
            alts = []
            timestamps = []

            for point in track_points:
                lat = point.get("lat", 0)
                lon = point.get("lon", 0)

                # Validate coordinates
                if is_valid_coordinate(lat, lon):
                    lats.append(lat)
                    lons.append(lon)
                    alts.append(point.get("alt_baro", 0))
                    timestamps.append(point.get("timestamp", 0))
                else:
                    logger.warning(f"Invalid coordinates filtered out: lat={lat}, lon={lon}")

            if not lats:
                logger.warning(f"No valid coordinates found for {aircraft_id}, cannot generate map")
                return None

            # Filter out outlier coordinates if needed
            if len(lats) > 2:  # Only filter if we have enough points
                # Store original indices to properly reconstruct filtered data
                original_indices = list(range(len(lats)))
                # Use 5000km threshold to allow long-haul flights (e.g., US to Middle East)
                # while still filtering truly erroneous coordinates
                filtered_lats, filtered_lons, filtered_indices = (
                    self._filter_coordinates_by_bounds_with_indices(
                        lats, lons, original_indices, max_jump_km=5000
                    )
                )

                # Reconstruct all arrays using the filtered indices
                if len(filtered_lats) != len(lats):
                    lats = filtered_lats
                    lons = filtered_lons
                    alts = [alts[i] for i in filtered_indices]
                    timestamps = [timestamps[i] for i in filtered_indices]

            # Create figure
            fig = go.Figure()

            # Add flight path
            fig.add_trace(
                go.Scattermapbox(
                    mode="markers+lines",
                    lon=lons,
                    lat=lats,
                    marker={"size": 8, "color": "red"},
                    line={"width": 2, "color": "blue"},
                    name="Flight Path",
                    hoverinfo="text",
                    hovertext=[
                        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}<br>"
                        f"Altitude: {alt} ft<br>"
                        f"Lat: {lat:.5f}, Lon: {lon:.5f}"
                        for ts, alt, lat, lon in zip(timestamps, alts, lats, lons)
                    ],
                )
            )

            # Add start and end points with distinct markers
            if len(lats) > 0:
                # Start point (green)
                fig.add_trace(
                    go.Scattermapbox(
                        mode="markers",
                        lon=[lons[0]],
                        lat=[lats[0]],
                        marker={"size": 12, "color": "green"},
                        name="Start",
                        hoverinfo="text",
                        hovertext=f"Start: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamps[0]))}",
                    )
                )

                # End point (red)
                fig.add_trace(
                    go.Scattermapbox(
                        mode="markers",
                        lon=[lons[-1]],
                        lat=[lats[-1]],
                        marker={"size": 12, "color": "red"},
                        name="Current",
                        hoverinfo="text",
                        hovertext=f"Current: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamps[-1]))}",
                    )
                )

            # Add area circle if provided
            if area_center and area_radius_km:
                # Add a semi-transparent circle for the area
                # Note: This is an approximation as the circle is not perfect on a map projection
                # Create a circle of points
                import numpy as np

                theta = np.linspace(0, 2 * np.pi, 100)

                # Convert radius from km to degrees (approximate)
                # 1 degree of latitude is approximately 111 km
                radius_deg = area_radius_km / 111

                circle_lats = [area_center[0] + radius_deg * np.cos(t) for t in theta]
                circle_lons = [
                    area_center[1] + radius_deg * np.cos(area_center[0] * np.pi / 180) * np.sin(t)
                    for t in theta
                ]

                fig.add_trace(
                    go.Scattermapbox(
                        mode="lines",
                        lon=circle_lons,
                        lat=circle_lats,
                        line={"width": 1, "color": "rgba(255, 0, 0, 0.5)"},
                        name=f"Area ({area_radius_km} km)",
                        hoverinfo="text",
                        hovertext=f"Monitored Area: {area_radius_km} km radius",
                    )
                )

            # Calculate bounds and determine optimal view
            bounds = self._calculate_optimal_bounds(lats, lons, area_center, area_radius_km)
            center_lat = bounds["center_lat"]
            center_lon = bounds["center_lon"]

            # Determine zoom level based on map_mode
            if map_mode == "globe":
                # Global view - show position on Earth
                zoom = 1
                map_style = "carto-positron"
            else:
                # Detail view - show local area details
                zoom = bounds["zoom"]
                map_style = "carto-positron"

            # Update layout
            fig.update_layout(
                mapbox={
                    "style": map_style,
                    "center": {"lat": center_lat, "lon": center_lon},
                    "zoom": zoom,
                },
                margin={"r": 0, "t": 50, "l": 0, "b": 0},
                height=600,
                width=800,
            )

            # Add title if provided
            if title:
                fig.update_layout(title=title)

            # Generate timestamp-based filename with map_mode
            timestamp = int(time.time())
            filename = f"{aircraft_id}_{map_mode}_{timestamp}.png"
            filepath = os.path.join(self.maps_dir, filename)

            # Save the map as a static image
            try:
                fig.write_image(filepath)
                logger.info(f"Flight track map ({map_mode} mode) saved to {filepath}")
                return filepath
            except Exception as img_error:
                logger.warning(f"Failed to generate static image: {img_error}")
                # Try to save as HTML instead
                html_filepath = filepath.replace(".png", ".html")
                fig.write_html(html_filepath)
                logger.info(f"Flight track map ({map_mode} mode) saved as HTML to {html_filepath}")
                return html_filepath

        except Exception as e:
            logger.error(f"Error generating flight track map: {e}")
            return None

    def generate_current_position_map(
        self,
        aircraft_data: dict,
        area_center: tuple[float, float] = None,
        area_radius_km: float = None,
        map_mode: str = "detail",
    ) -> str | None:
        """
        Generate a map showing just the current aircraft position.

        Args:
            aircraft_data: Aircraft data dictionary from the API
            area_center: Optional tuple of (latitude, longitude) for an area to highlight
            area_radius_km: Optional radius in kilometers for the area
            map_mode: Map display mode - "detail" for detailed local view, "globe" for global view

        Returns:
            Path to the generated map image, or None if generation failed
        """
        if not self.enable_maps:
            logger.debug("Map generation is disabled")
            return None

        try:
            # Extract required data
            lat = aircraft_data.get("lat")
            lon = aircraft_data.get("lon")

            if lat is None or lon is None:
                logger.warning("Aircraft data missing position information, cannot generate map")
                return None

            # Validate coordinates
            if not is_valid_coordinate(lat, lon):
                logger.warning(f"Invalid coordinates for aircraft: lat={lat}, lon={lon}")
                return None

            # Create identifier from available data
            identifier = (
                (aircraft_data.get("flight") or "").strip()
                or (aircraft_data.get("r") or "").strip()
                or aircraft_data.get("hex", "")
            ).upper()

            # Get other aircraft details for the map
            altitude = aircraft_data.get("alt_baro", "Unknown")
            speed = aircraft_data.get("gs", "Unknown")
            heading = aircraft_data.get("track", "Unknown")

            # Create hover text with aircraft details
            hover_text = f"Aircraft: {identifier}<br>"
            hover_text += f"Altitude: {altitude} ft<br>"
            hover_text += f"Speed: {speed} knots<br>"
            hover_text += f"Heading: {heading}°"

            # Create figure
            fig = go.Figure()

            # Add aircraft position
            fig.add_trace(
                go.Scattermapbox(
                    mode="markers",
                    lon=[lon],
                    lat=[lat],
                    marker={"size": 15, "color": "red"},
                    name="Aircraft",
                    hoverinfo="text",
                    hovertext=[hover_text],
                )
            )

            # Add area circle if provided
            if area_center and area_radius_km:
                # Add a semi-transparent circle for the area
                import numpy as np

                theta = np.linspace(0, 2 * np.pi, 100)

                # Convert radius from km to degrees (approximate)
                radius_deg = area_radius_km / 111

                circle_lats = [area_center[0] + radius_deg * np.cos(t) for t in theta]
                circle_lons = [
                    area_center[1] + radius_deg * np.cos(area_center[0] * np.pi / 180) * np.sin(t)
                    for t in theta
                ]

                fig.add_trace(
                    go.Scattermapbox(
                        mode="lines",
                        lon=circle_lons,
                        lat=circle_lats,
                        line={"width": 1, "color": "rgba(255, 0, 0, 0.5)"},
                        name=f"Area ({area_radius_km} km)",
                        hoverinfo="text",
                        hovertext=f"Monitored Area: {area_radius_km} km radius",
                    )
                )

            # Determine zoom level based on map_mode
            if map_mode == "globe":
                # Global view - show position on Earth
                zoom = 1
            else:
                # Detail view - show local area details
                zoom = 9

            # Update layout with zoom level based on map_mode
            fig.update_layout(
                mapbox={
                    "style": "carto-positron",
                    "center": {"lat": lat, "lon": lon},
                    "zoom": zoom,
                },
                margin={"r": 0, "t": 50, "l": 0, "b": 0},
                height=600,
                width=800,
                title=f"Current Position: {identifier}",
            )

            # Generate timestamp-based filename with map_mode
            timestamp = int(time.time())
            filename = f"{identifier}_{map_mode}_{timestamp}.png"
            filepath = os.path.join(self.maps_dir, filename)

            # Save the map as a static image
            try:
                fig.write_image(filepath)
                logger.info(f"Current position map ({map_mode} mode) saved to {filepath}")
                return filepath
            except Exception as img_error:
                logger.warning(f"Failed to generate static image: {img_error}")
                # Try to save as HTML instead
                html_filepath = filepath.replace(".png", ".html")
                fig.write_html(html_filepath)
                logger.info(
                    f"Current position map ({map_mode} mode) saved as HTML to {html_filepath}"
                )
                return html_filepath

        except Exception as e:
            logger.error(f"Error generating current position map: {e}")
            return None

    def _calculate_optimal_bounds(
        self,
        lats: list[float],
        lons: list[float],
        area_center: tuple[float, float] = None,
        area_radius_km: float = None,
    ) -> dict:
        """
        Calculate optimal map bounds and zoom level to fit all coordinates.

        Args:
            lats: List of latitudes
            lons: List of longitudes
            area_center: Optional area center to include in bounds
            area_radius_km: Optional area radius to include in bounds

        Returns:
            Dictionary with center_lat, center_lon, zoom, and bounds
        """
        try:
            # Combine flight track coordinates with area bounds if provided
            all_lats = list(lats) if lats else []
            all_lons = list(lons) if lons else []

            # Add area bounds if provided
            if area_center and area_radius_km:
                # Convert radius from km to degrees (approximate)
                radius_deg = area_radius_km / 111
                area_lat, area_lon = area_center

                # Add area boundary points
                all_lats.extend([area_lat - radius_deg, area_lat + radius_deg])
                all_lons.extend(
                    [
                        area_lon - radius_deg / np.cos(np.radians(area_lat)),
                        area_lon + radius_deg / np.cos(np.radians(area_lat)),
                    ]
                )

            if not all_lats or not all_lons:
                # Fallback to default if no coordinates
                return {"center_lat": 0, "center_lon": 0, "zoom": 2, "bounds": None}

            # Calculate bounds
            min_lat, max_lat = min(all_lats), max(all_lats)
            min_lon, max_lon = min(all_lons), max(all_lons)

            # Calculate center
            center_lat = (min_lat + max_lat) / 2
            center_lon = (min_lon + max_lon) / 2

            # Calculate ranges
            lat_range = max_lat - min_lat
            lon_range = max_lon - min_lon
            max_range = max(lat_range, lon_range)

            # Add padding to ensure all points are visible
            # Use adaptive padding - more for smaller ranges, less for larger ranges
            if max_range < 0.01:
                padding_factor = 0.5  # 50% padding for very small ranges
            elif max_range < 0.1:
                padding_factor = 0.3  # 30% padding for small ranges
            elif max_range < 1.0:
                padding_factor = 0.2  # 20% padding for medium ranges
            else:
                padding_factor = 0.1  # 10% padding for large ranges

            lat_padding = max(lat_range * padding_factor, 0.001)  # Minimum padding
            lon_padding = max(lon_range * padding_factor, 0.001)  # Minimum padding

            # Adjust bounds with padding
            padded_min_lat = min_lat - lat_padding
            padded_max_lat = max_lat + lat_padding
            padded_min_lon = min_lon - lon_padding
            padded_max_lon = max_lon + lon_padding

            # Determine zoom level based on the range with better granularity
            # Ensure flight tracks are clearly visible by using higher zoom levels
            if max_range < 0.001:  # Very close points (less than ~100m)
                zoom = 16
            elif max_range < 0.005:  # ~500m
                zoom = 15
            elif max_range < 0.01:  # ~1km
                zoom = 14
            elif max_range < 0.05:  # ~5km
                zoom = 12
            elif max_range < 0.1:  # ~10km
                zoom = 11
            elif max_range < 0.5:  # ~50km
                zoom = 9
            elif max_range < 1.0:  # ~100km
                zoom = 8
            elif max_range < 2.0:  # ~200km
                zoom = 7
            elif max_range < 5.0:  # ~500km
                zoom = 6
            elif max_range < 10.0:  # ~1000km
                zoom = 5
            elif max_range < 20.0:  # ~2000km
                zoom = 4
            elif max_range < 50.0:  # ~5000km
                zoom = 3
            else:
                zoom = 2

            return {
                "center_lat": center_lat,
                "center_lon": center_lon,
                "zoom": zoom,
                "bounds": {
                    "min_lat": padded_min_lat,
                    "max_lat": padded_max_lat,
                    "min_lon": padded_min_lon,
                    "max_lon": padded_max_lon,
                    "lat_range": lat_range,
                    "lon_range": lon_range,
                },
            }

        except Exception as e:
            logger.error(f"Error calculating optimal bounds: {e}")
            return {"center_lat": 0, "center_lon": 0, "zoom": 2, "bounds": None}

    def _filter_coordinates_by_bounds_with_indices(
        self, lats: list[float], lons: list[float], indices: list[int], max_jump_km: float = 2000
    ) -> tuple[list[float], list[float], list[int]]:
        """
        Filter out coordinates with abnormal jumps between consecutive points.

        This approach detects outliers by checking if a point creates an unrealistic
        jump from its neighbors, rather than using distance from center which fails
        for long-haul flights.

        Args:
            lats: List of latitudes (should be in time order)
            lons: List of longitudes (should be in time order)
            indices: List of original indices
            max_jump_km: Maximum allowed jump between consecutive points (default 2000km)
                        Commercial aircraft typically fly ~900 km/h, so 2000km allows
                        for ~2 hour gaps in tracking data

        Returns:
            Tuple of filtered (lats, lons, indices)
        """
        if not lats or not lons or len(lats) <= 2:
            return lats, lons, indices

        try:
            n = len(lats)
            filtered_lats = []
            filtered_lons = []
            filtered_indices = []

            for i in range(n):
                lat, lon, idx = lats[i], lons[i], indices[i]

                # Always keep first point
                if i == 0:
                    filtered_lats.append(lat)
                    filtered_lons.append(lon)
                    filtered_indices.append(idx)
                    continue

                # Calculate distance from previous kept point
                if filtered_lats:
                    prev_lat, prev_lon = filtered_lats[-1], filtered_lons[-1]
                    jump_distance = haversine_distance(prev_lat, prev_lon, lat, lon)

                    # Check if this point creates a reasonable jump
                    if jump_distance <= max_jump_km:
                        filtered_lats.append(lat)
                        filtered_lons.append(lon)
                        filtered_indices.append(idx)
                    else:
                        # Check if the NEXT point would make this point valid
                        # (i.e., this might be a valid point and previous was the outlier)
                        if i + 1 < n:
                            next_lat, next_lon = lats[i + 1], lons[i + 1]
                            dist_to_next = haversine_distance(lat, lon, next_lat, next_lon)
                            dist_prev_to_next = haversine_distance(
                                prev_lat, prev_lon, next_lat, next_lon
                            )

                            # If current point is closer to next point than prev is,
                            # and distance to next is reasonable, keep current point
                            if dist_to_next <= max_jump_km and dist_to_next < dist_prev_to_next:
                                filtered_lats.append(lat)
                                filtered_lons.append(lon)
                                filtered_indices.append(idx)
                            else:
                                logger.warning(
                                    f"Filtering out outlier coordinate: {lat:.5f}, {lon:.5f} "
                                    f"(jump: {jump_distance:.1f} km from previous point)"
                                )
                        else:
                            logger.warning(
                                f"Filtering out outlier coordinate: {lat:.5f}, {lon:.5f} "
                                f"(jump: {jump_distance:.1f} km from previous point)"
                            )

            if not filtered_lats:
                logger.warning("All coordinates were filtered out, returning original data")
                return lats, lons, indices

            if len(filtered_lats) < len(lats):
                logger.info(f"Filtered coordinates: kept {len(filtered_lats)}/{len(lats)} points")
            return filtered_lats, filtered_lons, filtered_indices

        except Exception as e:
            logger.error(f"Error filtering coordinates: {e}")
            return lats, lons, indices

    def _filter_coordinates_by_bounds(
        self, lats: list[float], lons: list[float], max_jump_km: float = 2000
    ) -> tuple[list[float], list[float]]:
        """
        Filter out coordinates with abnormal jumps between consecutive points.

        This approach detects outliers by checking if a point creates an unrealistic
        jump from its neighbors, rather than using distance from center which fails
        for long-haul flights.

        Args:
            lats: List of latitudes (should be in time order)
            lons: List of longitudes (should be in time order)
            max_jump_km: Maximum allowed jump between consecutive points (default 2000km)

        Returns:
            Tuple of filtered (lats, lons)
        """
        if not lats or not lons or len(lats) <= 2:
            return lats, lons

        try:
            n = len(lats)
            filtered_lats = []
            filtered_lons = []

            for i in range(n):
                lat, lon = lats[i], lons[i]

                # Always keep first point
                if i == 0:
                    filtered_lats.append(lat)
                    filtered_lons.append(lon)
                    continue

                # Calculate distance from previous kept point
                if filtered_lats:
                    prev_lat, prev_lon = filtered_lats[-1], filtered_lons[-1]
                    jump_distance = haversine_distance(prev_lat, prev_lon, lat, lon)

                    # Check if this point creates a reasonable jump
                    if jump_distance <= max_jump_km:
                        filtered_lats.append(lat)
                        filtered_lons.append(lon)
                    else:
                        # Check if the NEXT point would make this point valid
                        if i + 1 < n:
                            next_lat, next_lon = lats[i + 1], lons[i + 1]
                            dist_to_next = haversine_distance(lat, lon, next_lat, next_lon)
                            dist_prev_to_next = haversine_distance(
                                prev_lat, prev_lon, next_lat, next_lon
                            )

                            if dist_to_next <= max_jump_km and dist_to_next < dist_prev_to_next:
                                filtered_lats.append(lat)
                                filtered_lons.append(lon)
                            else:
                                logger.warning(
                                    f"Filtering out outlier coordinate: {lat:.5f}, {lon:.5f} "
                                    f"(jump: {jump_distance:.1f} km from previous point)"
                                )
                        else:
                            logger.warning(
                                f"Filtering out outlier coordinate: {lat:.5f}, {lon:.5f} "
                                f"(jump: {jump_distance:.1f} km from previous point)"
                            )

            if not filtered_lats:
                logger.warning("All coordinates were filtered out, returning original data")
                return lats, lons

            if len(filtered_lats) < len(lats):
                logger.info(f"Filtered coordinates: kept {len(filtered_lats)}/{len(lats)} points")
            return filtered_lats, filtered_lons

        except Exception as e:
            logger.error(f"Error filtering coordinates: {e}")
            return lats, lons

    def get_map_bounds_info(self, track_points: list[dict]) -> dict:
        """
        Get information about the bounds of track points without generating a map.
        Useful for debugging coordinate issues.

        Args:
            track_points: List of track point dictionaries

        Returns:
            Dictionary with bounds information
        """
        try:
            if not track_points:
                return {"error": "No track points provided"}

            lats = []
            lons = []
            valid_points = 0
            invalid_points = 0

            for point in track_points:
                lat = point.get("lat", 0)
                lon = point.get("lon", 0)

                if is_valid_coordinate(lat, lon):
                    lats.append(lat)
                    lons.append(lon)
                    valid_points += 1
                else:
                    invalid_points += 1

            if not lats:
                return {
                    "error": "No valid coordinates found",
                    "total_points": len(track_points),
                    "valid_points": valid_points,
                    "invalid_points": invalid_points,
                }

            bounds = self._calculate_optimal_bounds(lats, lons)

            return {
                "total_points": len(track_points),
                "valid_points": valid_points,
                "invalid_points": invalid_points,
                "lat_range": {"min": min(lats), "max": max(lats), "span": max(lats) - min(lats)},
                "lon_range": {"min": min(lons), "max": max(lons), "span": max(lons) - min(lons)},
                "center": {"lat": bounds["center_lat"], "lon": bounds["center_lon"]},
                "recommended_zoom": bounds["zoom"],
                "bounds": bounds["bounds"],
            }

        except Exception as e:
            return {"error": f"Error analyzing bounds: {e!s}"}
