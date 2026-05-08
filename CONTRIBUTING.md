# Contributing to Flight Matrix

Thanks for your interest in contributing. This document describes how to set
up a development environment, the code style we enforce, and the pull-request
workflow.

## Code of conduct

By participating in this project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md). Report unacceptable behavior via the
contact channel listed there.

## Development setup

### Prerequisites

- **Python 3.11 or 3.12** (CI runs both).
- [**`uv`**](https://docs.astral.sh/uv/) for dependency management.
- **Chromium** installed locally if you plan to work on the scraper
  (DrissionPage uses it). On Ubuntu: `sudo apt install chromium-browser`.
- A **PostgreSQL** instance (optional — SQLite works for most dev tasks).

### First-time setup

```bash
git clone --recurse-submodules https://github.com/outstandingcandy/flight-matrix.git
cd flight-matrix
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env
# Edit .env — the defaults bootstrap a SQLite-backed local dev instance.
```

### Running the app

```bash
# Web (Flask, auth bypassed, mock admin user)
uv run python web_app.py --skip-auth

# Scraper worker against one source in debug mode
uv run python src/scraper_main.py --local --debug --scrapers fr24_flights

# Track service (ADS-B → DB)
uv run python src/track_main.py --local

# Report service (DB → email digest)
uv run python src/report_main.py --local
```

Configuration is documented in [docs/configuration.md](docs/configuration.md).

## Code style

All checks must pass locally before you push.

```bash
uv run ruff check src/ tests/       # Lint
uv run ruff format src/ tests/      # Format
uv run mypy src/                    # Type-check (strict on src/core; non-strict elsewhere)
uv run pytest tests/                # Run tests
```

Or install the pre-commit hooks to get the same gates automatically on every
commit (recommended):

```bash
uv pip install pre-commit
pre-commit install
pre-commit run --all-files   # First-time full pass
```

Pre-commit mirrors what CI runs, so if it passes locally, CI should too.

House rules (also enforced by CI):

- **Python** — type hints on every public function and method argument.
  `mypy --strict` must pass.
- **Docstrings** — Google style on every public API. English only.
- **Logging** — use `structlog` (or `logging` where structlog is unavailable).
  No `print()` in `src/`. User-facing scripts under `scripts/` may use `print`.
- **Exceptions** — catch specific exception types (`SQLAlchemyError`,
  `RequestException`, `botocore.exceptions.ClientError`, custom errors from
  `src/core/exceptions.py`). Bare `except:` is forbidden and enforced by
  ruff (E722). `except Exception` is tolerated at service boundaries
  (route handlers, long-running worker loops) but must include a comment
  explaining why a catch-all is correct, and a `logger.exception(...)` or
  explicit re-raise. New code should narrow the type; 256 legacy call sites
  are grandfathered and migrated module-by-module as part of Phase 5
  refactors.
- **Imports** — absolute only (`from src.services.aircraft_service import ...`).
- **SQL** — always via SQLAlchemy ORM or `text()` with bound parameters.
  Never string-format user input into SQL.
- **Commits** — conventional format: `type(scope): short summary`.
  Common types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`.

### Frontend

HTML templates live in `web_templates/`; CSS and JS in `web_static/`. Avoid
inline `<style>` and `<script>` blocks — extract into the static directories.
There is no JS build step; write modern ES202x and rely on browsers.

Details and the extraction procedure for legacy inline blocks:
[docs/frontend.md](docs/frontend.md).

## Testing

- Use `pytest` + `pytest-asyncio`.
- Tests use **SQLite in-memory by default**. Override with `TEST_DATABASE_URL`
  if you need to run against Postgres.
- Put unit tests next to the module they cover under `tests/<subpackage>/`.
- Add a Flask-test-client integration test when you add an HTTP route.

```bash
uv run pytest tests/                              # Full suite
uv run pytest tests/scraper/test_jetphotos.py -v  # One file
uv run pytest --cov=src --cov-report=term-missing # Coverage
```

## Pull-request workflow

1. **Fork** the repo and create a feature branch from `main`:
   `git checkout -b feat/my-change`.
2. **Make focused commits**. Prefer several small commits over one giant one.
3. **Run the full check suite** (`ruff`, `mypy`, `pytest`) and make sure it
   is green.
4. **Open a PR** against `main`. Fill in the PR template — describe the change,
   the reasoning, and how you tested it. Link related issues.
5. **CI runs** on every push. The PR cannot merge until ruff, mypy, pytest,
   and secret-scan all pass.
6. **Address review feedback** by pushing additional commits (don't force-push
   unless asked; we squash on merge).

### Scope tips

- One concern per PR. A refactor and a feature belong in separate PRs.
- Don't rename modules in the same PR that changes their behavior — it makes
  review painful. Split the rename into its own commit or PR.
- Update docs (`docs/`, `CLAUDE.md`, `README.md`) when you change something
  user-facing.

## Reporting bugs / requesting features

- **Bug reports** — open a GitHub issue using the bug-report template.
  Include reproduction steps, expected vs. actual behavior, and environment
  details.
- **Feature requests** — use the feature-request template. Explain the use
  case first; the design second.
- **Security issues** — open a regular GitHub issue for vulnerability
  reports. There is no private channel, so if a flaw has serious impact
  and you want to coordinate disclosure, note that in the first line of
  the issue and keep details minimal until a maintainer responds.

## Getting help

If you get stuck setting up the dev environment or interpreting the code, open
a GitHub Discussion (or an issue if Discussions aren't enabled yet) with the
`question` label. For questions about the architecture, skim
[docs/architecture.md](docs/architecture.md) and [CLAUDE.md](CLAUDE.md) first —
the latter has concise orientation notes aimed at LLM-assisted development but
reads fine for humans too.
