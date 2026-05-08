"""Health check runner and output formatting.

This module provides the main runner class that orchestrates all
health checks and formats output for the console.
"""

import asyncio
import logging
import sys
import time
from typing import Any

from tests.deployment_check.api_check import APIHealthCheck
from tests.deployment_check.aws_check import AWSHealthCheck
from tests.deployment_check.base import BaseHealthCheck, CheckResult, CheckStatus
from tests.deployment_check.component_check import ComponentHealthCheck
from tests.deployment_check.config_check import ConfigHealthCheck
from tests.deployment_check.database_check import DatabaseHealthCheck
from tests.deployment_check.endpoint_check import EndpointHealthCheck

logger = logging.getLogger("deployment_check.runner")

# ANSI color codes
COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "green": "\033[92m",
    "red": "\033[91m",
    "yellow": "\033[93m",
    "cyan": "\033[96m",
    "dim": "\033[2m",
}


def _colorize(text: str, color: str) -> str:
    """Apply ANSI color to text.

    Args:
        text: Text to colorize.
        color: Color name from COLORS dict.

    Returns:
        Colorized text string.
    """
    return f"{COLORS.get(color, '')}{text}{COLORS['reset']}"


def _status_color(status: CheckStatus) -> str:
    """Get color for a check status.

    Args:
        status: The check status.

    Returns:
        Color name for the status.
    """
    status_colors = {
        CheckStatus.PASS: "green",
        CheckStatus.FAIL: "red",
        CheckStatus.WARN: "yellow",
        CheckStatus.SKIP: "dim",
    }
    return status_colors.get(status, "reset")


class HealthCheckRunner:
    """Runner for executing and reporting health checks.

    This class coordinates the execution of all health check categories
    and produces formatted console output.

    Example:
        runner = HealthCheckRunner(
            config_file="config.yaml",
            verbose=True,
            skip_categories=["AWS Services"]
        )
        exit_code = asyncio.run(runner.run_all())
        sys.exit(exit_code)
    """

    # Ordered list of health check classes
    CHECK_CLASSES: list[type[BaseHealthCheck]] = [
        ConfigHealthCheck,
        DatabaseHealthCheck,
        AWSHealthCheck,
        APIHealthCheck,
        ComponentHealthCheck,
        EndpointHealthCheck,
    ]

    def __init__(
        self,
        config_file: str = "config.yaml",
        verbose: bool = False,
        skip_categories: list[str] | None = None,
    ) -> None:
        """Initialize the health check runner.

        Args:
            config_file: Path to the configuration file.
            verbose: Whether to show detailed output.
            skip_categories: List of category names to skip.
        """
        self.config_file = config_file
        self.verbose = verbose
        self.skip_categories = set(skip_categories or [])
        self.config: Any | None = None

    async def run_all(self) -> int:
        """Execute all health checks and print results.

        Returns:
            Exit code: 0 if all checks pass, 1 if any fail.
        """
        start_time = time.perf_counter()

        # Print header
        self._print_header()

        # Load config first (needed by most checks)
        self._load_config()

        # Collect all results
        all_results: list[tuple[str, list[CheckResult]]] = []
        total_passed = 0
        total_failed = 0
        total_warned = 0
        total_skipped = 0

        # Run each check category
        for check_class in self.CHECK_CLASSES:
            category = check_class.category

            # Skip if category is in skip list
            if category in self.skip_categories:
                print(f"\n{_colorize(category, 'bold')}")
                print(f"  {_colorize('[SKIP]', 'dim')} Category skipped by user")
                continue

            # Instantiate check
            if check_class == ConfigHealthCheck:
                check = ConfigHealthCheck(
                    config_file=self.config_file,
                    config=self.config,
                )
            else:
                check = check_class(config=self.config)

            # Run checks
            print(f"\n{_colorize(category, 'bold')}")
            results = await check.run()

            # Update config from ConfigHealthCheck if it was loaded there
            if isinstance(check, ConfigHealthCheck) and check.config:
                self.config = check.config

            # Print and count results
            category_passed = 0
            category_failed = 0
            category_warned = 0
            category_skipped = 0

            for result in results:
                self._print_result(result)

                if result.status == CheckStatus.PASS:
                    category_passed += 1
                    total_passed += 1
                elif result.status == CheckStatus.FAIL:
                    category_failed += 1
                    total_failed += 1
                elif result.status == CheckStatus.WARN:
                    category_warned += 1
                    total_warned += 1
                elif result.status == CheckStatus.SKIP:
                    category_skipped += 1
                    total_skipped += 1

            # Print category summary
            total_in_category = len(results)
            if total_in_category > 0:
                summary = f"  Result: {category_passed}/{total_in_category} passed"
                if category_failed > 0:
                    summary += f", {_colorize(f'{category_failed} failed', 'red')}"
                if category_warned > 0:
                    summary += f", {_colorize(f'{category_warned} warnings', 'yellow')}"
                print(summary)

            all_results.append((category, results))

        # Print summary
        duration = time.perf_counter() - start_time
        self._print_summary(total_passed, total_failed, total_warned, total_skipped, duration)

        # Return exit code
        return 0 if total_failed == 0 else 1

    def _load_config(self) -> None:
        """Load configuration file."""
        try:
            from src.utils.yaml_config import YAMLConfig

            self.config = YAMLConfig(self.config_file)
        except Exception as e:
            logger.warning(f"Could not pre-load config: {e}")

    def _print_header(self) -> None:
        """Print the health check header."""
        print("=" * 80)
        print(_colorize("FLIGHT MATRIX DEPLOYMENT HEALTH CHECK", "bold"))
        print("=" * 80)
        print(f"Config file: {self.config_file}")
        if self.skip_categories:
            print(f"Skipping: {', '.join(sorted(self.skip_categories))}")

    def _print_result(self, result: CheckResult) -> None:
        """Print a single check result.

        Args:
            result: The CheckResult to print.
        """
        status_str = f"[{result.status.value}]"
        colored_status = _colorize(status_str, _status_color(result.status))

        # Format duration
        if result.duration_ms < 1:
            duration_str = "<1ms"
        elif result.duration_ms < 1000:
            duration_str = f"{result.duration_ms:.0f}ms"
        else:
            duration_str = f"{result.duration_ms / 1000:.1f}s"

        # Truncate name if too long
        max_name_len = 40
        name = result.name
        if len(name) > max_name_len:
            name = name[: max_name_len - 3] + "..."

        # Print main line
        print(f"  {colored_status} {name:<{max_name_len}} ({duration_str})")

        # Print message for failures/warnings or in verbose mode
        if result.status in (CheckStatus.FAIL, CheckStatus.WARN):
            print(f"         {_colorize(result.message, _status_color(result.status))}")
        elif self.verbose and result.message:
            print(f"         {_colorize(result.message, 'dim')}")

        # Print details in verbose mode
        if self.verbose and result.details:
            for key, value in result.details.items():
                print(f"         {_colorize(f'{key}: {value}', 'dim')}")

    def _print_summary(
        self,
        passed: int,
        failed: int,
        warned: int,
        skipped: int,
        duration: float,
    ) -> None:
        """Print the final summary.

        Args:
            passed: Number of passed checks.
            failed: Number of failed checks.
            warned: Number of warnings.
            skipped: Number of skipped checks.
            duration: Total duration in seconds.
        """
        print()
        print("=" * 80)
        print(_colorize("SUMMARY", "bold"))
        print("=" * 80)

        total = passed + failed + warned + skipped

        # Build summary line
        parts = [f"Total: {total} checks"]
        parts.append(_colorize(f"Passed: {passed}", "green"))
        if failed > 0:
            parts.append(_colorize(f"Failed: {failed}", "red"))
        else:
            parts.append(f"Failed: {failed}")
        if warned > 0:
            parts.append(_colorize(f"Warnings: {warned}", "yellow"))
        else:
            parts.append(f"Warnings: {warned}")
        if skipped > 0:
            parts.append(f"Skipped: {skipped}")

        print(" | ".join(parts))
        print()
        print(f"Duration: {duration:.3f}s")

        if failed == 0:
            print(_colorize("Status: ALL CHECKS PASSED", "green"))
        else:
            print(_colorize("Status: CHECKS FAILED", "red"))

        print()
        print(f"Exit code: {1 if failed > 0 else 0}")


def main() -> int:
    """Main entry point for the health check runner.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Flight Matrix Deployment Health Check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m tests.deployment_check
  python -m tests.deployment_check --config /path/to/config.yaml
  python -m tests.deployment_check -v
  python -m tests.deployment_check --skip "AWS Services" --skip "External APIs"
""",
    )

    parser.add_argument(
        "--config",
        "-c",
        default="config/config.yaml",
        help="Path to configuration file (default: config/config.yaml)",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output including success messages",
    )

    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="CATEGORY",
        help="Skip a category of checks (can be used multiple times)",
    )

    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List available check categories and exit",
    )

    args = parser.parse_args()

    # List categories if requested
    if args.list_categories:
        print("Available check categories:")
        for check_class in HealthCheckRunner.CHECK_CLASSES:
            print(f"  - {check_class.category}")
        return 0

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Run health checks
    runner = HealthCheckRunner(
        config_file=args.config,
        verbose=args.verbose,
        skip_categories=args.skip,
    )

    return asyncio.run(runner.run_all())


if __name__ == "__main__":
    sys.exit(main())
