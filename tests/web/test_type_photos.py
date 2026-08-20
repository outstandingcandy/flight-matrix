"""Tests for `GET /api/airports/<code>/type-photos`.

The flight list uses this to fall back to "same type, photographed here" when an
aircraft has no photo of its own. Two things make that fallback fragile:

- Two thirds of the ZBAA photo rows are metadata with no file behind them
  (`image_path = ''`). Serving those is not a cosmetic problem — the list would
  render a wall of broken images.
- The list works in IATA (`PEK`) and the photos record ICAO (`ZBAA`). Getting the
  translation wrong returns an empty result that looks like "no photos here".

Ordering is asserted too, because `likes DESC` sorts NULLs to opposite ends on
PostgreSQL and SQLite, and the query has to pick one.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

URL = "/api/airports/PEK/type-photos"


@pytest.fixture
def photos(app_client: Any) -> Any:
    """PEK/ZBAA plus a small photo set: three B738s, one A359, and rows that
    must never be served — an empty path and a photo from another airport."""
    web_app = app_client.application_module
    engine = web_app.db_manager.engine
    from src.data.models import AircraftImage, AircraftStaticInfo, Airport

    for model in (Airport, AircraftImage, AircraftStaticInfo):
        model.__table__.create(engine, checkfirst=True)

    aircraft = [
        ("B-1111", "B738"),
        ("B-2222", "B738"),
        ("B-3333", "B738"),
        ("B-4444", "A359"),
        ("B-5555", "B77W"),
    ]
    # (registration, image_path, icao, likes, photographer)
    images = [
        ("B-1111", "data/jetphotos_images/B-1111_full_1.jpg", "ZBAA", 50, "Alice"),
        ("B-2222", "data/jetphotos_images/B-2222_full_1.jpg", "ZBAA", 90, "Bob"),
        ("B-3333", "data/jetphotos_images/B-3333_full_1.jpg", "ZBAA", None, "Carol"),
        ("B-4444", "data/jetphotos_images/B-4444_full_1.jpg", "ZBAA", 10, "Dave"),
        # No file was ever downloaded for this one.
        ("B-5555", "", "ZBAA", 999, "Eve"),
        # Right type, wrong airport.
        ("B-1111", "data/jetphotos_images/B-1111_full_2.jpg", "ZSPD", 999, "Frank"),
    ]

    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO airports (icao_code, iata_code, name, latitude, longitude) "
                "VALUES ('ZBAA', 'PEK', 'Beijing Capital', 40.08, 116.58)"
            )
        )
        for registration, aircraft_type in aircraft:
            conn.execute(
                text(
                    "INSERT INTO aircraft_static_info (registration, aircraft_type) "
                    "VALUES (:registration, :aircraft_type)"
                ),
                {"registration": registration, "aircraft_type": aircraft_type},
            )
        for registration, path, icao, likes, photographer in images:
            conn.execute(
                text(
                    "INSERT INTO aircraft_images "
                    "(registration, image_path, airport_icao, likes, photographer, photo_date) "
                    "VALUES (:registration, :path, :icao, :likes, :photographer, '2026-01-01')"
                ),
                {
                    "registration": registration,
                    "path": path,
                    "icao": icao,
                    "likes": likes,
                    "photographer": photographer,
                },
            )
        conn.commit()
    return app_client


def types_of(response: Any) -> dict[str, list[dict[str, Any]]]:
    body = response.get_json()
    assert body["success"] is True, body
    return body["types"]


class TestWhatIsReturned:
    def test_photos_are_grouped_by_type(self, photos: Any) -> None:
        result = types_of(photos.get(f"{URL}?types=B738,A359"))

        assert sorted(result) == ["A359", "B738"]
        assert len(result["B738"]) == 3
        assert len(result["A359"]) == 1

    def test_the_iata_code_resolves_to_the_photos_icao(self, photos: Any) -> None:
        body = photos.get(f"{URL}?types=B738").get_json()

        assert body["airport_icao"] == "ZBAA"

    def test_the_icao_code_works_too(self, photos: Any) -> None:
        result = types_of(photos.get("/api/airports/ZBAA/type-photos?types=B738"))

        assert len(result["B738"]) == 3

    def test_each_photo_carries_what_the_caption_needs(self, photos: Any) -> None:
        photo = types_of(photos.get(f"{URL}?types=A359"))["A359"][0]

        assert photo["registration"] == "B-4444"
        assert photo["photographer"] == "Dave"
        assert photo["photo_date"].startswith("2026-01-01")
        assert photo["image_url"].endswith("B-4444_full_1.jpg")

    def test_a_type_with_no_photo_here_is_simply_absent(self, photos: Any) -> None:
        """Absent rather than an empty list, so the caller cannot tell "no photo"
        from "photo pending"."""
        assert "A320" not in types_of(photos.get(f"{URL}?types=B738,A320"))


class TestRowsThatMustNotBeServed:
    def test_metadata_without_a_file_is_excluded(self, photos: Any) -> None:
        """B77W's only ZBAA row has `image_path = ''` — 48,734 of the 70,181 real
        ZBAA rows look like that."""
        assert "B77W" not in types_of(photos.get(f"{URL}?types=B77W"))

    def test_a_photo_from_another_airport_is_excluded(self, photos: Any) -> None:
        urls = [p["image_url"] for p in types_of(photos.get(f"{URL}?types=B738"))["B738"]]

        assert not any("_full_2" in url for url in urls)


class TestOrderingAndLimits:
    def test_the_best_liked_photo_comes_first(self, photos: Any) -> None:
        first = types_of(photos.get(f"{URL}?types=B738"))["B738"][0]

        assert first["registration"] == "B-2222"

    def test_a_photo_with_no_likes_sorts_last(self, photos: Any) -> None:
        """`likes DESC` alone would put it first on PostgreSQL."""
        last = types_of(photos.get(f"{URL}?types=B738"))["B738"][-1]

        assert last["registration"] == "B-3333"

    def test_limit_caps_each_type_independently(self, photos: Any) -> None:
        result = types_of(photos.get(f"{URL}?types=B738,A359&limit=2"))

        assert len(result["B738"]) == 2
        assert len(result["A359"]) == 1

    def test_an_oversized_limit_is_clamped(self, photos: Any) -> None:
        assert photos.get(f"{URL}?types=B738&limit=500").status_code == 200

    def test_a_non_numeric_limit_is_a_bad_request(self, photos: Any) -> None:
        assert photos.get(f"{URL}?types=B738&limit=lots").status_code == 400


class TestBadRequests:
    def test_no_types_is_a_bad_request(self, photos: Any) -> None:
        assert photos.get(URL).status_code == 400

    def test_an_empty_types_list_is_a_bad_request(self, photos: Any) -> None:
        assert photos.get(f"{URL}?types=,,").status_code == 400

    def test_an_unknown_airport_is_not_found(self, photos: Any) -> None:
        assert photos.get("/api/airports/XXX/type-photos?types=B738").status_code == 404


class TestCaching:
    def test_a_repeat_request_does_not_query_again(self, photos: Any) -> None:
        """The join is the expensive part of rendering the list; it changes only
        when JetPhotos is scraped."""
        first = types_of(photos.get(f"{URL}?types=B738"))

        web_app = photos.application_module
        with web_app.db_manager.engine.connect() as conn:
            conn.execute(text("DELETE FROM aircraft_images"))
            conn.commit()

        assert types_of(photos.get(f"{URL}?types=B738")) == first

    def test_a_different_type_set_is_a_different_entry(self, photos: Any) -> None:
        photos.get(f"{URL}?types=B738")

        assert "A359" in types_of(photos.get(f"{URL}?types=A359"))

    def test_the_type_order_in_the_query_does_not_split_the_cache(self, photos: Any) -> None:
        first = types_of(photos.get(f"{URL}?types=B738,A359"))

        web_app = photos.application_module
        with web_app.db_manager.engine.connect() as conn:
            conn.execute(text("DELETE FROM aircraft_images"))
            conn.commit()

        assert types_of(photos.get(f"{URL}?types=A359,B738")) == first
