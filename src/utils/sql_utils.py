"""
SQL utility functions.

This module provides common SQL processing functions used across the application.
"""

import re


def strip_sql_comments(sql: str) -> str:
    """Strip SQL comments from a query string.

    Removes:
    - Single line comments (-- comment)
    - Multi-line comments (/* comment */)
    - Trailing commas before closing parentheses

    Args:
        sql: SQL string potentially containing comments

    Returns:
        SQL string with comments removed
    """
    # Remove single-line comments (-- to end of line)
    sql = re.sub(r"--[^\n]*", "", sql)

    # Remove multi-line comments (/* ... */)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)

    # Remove trailing commas before closing parentheses
    # e.g., "'value1', 'value2',)" -> "'value1', 'value2')"
    sql = re.sub(r",\s*\)", ")", sql)

    # Clean up extra whitespace
    sql = re.sub(r"\n\s*\n", "\n", sql)

    return sql
