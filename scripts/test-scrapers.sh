#!/usr/bin/env bash
#
# Live smoke tests for every non-submodule scraper, using the PRODUCTION
# config (non-headless DrissionPage under an Xvfb virtual display).
#
# This script is a thin wrapper over `pytest -m integration`. The actual
# assertions live in tests/scraper/test_live_scrapers.py, so pytest
# output and pytest assertions are the source of truth. The script's job
# is to (a) start Xvfb if needed, (b) set DISPLAY, (c) invoke pytest
# with the integration marker enabled.
#
# Why this script exists: it's really easy to write a one-off Python
# snippet that forces `headless=True`, get Cloudflare blocks, and
# mis-conclude the scraper is broken. Running the official integration
# tests avoids that mistake.
#
# Usage:
#   ./scripts/test-scrapers.sh                # run every live scraper test
#   ./scripts/test-scrapers.sh airport        # just airport-data.com
#   ./scripts/test-scrapers.sh jetphotos      # just JetPhotos
#   ./scripts/test-scrapers.sh fr24           # every FR24 class
#   ./scripts/test-scrapers.sh TestFR24Map    # any pytest -k pattern

set -euo pipefail

if [[ -t 1 ]]; then
    GREEN=$'\033[32m' RED=$'\033[31m' BLUE=$'\033[34m' BOLD=$'\033[1m' RESET=$'\033[0m'
else
    GREEN='' RED='' BLUE='' BOLD='' RESET=''
fi

step() { printf "${BLUE}==>${RESET} ${BOLD}%s${RESET}\n" "$*"; }
ok()   { printf "    ${GREEN}✓${RESET} %s\n" "$*"; }
die()  { printf "    ${RED}✗${RESET} %s\n" "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Map the friendly args to pytest -k patterns.
case "${1:-all}" in
    all|"") K_PATTERN="" ;;
    airport) K_PATTERN="TestAirportDataScraper" ;;
    jetphotos) K_PATTERN="TestJetPhotosScraper" ;;
    fr24) K_PATTERN="FR24" ;;
    *) K_PATTERN="$1" ;;
esac

# Prereqs.
step "Checking prerequisites"
command -v Xvfb >/dev/null 2>&1 || die "Xvfb not found (apt install xvfb)"
command -v chromium-browser >/dev/null 2>&1 || command -v google-chrome >/dev/null 2>&1 \
    || die "no chromium/chrome browser found"
[[ -x .venv/bin/python3 ]] || die "run ./scripts/quickstart.sh first (no .venv)"
ok "prereqs ok"

# Xvfb.
DISPLAY_NUM=55
if ! [[ -e /tmp/.X11-unix/X${DISPLAY_NUM} ]]; then
    step "Starting Xvfb :${DISPLAY_NUM}"
    Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 > /tmp/xvfb.log 2>&1 &
    sleep 2
    ok "Xvfb started (PID $!)"
else
    ok "Xvfb :${DISPLAY_NUM} already running"
fi
export DISPLAY=":${DISPLAY_NUM}"

# Hand off to pytest.
step "Running live scraper tests"
pytest_args=(
    "tests/scraper/test_live_scrapers.py"
    -m integration
    -v
)
if [[ -n $K_PATTERN ]]; then
    pytest_args+=(-k "$K_PATTERN")
fi

exec .venv/bin/python3 -m pytest "${pytest_args[@]}"
