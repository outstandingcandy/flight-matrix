"""An in-memory stand-in for `opensearchpy.OpenSearch`.

Only the calls `src.search` makes are implemented, and they keep enough state
that a test can index documents and then read them back — the alternative,
asserting on recorded call arguments, passes just as happily when the two halves
of a bulk body are in the wrong order.

Search matching itself is *not* modelled: relevance is OpenSearch's job, so
`queue_hits` programs what a query returns and `last_search_body` exposes what
was asked, which is where query-shape assertions belong.
"""

from __future__ import annotations

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
        self.bulk_response: dict[str, Any] | None = None
        self.fail_with = fail_with
        self.indices = FakeIndices(self)

    # -- helpers used by tests --------------------------------------------

    def queue_hits(self, *registrations: str) -> None:
        """Program the ids the next search returns."""
        self.queued_hits = list(registrations)

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

        if "aggs" in body:
            values = [
                doc["last_updated"] for doc in self.documents.values() if doc.get("last_updated")
            ]
            newest = max(values) if values else None
            return {
                "aggregations": {
                    "newest": {
                        "value": None if newest is None else 0,
                        "value_as_string": None if newest is None else _isoformat(newest),
                    }
                }
            }

        return {"hits": {"hits": [{"_id": doc_id} for doc_id in self.queued_hits]}}

    def count(self, index: str) -> dict[str, Any]:
        self._maybe_fail()
        return {"count": len(self.documents)}

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with


def _isoformat(value: Any) -> str:
    """Render a stored `last_updated` the way OpenSearch would."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
