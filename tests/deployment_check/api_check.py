"""External API health checks.

This module verifies connectivity and authentication to external
APIs used by flight-matrix, including ADS-B Exchange and Tavily.
"""

import logging
import re
import time

from tests.deployment_check.base import BaseHealthCheck, CheckResult, CheckStatus

logger = logging.getLogger("deployment_check.api")


class APIHealthCheck(BaseHealthCheck):
    """Health checks for external APIs.

    Checks performed:
    - ADS-B API key format validation
    - ADS-B API test call
    - Tavily API key format validation
    - Tavily search test
    """

    category = "External APIs"

    async def run(self) -> list[CheckResult]:
        """Execute all external API checks.

        Returns:
            List of CheckResult objects for each API check.
        """
        results: list[CheckResult] = []

        # ADS-B API checks
        results.extend(await self._check_adsb_api())

        # Tavily API checks
        results.extend(await self._check_tavily_api())

        return results

    async def _check_adsb_api(self) -> list[CheckResult]:
        """Check ADS-B Exchange API.

        Returns:
            List of CheckResult objects for ADS-B API checks.
        """
        results: list[CheckResult] = []

        if not self.config:
            start_time = time.perf_counter()
            results.append(
                self._skip(
                    "ADS-B API key format",
                    "Config not loaded",
                    start_time,
                )
            )
            return results

        api_config = self.config.get_api_config()
        api_key = api_config.get("adsb_api_key")
        api_url = api_config.get("adsb_api_url", "https://adsbexchange.com/api/aircraft/v2/all")

        # Check API key format
        format_result = self._check_adsb_key_format(api_key)
        results.append(format_result)

        # Only test API call if key format is valid
        if format_result.status == CheckStatus.PASS:
            results.append(await self._check_adsb_api_call(api_key, api_url))
        else:
            start_time = time.perf_counter()
            results.append(
                self._skip(
                    "ADS-B API call",
                    "Skipped due to invalid API key format",
                    start_time,
                )
            )

        return results

    def _check_adsb_key_format(self, api_key: str | None) -> CheckResult:
        """Check ADS-B API key format.

        Args:
            api_key: The API key to validate.

        Returns:
            CheckResult indicating if key format is valid.
        """
        start_time = time.perf_counter()

        if not api_key:
            return self._fail(
                "ADS-B API key format",
                "API key not configured",
                start_time,
            )

        # ADS-B Exchange API keys are typically UUIDs or hex strings
        # Check for reasonable length and alphanumeric format
        if len(api_key) < 16:
            return self._fail(
                "ADS-B API key format",
                "API key too short (expected 16+ characters)",
                start_time,
            )

        # Check for placeholder patterns
        placeholder_patterns = [
            r"^your[_-]?",
            r"^xxx+$",
            r"^placeholder",
            r"^changeme",
            r"^TODO",
        ]

        if any(re.match(pattern, api_key, re.IGNORECASE) for pattern in placeholder_patterns):
            return self._fail(
                "ADS-B API key format",
                "API key appears to be a placeholder",
                start_time,
            )

        masked_key = f"{api_key[:4]}...{api_key[-4:]}"
        return self._pass(
            "ADS-B API key format",
            f"Valid format ({masked_key})",
            start_time,
        )

    async def _check_adsb_api_call(self, api_key: str, api_url: str) -> CheckResult:
        """Test ADS-B API with a real call.

        Args:
            api_key: The API key to use.
            api_url: The API endpoint URL.

        Returns:
            CheckResult indicating if API call succeeds.
        """
        start_time = time.perf_counter()

        try:
            import aiohttp

            # Detect API type and set appropriate headers/URL
            if "rapidapi.com" in api_url:
                # RapidAPI requires specific headers and endpoint
                headers = {
                    "X-RapidAPI-Key": api_key,
                    "X-RapidAPI-Host": "adsbexchange-com1.p.rapidapi.com",
                    "Accept": "application/json",
                }
                # Use /mil endpoint for testing (returns military aircraft)
                test_url = f"{api_url.rstrip('/')}/mil"
            else:
                # Standard ADS-B Exchange API
                headers = {
                    "api-auth": api_key,
                    "Accept": "application/json",
                }
                test_url = api_url

            timeout = aiohttp.ClientTimeout(total=10)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(test_url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        aircraft_count = len(data.get("ac", data.get("aircraft", [])))
                        return self._pass(
                            "ADS-B API call",
                            f"API responded ({aircraft_count} aircraft)",
                            start_time,
                            {"status_code": 200, "aircraft_count": aircraft_count},
                        )
                    elif response.status == 401:
                        return self._fail(
                            "ADS-B API call",
                            "Authentication failed (401 Unauthorized)",
                            start_time,
                        )
                    elif response.status == 403:
                        return self._fail(
                            "ADS-B API call",
                            "Access denied (403 Forbidden)",
                            start_time,
                        )
                    elif response.status == 429:
                        return self._warn(
                            "ADS-B API call",
                            "Rate limited (429) - API key valid but quota exceeded",
                            start_time,
                        )
                    else:
                        return self._fail(
                            "ADS-B API call",
                            f"Unexpected status: {response.status}",
                            start_time,
                        )

        except ImportError:
            # Fall back to requests if aiohttp not available
            try:
                import requests

                # Detect API type and set appropriate headers/URL
                if "rapidapi.com" in api_url:
                    headers = {
                        "X-RapidAPI-Key": api_key,
                        "X-RapidAPI-Host": "adsbexchange-com1.p.rapidapi.com",
                        "Accept": "application/json",
                    }
                    test_url = f"{api_url.rstrip('/')}/mil"
                else:
                    headers = {
                        "api-auth": api_key,
                        "Accept": "application/json",
                    }
                    test_url = api_url

                response = requests.get(test_url, headers=headers, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    aircraft_count = len(data.get("ac", data.get("aircraft", [])))
                    return self._pass(
                        "ADS-B API call",
                        f"API responded ({aircraft_count} aircraft)",
                        start_time,
                        {"status_code": 200, "aircraft_count": aircraft_count},
                    )
                elif response.status_code == 401:
                    return self._fail(
                        "ADS-B API call",
                        "Authentication failed (401 Unauthorized)",
                        start_time,
                    )
                elif response.status_code == 403:
                    return self._fail(
                        "ADS-B API call",
                        "Access denied (403 Forbidden)",
                        start_time,
                    )
                elif response.status_code == 429:
                    return self._warn(
                        "ADS-B API call",
                        "Rate limited (429) - API key valid but quota exceeded",
                        start_time,
                    )
                else:
                    return self._fail(
                        "ADS-B API call",
                        f"Unexpected status: {response.status_code}",
                        start_time,
                    )

            except Exception as e:
                return self._fail(
                    "ADS-B API call",
                    f"Request failed: {e}",
                    start_time,
                )

        except Exception as e:
            return self._fail(
                "ADS-B API call",
                f"Request failed: {e}",
                start_time,
            )

    async def _check_tavily_api(self) -> list[CheckResult]:
        """Check Tavily Search API.

        Returns:
            List of CheckResult objects for Tavily API checks.
        """
        results: list[CheckResult] = []

        if not self.config:
            start_time = time.perf_counter()
            results.append(
                self._skip(
                    "Tavily API key format",
                    "Config not loaded",
                    start_time,
                )
            )
            return results

        # Tavily key might be in environment or config
        import os

        api_key = os.environ.get("TAVILY_API_KEY")

        # Also check config if not in environment
        if not api_key:
            # Try to get from search config or llm config
            search_config = self.config.get("search", {})
            api_key = search_config.get("tavily_api_key")

        # Check API key format
        format_result = self._check_tavily_key_format(api_key)
        results.append(format_result)

        # Only test API call if key format is valid
        if format_result.status == CheckStatus.PASS:
            results.append(await self._check_tavily_search(api_key))
        elif format_result.status != CheckStatus.SKIP:
            start_time = time.perf_counter()
            results.append(
                self._skip(
                    "Tavily search test",
                    "Skipped due to invalid API key format",
                    start_time,
                )
            )
        else:
            start_time = time.perf_counter()
            results.append(
                self._skip(
                    "Tavily search test",
                    "Tavily not configured",
                    start_time,
                )
            )

        return results

    def _check_tavily_key_format(self, api_key: str | None) -> CheckResult:
        """Check Tavily API key format.

        Args:
            api_key: The API key to validate.

        Returns:
            CheckResult indicating if key format is valid.
        """
        start_time = time.perf_counter()

        if not api_key:
            return self._skip(
                "Tavily API key format",
                "Tavily API key not configured (optional)",
                start_time,
            )

        # Tavily API keys typically start with "tvly-" and are 32+ chars
        if not api_key.startswith("tvly-"):
            return self._warn(
                "Tavily API key format",
                "Key doesn't match expected format (tvly-...)",
                start_time,
            )

        if len(api_key) < 20:
            return self._fail(
                "Tavily API key format",
                "API key too short",
                start_time,
            )

        masked_key = f"{api_key[:8]}...{api_key[-4:]}"
        return self._pass(
            "Tavily API key format",
            f"Valid format ({masked_key})",
            start_time,
        )

    async def _check_tavily_search(self, api_key: str | None) -> CheckResult:
        """Test Tavily Search API with a simple query.

        Args:
            api_key: The API key to use.

        Returns:
            CheckResult indicating if search succeeds.
        """
        start_time = time.perf_counter()

        if not api_key:
            return self._skip(
                "Tavily search test",
                "Tavily not configured",
                start_time,
            )

        try:
            import aiohttp

            url = "https://api.tavily.com/search"
            headers = {
                "Content-Type": "application/json",
            }
            payload = {
                "api_key": api_key,
                "query": "test",
                "max_results": 1,
            }

            timeout = aiohttp.ClientTimeout(total=10)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status == 200:
                        return self._pass(
                            "Tavily search test",
                            "Search API responded successfully",
                            start_time,
                        )
                    elif response.status == 401:
                        return self._fail(
                            "Tavily search test",
                            "Authentication failed (401)",
                            start_time,
                        )
                    elif response.status == 429:
                        return self._warn(
                            "Tavily search test",
                            "Rate limited (429) - key valid but quota exceeded",
                            start_time,
                        )
                    else:
                        return self._fail(
                            "Tavily search test",
                            f"Unexpected status: {response.status}",
                            start_time,
                        )

        except ImportError:
            # Fall back to requests
            try:
                import requests

                url = "https://api.tavily.com/search"
                payload = {
                    "api_key": api_key,
                    "query": "test",
                    "max_results": 1,
                }

                response = requests.post(url, json=payload, timeout=10)

                if response.status_code == 200:
                    return self._pass(
                        "Tavily search test",
                        "Search API responded successfully",
                        start_time,
                    )
                elif response.status_code == 401:
                    return self._fail(
                        "Tavily search test",
                        "Authentication failed (401)",
                        start_time,
                    )
                elif response.status_code == 429:
                    return self._warn(
                        "Tavily search test",
                        "Rate limited (429) - key valid but quota exceeded",
                        start_time,
                    )
                else:
                    return self._fail(
                        "Tavily search test",
                        f"Unexpected status: {response.status_code}",
                        start_time,
                    )

            except Exception as e:
                return self._fail(
                    "Tavily search test",
                    f"Request failed: {e}",
                    start_time,
                )

        except Exception as e:
            return self._fail(
                "Tavily search test",
                f"Request failed: {e}",
                start_time,
            )
