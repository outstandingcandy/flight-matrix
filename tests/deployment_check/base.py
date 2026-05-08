"""Base classes and types for deployment health checks.

This module defines the core abstractions for health checks including
the check status enum, result model, and abstract base class for
implementing specific health checks.
"""

import logging
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("deployment_check.base")


class CheckStatus(str, Enum):
    """Status of a health check execution."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARN = "WARN"


class CheckResult(BaseModel):
    """Result of a single health check.

    Attributes:
        name: Human-readable name of the check.
        status: Outcome of the check execution.
        message: Description of the result or error details.
        duration_ms: Time taken to execute the check in milliseconds.
        details: Optional additional information about the check.
    """

    name: str
    status: CheckStatus
    message: str
    duration_ms: float = Field(ge=0)
    details: dict[str, Any] | None = None


class BaseHealthCheck(ABC):
    """Abstract base class for health check implementations.

    Each health check implementation should:
    - Set a descriptive `category` class attribute
    - Implement the `run()` method to perform checks
    - Return a list of CheckResult objects

    Example:
        class MyHealthCheck(BaseHealthCheck):
            category = "My Category"

            async def run(self) -> list[CheckResult]:
                results = []
                result = await self._check_something()
                results.append(result)
                return results
    """

    category: str = "Uncategorized"

    def __init__(self, config: Any | None = None) -> None:
        """Initialize the health check.

        Args:
            config: Optional YAMLConfig instance for accessing configuration.
        """
        self.config = config

    @abstractmethod
    async def run(self) -> list[CheckResult]:
        """Execute all checks in this category.

        Returns:
            A list of CheckResult objects, one for each check performed.
        """
        pass

    def _make_result(
        self,
        name: str,
        status: CheckStatus,
        message: str,
        start_time: float,
        details: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Create a CheckResult with calculated duration.

        Args:
            name: Name of the check.
            status: Status of the check.
            message: Result message or error description.
            start_time: Time when the check started (from time.perf_counter()).
            details: Optional additional details.

        Returns:
            A CheckResult instance with duration calculated.
        """
        duration_ms = (time.perf_counter() - start_time) * 1000
        return CheckResult(
            name=name,
            status=status,
            message=message,
            duration_ms=duration_ms,
            details=details,
        )

    def _pass(
        self,
        name: str,
        message: str,
        start_time: float,
        details: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Create a passing CheckResult.

        Args:
            name: Name of the check.
            message: Success message.
            start_time: Time when the check started.
            details: Optional additional details.

        Returns:
            A CheckResult with PASS status.
        """
        return self._make_result(name, CheckStatus.PASS, message, start_time, details)

    def _fail(
        self,
        name: str,
        message: str,
        start_time: float,
        details: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Create a failing CheckResult.

        Args:
            name: Name of the check.
            message: Error message describing the failure.
            start_time: Time when the check started.
            details: Optional additional details.

        Returns:
            A CheckResult with FAIL status.
        """
        return self._make_result(name, CheckStatus.FAIL, message, start_time, details)

    def _warn(
        self,
        name: str,
        message: str,
        start_time: float,
        details: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Create a warning CheckResult.

        Args:
            name: Name of the check.
            message: Warning message.
            start_time: Time when the check started.
            details: Optional additional details.

        Returns:
            A CheckResult with WARN status.
        """
        return self._make_result(name, CheckStatus.WARN, message, start_time, details)

    def _skip(
        self,
        name: str,
        message: str,
        start_time: float,
        details: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Create a skipped CheckResult.

        Args:
            name: Name of the check.
            message: Reason for skipping the check.
            start_time: Time when the check started.
            details: Optional additional details.

        Returns:
            A CheckResult with SKIP status.
        """
        return self._make_result(name, CheckStatus.SKIP, message, start_time, details)
