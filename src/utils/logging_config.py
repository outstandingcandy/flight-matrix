"""
Unified logging configuration.

This module provides a common logging setup function used across entry points.
"""

import logging
import os

LOG_LEVEL_MAPPING = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def setup_logging(
    config_file: str | None = None,
    log_file: str = "app.log",
    default_level: int = logging.INFO,
    log_level_key: str = "system.log_level",
    log_file_key: str = "system.log_file",
) -> None:
    """Setup logging configuration from config file or defaults.

    Args:
        config_file: Path to YAML config file (optional)
        log_file: Default log file name if not in config
        default_level: Default logging level if not in config
        log_level_key: Config key path for log level (e.g., "system.log_level")
        log_file_key: Config key path for log file (e.g., "system.track_log_file")
    """
    log_level = default_level
    final_log_file = log_file

    if config_file and os.path.exists(config_file):
        try:
            from src.utils.yaml_config import YAMLConfig

            config = YAMLConfig(config_file)

            # Handle both direct key access and nested access
            if "." in log_level_key:
                # Try direct get first
                log_level_str = config.get(log_level_key, "INFO")
            else:
                log_level_str = config.config.get(log_level_key, "INFO")

            if "." in log_file_key:
                final_log_file = config.get(log_file_key, log_file)
            else:
                final_log_file = config.config.get(log_file_key, log_file)

            if isinstance(log_level_str, str):
                log_level = LOG_LEVEL_MAPPING.get(log_level_str.upper(), default_level)
        except Exception:
            pass

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(final_log_file), logging.StreamHandler()],
    )
