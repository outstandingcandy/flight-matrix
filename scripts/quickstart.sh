#!/usr/bin/env bash
#
# Flight Matrix — one-command local setup.
#
# What it does:
#   1. Verify Python 3.11+ and uv are installed.
#   2. Create a .venv and install the project (+ dev extras).
#   3. Seed .env from .env.example if missing, generate a FLASK_SECRET_KEY.
#   4. Initialise a local SQLite database.
#   5. Run the test suite as a smoke check.
#   6. Optionally start the web app with auth bypassed.
#
# Idempotent — safe to run repeatedly.
#
# Usage:
#   ./scripts/quickstart.sh          # interactive
#   ./scripts/quickstart.sh --yes    # accept all defaults, don't start server
#   ./scripts/quickstart.sh --start  # also launch web_app.py at the end
#   ./scripts/quickstart.sh --help

set -euo pipefail

# --- Pretty output ----------------------------------------------------------
if [[ -t 1 ]]; then
    RED=$'\033[31m' GREEN=$'\033[32m' YELLOW=$'\033[33m' BLUE=$'\033[34m'
    BOLD=$'\033[1m' RESET=$'\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' BOLD='' RESET=''
fi

step()    { printf "${BLUE}==>${RESET} ${BOLD}%s${RESET}\n" "$*"; }
ok()      { printf "    ${GREEN}✓${RESET} %s\n" "$*"; }
warn()    { printf "    ${YELLOW}!${RESET} %s\n" "$*"; }
fail()    { printf "    ${RED}✗${RESET} %s\n" "$*" >&2; }
die()     { fail "$*"; exit 1; }

# --- Arg parsing ------------------------------------------------------------
AUTO_YES=0
START_SERVER=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)    AUTO_YES=1 ;;
        --start)     START_SERVER=1 ;;
        -h|--help)
            sed -n 's/^# //;s/^#//;/^Usage/q;p' "$0" | head -30
            exit 0
            ;;
        *) die "unknown flag: $1 (use --help)" ;;
    esac
    shift
done

# --- Locate project root ----------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"
ok "project root: $PROJECT_ROOT"

# --- 1. Prerequisites -------------------------------------------------------
step "Checking prerequisites"

if ! command -v python3 >/dev/null 2>&1; then
    die "python3 not found. Install Python 3.11 or newer."
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
PY_MAJOR=${PY_VER%%.*}
PY_MINOR=${PY_VER##*.}
if (( PY_MAJOR < 3 )) || (( PY_MAJOR == 3 && PY_MINOR < 11 )); then
    die "python3 is $PY_VER — need 3.11+."
fi
ok "python3 ($PY_VER)"

if ! command -v uv >/dev/null 2>&1; then
    warn "uv not found."
    cat <<'EOF'
    uv is the project's package manager. Install it once:
        curl -LsSf https://astral.sh/uv/install.sh | sh
    Then rerun this script.
EOF
    exit 1
fi
ok "uv ($(uv --version | awk '{print $2}'))"

# --- 2. Virtual env + deps -------------------------------------------------
step "Setting up virtual environment"
if [[ ! -d .venv ]]; then
    uv venv --python 3.11 .venv
    ok "created .venv"
else
    ok ".venv already exists"
fi

step "Installing project + dev dependencies"
# pyproject.toml's [project.optional-dependencies].dev gives us pytest, ruff, mypy, coverage.
if ! uv pip install --quiet -e '.[dev]'; then
    die "uv pip install failed"
fi
ok "dependencies installed"

# Install the resilient-scraper submodule if it's been checked out.
# Scrapers for Planespotters + Xiaohongshu live there. The main scraper
# worker lazily imports them, so this is optional — but if the submodule
# is present, installing it means `./scripts/start-all.sh` with those
# scrapers just works.
if [[ -f lib/resilient-scraper/pyproject.toml ]]; then
    step "Installing resilient-scraper submodule"
    if uv pip install --quiet -e ./lib/resilient-scraper; then
        ok "resilient-scraper installed"
    else
        warn "resilient-scraper install failed — Planespotters/Xiaohongshu scrapers"
        warn "will fail at runtime; everything else still works."
    fi
else
    warn "lib/resilient-scraper not found — skipping. Clone with:"
    warn "  git submodule update --init --recursive"
fi

# --- 3. .env bootstrap ------------------------------------------------------
step "Configuring .env"
if [[ -f .env ]]; then
    ok ".env already exists — not touched"
else
    cp .env.example .env
    # Generate a random Flask secret.
    SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    # sed -i portability: GNU vs BSD.
    if sed --version >/dev/null 2>&1; then
        sed -i "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$SECRET|" .env
    else
        sed -i '' "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$SECRET|" .env
    fi
    ok "created .env with a fresh FLASK_SECRET_KEY"
fi

# Make sure SKIP_AUTH and a SQLite DATABASE_URL are set for local dev.
ensure_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" .env; then
        return
    fi
    printf "%s=%s\n" "$key" "$value" >> .env
    ok "appended $key to .env"
}
ensure_env STAGE local
ensure_env SKIP_AUTH true
ensure_env DATABASE_URL "sqlite:///aircraft_data.db"
ensure_env LOCAL_DEV_EMAIL "dev@example.com"
ensure_env LOCAL_DEV_GROUPS "admins,flight-schedules-viewers"

# --- 4. Database ------------------------------------------------------------
step "Initialising local SQLite database"
# DatabaseManager() bootstraps the schema in __init__; just instantiating it
# is enough to create aircraft_data.db with all tables.
uv run python3 -c "
from src.data.db_manager import DatabaseManager
dm = DatabaseManager('sqlite:///aircraft_data.db')
dm.ensure_multi_user_tables_exist()
print('  tables ok, path:', dm.database_url)
dm.close()
"
ok "SQLite schema ready"

# --- 5. Smoke tests ---------------------------------------------------------
step "Running test suite (smoke check)"
if uv run pytest tests/core tests/data tests/web -q 2>&1 | tail -10; then
    ok "tests passed"
else
    warn "some tests failed — not fatal for quickstart, but worth investigating"
fi

# --- 6. Optional: launch the web app ---------------------------------------
if (( START_SERVER )); then
    step "Starting Flask dev server (Ctrl-C to stop)"
    echo "    visit: http://localhost:5000"
    exec uv run python web_app.py --skip-auth
fi

if (( ! AUTO_YES )); then
    echo
    printf "${BOLD}Setup complete.${RESET} Next:\n"
    echo "    source .venv/bin/activate"
    echo "    uv run python web_app.py --skip-auth"
    echo "    # then open http://localhost:5000"
fi
