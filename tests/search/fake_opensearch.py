"""An in-memory stand-in for `opensearchpy.OpenSearch`.

Only the calls `src.search` makes are implemented, and they keep enough state
that a test can index documents and then read them back — the alternative,
asserting on recorded call arguments, passes just as happily when the two halves
of a bulk body are in the wrong order.

Search matching itself is *not* modelled: relevance is OpenSearch's job, so
`queue_hits` programs what a query returns and `last_search_body` exposes what
was asked, which is where query-shape assertions belong.

Aggregations are the exception. A terms or filter aggregation has no relevance in
it — it is counting — so counting the indexed documents here is both easy and
more useful than a canned answer. Only the clause shapes `aircraft_index` builds
are understood, and anything else raises rather than quietly counting nothing.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any


class FakeIndices:
    """The `client.indices` namespace."""

    def __init__(self, cluster: FakeOpenSearch) -> None:
        self.cluster = cluster

    def exists(self, index: str) -> bool:
        self.cluster._maybe_fail()
        return index in self.cluster.created

    def create(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.cluster._maybe_fail()
        if index in self.cluster.created:
            raise RuntimeError(f"resource_already_exists_exception: {index}")
        self.cluster.created[index] = body
        return {"acknowledged": True}

    def put_mapping(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.cluster._maybe_fail()
        self.cluster.mapping_updates.append((index, body))
        return {"acknowledged": True}

    def refresh(self, index: str) -> dict[str, Any]:
        self.cluster._maybe_fail()
        self.cluster.refreshes.append(index)
        return {"_shards": {"failed": 0}}

    def delete(self, index: str, ignore_unavailable: bool = False) -> dict[str, Any]:
        self.cluster.created.pop(index, None)
        self.cluster.documents.clear()
        return {"acknowledged": True}


class FakeOpenSearch:
    """A single-index cluster held in a dict.

    Args:
        fail_with: Exception raised by every request, for the "cluster is down"
            cases.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.mapping_updates: list[tuple[str, dict[str, Any]]] = []
        self.refreshes: list[str] = []
        self.bulk_calls: list[dict[str, Any]] = []
        self.searches: list[dict[str, Any]] = []
        self.queued_hits: list[str] = []
        self.queued_total: int | None = None
        self.bulk_response: dict[str, Any] | None = None
        self.fail_with = fail_with
        self.indices = FakeIndices(self)

    # -- helpers used by tests --------------------------------------------

    def queue_hits(self, *registrations: str, total: int | None = None) -> None:
        """Program the ids the next search returns.

        Args:
            registrations: The ids, in order.
            total: What `hits.total` reports, for the paged list query where the
                page is a window onto a larger match set. Defaults to the number
                of ids given — and, until this is called at all, to the number of
                indexed documents, which is what a `match_all` really counts.
        """
        self.queued_hits = list(registrations)
        self.queued_total = len(registrations) if total is None else total

    @property
    def last_search_body(self) -> dict[str, Any]:
        return self.searches[-1]

    # -- client surface ---------------------------------------------------

    def bulk(self, body: list[Any], refresh: bool = False) -> dict[str, Any]:
        self._maybe_fail()
        self.bulk_calls.append({"body": body, "refresh": refresh})
        if self.bulk_response is not None:
            return self.bulk_response

        items: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None
        for entry in body:
            if pending is None and isinstance(entry, dict) and set(entry) & {"index", "delete"}:
                pending = entry
                if "delete" in entry:
                    doc_id = entry["delete"]["_id"]
                    existed = self.documents.pop(doc_id, None) is not None
                    items.append({"delete": {"_id": doc_id, "status": 200 if existed else 404}})
                    pending = None
                continue
            assert pending is not None, "a document arrived without its action line"
            self.documents[pending["index"]["_id"]] = entry
            items.append({"index": {"_id": pending["index"]["_id"], "status": 201}})
            pending = None

        assert pending is None, "an action line arrived without its document"
        return {"errors": False, "items": items}

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail()
        self.searches.append(body)

        # An aggregation counts what the query selected, so the query has to be
        # applied — but only here. Free-text search bodies are full of clauses
        # `_matches` deliberately refuses, and they never carry an aggregation.
        scope = list(self.documents.values())
        if "aggs" in body:
            scope = [doc for doc in scope if _matches(doc, body.get("query", {"match_all": {}}))]

        total = len(scope) if self.queued_total is None else self.queued_total
        response: dict[str, Any] = {
            "hits": {
                "hits": [{"_id": doc_id} for doc_id in self.queued_hits],
                "total": {"value": total, "relation": "eq"},
            }
        }
        if "aggs" in body:
            response["aggregations"] = {
                name: self._aggregate(name, definition, scope)
                for name, definition in body["aggs"].items()
            }
        return response

    def _aggregate(
        self, name: str, definition: dict[str, Any], scope: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Compute one aggregation over the documents the query selected.

        Args:
            name: The aggregation's name, used in the result and in errors.
            definition: Its body, which must be a `max`, `terms` or `filter`.
            scope: The documents to aggregate.

        Returns:
            The aggregation result in OpenSearch's shape.

        Raises:
            NotImplementedError: For any other aggregation type, so that a new
                one cannot silently return zero.
        """
        if "max" in definition:
            field = definition["max"]["field"]
            values = [doc[field] for doc in scope if doc.get(field)]
            newest = max(values) if values else None
            return {
                "value": None if newest is None else 0,
                "value_as_string": None if newest is None else _isoformat(newest),
            }

        if "terms" in definition:
            field = definition["terms"]["field"].removesuffix(".keyword")
            counts: dict[str, int] = {}
            for doc in scope:
                value = doc.get(field)
                if value:
                    counts[value] = counts.get(value, 0) + 1
            # Most common first, then by value, which is what OpenSearch does.
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            size = definition["terms"].get("size", 10)
            return {"buckets": [{"key": key, "doc_count": count} for key, count in ranked[:size]]}

        if "filter" in definition:
            return {"doc_count": sum(1 for doc in scope if _matches(doc, definition["filter"]))}

        raise NotImplementedError(f"The fake cluster cannot compute the {name} aggregation")

    def count(self, index: str) -> dict[str, Any]:
        self._maybe_fail()
        return {"count": len(self.documents)}

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with


def _matches(document: dict[str, Any], clause: dict[str, Any]) -> bool:
    """Whether a document satisfies a non-scoring clause.

    Args:
        document: An indexed document body.
        clause: A `match_all`, `term`, `terms` or `wildcard` clause — the shapes
            `aircraft_index` uses where the answer is yes or no rather than a
            relevance score. Wildcard escaping (`\\*`) is not modelled.

    Returns:
        True if the document matches.

    Raises:
        NotImplementedError: For any other clause, so a new one cannot pass here
            by being silently ignored.
    """
    if "match_all" in clause:
        return True
    if "term" in clause:
        field, expected = next(iter(clause["term"].items()))
        return bool(document.get(field.removesuffix(".keyword")) == expected)
    if "terms" in clause:
        field, expected = next(iter(clause["terms"].items()))
        return document.get(field.removesuffix(".keyword")) in expected
    if "wildcard" in clause:
        field, options = next(iter(clause["wildcard"].items()))
        value = document.get(field.removesuffix(".keyword"))
        if not isinstance(value, str):
            return False
        pattern = options["value"]
        if options.get("case_insensitive"):
            value, pattern = value.lower(), pattern.lower()
        return fnmatchcase(value, pattern)
    raise NotImplementedError(f"The fake cluster cannot evaluate {sorted(clause)}")


def _isoformat(value: Any) -> str:
    """Render a stored `last_updated` the way OpenSearch would."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
