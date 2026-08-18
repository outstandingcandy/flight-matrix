"""SQL fragments that differ between PostgreSQL and SQLite.

Production runs on Aurora PostgreSQL; local development and the whole test
suite run on SQLite. Raw SQL written against one dialect and never executed on
the other is a defect that no amount of running the app locally will surface —
it only shows up as a 500 in whichever environment wasn't exercised.

Everything here takes an explicit ``is_postgres`` flag rather than sniffing a
connection, so the returned SQL is a pure function of its inputs and both
branches can be unit-tested without either database.

Constructs deliberately *not* wrapped, because they need no branch:

``NULLS LAST``
    Supported by SQLite since 3.30.
``COUNT(*) FILTER (WHERE ...)``
    Supported by SQLite since 3.30.
``CAST(x AS TEXT)``
    Standard SQL, understood by both. Use it instead of PostgreSQL's ``x::text``.

And one that must never be used: ``CAST(x AS DATE)`` parses on SQLite but
returns the *year* as an integer (``2026``) rather than a date. It is a silently
wrong answer, not an error. Use :func:`day_of`.
"""

from __future__ import annotations

__all__ = ["beijing_date", "day_of", "latest_rows", "minutes_ago", "minutes_from_now"]

# Asia/Shanghai has been a fixed UTC+8 with no daylight saving since 1991, so a
# constant offset is exact for every timestamp the application stores.
_BEIJING_OFFSET_HOURS = 8


def latest_rows(
    *,
    columns: str,
    source: str,
    partition_by: str,
    order_by: str,
    where: str = "",
    is_postgres: bool,
) -> str:
    """Build SQL selecting exactly one row per ``partition_by`` group.

    PostgreSQL gets ``DISTINCT ON``, which is what the flight-schedules query
    was tuned for: at the ~100k row scale it beats a window function because it
    can stop at the first row of each group while walking the index. Other
    dialects get ``ROW_NUMBER() OVER (PARTITION BY ...) = 1``, which is
    semantically identical — both return one row per group, the first under
    ``order_by``.

    Args:
        columns: Select list for the returned rows, without a trailing comma.
        source: Everything the rows come from — table name plus any joins.
        partition_by: Grouping expressions, comma-separated. Rows are unique
            per distinct combination of these.
        order_by: Tie-break expressions, comma-separated, that decide which row
            of each group wins. Must not repeat ``partition_by``; this function
            prepends it where the dialect requires it.
        where: Optional filter, without the ``WHERE`` keyword.
        is_postgres: True to emit the PostgreSQL form.

    Returns:
        A parenthesis-free ``SELECT`` statement, safe to embed in a CTE or a
        derived table.

        The non-PostgreSQL form carries one extra column, ``_dialect_rn``, which
        a wrapping ``SELECT *`` will expose. Harmless for callers that read
        columns by name; name them explicitly if the row is serialised whole.
    """
    where_clause = f"WHERE {where}" if where.strip() else ""

    if is_postgres:
        return f"""
            SELECT DISTINCT ON ({partition_by})
                {columns}
            FROM {source}
            {where_clause}
            ORDER BY {partition_by}, {order_by}
        """

    # ROW_NUMBER needs the winning row marked in a subquery, then filtered. The
    # select list is repeated so the outer query exposes the same columns as the
    # PostgreSQL branch and callers can swap between them freely.
    return f"""
        SELECT * FROM (
            SELECT
                {columns},
                ROW_NUMBER() OVER (
                    PARTITION BY {partition_by}
                    ORDER BY {order_by}
                ) AS _dialect_rn
            FROM {source}
            {where_clause}
        ) _ranked
        WHERE _dialect_rn = 1
    """


def day_of(expr: str, *, is_postgres: bool) -> str:
    """Truncate a timestamp expression to its calendar date.

    Args:
        expr: A SQL expression yielding a timestamp.
        is_postgres: True to emit the PostgreSQL form.

    Returns:
        A SQL expression yielding a date. On SQLite that is a ``YYYY-MM-DD``
        string, which compares and groups correctly against other dates
        produced the same way.
    """
    if is_postgres:
        return f"({expr})::date"
    return f"DATE({expr})"


def beijing_date(expr: str, *, is_postgres: bool) -> str:
    """Convert a UTC timestamp expression to the calendar date in Beijing.

    Timestamps are stored in UTC but the UI groups flights by local day, so the
    conversion has to happen in SQL for ``GROUP BY`` to produce the right
    buckets.

    Args:
        expr: A SQL expression yielding a UTC timestamp.
        is_postgres: True to emit the PostgreSQL form.

    Returns:
        A SQL expression yielding a ``YYYY-MM-DD`` string. PostgreSQL routes
        through the timezone database; SQLite uses a fixed +8 offset, which is
        exact because Asia/Shanghai has observed no daylight saving since 1991.
    """
    if is_postgres:
        return (
            f"TO_CHAR(DATE(({expr}) AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD')"
        )
    return f"DATE({expr}, '+{_BEIJING_OFFSET_HOURS} hours')"


def minutes_from_now(minutes: int, *, is_postgres: bool) -> str:
    """Build an expression for a timestamp offset from now.

    Args:
        minutes: Offset in minutes; negative goes into the past. Interpolated
            into the SQL text, so callers must pass an ``int`` — never a value
            straight off a request.
        is_postgres: True to emit the PostgreSQL form.

    Returns:
        A SQL expression yielding a timestamp, comparable against a stored
        timestamp column on either dialect.
    """
    count = int(minutes)
    operator = "-" if count < 0 else "+"
    magnitude = abs(count)
    if is_postgres:
        return f"NOW() {operator} INTERVAL '{magnitude} minutes'"
    return f"datetime('now', '{operator}{magnitude} minutes')"


def minutes_ago(minutes: int, *, is_postgres: bool) -> str:
    """Build an expression for a timestamp N minutes before now.

    Args:
        minutes: How far back to go, as a positive number of minutes. Same
            interpolation caveat as :func:`minutes_from_now`.
        is_postgres: True to emit the PostgreSQL form.

    Returns:
        A SQL expression yielding a timestamp, comparable against a stored
        timestamp column on either dialect.
    """
    return minutes_from_now(-int(minutes), is_postgres=is_postgres)
