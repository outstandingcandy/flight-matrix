"""Tests for the thumbnail step `JetPhotosSink` runs after a scrape.

This is the whole point of the feature, so it is worth stating plainly: before
it existed, an image that reached storage on the `gcp` or `local` target had no
thumbnail, and the pages that ask for one — which derive the URL client-side and
never check — showed a broken image until somebody ran
`scripts/generate_thumbnails.py` by hand. The AWS deployment got one from an S3
event Lambda, which is why it was never noticed.

`LocalStorage` over a tmp dir stands in for the bucket, so these assert against
the real `ObjectStorage` contract on the same code path S3 and GCS take.

The DB half is included because the two must not be coupled: a thumbnail
failure cannot cost a row, and a database failure cannot cost the thumbnail.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from resilient_scraper.scrapers.aviation.jetphotos.models import ImageMetadata
from sqlalchemy import create_engine, text

from src.media.thumbnails import SOURCE_PREFIX, THUMB_PREFIX
from src.scraper.sinks.jetphotos_sink import JetPhotosSink
from src.storage.local import LocalStorage

REGISTRATION = "B-1234"
FILENAME = "B-1234_full_1772277610.jpg"
SOURCE_KEY = f"{SOURCE_PREFIX}{FILENAME}"
THUMB_KEY = f"{THUMB_PREFIX}B-1234_thumb_1772277610.jpg"


def _jpeg(width: int = 1600, height: int = 1067) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(root=tmp_path / "bucket")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """SQLite URL with the two tables the sink writes, and one aircraft row.

    The sink does not create its tables — the app's migrations own them — so
    the columns it writes are spelled out here.
    """
    url = f"sqlite:///{tmp_path / 'jetphotos.db'}"
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(
            text("""
                CREATE TABLE aircraft_static_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registration VARCHAR(16),
                    images_downloaded BOOLEAN,
                    images_updated_at DATETIME
                )
            """)
        )
        conn.execute(
            text("""
                CREATE TABLE aircraft_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registration VARCHAR(16),
                    aircraft_id INTEGER,
                    image_path TEXT,
                    source_url TEXT,
                    source VARCHAR(32),
                    photographer TEXT,
                    photo_date TEXT,
                    upload_date TEXT,
                    location TEXT,
                    airport_icao TEXT,
                    airport_name TEXT,
                    file_size_bytes INTEGER,
                    jetphotos_id VARCHAR(32),
                    notes TEXT,
                    camera TEXT,
                    views INTEGER,
                    likes INTEGER,
                    badges TEXT,
                    html_s3_path TEXT,
                    display_order INTEGER,
                    is_primary BOOLEAN,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """)
        )
        conn.execute(
            text("INSERT INTO aircraft_static_info (registration) VALUES (:r)"),
            {"r": REGISTRATION},
        )
        conn.commit()
    engine.dispose()
    return url


@pytest.fixture
def scraped_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A downloaded image on local disk, where the scraper leaves it."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / SOURCE_KEY
    path.parent.mkdir(parents=True)
    path.write_bytes(_jpeg())
    return SOURCE_KEY


def _metadata() -> list[ImageMetadata]:
    return [ImageMetadata(image_path=SOURCE_KEY, jetphotos_id="9876543")]


class TestThumbnailOnIngestion:
    def test_a_scraped_image_gets_its_thumbnail(
        self, db_url: str, storage: LocalStorage, scraped_image: str
    ) -> None:
        sink = JetPhotosSink(db_url, storage=storage, images_dir="data/jetphotos_images")

        sink.persist_images(REGISTRATION, [scraped_image], _metadata())

        assert storage.exists(THUMB_KEY)
        thumb = Image.open(io.BytesIO(storage.download_bytes(THUMB_KEY)))
        assert thumb.width == 400
        assert thumb.format == "JPEG"

    def test_the_key_is_the_one_the_frontend_derives(
        self, db_url: str, storage: LocalStorage, scraped_image: str
    ) -> None:
        """The pages rewrite the stored `image_path` in the browser, so the
        thumbnail has to be at exactly that key and nowhere else."""
        sink = JetPhotosSink(db_url, storage=storage)
        sink.persist_images(REGISTRATION, [scraped_image], _metadata())

        with create_engine(db_url).connect() as conn:
            stored = conn.execute(text("SELECT image_path FROM aircraft_images")).scalar_one()

        derived = str(stored).replace("/jetphotos_images/", "/jetphotos_thumbnails/")
        derived = derived.replace("_full_", "_thumb_")
        assert storage.exists(derived)

    def test_every_image_of_the_scrape_is_covered(
        self, db_url: str, storage: LocalStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        keys = [f"{SOURCE_PREFIX}{REGISTRATION}_full_{i}.jpg" for i in range(3)]
        for key in keys:
            path = tmp_path / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_jpeg(800, 600))
        sink = JetPhotosSink(db_url, storage=storage)

        sink.persist_images(REGISTRATION, keys, [])

        assert all(storage.exists(f"{THUMB_PREFIX}{REGISTRATION}_thumb_{i}.jpg") for i in range(3))

    def test_the_source_is_read_from_the_configured_images_dir(
        self, db_url: str, storage: LocalStorage, tmp_path: Path
    ) -> None:
        """`images_dir` is configurable and need not match the key's directory."""
        images_dir = tmp_path / "custom-downloads"
        images_dir.mkdir()
        (images_dir / FILENAME).write_bytes(_jpeg())
        sink = JetPhotosSink(db_url, storage=storage, images_dir=str(images_dir))

        sink.persist_images(REGISTRATION, [SOURCE_KEY], [])

        assert storage.exists(THUMB_KEY)

    def test_an_image_only_in_the_bucket_is_still_thumbnailed(
        self, db_url: str, storage: LocalStorage
    ) -> None:
        """`delete_local_after_upload` removes the file the moment it is stored."""
        storage.upload_bytes(SOURCE_KEY, _jpeg())
        sink = JetPhotosSink(db_url, storage=storage)

        sink.persist_images(REGISTRATION, [SOURCE_KEY], [])

        assert storage.exists(THUMB_KEY)

    def test_no_storage_means_no_thumbnails_and_no_crash(
        self, db_url: str, scraped_image: str
    ) -> None:
        """The `local` target with no provider configured, and the pre-existing
        construction path. The database work must be unaffected."""
        sink = JetPhotosSink(db_url)

        sink.persist_images(REGISTRATION, [scraped_image], _metadata())

        assert sink.thumbnails is None
        with create_engine(db_url).connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM aircraft_images")).scalar() == 1


class TestFailuresStayIndependent:
    def test_the_row_is_written_even_when_the_image_is_corrupt(
        self, db_url: str, storage: LocalStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A thumbnail is derived data. Losing it must not lose the metadata,
        which cost a page load and a Cloudflare wait to collect."""
        monkeypatch.chdir(tmp_path)
        path = tmp_path / SOURCE_KEY
        path.parent.mkdir(parents=True)
        path.write_bytes(b"truncated download")
        sink = JetPhotosSink(db_url, storage=storage, images_dir="data/jetphotos_images")

        sink.persist_images(REGISTRATION, [SOURCE_KEY], _metadata())

        assert not storage.exists(THUMB_KEY)
        with create_engine(db_url).connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM aircraft_images")).scalar() == 1
            assert conn.execute(text("SELECT images_downloaded FROM aircraft_static_info")).scalar()

    def test_the_thumbnail_is_written_even_when_the_row_fails(
        self, tmp_path: Path, storage: LocalStorage, scraped_image: str
    ) -> None:
        """A database URL pointing at a schema that does not exist: the metadata
        write logs and gives up, and the thumbnail still has to happen."""
        sink = JetPhotosSink(
            f"sqlite:///{tmp_path / 'empty.db'}",
            storage=storage,
            images_dir="data/jetphotos_images",
        )

        sink.persist_images(REGISTRATION, [scraped_image], _metadata())

        assert storage.exists(THUMB_KEY)

    def test_no_database_still_generates_thumbnails(
        self, storage: LocalStorage, scraped_image: str
    ) -> None:
        sink = JetPhotosSink("", storage=storage, images_dir="data/jetphotos_images")

        sink.persist_images(REGISTRATION, [scraped_image], [])

        assert storage.exists(THUMB_KEY)

    def test_an_empty_scrape_does_nothing(self, db_url: str, storage: LocalStorage) -> None:
        sink = JetPhotosSink(db_url, storage=storage)

        sink.persist_images(REGISTRATION, [], [])

        assert list(storage.list_keys(THUMB_PREFIX)) == []
