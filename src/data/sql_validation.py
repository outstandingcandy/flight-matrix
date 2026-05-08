"""Shared SQL validation helpers for user-provided WHERE clauses.

Both SQLFilterEngine (config-driven, admin filters) and FilterService
(per-user filters) need to reject dangerous SQL fragments before handing
the WHERE clause to the database. This module owns that policy so the
two layers cannot drift.
"""

from __future__ import annotations

import re

# Dangerous keywords and sequences that should never appear in a user- or
# config-provided WHERE clause. `\b` word-boundary matching avoids false
# positives like "Executive" matching "EXEC".
DANGEROUS_PATTERNS: list[str] = [
    r"\bDROP\b",
    r"\bDELETE\b",
    r"\bTRUNCATE\b",
    r"\bINSERT\b",
    r"\bUPDATE\b",
    r"\bALTER\b",
    r"\bCREATE\b",
    r"\bEXEC\b",
    r"\bEXECUTE\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
    r"--",  # SQL comment
    r";",  # Statement separator
    r"\bUNION\b.*\bSELECT\b",
]


class DangerousSQLError(ValueError):
    """Raised when a WHERE clause contains a prohibited pattern."""


def check_dangerous_patterns(sql: str) -> str | None:
    """Return the first matched dangerous pattern, or None if clean.

    Pure function — no logging. Callers decide whether to log/warn.
    """
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, sql, re.IGNORECASE):
            return pattern
    return None


def assert_safe_where_clause(sql: str) -> str:
    """Raise DangerousSQLError if `sql` matches any dangerous pattern.

    Returns the input unchanged on success, so call sites can do:

        where = assert_safe_where_clause(user_input)
    """
    hit = check_dangerous_patterns(sql)
    if hit:
        raise DangerousSQLError(f"SQL contains prohibited pattern: {hit}")
    return sql
