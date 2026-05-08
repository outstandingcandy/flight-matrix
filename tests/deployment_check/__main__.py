"""Entry point for running deployment health checks as a module.

Usage:
    python -m tests.deployment_check [OPTIONS]

Options:
    --config, -c PATH    Path to configuration file (default: config/config.yaml)
    --verbose, -v        Show detailed output
    --skip CATEGORY      Skip a category of checks (can be repeated)
    --list-categories    List available check categories and exit
"""

import sys

from tests.deployment_check.runner import main

if __name__ == "__main__":
    sys.exit(main())
