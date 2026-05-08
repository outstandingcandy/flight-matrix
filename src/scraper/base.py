"""
Base scraper abstract class.

Provides the foundation for all scraper implementations with common functionality
for browser interaction, error handling, and result processing.
"""

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from src.scraper.models import ScraperResult, ScraperTask

logger = logging.getLogger("scraper.base")

T = TypeVar("T", bound=ScraperResult)


class BaseScraper(ABC, Generic[T]):
    """Abstract base class for web scrapers.

    Subclasses must implement:
        - task_type: Class attribute identifying the scraper type.
        - scrape(): Main scraping logic.
        - validate_task(): Task validation logic.

    Optional overrides:
        - build_url(): Construct URL from task.
        - parse_response(): Extract data from HTML.
        - post_process(): Transform extracted data.
        - setup(): Called before scraping starts.
        - teardown(): Called after scraping ends.
        - on_success(): Called after successful scrape.
        - on_failure(): Called after failed scrape.
        - should_retry(): Determine if task should be retried.

    Attributes:
        task_type: Identifier for this scraper type.
        default_delay: Tuple of (min, max) seconds between requests.
        requires_browser: Whether this scraper needs a browser.
        cloudflare_protected: Whether target site uses Cloudflare.
    """

    task_type: str = "base"

    # Configurable per scraper type
    default_delay: tuple[float, float] = (5.0, 15.0)
    requires_browser: bool = True
    cloudflare_protected: bool = False
    # Per-scraper task timeout in seconds. 0 means use worker default.
    task_timeout: int = 0

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize the scraper.

        Args:
            config: Optional configuration dictionary for this scraper.
        """
        self.config = config or {}
        self._setup_complete = False

    def _init_s3_client(
        self,
        s3_enabled: bool | None = None,
        s3_bucket: str | None = None,
        s3_prefix: str = "",
    ) -> Any | None:
        """Initialize and return an S3 client if S3 upload is enabled.

        Centralizes S3 client initialization to avoid code duplication
        across scrapers. Scrapers should store the returned client:

            self.s3_client = self._init_s3_client()

        Args:
            s3_enabled: Whether S3 upload is enabled. If None, reads from config.
            s3_bucket: S3 bucket name. If None, reads from config.
            s3_prefix: S3 key prefix for uploads. If empty, reads from config.

        Returns:
            Boto3 S3 client if successfully initialized, None otherwise.
        """
        s3_enabled = s3_enabled if s3_enabled is not None else self.config.get("s3_upload", False)
        s3_bucket = s3_bucket or self.config.get("s3_bucket", "")
        s3_prefix = s3_prefix or self.config.get("s3_prefix", "")

        if not s3_enabled or not s3_bucket:
            logger.debug(
                f"[{self.task_type}] S3 upload disabled (enabled={s3_enabled}, bucket={s3_bucket})"
            )
            return None

        try:
            import boto3

            client = boto3.client("s3")
            logger.info(f"[{self.task_type}] S3 upload enabled: s3://{s3_bucket}/{s3_prefix}")
            return client
        except Exception as e:
            logger.error(f"[{self.task_type}] Failed to initialize S3 client: {e}")
            return None

    def _init_db_engine(
        self,
        database_url: str | None = None,
        echo: bool = False,
        pool_pre_ping: bool = True,
    ) -> Any | None:
        """Initialize and return a SQLAlchemy database engine.

        Centralizes database engine initialization to avoid code duplication
        across scrapers. Scrapers should store the returned engine:

            self.db_engine = self._init_db_engine()

        Args:
            database_url: Database connection URL. If None, reads from config.
            echo: Enable SQLAlchemy query logging.
            pool_pre_ping: Enable connection health checking.

        Returns:
            SQLAlchemy Engine if successfully initialized, None otherwise.
        """
        database_url = database_url or self.config.get("database_url", "")

        if not database_url:
            logger.debug(f"[{self.task_type}] Database sync disabled (no database_url)")
            return None

        try:
            from sqlalchemy import create_engine

            engine = create_engine(
                database_url,
                echo=echo,
                pool_pre_ping=pool_pre_ping,
            )
            logger.info(f"[{self.task_type}] Database engine initialized")
            return engine
        except Exception as e:
            logger.error(f"[{self.task_type}] Failed to initialize database engine: {e}")
            return None

    @abstractmethod
    def scrape(self, task: ScraperTask, browser: Any | None = None) -> T:
        """Execute the scraping operation.

        Args:
            task: The task to process.
            browser: Browser instance (DrissionPage) if requires_browser is True.

        Returns:
            ScraperResult or subclass with extracted data.

        Raises:
            ScraperError: If scraping fails.
        """
        ...

    @abstractmethod
    def validate_task(self, task: ScraperTask) -> bool:
        """Validate that a task can be processed by this scraper.

        Args:
            task: The task to validate.

        Returns:
            True if the task is valid, False otherwise.
        """
        ...

    def build_url(self, task: ScraperTask) -> str:
        """Construct the target URL from task data.

        Override this method to customize URL construction.

        Args:
            task: The task containing URL parameters.

        Returns:
            The target URL to scrape.
        """
        return task.payload.get("url", "")

    def parse_response(self, html: str, task: ScraperTask) -> dict[str, Any]:
        """Parse HTML response and extract data.

        Override this method to implement custom parsing logic.

        Args:
            html: Raw HTML content.
            task: The task being processed.

        Returns:
            Dictionary of extracted data.
        """
        return {"raw_html_length": len(html)}

    def post_process(self, data: dict[str, Any], task: ScraperTask) -> dict[str, Any]:
        """Transform extracted data before returning.

        Override this method to implement data transformations.

        Args:
            data: Extracted data from parse_response.
            task: The task being processed.

        Returns:
            Transformed data dictionary.
        """
        return data

    def setup(self) -> None:
        """Perform setup operations before scraping.

        Called once before the first scrape operation.
        Override to initialize resources, authenticate, etc.
        """
        self._setup_complete = True
        logger.debug(f"Scraper {self.task_type} setup complete")

    def teardown(self) -> None:
        """Perform cleanup operations after scraping.

        Called when the worker shuts down.
        Override to close connections, save state, etc.
        """
        self._setup_complete = False
        logger.debug(f"Scraper {self.task_type} teardown complete")

    def on_success(self, task: ScraperTask, result: T) -> None:
        """Handle successful scrape completion.

        Override to implement post-success actions like logging or notifications.

        Args:
            task: The completed task.
            result: The scrape result.
        """
        logger.info(f"[{self.task_type}] Task {task.task_key} completed successfully")

    def on_failure(self, task: ScraperTask, error: Exception) -> None:
        """Handle scrape failure.

        Override to implement error handling, alerting, etc.

        Args:
            task: The failed task.
            error: The exception that caused the failure.
        """
        logger.error(f"[{self.task_type}] Task {task.task_key} failed: {error}")

    def should_retry(self, task: ScraperTask, error: Exception) -> bool:
        """Determine if a failed task should be retried.

        Override to implement custom retry logic based on error types.

        Args:
            task: The failed task.
            error: The exception that caused the failure.

        Returns:
            True if the task should be retried, False otherwise.
        """
        # Default: retry if under max attempts
        return task.attempts < task.max_attempts

    def get_delay(self) -> float:
        """Get a randomized delay between requests.

        Returns:
            Delay in seconds.
        """
        min_delay, max_delay = self.default_delay
        return random.uniform(min_delay, max_delay)

    def wait_delay(self) -> None:
        """Wait for the configured delay between requests."""
        delay = self.get_delay()
        logger.debug(f"[{self.task_type}] Waiting {delay:.1f}s between requests")
        time.sleep(delay)

    def _dismiss_cookie_consent(self, browser: Any, context_key: str = "") -> None:
        """Dismiss cookie consent dialogs commonly found on aviation sites.

        Tries multiple selector strategies to find and click dismiss/reject buttons.
        Safe to call even if no dialog is present.

        Args:
            browser: Browser instance.
            context_key: Identifier for logging (e.g., airport code, registration).
        """
        try:
            consent_selectors = [
                "text:Disagree and close",
                "text:Reject all",
                "text:Agree and close",
                "text:Accept all",
                "text:Agree",
                "button:contains(Disagree)",
                "button:contains(Reject)",
            ]

            for selector in consent_selectors:
                try:
                    btn = browser.ele(selector, timeout=2)
                    if btn:
                        try:
                            if btn.states.is_displayed:
                                btn.click()
                                logger.info(
                                    f"[{context_key}] Dismissed cookie consent via '{selector}'"
                                )
                                time.sleep(2)
                                return
                        except Exception:
                            btn.click()
                            logger.info(
                                f"[{context_key}] Dismissed cookie consent via '{selector}'"
                            )
                            time.sleep(2)
                            return
                except Exception:
                    continue

            # Try FC-specific consent button (Funding Choices)
            try:
                btn = browser.ele("@class=fc-cta-consent", timeout=2)
                if btn:
                    btn.click()
                    logger.info(f"[{context_key}] Dismissed cookie consent via fc-cta-consent")
                    time.sleep(2)
                    return
            except Exception:
                pass

            logger.debug(f"[{context_key}] No cookie consent dialog found or already dismissed")

        except Exception as e:
            logger.debug(f"[{context_key}] Error handling cookie consent: {e}")

    def handle_cloudflare(
        self,
        browser: Any,
        max_wait: int = 120,
        screenshot_dir: str = "data/cloudflare_screenshots",
    ) -> bool:
        """Handle Cloudflare challenge if detected.

        Simplified approach matching image_service.py - just wait and check.
        Excessive interactions can trigger more detection.

        Args:
            browser: Browser instance.
            max_wait: Maximum seconds to wait for challenge resolution.
            screenshot_dir: Directory to save Cloudflare challenge screenshots.

        Returns:
            True if challenge was resolved or not present.
        """
        if not self.cloudflare_protected:
            return True

        html = browser.html.lower()
        title = (browser.title or "").lower()

        # Check for Cloudflare challenge indicators
        # Must check title first - Cloudflare challenge has specific titles
        cf_title_indicators = ["just a moment", "attention required", "checking your browser"]
        cf_body_indicators = [
            "checking if the site connection is secure",
            "enable javascript and cookies to continue",
            "ray id:",  # Cloudflare Ray ID typically appears on challenge pages
        ]

        # If title doesn't indicate Cloudflare challenge, page is likely loaded
        if not any(indicator in title for indicator in cf_title_indicators):
            # Double-check body for challenge-specific content
            if not any(indicator in html for indicator in cf_body_indicators):
                return True

        logger.info(
            f"[{self.task_type}] Cloudflare challenge detected, waiting up to {max_wait}s..."
        )

        # Save initial screenshot
        self._save_cloudflare_screenshot(browser, screenshot_dir, "initial")

        start_time = time.time()
        check_count = 0

        # Simple wait approach - matching jetphotos_simple.py
        while time.time() - start_time < max_wait:
            time.sleep(5)  # Increased from 3s to 5s
            check_count += 1

            try:
                html = browser.html.lower()
                title = (browser.title or "").lower()
            except Exception:
                continue

            # Check if challenge is resolved
            title_clear = not any(ind in title for ind in cf_title_indicators)
            body_clear = not any(ind in html for ind in cf_body_indicators)

            if title_clear and body_clear:
                elapsed = time.time() - start_time
                logger.info(
                    f"[{self.task_type}] Cloudflare challenge resolved after {elapsed:.1f}s"
                )
                return True

            # Save screenshot every 30 seconds
            if check_count % 6 == 0:
                elapsed = int(time.time() - start_time)
                self._save_cloudflare_screenshot(browser, screenshot_dir, f"check_{elapsed}s")

        # Save final screenshot on timeout
        self._save_cloudflare_screenshot(browser, screenshot_dir, "timeout")
        logger.warning(f"[{self.task_type}] Cloudflare challenge timeout after {max_wait}s")
        return False

    def _save_cloudflare_screenshot(self, browser: Any, screenshot_dir: str, suffix: str) -> None:
        """Save a screenshot for Cloudflare debugging.

        Args:
            browser: Browser instance.
            screenshot_dir: Directory to save screenshots.
            suffix: Suffix for the filename.
        """
        try:
            import os
            from datetime import datetime

            os.makedirs(screenshot_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{screenshot_dir}/cf_{self.task_type}_{timestamp}_{suffix}.png"

            # DrissionPage screenshot method
            if hasattr(browser, "get_screenshot"):
                browser.get_screenshot(path=filename)
            elif hasattr(browser, "save_screenshot"):
                browser.save_screenshot(filename)
            else:
                # Try generic screenshot
                browser.get_screenshot(path=filename, full_page=True)

            logger.info(f"[{self.task_type}] Saved Cloudflare screenshot: {filename}")
        except Exception as e:
            logger.warning(f"[{self.task_type}] Failed to save screenshot: {e}")


class ScraperError(Exception):
    """Base exception for scraper errors."""

    def __init__(
        self,
        message: str,
        task_key: str | None = None,
        retryable: bool = True,
    ) -> None:
        """Initialize the scraper error.

        Args:
            message: Error message.
            task_key: Key of the task that failed.
            retryable: Whether this error is recoverable by retrying.
        """
        super().__init__(message)
        self.task_key = task_key
        self.retryable = retryable


class CloudflareBlockedError(ScraperError):
    """Raised when Cloudflare blocks the request."""

    def __init__(self, task_key: str | None = None) -> None:
        super().__init__(
            "Request blocked by Cloudflare",
            task_key=task_key,
            retryable=True,
        )


class PageLoadError(ScraperError):
    """Raised when a page fails to load properly."""

    def __init__(self, url: str, task_key: str | None = None) -> None:
        super().__init__(
            f"Failed to load page: {url}",
            task_key=task_key,
            retryable=True,
        )


class NoDataFoundError(ScraperError):
    """Raised when no data is found on the page."""

    def __init__(self, task_key: str | None = None) -> None:
        super().__init__(
            "No data found on page",
            task_key=task_key,
            retryable=False,
        )
