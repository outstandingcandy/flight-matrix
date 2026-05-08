"""
Reverse geocoding module.
Determines the country an aircraft is in based on its lat/lon coordinates.
"""

import logging
import threading

import reverse_geocoder as rg

# Set up logging
logger = logging.getLogger("geo_locator")


class GeoLocator:
    """Geo locator"""

    def __init__(self):
        """Initialize the geo locator"""
        self.cache = {}  # Simple in-memory cache
        self.cache_max_size = 1000
        self._init_lock = threading.Lock()  # Initialization lock
        self._rg_initialized = False  # reverse_geocoder initialization flag
        logger.info("GeoLocator initialized with reverse geocoding support")

    def _ensure_rg_initialized(self):
        """Ensure reverse_geocoder is initialized (thread-safe)"""
        if not self._rg_initialized:
            with self._init_lock:
                if not self._rg_initialized:
                    # Warm up reverse_geocoder to avoid concurrent initialization
                    try:
                        rg.search((0, 0))  # Warm-up call
                        self._rg_initialized = True
                        logger.info("reverse_geocoder initialized and warmed up")
                    except Exception as e:
                        logger.warning(f"Failed to warm up reverse_geocoder: {e}")

    def get_country_from_coordinates(self, latitude: float, longitude: float) -> str | None:
        """
        Get country name from latitude/longitude (synchronous version).

        Args:
            latitude: Latitude
            longitude: Longitude

        Returns:
            Country name, or None if lookup fails
        """
        if latitude is None or longitude is None:
            return None

        # Validate coordinates
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            logger.warning(f"Invalid coordinates: lat={latitude}, lon={longitude}")
            return None

        # Generate cache key (rounded to 0.1 degree to reduce cache size)
        cache_key = (round(latitude, 1), round(longitude, 1))

        # Check cache
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Ensure reverse_geocoder is initialized
        self._ensure_rg_initialized()

        try:
            # Use reverse_geocoder to look up
            results = rg.search((latitude, longitude))

            if results and len(results) > 0:
                country = results[0].get("cc")  # Country code

                # Get full country name
                if country:
                    # Map country code to full name
                    full_country_name = self._get_full_country_name(country)

                    # Cache the result
                    self._add_to_cache(cache_key, full_country_name)

                    logger.debug(
                        f"Located coordinates ({latitude}, {longitude}) in {full_country_name} ({country})"
                    )
                    return full_country_name
                else:
                    logger.warning(f"No country found for coordinates ({latitude}, {longitude})")
                    return None
            else:
                logger.warning(f"No results for coordinates ({latitude}, {longitude})")
                return None

        except Exception as e:
            logger.error(f"Error in reverse geocoding for ({latitude}, {longitude}): {e}")
            return None

    async def async_batch_get_countries(
        self, coordinates_list: list[tuple[float, float]]
    ) -> list[str | None]:
        """
        Asynchronously get country names for multiple coordinates in batch.

        Args:
            coordinates_list: Coordinate list [(lat1, lon1), (lat2, lon2), ...]

        Returns:
            List of country names corresponding to the input coordinates
        """
        if not coordinates_list:
            return []

        # Warm up reverse_geocoder first
        self._ensure_rg_initialized()

        # Check cache, separate cached from uncached coordinates
        cached_results = {}
        uncached_coords = []
        uncached_indices = []

        for i, (lat, lon) in enumerate(coordinates_list):
            if lat is None or lon is None:
                cached_results[i] = None
                continue

            cache_key = (round(lat, 1), round(lon, 1))
            if cache_key in self.cache:
                cached_results[i] = self.cache[cache_key]
            else:
                uncached_coords.append((lat, lon))
                uncached_indices.append(i)

        # Batch lookup uncached coordinates
        if uncached_coords:
            # Directly invoke batch search (handled in parallel internally)
            try:
                uncached_results = self._batch_search(uncached_coords)

                # Add results to cache and merge
                for i, coord_idx in enumerate(uncached_indices):
                    if i < len(uncached_results):
                        result = uncached_results[i]
                        cached_results[coord_idx] = result

                        # Add to cache
                        if result:
                            lat, lon = uncached_coords[i]
                            cache_key = (round(lat, 1), round(lon, 1))
                            self._add_to_cache(cache_key, result)

            except Exception as e:
                logger.error(f"Error in batch reverse geocoding: {e}")
                # Return None for failed coordinates
                for coord_idx in uncached_indices:
                    if coord_idx not in cached_results:
                        cached_results[coord_idx] = None

        # Return results in original order
        return [cached_results.get(i) for i in range(len(coordinates_list))]

    def _batch_search(self, coordinates_list: list[tuple[float, float]]) -> list[str | None]:
        """Batch search coordinates (using reverse_geocoder's batch API)"""
        if not coordinates_list:
            return []

        try:
            # Filter valid coordinates
            valid_coords = []
            coord_indices = []

            for i, (lat, lon) in enumerate(coordinates_list):
                if (
                    lat is not None
                    and lon is not None
                    and (-90 <= lat <= 90)
                    and (-180 <= lon <= 180)
                ):
                    valid_coords.append((lat, lon))
                    coord_indices.append(i)

            # Initialize result list
            results = [None] * len(coordinates_list)

            if valid_coords:
                # Use reverse_geocoder's batch search feature
                logger.info(f"Batch searching {len(valid_coords)} coordinates")
                rg_results = rg.search(valid_coords)  # Pass coordinate list for batch query

                # Process batch results
                for i, result in enumerate(rg_results):
                    if i < len(coord_indices):
                        coord_idx = coord_indices[i]
                        if result and "cc" in result:
                            country_code = result["cc"]
                            if country_code:
                                results[coord_idx] = self._get_full_country_name(country_code)
                                logger.debug(
                                    f"Located coordinates {valid_coords[i]} in {results[coord_idx]}"
                                )

            return results

        except Exception as e:
            logger.error(f"Error in batch reverse geocoding: {e}")
            return [None] * len(coordinates_list)

    def _get_full_country_name(self, country_code: str) -> str:
        """
        Convert country code to full country name.

        Args:
            country_code: Two-letter country code (ISO 3166-1 alpha-2)

        Returns:
            Full country name
        """
        # Common country code mapping
        country_mapping = {
            "US": "United States",
            "CN": "China",
            "RU": "Russia",
            "GB": "United Kingdom",
            "DE": "Germany",
            "FR": "France",
            "JP": "Japan",
            "IN": "India",
            "CA": "Canada",
            "AU": "Australia",
            "BR": "Brazil",
            "MX": "Mexico",
            "IT": "Italy",
            "ES": "Spain",
            "KR": "South Korea",
            "TR": "Turkey",
            "SA": "Saudi Arabia",
            "AE": "United Arab Emirates",
            "EG": "Egypt",
            "ZA": "South Africa",
            "NG": "Nigeria",
            "AR": "Argentina",
            "CL": "Chile",
            "PE": "Peru",
            "CO": "Colombia",
            "VE": "Venezuela",
            "NL": "Netherlands",
            "BE": "Belgium",
            "CH": "Switzerland",
            "AT": "Austria",
            "SE": "Sweden",
            "NO": "Norway",
            "DK": "Denmark",
            "FI": "Finland",
            "PL": "Poland",
            "CZ": "Czech Republic",
            "HU": "Hungary",
            "RO": "Romania",
            "BG": "Bulgaria",
            "GR": "Greece",
            "PT": "Portugal",
            "IE": "Ireland",
            "IS": "Iceland",
            "UA": "Ukraine",
            "BY": "Belarus",
            "LT": "Lithuania",
            "LV": "Latvia",
            "EE": "Estonia",
            "RS": "Serbia",
            "HR": "Croatia",
            "SI": "Slovenia",
            "SK": "Slovakia",
            "MD": "Moldova",
            "AL": "Albania",
            "MK": "North Macedonia",
            "BA": "Bosnia and Herzegovina",
            "ME": "Montenegro",
            "KG": "Kyrgyzstan",
            "KZ": "Kazakhstan",
            "UZ": "Uzbekistan",
            "TM": "Turkmenistan",
            "TJ": "Tajikistan",
            "AF": "Afghanistan",
            "PK": "Pakistan",
            "BD": "Bangladesh",
            "LK": "Sri Lanka",
            "MM": "Myanmar",
            "TH": "Thailand",
            "VN": "Vietnam",
            "KH": "Cambodia",
            "LA": "Laos",
            "MY": "Malaysia",
            "SG": "Singapore",
            "ID": "Indonesia",
            "PH": "Philippines",
            "BN": "Brunei",
            "MN": "Mongolia",
            "KP": "North Korea",
            "TW": "Taiwan",
            "HK": "Hong Kong",
            "MO": "Macau",
            "NZ": "New Zealand",
            "PG": "Papua New Guinea",
            "FJ": "Fiji",
            "IL": "Israel",
            "PS": "Palestine",
            "JO": "Jordan",
            "LB": "Lebanon",
            "SY": "Syria",
            "IQ": "Iraq",
            "IR": "Iran",
            "KW": "Kuwait",
            "BH": "Bahrain",
            "QA": "Qatar",
            "OM": "Oman",
            "YE": "Yemen",
            "GE": "Georgia",
            "AM": "Armenia",
            "AZ": "Azerbaijan",
        }

        return country_mapping.get(country_code.upper(), country_code.upper())

    def _add_to_cache(self, key: tuple[float, float], value: str):
        """Add to cache"""
        if len(self.cache) >= self.cache_max_size:
            # Simple FIFO cache eviction
            first_key = next(iter(self.cache))
            del self.cache[first_key]

        self.cache[key] = value

    def get_cache_stats(self) -> dict:
        """Get cache statistics"""
        return {"cache_size": len(self.cache), "max_cache_size": self.cache_max_size}


# Global instance
_geo_locator = None


def get_geo_locator() -> GeoLocator:
    """Get the global geo locator instance"""
    global _geo_locator
    if _geo_locator is None:
        _geo_locator = GeoLocator()
    return _geo_locator
