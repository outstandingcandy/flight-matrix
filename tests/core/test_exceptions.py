"""Tests for src.core.exceptions."""

from __future__ import annotations

import pytest

from src.core.exceptions import (
    AnalysisError,
    APIError,
    ConfigurationError,
    CooldownActiveError,
    DatabaseError,
    FlightMatrixError,
    GeoLocationError,
    NotificationError,
    ResourceNotFoundError,
    ScraperError,
    SearchError,
    ValidationError,
)


class TestBaseHierarchy:
    def test_all_errors_subclass_flight_matrix_error(self) -> None:
        for cls in (
            ConfigurationError,
            DatabaseError,
            APIError,
            NotificationError,
            SearchError,
            GeoLocationError,
            AnalysisError,
            ScraperError,
            ValidationError,
            CooldownActiveError,
            ResourceNotFoundError,
        ):
            assert issubclass(cls, FlightMatrixError)
            assert issubclass(cls, Exception)

    def test_plain_instantiation(self) -> None:
        err = FlightMatrixError("boom")
        assert str(err) == "boom"


class TestAPIError:
    def test_defaults_are_none(self) -> None:
        err = APIError("timeout")
        assert err.status_code is None
        assert err.response is None

    def test_kwargs_attach(self) -> None:
        err = APIError("bad", status_code=503, response="upstream unavailable")
        assert err.status_code == 503
        assert err.response == "upstream unavailable"


class TestScraperError:
    def test_retryable_defaults_false(self) -> None:
        err = ScraperError("page load")
        assert err.retryable is False
        assert err.url is None

    def test_retryable_true(self) -> None:
        err = ScraperError("cloudflare", retryable=True, url="https://example.com/x")
        assert err.retryable is True
        assert err.url == "https://example.com/x"


class TestValidationError:
    def test_field_and_value(self) -> None:
        err = ValidationError("bad email", field="email", value="not-an-email")
        assert err.field == "email"
        assert err.value == "not-an-email"

    def test_value_can_be_any_object(self) -> None:
        err = ValidationError("bad", field="x", value={"a": 1})
        assert err.value == {"a": 1}


class TestCooldownActiveError:
    def test_all_optional(self) -> None:
        err = CooldownActiveError("chill")
        assert err.remaining_seconds is None
        assert err.aircraft_hex is None
        assert err.user_id is None

    def test_every_field(self) -> None:
        err = CooldownActiveError(
            "cool it",
            remaining_seconds=42.5,
            aircraft_hex="abc123",
            user_id=7,
        )
        assert err.remaining_seconds == 42.5
        assert err.aircraft_hex == "abc123"
        assert err.user_id == 7


class TestResourceNotFoundError:
    def test_field_attached(self) -> None:
        err = ResourceNotFoundError("missing user", resource_type="user", resource_id=99)
        assert err.resource_type == "user"
        assert err.resource_id == 99


class TestRaising:
    def test_raise_and_catch_via_base(self) -> None:
        with pytest.raises(FlightMatrixError):
            raise DatabaseError("boom")

    def test_raise_and_catch_concrete(self) -> None:
        with pytest.raises(APIError) as exc:
            raise APIError("gateway", status_code=502)
        assert exc.value.status_code == 502
