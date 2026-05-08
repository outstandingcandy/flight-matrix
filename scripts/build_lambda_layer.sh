#!/bin/bash
# Build Lambda layers for flight-matrix deployment
# Creates two layers:
#   1. Dependencies layer (Python packages)
#   2. Config layer (config.yaml)

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==============================================="
echo "Building Lambda Layers for Flight-Matrix"
echo "==============================================="

# First, sync source code to lambda_code directory
echo "Step 1: Syncing source code..."
"$SCRIPT_DIR/sync_lambda_code.sh"
echo ""

# Clean up old layers
echo "Cleaning up old layer directories..."
rm -rf layers/dependencies layers/config

# Create layer directory structures
echo "Creating layer directory structure..."
mkdir -p layers/dependencies/python
mkdir -p layers/config

# Build dependencies layer
echo ""
echo "Building dependencies layer..."
echo "Installing packages from lambda_code/requirements.txt..."
pip install -r lambda_code/requirements.txt \
    -t layers/dependencies/python/ \
    --platform manylinux2014_x86_64 \
    --only-binary=:all: \
    --upgrade

# Remove unnecessary files to reduce layer size
echo "Optimizing layer size..."
cd layers/dependencies/python

# Remove __pycache__ directories
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Remove .pyc files
find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Remove test directories
find . -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
find . -type d -name "test" -exec rm -rf {} + 2>/dev/null || true

# Strip debug symbols from .so files (optional, saves space)
# find . -name "*.so" -exec strip {} \; 2>/dev/null || true

cd ../../../

# Build config layer
echo ""
echo "Building config layer..."
if [ -f "config.yaml" ]; then
    cp config.yaml layers/config/
    echo "Copied config.yaml to config layer"
else
    echo "WARNING: config.yaml not found - using config/config_template.yaml"
    if [ -f "config/config_template.yaml" ]; then
        cp config/config_template.yaml layers/config/config.yaml
    else
        echo "ERROR: Neither config.yaml nor config/config_template.yaml found!"
        exit 1
    fi
fi

# Display layer sizes
echo ""
echo "==============================================="
echo "Lambda Layers Built Successfully!"
echo "==============================================="
echo "Dependencies layer size: $(du -sh layers/dependencies | cut -f1)"
echo "Config layer size: $(du -sh layers/config | cut -f1)"
echo ""
echo "Total uncompressed size: $(du -sh layers | cut -f1)"
echo ""
echo "Note: Lambda has a 250MB uncompressed limit per deployment package"
echo "==============================================="

# Check if total size exceeds limits
TOTAL_SIZE=$(du -s layers | cut -f1)
MAX_SIZE=$((250 * 1024))  # 250MB in KB

if [ $TOTAL_SIZE -gt $MAX_SIZE ]; then
    echo "WARNING: Total layer size ($((TOTAL_SIZE / 1024))MB) exceeds 250MB!"
    echo "Consider removing optional dependencies or splitting into more layers"
else
    echo "✓ Layer size is within Lambda limits"
fi

echo ""
echo "Next steps:"
echo "  1. Run 'sam build' to prepare for deployment"
echo "  2. Run 'sam deploy' to deploy to AWS"
