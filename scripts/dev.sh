#!/bin/bash
#
# Local Development Server Startup Script
#
# Usage:
#   ./scripts/dev.sh          # Start with auth bypass (default)
#   ./scripts/dev.sh --auth   # Start with real Cognito authentication
#   ./scripts/dev.sh --livereload  # Start with browser auto-refresh
#
# Environment variables (can be overridden):
#   LOCAL_DEV_EMAIL   - Mock user email (default: dev@local.test)
#   LOCAL_DEV_GROUPS  - Mock user groups (default: admins,flight-schedules-viewers)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Default settings
SKIP_AUTH="true"
USE_LIVERELOAD="false"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --auth)
            SKIP_AUTH="false"
            shift
            ;;
        --livereload)
            USE_LIVERELOAD="true"
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --auth        Enable real Cognito authentication"
            echo "  --livereload  Enable browser auto-refresh on file changes"
            echo "  -h, --help    Show this help message"
            echo ""
            echo "Environment variables:"
            echo "  LOCAL_DEV_EMAIL   Mock user email (default: dev@local.test)"
            echo "  LOCAL_DEV_GROUPS  Mock user groups (default: admins,flight-schedules-viewers)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Load .env file if exists
if [[ -f "$PROJECT_DIR/.env" ]]; then
    echo "Loading environment from .env file..."
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

# Set local development environment variables
export STAGE="local"
export SKIP_AUTH="$SKIP_AUTH"
export FLASK_ENV="development"
export FLASK_DEBUG="1"

# Activate virtual environment if present
if [[ -d "$PROJECT_DIR/.venv" ]]; then
    echo "Activating virtual environment..."
    source "$PROJECT_DIR/.venv/bin/activate"
fi

# Display startup info
echo ""
echo "=========================================="
echo "  Flight Matrix - Local Development"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  STAGE:       $STAGE"
echo "  SKIP_AUTH:   $SKIP_AUTH"
if [[ "$SKIP_AUTH" == "true" ]]; then
    echo "  Mock Email:  ${LOCAL_DEV_EMAIL:-dev@local.test}"
    echo "  Mock Groups: ${LOCAL_DEV_GROUPS:-admins,flight-schedules-viewers}"
fi
echo "  Livereload:  $USE_LIVERELOAD"
echo ""
echo "Server URL: http://localhost:5000"
echo ""
echo "=========================================="
echo ""

if [[ "$USE_LIVERELOAD" == "true" ]]; then
    # Check if livereload is installed
    if ! python -c "import livereload" 2>/dev/null; then
        echo "Error: livereload is not installed."
        echo "Install it with: pip install livereload"
        exit 1
    fi

    # Run with livereload
    python -c "
from livereload import Server
from web_app import app

server = Server(app.wsgi_app)

# Watch templates and static files
server.watch('web_templates/')
server.watch('web_static/')

print('Starting livereload server...')
server.serve(port=5000, host='0.0.0.0', debug=True)
"
else
    # Run standard Flask development server
    python web_app.py
fi
