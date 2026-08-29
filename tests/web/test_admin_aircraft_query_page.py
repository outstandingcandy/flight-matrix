"""Tests for the list-first `/admin/aircraft-query` page and the JSON it reads.

The page used to be search-only: it rendered nothing until an admin typed a
full registration. The paginated list it now opens on was already implemented
server-side — `/api/admin/aircraft` plus the three autocomplete endpoints — but
no template rendered it, so the whole feature was unreachable and nothing tested
that the two halves fit together.

Two things are pinned here, because a template is not type-checked and JS
failures are silent in a browser:

* the page really is list-first, and really references both APIs (a merge that
  half-landed would still return 200 with an empty table);
* every field the list renderer reads exists in the payload, with the shape it
  assumes. `total == 1` is load-bearing beyond display: pressing Enter on a
  single-match search jumps straight to that aircraft's detail view.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

# Naive UTC, which is what every writer in this project stores.
NOW = datetime.now(UTC).replace(tzinfo=None, microsecond=0)

PAGE_PATH = "/admin/aircraft-query"
LIST_PATH = "/api/admin/aircraft"


def _image(
    image_id: int,
    registration: str,
    display_order: int,
    photographer: str | None,
    source: str = "jetphotos",
) -> tuple[str, dict[str, Any]]:
    """Build the INSERT for one `aircraft_images` row."""
    return (
        """
        INSERT INTO aircraft_images
            (id, registration, image_path, source, photographer, display_order,
             is_primary, created_at)
        VALUES (:id, :registration, :image_path, :source, :photographer, :display_order,
                :is_primary, :ts)
        """,
        {
            "id": image_id,
            "registration": registration,
            "image_path": f"data/jetphotos_images/{registration}_{display_order + 1:03d}.jpg",
            "source": source,
            "photographer": photographer,
            "display_order": display_order,
            "is_primary": display_order == 0,
            "ts": NOW,
        },
    )


def _seed(client: Any) -> None:
    """Insert two aircraft, one of them with images and a high attention level.

    Two rows are the minimum that can prove pagination advances rather than
    returning the same row twice.

    `N12345`'s five images are shaped to pin the photographer count at **2**: one
    photographer appears twice, one row has no photographer, one has an empty
    string, and one is from another source. Any of those four rules breaking
    pushes the count to 3 or more.
    """
    db_manager = client.application_module.db_manager
    session = db_manager.get_session()
    try:
        session.execute(
            text("""
            INSERT INTO aircraft_static_info
                (id, registration, hex_code, aircraft_type, manufacturer, model,
                 livery_name, attention_level, images_downloaded, last_updated)
            VALUES (1, 'N12345', 'abc123', 'B738', 'Boeing', '737-800',
                    'Retro Livery', '极高', 1, :ts)
            """),
            {"ts": NOW},
        )
        session.execute(
            text("""
            INSERT INTO aircraft_static_info
                (id, registration, hex_code, aircraft_type, manufacturer, model,
                 livery_name, attention_level, images_downloaded, last_updated)
            VALUES (2, 'B-1234', 'def456', 'A320', 'Airbus', 'A320-200',
                    NULL, NULL, 0, :ts)
            """),
            {"ts": NOW},
        )
        for statement, values in (
            _image(1, "N12345", 0, "Ann Lee"),
            _image(2, "N12345", 1, "Bo Chen"),
            _image(3, "N12345", 2, "Ann Lee"),  # same contributor twice
            _image(4, "N12345", 3, None),
            _image(5, "N12345", 4, ""),
            _image(6, "N12345", 5, "Cara Diaz", source="planespotters"),
        ):
            session.execute(text(statement), values)
        session.commit()
    finally:
        session.close()


@pytest.fixture
def seeded_client(app_client: Any) -> Any:
    _seed(app_client)
    return app_client


def _payload(response: Any, path: str) -> dict[str, Any]:
    assert response.status_code == 200, (
        f"{path} → {response.status_code}: {response.text[:400]}"
    )
    body = response.json()
    assert body["success"] is True, body
    return body


class TestPageWiring:
    """The template must open on the list and still be able to show a detail."""

    @pytest.fixture
    def html(self, app_client: Any) -> str:
        response = app_client.get(PAGE_PATH)
        assert response.status_code == 200, response.text[:400]
        return response.text

    def test_the_list_view_is_the_one_that_starts_visible(self, html: str) -> None:
        # "Open on the list, click a row for the detail" is exactly this: the
        # detail pane ships hidden, the list pane does not.
        assert '<div id="detailView" style="display: none;">' in html, (
            "the detail view should start hidden, so the page opens on the list"
        )
        assert '<div id="listView">' in html, "the list view should start visible"

    def test_the_list_is_wired_to_the_paginated_api(self, html: str) -> None:
        for fragment in (
            "/api/admin/aircraft?",  # the list fetch
            'id="aircraftTableBody"',  # where rows land
            'id="pagination"',  # the pager
        ):
            assert fragment in html, f"the list view is missing {fragment}"

    def test_the_detail_view_is_still_wired(self, html: str) -> None:
        assert "/api/admin/aircraft-query/" in html, (
            "merging the list in must not drop the per-registration lookup"
        )

    def test_a_detail_view_is_addressable(self, html: str) -> None:
        # Row clicks route through the hash, which is what makes the browser's
        # back button return to the list instead of leaving the page.
        assert "'aircraft=' + encodeURIComponent(" in html, (
            "the detail view should be selected by URL hash, not by hidden state"
        )

    def test_the_photographer_column_is_sortable(self, html: str) -> None:
        # Sorting by a number the table does not show would be unusable, so the
        # column and the header that sorts it are one feature.
        assert 'data-sort="photographers"' in html, "no header sorts by photographer count"
        assert "Photographers" in html, "the photographer count has no column"
        assert "sort: currentSort" in html, "the list fetch does not send the sort key"

    def test_the_filter_endpoints_are_wired(self, html: str) -> None:
        for path in (
            "/api/admin/aircraft/stats",
            "/api/admin/aircraft/types?search=",
            "/api/admin/aircraft/liveries?search=",
            "/api/admin/aircraft/registrations?search=",
        ):
            assert path in html, f"the list view is missing {path}"


class TestListPayload:
    """Every field the row renderer reads, and the paging it relies on."""

    def test_rows_carry_the_fields_the_table_renders(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(LIST_PATH), LIST_PATH)

        assert body["total"] == 2
        assert body["page"] == 1
        assert body["pages"] == 1

        rows = {row["registration"]: row for row in body["aircraft"]}
        assert set(rows) == {"N12345", "B-1234"}

        photographed = rows["N12345"]
        assert photographed["hex_code"] == "abc123"
        # The Type column falls back to `aircraft_type`, so the code field the
        # renderer prefers has to be present even when it is a copy.
        assert photographed["aircraft_type_code"] == "B738"
        assert photographed["livery_name"] == "Retro Livery"
        assert photographed["is_special"] is True
        assert photographed["last_updated"], "the Updated column would render '-'"
        assert photographed["image_url"] and "N12345_001.jpg" in photographed["image_url"], (
            f"the thumbnail cell needs a URL, got {photographed['image_url']!r}"
        )

        # An aircraft with no image, livery or attention level must still render:
        # these are the placeholder branches of the row template.
        plain = rows["B-1234"]
        assert plain["image_url"] is None
        assert plain["livery_name"] is None
        assert plain["is_special"] is False

    def test_pages_do_not_overlap(self, seeded_client: Any) -> None:
        # Both seeded rows share one `last_updated`, which is the case that
        # matters: without a tie-break in the ORDER BY, the order across the two
        # LIMIT/OFFSET queries is unspecified and a row can land on both pages.
        first = _payload(seeded_client.get(f"{LIST_PATH}?page=1&limit=1"), LIST_PATH)
        second = _payload(seeded_client.get(f"{LIST_PATH}?page=2&limit=1"), LIST_PATH)

        assert first["pages"] == 2 and first["total"] == 2
        assert len(first["aircraft"]) == 1
        assert len(second["aircraft"]) == 1
        assert first["aircraft"][0]["registration"] != second["aircraft"][0]["registration"], (
            "page 2 returned page 1's row — the offset is not being applied"
        )
        assert second["page"] == 2

    def test_a_page_past_the_end_is_empty_rather_than_an_error(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(f"{LIST_PATH}?page=99&limit=30"), LIST_PATH)
        assert body["aircraft"] == []

    def test_a_full_registration_narrows_to_one_row(self, seeded_client: Any) -> None:
        # This is what the Enter key reads to decide whether to open the detail
        # view directly, so it is behaviour and not just a filter.
        body = _payload(seeded_client.get(f"{LIST_PATH}?search=B-1234"), LIST_PATH)
        assert body["total"] == 1
        assert body["aircraft"][0]["registration"] == "B-1234"

    def test_search_also_matches_the_icao_hex(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(f"{LIST_PATH}?search=abc123"), LIST_PATH)
        assert body["total"] == 1
        assert body["aircraft"][0]["registration"] == "N12345"

    def test_a_partial_search_leaves_more_than_one_row(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(f"{LIST_PATH}?search=1234"), LIST_PATH)
        assert body["total"] == 2, "both seeded aircraft contain '1234'; Enter must not jump"

    def test_type_filter(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(f"{LIST_PATH}?aircraft_type=A320"), LIST_PATH)
        assert [row["registration"] for row in body["aircraft"]] == ["B-1234"]

    def test_livery_filter(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(f"{LIST_PATH}?livery=Retro+Livery"), LIST_PATH)
        assert [row["registration"] for row in body["aircraft"]] == ["N12345"]

    def test_special_category_filter(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(f"{LIST_PATH}?category=special"), LIST_PATH)
        assert [row["registration"] for row in body["aircraft"]] == ["N12345"]


class TestSorting:
    """`sort`/`order`, and the JetPhotos contributor count they can sort on."""

    def test_the_default_sort_is_the_most_recently_updated(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(LIST_PATH), LIST_PATH)
        assert body["sort"] == "last_updated"
        assert body["order"] == "desc"

    def test_rows_carry_the_distinct_jetphotos_contributor_count(self, seeded_client: Any) -> None:
        body = _payload(seeded_client.get(LIST_PATH), LIST_PATH)
        counts = {row["registration"]: row["photographer_count"] for row in body["aircraft"]}
        # Five JetPhotos rows, one repeated contributor, one NULL, one empty
        # string; plus a sixth row from another source.
        assert counts["N12345"] == 2, "duplicate, empty, NULL or non-JetPhotos rows leaked in"
        assert counts["B-1234"] == 0, "an aircraft with no photos must report 0, not null"

    def test_sorting_by_photographers_puts_the_most_photographed_first(
        self, seeded_client: Any
    ) -> None:
        path = f"{LIST_PATH}?sort=photographers"
        body = _payload(seeded_client.get(path), path)
        assert body["sort"] == "photographers"
        assert body["order"] == "desc", "a count should default to most-first"
        assert [row["registration"] for row in body["aircraft"]] == ["N12345", "B-1234"]

    def test_the_photographer_sort_can_be_reversed(self, seeded_client: Any) -> None:
        path = f"{LIST_PATH}?sort=photographers&order=asc"
        body = _payload(seeded_client.get(path), path)
        assert body["order"] == "asc"
        assert [row["registration"] for row in body["aircraft"]] == ["B-1234", "N12345"]

    def test_sorting_by_registration(self, seeded_client: Any) -> None:
        path = f"{LIST_PATH}?sort=registration&order=asc"
        body = _payload(seeded_client.get(path), path)
        assert [row["registration"] for row in body["aircraft"]] == ["B-1234", "N12345"]

    def test_sorting_composes_with_a_filter(self, seeded_client: Any) -> None:
        path = f"{LIST_PATH}?sort=photographers&category=special"
        body = _payload(seeded_client.get(path), path)
        assert [row["registration"] for row in body["aircraft"]] == ["N12345"]

    @pytest.mark.parametrize(
        "query",
        [
            "sort=photographer_count",  # plausible but not the public key
            "sort=asi.registration; DROP TABLE aircraft_static_info",
            "order=; DROP TABLE aircraft_static_info",
            "sort=&order=",
        ],
    )
    def test_an_unusable_sort_falls_back_instead_of_reaching_the_sql(
        self, seeded_client: Any, query: str
    ) -> None:
        # The request value only ever indexes a whitelist, so garbage degrades to
        # the default order rather than becoming SQL or a 500.
        path = f"{LIST_PATH}?{query}"
        body = _payload(seeded_client.get(path), path)
        assert body["sort"] == "last_updated"
        assert body["order"] == "desc"
        assert body["total"] == 2, "the table should still be there"

    def test_pages_do_not_overlap_when_the_sort_key_ties(self, seeded_client: Any) -> None:
        # Almost every aircraft has zero JetPhotos contributors, so this sort key
        # is one enormous tie — exactly the case where LIMIT/OFFSET needs the
        # registration tie-break to stay deterministic.
        db_manager = seeded_client.application_module.db_manager
        session = db_manager.get_session()
        try:
            session.execute(
                text("""
                INSERT INTO aircraft_static_info
                    (id, registration, hex_code, aircraft_type, last_updated)
                VALUES (3, 'G-ABCD', 'aa0001', 'A320', :ts)
                """),
                {"ts": NOW},
            )
            session.commit()
        finally:
            session.close()

        seen = []
        for page in (1, 2, 3):
            path = f"{LIST_PATH}?sort=photographers&limit=1&page={page}"
            body = _payload(seeded_client.get(path), path)
            assert body["pages"] == 3
            assert len(body["aircraft"]) == 1
            seen.append(body["aircraft"][0]["registration"])

        assert sorted(seen) == ["B-1234", "G-ABCD", "N12345"], (
            f"paging over tied photographer counts repeated or skipped a row: {seen}"
        )
        assert seen[0] == "N12345", "the only photographed aircraft should still lead"


class TestFilterEndpointShapes:
    """The suggestion dropdowns and stat cards read specific key names."""

    def test_stats_feed_the_three_cards(self, seeded_client: Any) -> None:
        path = "/api/admin/aircraft/stats"
        stats = _payload(seeded_client.get(path), path)["stats"]
        assert stats["total"] == 2
        assert stats["with_images"] == 1
        assert stats["special"] == 1

    def test_type_suggestions(self, seeded_client: Any) -> None:
        path = "/api/admin/aircraft/types?search=A3"
        types = _payload(seeded_client.get(path), path)["types"]
        assert types == [{"code": "A320", "full_name": "A320", "count": 1}]

    def test_livery_suggestions(self, seeded_client: Any) -> None:
        path = "/api/admin/aircraft/liveries?search=Retro"
        liveries = _payload(seeded_client.get(path), path)["liveries"]
        assert liveries == [{"name": "Retro Livery", "count": 1}]

    def test_registration_suggestions_carry_what_the_dropdown_shows(
        self, seeded_client: Any
    ) -> None:
        path = "/api/admin/aircraft/registrations?search=N12"
        registrations = _payload(seeded_client.get(path), path)["registrations"]
        assert registrations == [
            {"registration": "N12345", "hex_code": "abc123", "aircraft_type": "B738"}
        ]

    def test_a_one_character_query_suggests_nothing(self, seeded_client: Any) -> None:
        # The input only calls out at two characters; the endpoint agrees, so a
        # stray single-character fetch cannot return the whole table.
        path = "/api/admin/aircraft/registrations?search=N"
        assert _payload(seeded_client.get(path), path)["registrations"] == []
