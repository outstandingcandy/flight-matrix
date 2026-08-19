"""The OpenSearch index over ``aircraft_static_info``.

One document per aircraft, keyed by registration, holding the fields an admin
would plausibly type into a search box. Queries return **registrations only**
(``_source: false``): PostgreSQL still owns the data and hydrates every row, so
the index can be rebuilt from scratch at any time and a stale document costs a
missing or extra match, never a wrong one.

The index answers the admin list page whole — text, the type / livery / category
filters, the sort, the page window, the totals and the filter dropdowns — not
just the search box (:meth:`AircraftSearchIndex.query_page`,
:meth:`~AircraftSearchIndex.field_counts`,
:meth:`~AircraftSearchIndex.summary_counts`). Two consequences follow:

* **Ordering must be total.** Thousands of rows share one ``last_updated``, and
  a tie under ``from``/``size`` lets a row appear on two pages or on none, so
  ``registration`` always breaks it.
* **Freshness is now visible on the first page load,** not only in search
  results. A row the incremental sync has not reached yet is ordered by its
  older ``last_updated``, and one it has never reached is absent from the list
  entirely. Nothing in this project deletes from ``aircraft_static_info``, so
  the opposite drift — a document with no row — only arises from manual
  deletion; the caller hydrates from PostgreSQL and simply skips those.

Two shapes of match have to work at once:

* substring on identity fields — ``B-12`` finding ``B-1234``, which is what the
  previous ``LIKE '%…%'`` filter promised. An n-gram analyser provides it;
  ``multi_match`` alone would not.
* whole-word match on everything else — ``pan am`` finding an aircraft nothing
  in its registration or hex code identifies as one.

Index settings (the analysers below) are static in OpenSearch: they can only be
set at creation time. :meth:`AircraftSearchIndex.ensure_index` therefore adds
new *fields* to a live index but cannot change analysis — that needs a rebuild
into a fresh index (``scripts/reindex_aircraft.py --recreate``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.core.exceptions import SearchError
from src.search.opensearch_client import DEFAULT_INDEX

logger = logging.getLogger("search.aircraft_index")

__all__ = [
    "AIRCRAFT_INDEX",
    "DERIVED_FIELDS",
    "DOCUMENT_FIELDS",
    "INDEX_SETTINGS",
    "MAPPINGS",
    "MAX_WINDOW",
    "PROSE_FIELD",
    "SEARCH_FIELDS",
    "SORT_FIELDS",
    "AircraftPage",
    "AircraftSearchIndex",
    "build_document",
]

AIRCRAFT_INDEX = DEFAULT_INDEX

# Identity fields, matched by substring. Registrations and hex codes are short
# and admins type fragments of them. They are mapped individually below: a
# registration's separator is optional, a hex code has none.
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

INTEGER_FIELDS = ("year_built", "photographer_count")

# Fields no ``aircraft_static_info`` column supplies. ``photographer_count`` is
# aggregated from ``aircraft_images`` by :mod:`src.search.aircraft_sync`, because
# the list page sorts on it and the index cannot sort on a value it does not
# hold. Listed separately so the sync does not look for a column of that name.
DERIVED_FIELDS = ("photographer_count",)

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
)

# The AI report is searched as a *phrase*, not as a bag of words, so it gets its
# own clause. Loose word matching over paragraphs of prose is where a short
# identity query goes wrong: on the real fleet `B-12` matched 239 reports that
# happened to contain a `B` and a `12`, against 1 that contained the phrase —
# while the queries this field exists to serve lose nothing ("special livery"
# 3 either way, "a350" 252 either way).
PROSE_FIELD = "ai_analysis"

# Sort keys the list page offers -> the field that implements each. The keys are
# the endpoint's own (`sort=photographers`), so an unknown value can never reach
# a field name.
SORT_FIELDS: dict[str, str] = {
    "last_updated": "last_updated",
    "photographers": "photographer_count",
    "registration": "registration.keyword",
}

_DEFAULT_SORT = "last_updated"

# Every sort ends here, so that rows tied on the requested field still have one
# definite order and `from`/`size` cannot show a row twice or skip it.
_TIEBREAK = "registration.keyword"

# `index.max_result_window`, the cluster's own default. `from + size` past this
# is rejected outright, so pages that deep have to be served by SQL — which
# orders by the same two fields, though its tie-breaking collation is the
# database's rather than bytewise, so the two are not identical at a boundary.
MAX_WINDOW = 10_000

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
        "char_filter": {
            # `B-1234` and `B1234` are the same aircraft, and an admin types
            # either. Folding the separator away at both index and search time
            # is also what lets the whole value be one token, which the n-gram
            # filter below then slices — an n-gram *tokenizer* would treat the
            # hyphen as a boundary and throw the leading `B` away for being
            # shorter than min_gram.
            "identity_strip": {
                "type": "pattern_replace",
                "pattern": "[^A-Za-z0-9]",
                "replacement": "",
            }
        },
        "filter": {
            "identity_ngram": {"type": "ngram", "min_gram": _MIN_GRAM, "max_gram": _MAX_GRAM}
        },
        "analyzer": {
            # Indexing slices `B-1234` into every 2..20 character substring of
            # `b1234`, so any fragment an admin types can match.
            "identity_index": {
                "type": "custom",
                "char_filter": ["identity_strip"],
                "tokenizer": "keyword",
                "filter": ["lowercase", "identity_ngram"],
            },
            # Searching does not slice: the query *is* the fragment. Re-gramming
            # it would make `B-12` match on any two shared characters.
            "identity_search": {
                "type": "custom",
                "char_filter": ["identity_strip"],
                "tokenizer": "keyword",
                "filter": ["lowercase"],
            },
            # Hex codes are the one identity field with no separators, so the
            # hyphen in a query is information: `B-12` is a registration, and
            # folding it away would match it against the 261 aircraft whose hex
            # merely contains `b12`. The SQL filter this replaces matched none of
            # them, for the same reason.
            "hex_search": {
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

# Same n-grams, but a query is compared to them verbatim; see `hex_search`.
_HEX: dict[str, Any] = {**_IDENTITY, "search_analyzer": "hex_search"}

MAPPINGS: dict[str, Any] = {
    "properties": {
        "registration": _IDENTITY,
        "hex_code": _HEX,
        **dict.fromkeys(TEXT_FIELDS, _TEXT_WITH_KEYWORD),
        **{name: {"type": "keyword"} for name in KEYWORD_FIELDS},
        **{name: {"type": "boolean"} for name in BOOLEAN_FIELDS},
        **{name: {"type": "integer"} for name in INTEGER_FIELDS},
        **{name: {"type": "text", "norms": False} for name in LONG_TEXT_FIELDS},
        **{name: {"type": "date"} for name in DATE_FIELDS},
    }
}


@dataclass(frozen=True)
class AircraftPage:
    """One page of the admin aircraft list, as the index sees it.

    Attributes:
        registrations: Registrations in the order they should be rendered.
        total: How many aircraft match the filters in total, across all pages.
    """

    registrations: list[str]
    total: int


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
                            # Deliberately *not* fuzzy. The caller re-sorts these
                            # registrations by its own column and pages through
                            # them in SQL, so relevance order is discarded and a
                            # loose match is not a low-ranked row — it is an
                            # equal one. Measured on the real fleet, `AUTO`
                            # turned a 40-row answer for `B-12` into 241 rows of
                            # aircraft whose AI report merely contained a `12`.
                        }
                    },
                    {"match_phrase": {PROSE_FIELD: {"query": text}}},
                ],
                "minimum_should_match": 1,
            }
        }

    def query_page(
        self,
        *,
        text: str = "",
        aircraft_type: str = "",
        livery: str = "",
        attention_levels: Sequence[str] = (),
        sort: str = _DEFAULT_SORT,
        order: str = "desc",
        offset: int = 0,
        limit: int = 20,
    ) -> AircraftPage:
        """Answer the admin list query: filter, sort, page and count in one request.

        Unlike :meth:`search_registrations` this is the whole query, including the
        case where the admin typed nothing at all — the first load of the page is
        a ``match_all`` sorted by ``last_updated``.

        Args:
            text: Free-text search, empty for "no text filter".
            aircraft_type: Exact ``aircraft_type`` to keep, empty for all.
            livery: Exact ``livery_name`` to keep, empty for all.
            attention_levels: ``attention_level`` values to keep; empty for all.
                This is how the page's ``category=special`` filter is expressed.
            sort: A key of :data:`SORT_FIELDS`; anything else sorts by
                ``last_updated`` rather than failing, so a stale bookmark still
                renders a list.
            order: ``asc`` or ``desc``; anything else is ``desc``.
            offset: Documents to skip. ``offset + limit`` must not exceed
                :data:`MAX_WINDOW`.
            limit: Page size.

        Returns:
            The registrations for this page and the total match count.

        Raises:
            SearchError: If the cluster cannot be reached or rejects the query,
                which includes a window deeper than :data:`MAX_WINDOW`.
        """
        query = self._build_list_query(
            text=text.strip(),
            aircraft_type=aircraft_type.strip(),
            livery=livery.strip(),
            attention_levels=[level for level in attention_levels if level],
        )
        field = SORT_FIELDS.get(sort, SORT_FIELDS[_DEFAULT_SORT])
        direction = "asc" if order == "asc" else "desc"
        # `missing: _last` is the `NULLS LAST` the SQL path uses, so a page looks
        # the same whichever backend answered it.
        sort_clauses: list[Any] = [{field: {"order": direction, "missing": "_last"}}]
        if field != _TIEBREAK:
            sort_clauses.append({_TIEBREAK: {"order": "asc"}})

        try:
            response = self.client.search(
                index=self.index,
                body={
                    "query": query,
                    "_source": False,
                    "from": max(offset, 0),
                    "size": max(limit, 0),
                    "sort": sort_clauses,
                    # The page count needs the real number of matches; the
                    # default stops counting at 10,000 and reports a lower bound.
                    "track_total_hits": True,
                },
            )
        except Exception as e:
            raise SearchError(f"List query on {self.index} failed: {e}") from e

        hits = response.get("hits", {})
        return AircraftPage(
            registrations=[hit["_id"] for hit in hits.get("hits", []) if hit.get("_id")],
            total=_total_hits(hits),
        )

    def field_counts(
        self, field: str, *, contains: str = "", limit: int = 200
    ) -> list[tuple[str, int]]:
        """Return a field's most common values and how many aircraft have each.

        This is what fills the type and livery dropdowns, which the page loads
        before the admin has typed anything.

        Args:
            field: A field from :data:`TEXT_FIELDS`. Its ``.keyword`` sub-field
                is aggregated, so ``Air China Cargo`` counts as one value rather
                than three words.
            contains: Case-insensitive substring the value must contain, empty
                for all values.
            limit: Most values to return.

        Returns:
            ``(value, count)`` pairs, most common first. Values that are empty or
            absent are never indexed, so they cannot appear; values longer than
            the ``ignore_above`` of 256 characters have no keyword and cannot
            either.

        Raises:
            SearchError: If ``field`` has no keyword sub-field to aggregate, or
                the request fails.
        """
        if field not in TEXT_FIELDS:
            raise SearchError(f"{field} is not one of the aggregatable text fields")

        keyword = f"{field}.keyword"
        fragment = contains.strip()
        query: dict[str, Any] = {"match_all": {}}
        if fragment:
            # A whole-value wildcard, because the SQL this replaces was
            # `LIKE '%…%'` on the column, not a word match.
            query = {
                "wildcard": {
                    keyword: {"value": f"*{_escape_wildcard(fragment)}*", "case_insensitive": True}
                }
            }

        try:
            response = self.client.search(
                index=self.index,
                body={
                    "query": query,
                    "size": 0,
                    "aggs": {"values": {"terms": {"field": keyword, "size": max(limit, 1)}}},
                    "track_total_hits": False,
                },
            )
        except Exception as e:
            raise SearchError(f"Aggregation on {self.index}.{field} failed: {e}") from e

        buckets = response.get("aggregations", {}).get("values", {}).get("buckets", [])
        return [
            (bucket["key"], int(bucket["doc_count"])) for bucket in buckets if bucket.get("key")
        ]

    def summary_counts(self, special_attention_levels: Sequence[str] = ()) -> dict[str, int]:
        """Return the totals the list page's header shows.

        Args:
            special_attention_levels: ``attention_level`` values that count as
                "special"; empty means none do.

        Returns:
            ``total``, ``with_images`` and ``special`` counts. ``total`` is the
            number of *indexed* aircraft, which lags the table by at most one
            sync interval.

        Raises:
            SearchError: If the request fails.
        """
        levels = [level for level in special_attention_levels if level]
        aggregations: dict[str, Any] = {
            "with_images": {"filter": {"term": {"images_downloaded": True}}}
        }
        if levels:
            aggregations["special"] = {"filter": {"terms": {"attention_level": levels}}}

        try:
            response = self.client.search(
                index=self.index,
                body={
                    "query": {"match_all": {}},
                    "size": 0,
                    "aggs": aggregations,
                    "track_total_hits": True,
                },
            )
        except Exception as e:
            raise SearchError(f"Summary aggregation on {self.index} failed: {e}") from e

        results = response.get("aggregations", {})
        return {
            "total": _total_hits(response.get("hits", {})),
            "with_images": int(results.get("with_images", {}).get("doc_count", 0)),
            "special": int(results.get("special", {}).get("doc_count", 0)),
        }

    def suggest_registrations(self, query: str, limit: int = 15) -> list[str]:
        """Return registrations for the autocomplete dropdown, prefix matches first.

        Args:
            query: The fragment typed so far. Empty returns nothing.
            limit: Most registrations to return.

        Returns:
            Registrations, those starting with the fragment before those merely
            containing it or matching on hex code.

        Raises:
            SearchError: If the request fails.
        """
        fragment = query.strip()
        if not fragment:
            return []

        try:
            response = self.client.search(
                index=self.index,
                body={
                    "query": {
                        "bool": {
                            "should": [
                                {
                                    "prefix": {
                                        "registration.keyword": {
                                            "value": fragment,
                                            "case_insensitive": True,
                                            "boost": 10,
                                        }
                                    }
                                },
                                {"match": {"registration": {"query": fragment, "boost": 2}}},
                                {"match": {"hex_code": {"query": fragment}}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                    "_source": False,
                    "size": max(limit, 1),
                    # Relevance first so prefix matches lead, then alphabetical,
                    # which is the order the SQL `CASE WHEN … THEN 0` produced.
                    "sort": ["_score", {_TIEBREAK: {"order": "asc"}}],
                    "track_total_hits": False,
                },
            )
        except Exception as e:
            raise SearchError(f"Suggest on {self.index} failed: {e}") from e

        hits = response.get("hits", {}).get("hits", [])
        return [hit["_id"] for hit in hits if hit.get("_id")]

    @staticmethod
    def _build_list_query(
        *, text: str, aircraft_type: str, livery: str, attention_levels: Sequence[str]
    ) -> dict[str, Any]:
        """Build the list query: the text clause, if any, plus the exact filters.

        Args:
            text: Stripped free-text query, empty for none.
            aircraft_type: Stripped exact type, empty for none.
            livery: Stripped exact livery name, empty for none.
            attention_levels: Non-empty ``attention_level`` values to keep.

        Returns:
            An OpenSearch ``query`` clause; ``match_all`` when nothing filters.
        """
        # `filter` rather than `must`: these are yes/no, and scoring them would
        # only add noise to the relevance the text clause produces.
        filters: list[dict[str, Any]] = []
        if aircraft_type:
            filters.append({"term": {"aircraft_type.keyword": aircraft_type}})
        if livery:
            filters.append({"term": {"livery_name.keyword": livery}})
        if attention_levels:
            filters.append({"terms": {"attention_level": list(attention_levels)}})

        if not text and not filters:
            return {"match_all": {}}

        clause: dict[str, Any] = {}
        if text:
            clause["must"] = [AircraftSearchIndex._build_query(text)]
        if filters:
            clause["filter"] = filters
        return {"bool": clause}

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


def _total_hits(hits: Mapping[str, Any]) -> int:
    """Read the match count out of a ``hits`` block.

    Args:
        hits: The ``hits`` object of a search response. ``total`` is a
            ``{"value": n, "relation": …}`` object on current versions and a bare
            integer on older ones.

    Returns:
        The number of matching documents, 0 when the response carries no total.
    """
    total = hits.get("total")
    if isinstance(total, Mapping):
        return int(total.get("value", 0))
    if total is None:
        return 0
    return int(total)


def _escape_wildcard(value: str) -> str:
    """Escape the characters a ``wildcard`` query would otherwise interpret.

    Args:
        value: A literal substring an admin typed.

    Returns:
        The same substring with ``\\``, ``*`` and ``?`` escaped, so that typing
        ``*`` looks for an asterisk rather than matching everything.
    """
    for character in ("\\", "*", "?"):
        value = value.replace(character, f"\\{character}")
    return value


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
