"""Web endpoint health checks.

This module verifies web application endpoints, API functionality,
and static asset delivery via CloudFront.
"""

import logging
import time
from typing import Any

from tests.deployment_check.base import BaseHealthCheck, CheckResult, CheckStatus

logger = logging.getLogger("deployment_check.endpoint")


class EndpointHealthCheck(BaseHealthCheck):
    """Health checks for web application endpoints.

    Checks performed:
    - API health endpoint
    - API search endpoint functionality
    - CloudFront static asset delivery
    - Frontend page accessibility
    """

    category = "Web Endpoints"

    def __init__(self, config: Any | None = None) -> None:
        """Initialize endpoint health check.

        Args:
            config: YAMLConfig instance for accessing configuration.
        """
        super().__init__(config)
        self._api_base_url: str | None = None
        self._cloudfront_domain: str | None = None

    async def run(self) -> list[CheckResult]:
        """Execute all endpoint checks.

        Returns:
            List of CheckResult objects for each endpoint check.
        """
        results: list[CheckResult] = []

        # Get endpoint configuration
        self._get_endpoint_config()

        # Check API health endpoint
        results.append(await self._check_api_health())

        # Check API search functionality
        if results[-1].status != CheckStatus.SKIP:
            results.append(await self._check_api_search())

        # Check CloudFront static assets
        results.append(await self._check_cloudfront_assets())

        # Check frontend page
        if self._api_base_url:
            results.append(await self._check_frontend_page())

        return results

    def _get_endpoint_config(self) -> None:
        """Extract endpoint configuration from config or environment."""
        import os

        # Try to get from environment first (Lambda-style)
        self._cloudfront_domain = os.environ.get("CLOUDFRONT_DOMAIN")

        # Try to get API base URL from environment or config
        self._api_base_url = os.environ.get("API_BASE_URL")

        if self.config:
            web_config = self.config.get("web", {})
            if not self._api_base_url:
                self._api_base_url = web_config.get("api_base_url")
            if not self._cloudfront_domain:
                self._cloudfront_domain = web_config.get("cloudfront_domain")

    async def _check_api_health(self) -> CheckResult:
        """Check API health endpoint.

        Returns:
            CheckResult indicating if health endpoint responds.
        """
        start_time = time.perf_counter()

        if not self._api_base_url:
            return self._skip(
                "API health endpoint",
                "API base URL not configured",
                start_time,
            )

        try:
            import aiohttp

            url = f"{self._api_base_url.rstrip('/')}/api/health"
            timeout = aiohttp.ClientTimeout(total=10)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        status = data.get("status", "unknown")
                        return self._pass(
                            "API health endpoint",
                            f"Healthy (status: {status})",
                            start_time,
                            {"status_code": 200, "health_status": status},
                        )
                    elif response.status == 404:
                        # Health endpoint not implemented - warn instead of fail
                        return self._warn(
                            "API health endpoint",
                            "Health endpoint not found (404) - consider adding /api/health",
                            start_time,
                        )
                    return self._fail(
                        "API health endpoint",
                        f"Unexpected status: {response.status}",
                        start_time,
                    )

        except ImportError:
            return self._check_api_health_sync(start_time)
        except Exception as e:
            return self._fail(
                "API health endpoint",
                f"Request failed: {e}",
                start_time,
            )

    def _check_api_health_sync(self, start_time: float) -> CheckResult:
        """Synchronous fallback for API health check.

        Args:
            start_time: Performance counter start time.

        Returns:
            CheckResult indicating if health endpoint responds.
        """
        try:
            import requests

            url = f"{self._api_base_url.rstrip('/')}/api/health"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                status = data.get("status", "unknown")
                return self._pass(
                    "API health endpoint",
                    f"Healthy (status: {status})",
                    start_time,
                    {"status_code": 200, "health_status": status},
                )
            elif response.status_code == 404:
                return self._warn(
                    "API health endpoint",
                    "Health endpoint not found (404) - consider adding /api/health",
                    start_time,
                )
            return self._fail(
                "API health endpoint",
                f"Unexpected status: {response.status_code}",
                start_time,
            )

        except Exception as e:
            return self._fail(
                "API health endpoint",
                f"Request failed: {e}",
                start_time,
            )

    async def _check_api_search(self) -> CheckResult:
        """Check API search endpoint functionality.

        Returns:
            CheckResult indicating if search returns data.
        """
        start_time = time.perf_counter()

        if not self._api_base_url:
            return self._skip(
                "API search endpoint",
                "API base URL not configured",
                start_time,
            )

        try:
            import aiohttp

            # Search for any aircraft with registration starting with common prefix
            url = f"{self._api_base_url.rstrip('/')}/api/aircraft/search?q=N&limit=5"
            timeout = aiohttp.ClientTimeout(total=15)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("success", True):
                            count = data.get("count", len(data.get("data", [])))
                            return self._pass(
                                "API search endpoint",
                                f"Search returned {count} results",
                                start_time,
                                {"status_code": 200, "result_count": count},
                            )
                        return self._fail(
                            "API search endpoint",
                            f"Search failed: {data.get('error', 'unknown')}",
                            start_time,
                        )
                    return self._fail(
                        "API search endpoint",
                        f"Unexpected status: {response.status}",
                        start_time,
                    )

        except ImportError:
            return self._check_api_search_sync(start_time)
        except Exception as e:
            return self._fail(
                "API search endpoint",
                f"Request failed: {e}",
                start_time,
            )

    def _check_api_search_sync(self, start_time: float) -> CheckResult:
        """Synchronous fallback for API search check.

        Args:
            start_time: Performance counter start time.

        Returns:
            CheckResult indicating if search returns data.
        """
        try:
            import requests

            url = f"{self._api_base_url.rstrip('/')}/api/aircraft/search?q=N&limit=5"
            response = requests.get(url, timeout=15)

            if response.status_code == 200:
                data = response.json()
                if data.get("success", True):
                    count = data.get("count", len(data.get("data", [])))
                    return self._pass(
                        "API search endpoint",
                        f"Search returned {count} results",
                        start_time,
                        {"status_code": 200, "result_count": count},
                    )
                return self._fail(
                    "API search endpoint",
                    f"Search failed: {data.get('error', 'unknown')}",
                    start_time,
                )
            return self._fail(
                "API search endpoint",
                f"Unexpected status: {response.status_code}",
                start_time,
            )

        except Exception as e:
            return self._fail(
                "API search endpoint",
                f"Request failed: {e}",
                start_time,
            )

    async def _check_cloudfront_assets(self) -> CheckResult:
        """Check CloudFront static asset delivery.

        Returns:
            CheckResult indicating if CloudFront serves static files.
        """
        start_time = time.perf_counter()

        if not self._cloudfront_domain:
            return self._skip(
                "CloudFront static assets",
                "CloudFront domain not configured",
                start_time,
            )

        try:
            import aiohttp

            # Test accessing main JavaScript file
            url = f"https://{self._cloudfront_domain}/static/js/app.js"
            timeout = aiohttp.ClientTimeout(total=10)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.head(url) as response:
                    if response.status == 200:
                        content_length = response.headers.get("Content-Length", "0")
                        return self._pass(
                            "CloudFront static assets",
                            f"Assets accessible ({self._cloudfront_domain})",
                            start_time,
                            {
                                "domain": self._cloudfront_domain,
                                "content_length": content_length,
                            },
                        )
                    elif response.status == 403:
                        return self._fail(
                            "CloudFront static assets",
                            "Access denied (403) - check S3 bucket policy",
                            start_time,
                        )
                    elif response.status == 404:
                        return self._fail(
                            "CloudFront static assets",
                            "Asset not found (404) - check S3 bucket contents",
                            start_time,
                        )
                    return self._fail(
                        "CloudFront static assets",
                        f"Unexpected status: {response.status}",
                        start_time,
                    )

        except ImportError:
            return self._check_cloudfront_assets_sync(start_time)
        except Exception as e:
            # Check if it's a connection/DNS error
            error_str = str(e).lower()
            if "resolve" in error_str or "connection" in error_str:
                return self._fail(
                    "CloudFront static assets",
                    f"Cannot reach CloudFront domain: {self._cloudfront_domain}",
                    start_time,
                )
            return self._fail(
                "CloudFront static assets",
                f"Request failed: {e}",
                start_time,
            )

    def _check_cloudfront_assets_sync(self, start_time: float) -> CheckResult:
        """Synchronous fallback for CloudFront check.

        Args:
            start_time: Performance counter start time.

        Returns:
            CheckResult indicating if CloudFront serves static files.
        """
        try:
            import requests

            url = f"https://{self._cloudfront_domain}/static/js/app.js"
            response = requests.head(url, timeout=10)

            if response.status_code == 200:
                content_length = response.headers.get("Content-Length", "0")
                return self._pass(
                    "CloudFront static assets",
                    f"Assets accessible ({self._cloudfront_domain})",
                    start_time,
                    {
                        "domain": self._cloudfront_domain,
                        "content_length": content_length,
                    },
                )
            elif response.status_code == 403:
                return self._fail(
                    "CloudFront static assets",
                    "Access denied (403) - check S3 bucket policy",
                    start_time,
                )
            elif response.status_code == 404:
                return self._fail(
                    "CloudFront static assets",
                    "Asset not found (404) - check S3 bucket contents",
                    start_time,
                )
            return self._fail(
                "CloudFront static assets",
                f"Unexpected status: {response.status_code}",
                start_time,
            )

        except Exception as e:
            return self._fail(
                "CloudFront static assets",
                f"Request failed: {e}",
                start_time,
            )

    async def _check_frontend_page(self) -> CheckResult:
        """Check frontend page loads and references correct CloudFront.

        Returns:
            CheckResult indicating if frontend is properly configured.
        """
        start_time = time.perf_counter()

        try:
            import aiohttp

            url = self._api_base_url.rstrip("/") + "/"
            timeout = aiohttp.ClientTimeout(total=10)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        html = await response.text()

                        # Check if CloudFront domain is referenced
                        if self._cloudfront_domain and self._cloudfront_domain in html:
                            return self._pass(
                                "Frontend page",
                                f"Page loads with correct CloudFront ({self._cloudfront_domain})",
                                start_time,
                                {"cloudfront_found": True},
                            )
                        elif self._cloudfront_domain:
                            # Check if some other CloudFront is referenced
                            import re

                            cf_match = re.search(r"(d[a-z0-9]+\.cloudfront\.net)", html)
                            if cf_match:
                                found_domain = cf_match.group(1)
                                return self._warn(
                                    "Frontend page",
                                    f"Page uses different CloudFront: {found_domain}",
                                    start_time,
                                    {
                                        "expected": self._cloudfront_domain,
                                        "found": found_domain,
                                    },
                                )
                            return self._warn(
                                "Frontend page",
                                "Page loads but no CloudFront reference found",
                                start_time,
                            )
                        return self._pass(
                            "Frontend page",
                            "Page loads successfully",
                            start_time,
                        )
                    return self._fail(
                        "Frontend page",
                        f"Unexpected status: {response.status}",
                        start_time,
                    )

        except ImportError:
            return self._check_frontend_page_sync(start_time)
        except Exception as e:
            return self._fail(
                "Frontend page",
                f"Request failed: {e}",
                start_time,
            )

    def _check_frontend_page_sync(self, start_time: float) -> CheckResult:
        """Synchronous fallback for frontend page check.

        Args:
            start_time: Performance counter start time.

        Returns:
            CheckResult indicating if frontend is properly configured.
        """
        try:
            import re

            import requests

            url = self._api_base_url.rstrip("/") + "/"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                html = response.text

                if self._cloudfront_domain and self._cloudfront_domain in html:
                    return self._pass(
                        "Frontend page",
                        f"Page loads with correct CloudFront ({self._cloudfront_domain})",
                        start_time,
                        {"cloudfront_found": True},
                    )
                elif self._cloudfront_domain:
                    cf_match = re.search(r"(d[a-z0-9]+\.cloudfront\.net)", html)
                    if cf_match:
                        found_domain = cf_match.group(1)
                        return self._warn(
                            "Frontend page",
                            f"Page uses different CloudFront: {found_domain}",
                            start_time,
                            {
                                "expected": self._cloudfront_domain,
                                "found": found_domain,
                            },
                        )
                    return self._warn(
                        "Frontend page",
                        "Page loads but no CloudFront reference found",
                        start_time,
                    )
                return self._pass(
                    "Frontend page",
                    "Page loads successfully",
                    start_time,
                )
            return self._fail(
                "Frontend page",
                f"Unexpected status: {response.status_code}",
                start_time,
            )

        except Exception as e:
            return self._fail(
                "Frontend page",
                f"Request failed: {e}",
                start_time,
            )
