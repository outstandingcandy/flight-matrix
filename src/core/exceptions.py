"""Custom exceptions for the flight-matrix application.

This module defines a hierarchy of exceptions used throughout the application
for consistent error handling and reporting.
"""

from __future__ import annotations


class FlightMatrixError(Exception):
    """Base exception for all flight-matrix errors."""


class ConfigurationError(FlightMatrixError):
    """Raised when there is a configuration-related error.

    Examples:
        - Missing required configuration keys
        - Invalid configuration values
        - Configuration file not found
    """


class DatabaseError(FlightMatrixError):
    """Raised when there is a database operation error.

    Examples:
        - Connection failures
        - Query execution errors
        - Data integrity violations
    """


class APIError(FlightMatrixError):
    """Raised when there is an external API error.

    Examples:
        - ADS-B Exchange API failures
        - HTTP request timeouts
        - Rate limiting errors
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class NotificationError(FlightMatrixError):
    """Raised when there is an email notification error.

    Examples:
        - SMTP connection failures
        - AWS SES errors
        - Invalid recipient addresses
    """


class SearchError(FlightMatrixError):
    """Raised when there is a search provider error.

    Examples:
        - Tavily API failures
        - DuckDuckGo search errors
        - Search rate limiting
    """


class GeoLocationError(FlightMatrixError):
    """Raised when there is a geolocation error.

    Examples:
        - Invalid coordinates
        - Reverse geocoding failures
    """


class AnalysisError(FlightMatrixError):
    """Raised when there is an AI analysis error.

    Examples:
        - AWS Bedrock API failures
        - Model invocation errors
        - Token limit exceeded
    """


class ScraperError(FlightMatrixError):
    """Raised when there is a web scraping error.

    Includes a retryable flag to indicate whether the operation should be
    retried.

    Examples:
        - Page load failures
        - Element not found
        - Cloudflare blocking
        - Rate limiting

    Attributes:
        retryable: Whether this error can be retried.
        url: The URL being scraped when the error occurred.
    """

    def __init__(
        self,
        message: str,
        retryable: bool = False,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.url = url


class ValidationError(FlightMatrixError):
    """Raised when input validation fails.

    Examples:
        - Invalid filter SQL
        - Malformed email address
        - Invalid coordinates
        - Out-of-range values

    Attributes:
        field: The field that failed validation.
        value: The invalid value.
    """

    def __init__(
        self,
        message: str,
        field: str | None = None,
        value: object | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.value = value


class CooldownActiveError(FlightMatrixError):
    """Raised when an operation is blocked by an active cooldown.

    Used to signal that a report or notification should not be sent because
    the cooldown period has not elapsed.

    Examples:
        - Aircraft report cooldown not expired
        - User rate limit not reset
        - Notification frequency limit

    Attributes:
        remaining_seconds: Seconds until cooldown expires.
        aircraft_hex: The aircraft identifier (if applicable).
        user_id: The user ID (if applicable).
    """

    def __init__(
        self,
        message: str,
        remaining_seconds: float | None = None,
        aircraft_hex: str | None = None,
        user_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.remaining_seconds = remaining_seconds
        self.aircraft_hex = aircraft_hex
        self.user_id = user_id


class ResourceNotFoundError(FlightMatrixError):
    """Raised when a requested resource is not found.

    Examples:
        - User not found
        - Aircraft not in database
        - Filter not found

    Attributes:
        resource_type: Type of resource (user, aircraft, filter, etc.).
        resource_id: Identifier of the missing resource.
    """

    def __init__(
        self,
        message: str,
        resource_type: str | None = None,
        resource_id: object | None = None,
    ) -> None:
        super().__init__(message)
        self.resource_type = resource_type
        self.resource_id = resource_id
