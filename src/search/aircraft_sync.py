"""Keeping the aircraft index in step with ``aircraft_static_info``.

There is no single choke point through which aircraft rows are written: nine
raw-SQL statements across ``src/`` touch the table, one of them on the hot
ADS-B tracking path. Dual-writing from each of them would put a network call
into that path and leave nine places to forget.

So the index is synchronised instead of dual-written, from the one thing every
writer already maintains: ``last_updated``, which is indexed
(``idx_static_updated``). An incremental pass asks the index for its newest
document, subtracts a safety overlap, and re-indexes everything the database
has touched since. Re-indexing is idempotent — same ``_id``, same body — so an
overlap costs a little work and no correctness.

The overlap matters because the two clocks are not the same one and rows are
written inside transactions that commit after their timestamp is taken; without
it, a row committed moments after a sync read the table would never be picked up.

One indexed value is not a column: ``photographer_count`` is aggregated from
``aircraft_images`` because the list page sorts on it and the index cannot sort
on what it does not hold. Its freshness rides on the same watermark, which works
because ``jetphotos_sink`` bumps ``last_updated`` on the aircraft whenever it
stores new photos.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import bindparam, inspect, text

from src.search.aircraft_index import (
    DERIVED_FIELDS,
    DOCUMENT_FIELDS,
    AircraftSearchIndex,
    build_document,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger("search.aircraft_sync")

__all__ = [
    "DEFAULT_OVERLAP",
    "SyncStats",
    "incremental_start",
    "sync_aircraft_index",
]

# How far back an incremental pass reaches before its watermark. Generous on
# purpose: the cost is re-indexing a few minutes of rows, the cost of being
# wrong is a row that never gets indexed at all.
DEFAULT_OVERLAP = timedelta(minutes=15)

_TABLE = "aircraft_static_info"
_IMAGES_TABLE = "aircraft_images"


@dataclass(frozen=True)
class SyncStats:
    """Outcome of one sync pass.

    Attributes:
        indexed: Documents sent to OpenSearch.
        batches: ``_bulk`` requests issued.
        since: Watermark the pass started from, or ``None`` for a full pass.
    """

    indexed: int
    batches: int
    since: datetime | None


def incremental_start(
    index: AircraftSearchIndex, overlap: timedelta = DEFAULT_OVERLAP
) -> datetime | None:
    """Return the timestamp an incremental sync should start from.

    Args:
        index: The index to read the watermark from.
        overlap: Safety margin subtracted from the watermark.

    Returns:
        The start timestamp, or ``None`` when the index is empty — in which case
        the caller should run a full pass.
    """
    newest = index.max_last_updated()
    if newest is None:
        return None
    return newest - overlap


def _selectable_fields(engine: Engine) -> list[str]:
    """Return the document fields that this database actually has columns for.

    ``DOCUMENT_FIELDS`` is taken from the ORM model, and the deployed schema has
    drifted from it — production is missing four of the columns the model
    declares. Selecting a column that is not there fails the whole pass, which
    would mean no search at all rather than search without one field. Since the
    index is an accelerator, dropping the absent fields is the better trade.

    ``DERIVED_FIELDS`` are excluded: they are computed by the query rather than
    read from a column, so their absence here is expected and not worth a warning.

    Args:
        engine: Database engine to introspect.

    Returns:
        The subset of ``DOCUMENT_FIELDS`` present in ``aircraft_static_info``,
        in declaration order.

    Raises:
        ValueError: If the table has no ``registration`` column, which would
            leave every document without an id.
    """
    available = {column["name"] for column in inspect(engine).get_columns(_TABLE)}
    wanted = [field for field in DOCUMENT_FIELDS if field not in DERIVED_FIELDS]
    fields = [field for field in wanted if field in available]

    if "registration" not in available:
        raise ValueError(f"{_TABLE} has no registration column; nothing can be indexed")

    absent = [field for field in wanted if field not in available]
    if absent:
        # Not an error, but it silently narrows what an admin can search on, so
        # say it out loud on every pass rather than only on the first.
        logger.warning(
            "%s has no column for %s; those fields will not be searchable",
            _TABLE,
            ", ".join(absent),
        )
    return fields


def _photographer_count_sql(engine: Engine) -> tuple[str, str]:
    """Return the SELECT item and JOIN that count an aircraft's JetPhotos contributors.

    The list page can sort on this number, and sorting happens inside the index,
    so the value has to be denormalised into the document. It is aggregated once
    per pass and hash-joined rather than fetched per row — the same shape the
    endpoint used when it did this in SQL on every page load.

    Args:
        engine: Database engine to introspect.

    Returns:
        A ``(select_item, join_sql)`` pair, both empty strings when
        ``aircraft_images`` or the columns the count needs are absent. The field
        is then simply not indexed, and sorting by it falls back to SQL.
    """
    inspector = inspect(engine)
    if not inspector.has_table(_IMAGES_TABLE):
        logger.warning(
            "%s is absent; aircraft will be indexed without a photographer count", _IMAGES_TABLE
        )
        return "", ""

    columns = {column["name"] for column in inspector.get_columns(_IMAGES_TABLE)}
    if not {"registration", "photographer", "source"} <= columns:
        logger.warning(
            "%s lacks the columns a photographer count needs; indexing without it", _IMAGES_TABLE
        )
        return "", ""

    return (
        "COALESCE(pc.photographer_count, 0) AS photographer_count",
        f"""LEFT JOIN (
                SELECT registration, COUNT(DISTINCT photographer) AS photographer_count
                FROM {_IMAGES_TABLE}
                WHERE source = 'jetphotos'
                  AND photographer IS NOT NULL
                  AND photographer <> ''
                GROUP BY registration
            ) pc ON pc.registration = asi.registration""",
    )


def sync_aircraft_index(
    engine: Engine,
    index: AircraftSearchIndex,
    *,
    since: datetime | None = None,
    registrations: Sequence[str] | None = None,
    batch_size: int = 500,
) -> SyncStats:
    """Copy rows from ``aircraft_static_info`` into the index.

    Args:
        engine: Database engine to read from.
        index: Destination index. Must already exist; call
            :meth:`AircraftSearchIndex.ensure_index` first.
        since: Only index rows whose ``last_updated`` is at or after this.
            ``None`` indexes every row.
        registrations: Only index these registrations. Combines with ``since``
            as an ``AND``; normally used on its own to repair a few rows.
        batch_size: Rows per ``_bulk`` request.

    Returns:
        Counts for the pass.

    Raises:
        SearchError: If a bulk request or the final refresh fails.
        SQLAlchemyError: If the read fails.
        ValueError: If the table has no ``registration`` column.
    """
    # Every column is qualified with `asi.`: the photographer-count join below
    # also exposes a `registration` column, so a bare name would be ambiguous.
    select_items = [f"asi.{field}" for field in _selectable_fields(engine)]
    photographers, join_sql = _photographer_count_sql(engine)
    if photographers:
        select_items.append(photographers)

    clauses: list[str] = []
    params: dict[str, Any] = {}

    if since is not None:
        clauses.append("asi.last_updated >= :since")
        params["since"] = since
    if registrations is not None:
        if not registrations:
            return SyncStats(indexed=0, batches=0, since=since)
        clauses.append("asi.registration IN :registrations")
        params["registrations"] = list(registrations)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Ordered by primary key so that a pass which fails part-way through is
    # resumable by eye, and so tests see a deterministic batch split.
    statement = text(
        f"SELECT {', '.join(select_items)} FROM {_TABLE} asi {join_sql} {where_sql} ORDER BY asi.id"
    )
    if registrations is not None:
        statement = statement.bindparams(bindparam("registrations", expanding=True))

    indexed = 0
    batches = 0
    with engine.connect() as connection:
        # Streamed so that a full pass over a large fleet does not materialise
        # every row — including every AI report — in memory at once.
        result = connection.execution_options(stream_results=True).execute(statement, params)
        rows = result.mappings()
        while True:
            batch = rows.fetchmany(batch_size)
            if not batch:
                break
            # RowMapping is keyed by column name at runtime, but its declared
            # key type also admits Column objects, which Mapping's invariant
            # key parameter will not accept.
            documents = [build_document(cast("Mapping[str, Any]", row)) for row in batch]
            indexed += index.index_documents(documents)
            batches += 1
            logger.debug("Indexed batch %d (%d documents so far)", batches, indexed)

    if indexed:
        index.refresh()

    logger.info(
        "Aircraft index sync complete: %d documents in %d batches (since=%s)",
        indexed,
        batches,
        since.isoformat() if since else "beginning",
    )
    return SyncStats(indexed=indexed, batches=batches, since=since)
