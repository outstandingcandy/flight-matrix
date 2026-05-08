# src/core/

Core application components - exceptions and shared types.

## Exceptions (exceptions.py)

All custom exceptions inherit from `FlightMatrixError`:

```
FlightMatrixError (base)
├── ConfigurationError - Invalid config, missing keys
├── DatabaseError - Connection failures, query errors
├── APIError - External API failures (has status_code, response)
├── NotificationError - Email sending failures
├── SearchError - Web search provider errors
├── GeoLocationError - Geocoding failures
└── ScraperError - Web scraping failures
```

## Usage

```python
from src.core.exceptions import APIError, DatabaseError

try:
    response = call_external_api()
except RequestException as e:
    raise APIError(f"API call failed: {e}", status_code=500)

try:
    session.commit()
except SQLAlchemyError as e:
    raise DatabaseError(f"Database operation failed: {e}")
```

## Guidelines

- Never catch bare `Exception` - use specific exception types
- Include context in error messages
- Use `APIError` with `status_code` for HTTP errors
- Log errors before raising when appropriate
