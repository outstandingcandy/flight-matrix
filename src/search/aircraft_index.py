"""The OpenSearch index over ``aircraft_static_info``.

One document per aircraft, keyed by registration, holding the fields an admin
would plausibly type into a search box. Queries return **registrations only**
(``_source: false``): PostgreSQL still owns the data and hydrates every row, so
the index can be rebuilt from scratch at any time and a stale document costs a
missing or extra match, never a wrong one.

Two shapes of match have to work at once:

* substring on identity fields — ``B-12`` finding ``B-1234``, which is what the
  previous ``LIKE '%…%'`` filter promised. An n-gram analyser provides it;
  ``multi_match`` alone would not.
* fuzzy word match on everything else — ``lufthanza`` finding the Lufthansa
  fleet.

Index settings (the analysers below) are static in OpenSearch: they can only be
set at creation time. :meth:`AircraftSearchIndex.ensure_index` therefore adds
new *fields* to a live index but cannot change analysis — that needs a rebuild
into a fresh index (``scripts/reindex_aircraft.py --recreate``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.core.exceptions import SearchError
from src.search.opensearch_client import DEFAULT_INDEX

logger = logging.getLogger("search.aircraft_index")

__all__ = [
    "AIRCRAFT_INDEX",
    "DOCUMENT_FIELDS",
    "INDEX_SETTINGS",
    "MAPPINGS",
    "SEARCH_FIELDS",
    "AircraftSearchIndex",
    "build_document",
]

AIRCRAFT_INDEX = DEFAULT_INDEX

# Identity fields, matched by substring. Registrations and hex codes are short
# and admins type fragments of them.
IDENTITY_FIELDS = ("registration", "hex_code")

# Free text: analysed, with a `.keyword` sub-field for exact filters.
TEXT_FIELDS = (
    "aircraft_type",
    "manufacturer",
    "model",
    "operator",
    "owner",
    "organization",
    "livery_name",
    "livery_type",
    "serial_number",
    "country",
    "country_of_registration",
    "ad_owner",
    "ad_location",
    "ps_airline",
    "jp_airline",
    "jp_cn",
)

# Exact-value fields. Not worth analysing, but useful to filter on.
KEYWORD_FIELDS = ("data_source", "attention_level", "ad_status", "ps_status")

BOOLEAN_FIELDS = ("is_military", "is_government", "is_vip", "images_downloaded")

INTEGER_FIELDS = ("year_built",)

# The AI report. Analysed but given no `.keyword` sub-field and no norms: it is
# prose, nobody filters or sorts on it, and `ignore_above` would silently drop
# most of it anyway.
LONG_TEXT_FIELDS = ("ai_analysis",)

# Drives the incremental reindex watermark; see `src.search.aircraft_sync`.
DATE_FIELDS = ("last_updated",)

DOCUMENT_FIELDS: tuple[str, ...] = (
    *IDENTITY_FIELDS,
    *TEXT_FIELDS,
    *KEYWORD_FIELDS,
    *BOOLEAN_FIELDS,
    *INTEGER_FIELDS,
    *LONG_TEXT_FIELDS,
    *DATE_FIELDS,
)

# Fields the free-text query searches, with boosts. Identity fields are handled
# by their own clauses in `_build_query` because they need the n-gram analyser
# rather than `multi_match`'s per-field default.
SEARCH_FIELDS: tuple[str, ...] = (
    "operator^3",
    "owner^3",
    "organization^3",
    "serial_number^3",
    "manufacturer^2",
    "model^2",
    "aircraft_type^2",
    "livery_name^2",
    "ad_owner^2",
    "ps_airline^2",
    "jp_airline^2",
    "jp_cn^2",
    "livery_type",
    "country",
    "country_of_registration",
    "ad_location",
    "ai_analysis",
)

_MIN_GRAM = 2
_MAX_GRAM = 20

INDEX_SETTINGS: dict[str, Any] = {
    "index": {
        "number_of_shards": 1,
        # Single node: an unassignable replica would leave the cluster yellow
        # forever and the health check flapping.
        "number_of_replicas": 0,
        "max_ngram_diff": _MAX_GRAM - _MIN_GRAM,
    },
    "analysis": {
        "tokenizer": {
            "identity_ngram": {
                "type": "ngram",
                "min_gram": _MIN_GRAM,
                "max_gram": _MAX_GRAM,
                "token_chars": ["letter", "digit"],
            }
        },
        "analyzer": {
            # Indexing splits `B-1234` into every 2..20 character run so that a
            # fragment matches.
            "identity_index": {
                "type": "custom",
                "tokenizer": "identity_ngram",
                "filter": ["lowercase"],
            },
            # Searching does not: the query *is* the fragment, and re-gramming
            # it would match on any two shared characters.
            "identity_search": {
                "type": "custom",
                "tokenizer": "keyword",
                "filter": ["lowercase"],
            },
        },
    },
}

_TEXT_WITH_KEYWORD: dict[str, Any] = {
    "type": "text",
    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
}

_IDENTITY: dict[str, Any] = {
    "type": "text",
    "analyzer": "identity_index",
    "search_analyzer": "identity_search",
    "fields": {"keyword": {"type": "keyword"}},
}

MAPPINGS: dict[str, Any] = {
    "properties": {
        **dict.fromkeys(IDENTITY_FIELDS, _IDENTITY),
        **dict.fromkeys(TEXT_FIELDS, _TEXT_WITH_KEYWORD),
        **{name: {"type": "keyword"} for name in KEYWORD_FIELDS},
        **{name: {"type": "boolean"} for name in BOOLEAN_FIELDS},
        **{name: {"type": "integer"} for name in INTEGER_FIELDS},
        **{name: {"type": "text", "norms": False} for name in LONG_TEXT_FIELDS},
        **{name: {"type": "date"} for name in DATE_FIELDS},
    }
}


def build_document(row: Mapping[str, Any]) -> dict[str, Any]:
    """Turn one ``aircraft_static_info`` row into an index document.

    Absent and empty values are dropped rather than indexed as ``null`` so that
    a column added later does not require every existing document to carry it.

    Args:
        row: Mapping of column names to values; extra columns are ignored.

    Returns:
        The document body. The registration is also its ``_id``.

    Raises:
        ValueError: If the row has no registration, which would leave the
            document unaddressable and un-updatable.
    """
    registration = str(row.get("registration") or "").strip()
    if not registration:
        raise ValueError("Cannot index an aircraft row without a registration")

    document: dict[str, Any] = {"registration": registration}
    for field in DOCUMENT_FIELDS:
        if field == "registration":
            continue
        value = row.get(field)
        if value is None:
            continue
        if field in BOOLEAN_FIELDS:
            document[field] = bool(value)
            continue
        if field in INTEGER_FIELDS:
            try:
                document[field] = int(value)
            except (TypeError, ValueError):
                logger.debug("Skipping non-integer %s=%r for %s", field, value, registration)
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                document[field] = stripped
            continue
        document[field] = value

    return document


class AircraftSearchIndex:
    """Read and write side of the aircraft index.

    Args:
        client: An ``opensearchpy.OpenSearch``-shaped client.
        index: Index name.
        max_results: Default cap on how many registrations a query returns.
    """

    def __init__(
        self,
        client: Any,
        index: str = AIRCRAFT_INDEX,
        max_results: int = 1000,
    ) -> None:
        self.client = client
        self.index = index
        self.max_results = max_results

    # -- schema ------------------------------------------------------------

    def ensure_index(self) -> bool:
        """Create the index, or add any newly declared fields to it.

        Adding fields to a live index is safe; changing the type of an existing
        one is rejected by OpenSearch, and changing the analysers is impossible
        without a rebuild. Both surface here as a :class:`SearchError` rather
        than silently leaving the index on an older shape.

        Returns:
            True if the index was created, False if it already existed.

        Raises:
            SearchError: If the cluster rejects the create or the mapping
                update, or cannot be reached.
        """
        try:
            if not self.client.indices.exists(index=self.index):
                self.client.indices.create(
                    index=self.index,
                    body={"settings": INDEX_SETTINGS, "mappings": MAPPINGS},
                )
                logger.info("Created OpenSearch index %s", self.index)
                return True

            self.client.indices.put_mapping(index=self.index, body=MAPPINGS)
            logger.debug("Index %s already exists; mapping is up to date", self.index)
            return False
        except SearchError:
            raise
        except Exception as e:
            raise SearchError(f"Failed to ensure index {self.index}: {e}") from e

    def refresh(self) -> None:
        """Make recently indexed documents visible to search.

        Raises:
            SearchError: If the refresh fails.
        """
        try:
            self.client.indices.refresh(index=self.index)
        except Exception as e:
            raise SearchError(f"Failed to refresh index {self.index}: {e}") from e

    # -- writes ------------------------------------------------------------

    def index_documents(self, documents: Iterable[Mapping[str, Any]], refresh: bool = False) -> int:
        """Index or overwrite documents in one ``_bulk`` request.

        Args:
            documents: Bodies produced by :func:`build_document`.
            refresh: Whether the cluster should refresh before responding. Left
                off during a reindex — one refresh at the end is far cheaper
                than one per batch.

        Returns:
            The number of documents sent.

        Raises:
            SearchError: If the request fails or any item in it is rejected.
        """
        body: list[Any] = []
        count = 0
        for document in documents:
            registration = document.get("registration")
            if not registration:
                raise ValueError("Cannot index a document without a registration")
            body.append({"index": {"_index": self.index, "_id": registration}})
            body.append(dict(document))
            count += 1

        if not count:
            return 0

        try:
            response = self.client.bulk(body=body, refresh=refresh)
        except Exception as e:
            raise SearchError(f"Bulk index into {self.index} failed: {e}") from e

        self._raise_on_bulk_errors(response)
        return count

    def delete_registrations(self, registrations: Sequence[str], refresh: bool = False) -> int:
        """Remove documents by registration.

        A registration that is not in the index is not an error: the caller is
        reconciling, and "already gone" is the desired state.

        Args:
            registrations: Registrations to delete.
            refresh: Whether the cluster should refresh before responding.

        Returns:
            The number of delete actions sent.

        Raises:
            SearchError: If the request fails or an item fails for a reason
                other than "not found".
        """
        body: list[Any] = []
        for registration in registrations:
            if registration:
                body.append({"delete": {"_index": self.index, "_id": registration}})

        if not body:
            return 0

        try:
            response = self.client.bulk(body=body, refresh=refresh)
        except Exception as e:
            raise SearchError(f"Bulk delete from {self.index} failed: {e}") from e

        self._raise_on_bulk_errors(response, ignore_status={404})
        return len(body)

    @staticmethod
    def _raise_on_bulk_errors(
        response: Mapping[str, Any], ignore_status: set[int] | None = None
    ) -> None:
        """Raise if any item in a bulk response failed.

        Args:
            response: The ``_bulk`` response body.
            ignore_status: HTTP statuses to treat as success.

        Raises:
            SearchError: With the first offending item's error attached.
        """
        if not response.get("errors"):
            return

        ignored = ignore_status or set()
        for item in response.get("items", []):
            for result in item.values():
                status = result.get("status", 0)
                if "error" in result and status not in ignored:
                    raise SearchError(
                        f"Bulk item {result.get('_id')} failed with {status}: {result['error']}"
                    )

    # -- reads -------------------------------------------------------------

    def search_registrations(self, query: str, limit: int | None = None) -> list[str]:
        """Return the registrations matching a free-text query, best first.

        Args:
            query: Whatever the admin typed. Empty returns no results rather
                than everything, matching the endpoint's "no filter" branch.
            limit: Cap on results, defaulting to ``max_results``.

        Returns:
            Registrations in descending relevance order.

        Raises:
            SearchError: If the cluster cannot be reached or rejects the query.
        """
        text = query.strip()
        if not text:
            return []

        size = min(limit or self.max_results, self.max_results)
        try:
            response = self.client.search(
                index=self.index,
                body={
                    "query": self._build_query(text),
                    # Only ids are needed: PostgreSQL holds the row the caller
                    # will render, and shipping `_source` would double the
                    # response for nothing.
                    "_source": False,
                    "size": size,
                    "track_total_hits": False,
                },
            )
        except Exception as e:
            raise SearchError(f"Search on {self.index} failed: {e}") from e

        hits = response.get("hits", {}).get("hits", [])
        return [hit["_id"] for hit in hits if hit.get("_id")]

    @staticmethod
    def _build_query(text: str) -> dict[str, Any]:
        """Build the query body for a free-text search.

        Args:
            text: Non-empty search text.

        Returns:
            An OpenSearch ``query`` clause.
        """
        return {
            "bool": {
                "should": [
                    # An exact registration outranks everything: typing a full
                    # registration means "show me this aircraft".
                    {"term": {"registration.keyword": {"value": text.upper(), "boost": 20}}},
                    {"term": {"hex_code.keyword": {"value": text.lower(), "boost": 18}}},
                    # Substring, via the n-gram analyser.
                    {"match": {"registration": {"query": text, "boost": 8}}},
                    {"match": {"hex_code": {"query": text, "boost": 6}}},
                    {
                        "multi_match": {
                            "query": text,
                            "type": "best_fields",
                            "fields": list(SEARCH_FIELDS),
                            # Every word has to appear somewhere; "air china
                            # cargo" should not return the whole of Air China.
                            "operator": "and",
                            "fuzziness": "AUTO",
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }

    def document_count(self) -> int:
        """Return how many documents the index holds.

        Returns:
            The document count, or 0 when the index does not exist yet.

        Raises:
            SearchError: If the count fails for any other reason.
        """
        try:
            if not self.client.indices.exists(index=self.index):
                return 0
            response = self.client.count(index=self.index)
        except Exception as e:
            raise SearchError(f"Count on {self.index} failed: {e}") from e
        return int(response.get("count", 0))

    def max_last_updated(self) -> datetime | None:
        """Return the newest ``last_updated`` in the index.

        This is the watermark an incremental reindex resumes from, kept in the
        index itself so there is no separate state file to lose or to disagree
        with the data.

        Returns:
            A naive UTC datetime (what every writer in this project stores), or
            ``None`` when the index is empty or absent.

        Raises:
            SearchError: If the aggregation fails.
        """
        try:
            if not self.client.indices.exists(index=self.index):
                return None
            response = self.client.search(
                index=self.index,
                body={
                    "size": 0,
                    "aggs": {"newest": {"max": {"field": "last_updated"}}},
                    "track_total_hits": False,
                },
            )
        except Exception as e:
            raise SearchError(f"Watermark aggregation on {self.index} failed: {e}") from e

        newest = response.get("aggregations", {}).get("newest", {})
        return _parse_watermark(newest)


def _parse_watermark(aggregation: Mapping[str, Any]) -> datetime | None:
    """Convert a ``max`` date aggregation into a naive UTC datetime.

    Args:
        aggregation: The aggregation result, with ``value`` in epoch
            milliseconds and optionally ``value_as_string``.

    Returns:
        The datetime, or ``None`` when the aggregation matched no documents.
    """
    raw = aggregation.get("value_as_string")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed

    millis = aggregation.get("value")
    if millis is None:
        return None
    return datetime.fromtimestamp(float(millis) / 1000.0, tz=UTC).replace(tzinfo=None)
