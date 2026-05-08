# Flight Matrix

Real-time aircraft tracking and analysis system. Ingests ADS-B data, enriches it
with airline/airport/photo metadata scraped from public sources, applies
configurable filters, and emails AI-generated summary reports when matching
flights are observed.

> **Status:** early open-source release. APIs and internal structure may change.
> See [CHANGELOG.md](CHANGELOG.md).

English · [中文](README.zh-CN.md)

---

## What it does

- **Track** — polls ADS-B Exchange for aircraft positions and stores snapshots.
- **Scrape** — pulls flight schedules (FR24), aircraft photos (JetPhotos,
  Planespotters), airport metadata, and social-media mentions through a
  distributed scraper framework with browser pooling.
- **Filter** — three independent filter engines (snapshot-based, schedule-based,
  registration-match) evaluate rules against incoming data.
- **Report** — generates an HTML digest with maps, aircraft images, and a
  Claude-powered narrative analysis, then delivers via SMTP or AWS SES.
- **Web UI** — Flask app for queries, admin dashboards, and user filter/
  subscription management backed by Cognito auth.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Track Service  │     │ Report Service  │     │ Scraper Service │
│ (ADS-B → DB)    │     │ (DB → Email)    │     │ (FR24/JetPhotos)│
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 │
                        ┌────────┴────────┐
                        │   PostgreSQL    │
                        └────────┬────────┘
                                 │
                        ┌────────┴────────┐
                        │   Flask Web     │
                        │  (web_app.py)   │
                        └─────────────────┘
```

Three independent services share one PostgreSQL database. The Flask app runs
locally or on AWS Lambda (via a Mangum ASGI adapter).

More detail: [docs/architecture.md](docs/architecture.md).

## Tech stack

- **Python 3.11+** with [`uv`](https://docs.astral.sh/uv/) for dependency
  management.
- **Flask** web framework; **SQLAlchemy** ORM; **Pydantic v2** schemas.
- **PostgreSQL** (Aurora Serverless in production) or **SQLite** for local dev.
- **DrissionPage** + Chromium for scraping behind Cloudflare.
- **AWS** (Lambda, CDK, Cognito, SES, S3, CloudFront) for production deployment.
- **Anthropic Claude** via AWS Bedrock for flight-analysis narratives.

## Quick start

```bash
# 1. Clone and install dependencies
git clone --recurse-submodules https://github.com/outstandingcandy/flight-matrix.git
cd flight-matrix
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — at minimum set DATABASE_URL. SQLite works out of the box:
#   DATABASE_URL=sqlite:///aircraft_data.db

# 3. Run the Flask web app (auth bypassed for local dev)
uv run python web_app.py --skip-auth
# Visit http://localhost:5050

# 4. (Optional) Run the scraper locally against one source
uv run python src/scraper_main.py --local --debug --scrapers fr24_flights
```

Full setup notes (including Cognito, AWS, and production SSH-tunnel access):
[docs/configuration.md](docs/configuration.md).

## Common commands

```bash
# Lint + format
uv run ruff check src/
uv run ruff format src/

# Type-check
uv run mypy src/

# Tests
uv run pytest tests/
uv run pytest tests/scraper/test_jetphotos.py -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## Deployment

Production uses AWS CDK in "import mode" against pre-existing VPC/Aurora/S3.
See [docs/deployment.md](docs/deployment.md).

```bash
./deploy.sh deploy    # First deploy or full update
./deploy.sh update    # Lambda + scraper + static files only
./deploy.sh status    # Show stack outputs
```

## Project layout

```
flight-matrix/
├── config/                  # Modular YAML configuration (includes pattern)
├── docs/                    # Architecture, configuration, deployment notes
├── infra/                   # CDK stacks
├── lib/resilient-scraper/   # Submodule: standalone scraper service
├── scripts/                 # One-off operational scripts
├── src/
│   ├── aircraft/            # Aircraft-domain helpers
│   ├── analysis/            # AI flight-analysis agent
│   ├── auth/                # Cognito JWT verification
│   ├── core/                # Shared base classes and exceptions
│   ├── data/                # Models and database access
│   ├── geo/                 # Airport distance / geo queries
│   ├── media/               # Image handling
│   ├── notifications/       # Email delivery
│   ├── reporting/           # Report generation + filter engines
│   ├── scraper/             # Distributed scraper framework + sources
│   ├── search/              # Tavily integration
│   ├── services/            # BaseService + domain services
│   └── utils/               # Cross-cutting utilities (being thinned out)
├── tests/                   # pytest suite
├── web_static/              # CSS / JS / images
├── web_templates/           # Jinja2 templates
├── web_app.py               # Flask entry
├── lambda_handler.py        # Mangum ASGI adapter for AWS Lambda
└── cdk_app.py               # CDK entry
```

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development
setup, code style, and the PR workflow.

By participating, you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE).
