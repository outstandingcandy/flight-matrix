#!/bin/bash
# Sync source code from main project to lambda_code directory
# This ensures lambda_code always has the latest code without manual duplication

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LAMBDA_CODE_DIR="$PROJECT_ROOT/lambda_code"

echo "==============================================="
echo "Syncing source code to lambda_code directory"
echo "==============================================="
echo "Project root: $PROJECT_ROOT"
echo "Lambda code dir: $LAMBDA_CODE_DIR"
echo ""

# Create lambda_code directory if it doesn't exist
mkdir -p "$LAMBDA_CODE_DIR"

# Clean up files that should not be in lambda_code
echo "Cleaning up excluded files..."
rm -f "$LAMBDA_CODE_DIR/src/"*.db "$LAMBDA_CODE_DIR/src/"*.log 2>/dev/null || true

# Sync src directory (excluding __pycache__, .pyc files, databases, and logs)
echo "Syncing src/ directory..."
rsync -av --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.pyo' \
    --exclude '.pytest_cache' \
    --exclude '*.db' \
    --exclude '*.db-journal' \
    --exclude '*.log' \
    --exclude 'config.yaml' \
    --exclude 'data/maps/*' \
    --exclude 'data/aircraft_images/*' \
    "$PROJECT_ROOT/src/" "$LAMBDA_CODE_DIR/src/"

# Sync web_app.py
echo "Syncing web_app.py..."
cp "$PROJECT_ROOT/web_app.py" "$LAMBDA_CODE_DIR/web_app.py"

# Sync web_templates directory
echo "Syncing web_templates/ directory..."
rsync -av --delete \
    "$PROJECT_ROOT/web_templates/" "$LAMBDA_CODE_DIR/web_templates/"

# Sync web_static directory
echo "Syncing web_static/ directory..."
rsync -av --delete \
    "$PROJECT_ROOT/web_static/" "$LAMBDA_CODE_DIR/web_static/"

# Sync lambda_handler.py (from project root)
echo "Syncing lambda_handler.py..."
cp "$PROJECT_ROOT/lambda_handler.py" "$LAMBDA_CODE_DIR/lambda_handler.py"

# Sync config.yaml (always overwrite to keep Lambda config in sync)
if [ -f "$PROJECT_ROOT/config.yaml" ]; then
    echo "Syncing config.yaml..."
    cp "$PROJECT_ROOT/config.yaml" "$LAMBDA_CODE_DIR/config.yaml"
fi

echo ""
echo "==============================================="
echo "Sync completed successfully!"
echo "==============================================="
echo ""
echo "Synced:"
echo "  - src/"
echo "  - web_app.py"
echo "  - web_templates/"
echo "  - web_static/"
echo "  - lambda_handler.py"
echo ""
