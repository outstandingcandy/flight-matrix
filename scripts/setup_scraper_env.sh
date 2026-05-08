#!/bin/bash
#
# Setup script for scraper worker environment
#
# This script:
# 1. Installs Xvfb for headless browser support
# 2. Creates systemd service for Xvfb
# 3. Creates systemd service for scraper worker
# 4. Sets up log rotation
#
# Usage:
#   sudo ./scripts/setup_scraper_env.sh
#
# After running:
#   sudo systemctl start xvfb
#   sudo systemctl start scraper-worker
#

set -e

# Configuration
PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/Project/flight-matrix}"
DISPLAY_NUM="${DISPLAY_NUM:-55}"
PYTHON_PATH="${PYTHON_PATH:-/usr/bin/python}"
USER="${SCRAPER_USER:-ubuntu}"

echo "=== Flight Matrix Scraper Setup ==="
echo "Project directory: $PROJECT_DIR"
echo "Display number: $DISPLAY_NUM"
echo "User: $USER"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Install dependencies
echo "Installing dependencies..."
# Ignore apt-get update errors from third-party repos
apt-get update || true

# Install Xvfb and X11 utilities
apt-get install -y xvfb x11-utils wget gnupg || {
    echo "Failed to install base dependencies"
    exit 1
}

# Install Google Chrome (preferred) or fall back to Chromium
if ! command -v google-chrome &> /dev/null; then
    echo "Installing Google Chrome..."
    wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - 2>/dev/null || true
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
    apt-get update || true
    apt-get install -y google-chrome-stable || {
        echo "Google Chrome installation failed, trying Chromium..."
        apt-get install -y chromium-browser || apt-get install -y chromium || {
            echo "WARNING: No browser installed. Please install google-chrome or chromium manually."
        }
    }
else
    echo "Google Chrome already installed"
fi

# Verify browser installation
echo "Checking browser installation..."
if command -v google-chrome &> /dev/null; then
    echo "  ✓ google-chrome: $(google-chrome --version 2>/dev/null || echo 'installed')"
elif command -v chromium-browser &> /dev/null; then
    echo "  ✓ chromium-browser: $(chromium-browser --version 2>/dev/null || echo 'installed')"
elif command -v chromium &> /dev/null; then
    echo "  ✓ chromium: $(chromium --version 2>/dev/null || echo 'installed')"
else
    echo "  ✗ No browser found!"
fi

# Create Xvfb systemd service
echo "Creating Xvfb systemd service..."
cat > /etc/systemd/system/xvfb.service << EOF
[Unit]
Description=X Virtual Framebuffer
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :${DISPLAY_NUM} -screen 0 1920x1080x24
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Create scraper worker systemd service
echo "Creating scraper worker systemd service..."
cat > /etc/systemd/system/scraper-worker.service << EOF
[Unit]
Description=Flight Matrix Scraper Worker
After=network.target xvfb.service
Requires=xvfb.service

[Service]
Type=simple
User=${USER}
Group=${USER}
WorkingDirectory=${PROJECT_DIR}
Environment=DISPLAY=:${DISPLAY_NUM}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_PATH} -m src.scraper_main --config config/config.yaml
Restart=always
RestartSec=10
StandardOutput=append:/var/log/scraper-worker/worker.log
StandardError=append:/var/log/scraper-worker/worker.log

# Resource limits
LimitNOFILE=65536
MemoryMax=4G

[Install]
WantedBy=multi-user.target
EOF

# Create log directory
echo "Creating log directory..."
mkdir -p /var/log/scraper-worker
chown ${USER}:${USER} /var/log/scraper-worker

# Setup log rotation
echo "Setting up log rotation..."
cat > /etc/logrotate.d/scraper-worker << EOF
/var/log/scraper-worker/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0644 ${USER} ${USER}
    postrotate
        systemctl reload scraper-worker > /dev/null 2>&1 || true
    endscript
}
EOF

# Create images directory
echo "Creating images directory..."
mkdir -p ${PROJECT_DIR}/data/jetphotos_images
chown -R ${USER}:${USER} ${PROJECT_DIR}/data

# Reload systemd
echo "Reloading systemd..."
systemctl daemon-reload

# Enable services
echo "Enabling services..."
systemctl enable xvfb
systemctl enable scraper-worker

# Verify Python dependencies
echo "Checking Python dependencies..."
PYTHON_CHECK=$(sudo -u ${USER} ${PYTHON_PATH} -c "
import sys
missing = []
try:
    import DrissionPage
except ImportError:
    missing.append('DrissionPage')
try:
    import pydantic
except ImportError:
    missing.append('pydantic')
try:
    import sqlalchemy
except ImportError:
    missing.append('sqlalchemy')
try:
    import boto3
except ImportError:
    missing.append('boto3')
if missing:
    print('MISSING:' + ','.join(missing))
else:
    print('OK')
" 2>/dev/null)

if [[ "$PYTHON_CHECK" == MISSING:* ]]; then
    MISSING_DEPS="${PYTHON_CHECK#MISSING:}"
    echo "  ✗ Missing Python packages: $MISSING_DEPS"
    echo ""
    echo "Please install missing dependencies:"
    echo "  cd ${PROJECT_DIR} && pip install -r requirements.txt"
    echo ""
else
    echo "  ✓ All Python dependencies installed"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Commands:"
echo "  Start Xvfb:           sudo systemctl start xvfb"
echo "  Start worker:         sudo systemctl start scraper-worker"
echo "  View worker logs:     sudo journalctl -u scraper-worker -f"
echo "  View worker status:   sudo systemctl status scraper-worker"
echo ""
echo "Manual testing:"
echo "  DISPLAY=:${DISPLAY_NUM} python -m src.scraper_main --config config/config.yaml"
echo ""
echo "Don't forget to run database migration:"
echo "  python -m src.scraper_main --config config/config.yaml --migrate"
echo ""
