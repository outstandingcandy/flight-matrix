# tests/

Test suite using pytest.

## Structure

```
tests/
├── scraper/           # Scraper framework tests
│   ├── test_base.py
│   ├── test_browser_pool.py
│   ├── test_jetphotos.py
│   ├── test_fr24_flights.py
│   └── test_task_queue.py
├── deployment_check/  # Deployment verification tests
├── test_multi_user_services.py
├── test_tracking_cycle_mock.py
└── test_jetphotos_metadata.py
```

## Running Tests

```bash
# Run all tests
uv run pytest tests/

# Run specific test file
uv run pytest tests/scraper/test_jetphotos.py -v

# Run with coverage
uv run pytest tests/ --cov=src

# Run specific test function
uv run pytest tests/test_multi_user_services.py::test_user_creation -v
```

## Test Database

Tests use SQLite in-memory database by default. Set `TEST_DATABASE_URL` environment variable to use a different database.

## Fixtures

Common fixtures are defined in `conftest.py` files:
- `db_session` - SQLAlchemy session
- `test_config` - Test configuration dict
- `mock_browser` - Mocked browser for scraper tests
