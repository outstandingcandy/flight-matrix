"""Tests for `JetPhotosSink.store_object`, the scraper's storage exit.

The scraper used to write through its own boto3 client, so on the `gcp` and
`local` targets it stored nothing at all: downloaded images kept only their
local copy while the database recorded the object key they *would* have had, and
the saved page HTML — the input `src/scraper/reextractor.py` needs to re-run
extraction without re-scraping JetPhotos — was never written.

`LocalStorage` over a tmp dir stands in for the bucket, so these exercise the
real `ObjectStorage` contract on the code path S3 and GCS take. The last class
runs the submodule scraper's own upload methods through the wired callback,
which is the only place the two halves are proven to fit together.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from resilient_scraper.scrapers.aviation.jetphotos.scraper import JetPhotosScraper

from src.core.exceptions import StorageError
from src.scraper.sinks.jetphotos_sink import JetPhotosSink
from src.storage.base import ObjectStorage
from src.storage.local import LocalStorage

PREFIX = "data/jetphotos_images"
HTML_KEY = f"{PREFIX}/html/9876543.html"


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(root=tmp_path / "bucket")


class TestStoreObject:
    def test_the_bytes_land_under_the_key_the_scraper_chose(self, storage: LocalStorage) -> None:
        sink = JetPhotosSink("", storage=storage)

        assert sink.store_object(HTML_KEY, b"<html>page</html>", "text/html") is True
        assert storage.download_bytes(HTML_KEY) == b"<html>page</html>"

    def test_no_provider_reports_failure_instead_of_raising(self) -> None:
        """The `local` target with nothing configured. The scraper treats False
        as "no stored copy" and keeps the scrape."""
        sink = JetPhotosSink("")

        assert sink.store_object(HTML_KEY, b"<html/>") is False

    def test_a_storage_error_is_logged_and_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        class Failing(ObjectStorage):
            def download_bytes(self, key: str) -> bytes:  # pragma: no cover
                raise StorageError("no")

            def upload_bytes(
                self,
                key: str,
                data: bytes,
                content_type: str | None = None,
                cache_control: str | None = None,
            ) -> None:
                raise StorageError("bucket is not writable")

            def exists(self, key: str) -> bool:  # pragma: no cover
                return False

            def list_keys(self, prefix: str = "") -> Any:  # pragma: no cover
                return iter(())

        sink = JetPhotosSink("", storage=Failing())

        assert sink.store_object(HTML_KEY, b"<html/>") is False
        assert "bucket is not writable" in caplog.text


class TestThroughTheScraper:
    """The callback as the scraper actually calls it."""

    def _scraper(self, sink: JetPhotosSink) -> JetPhotosScraper:
        # Exactly what `_build_sinks_and_augment_configs` wires, plus the
        # unresolved bucket a non-AWS target really has.
        return JetPhotosScraper(
            {
                "s3_upload": True,
                "s3_bucket": "${S3_BUCKET_NAME}",
                "s3_prefix": PREFIX,
                "upload_callback": sink.store_object,
            }
        )

    def test_the_page_html_reaches_the_bucket(self, storage: LocalStorage) -> None:
        scraper = self._scraper(JetPhotosSink("", storage=storage))

        key = scraper._store_page_html("<html>photo page</html>", "9876543")

        assert key == HTML_KEY
        assert storage.download_bytes(HTML_KEY) == b"<html>photo page</html>"

    def test_the_reextractor_can_list_what_the_scraper_wrote(self, storage: LocalStorage) -> None:
        """`scripts/reextract_fields.py` lists this prefix and
        `src/scraper/reextractor.py` reads the ids back out of the filenames."""
        scraper = self._scraper(JetPhotosSink("", storage=storage))

        scraper._store_page_html("<html/>", "9876543")

        assert list(storage.list_keys(f"{PREFIX}/html/")) == [HTML_KEY]

    def test_the_image_reaches_the_bucket_under_its_stored_path(
        self, storage: LocalStorage, tmp_path: Path
    ) -> None:
        """`_handle_upload`'s return value is what goes into
        `aircraft_images.image_path`, so the key and the object must agree."""
        scraper = self._scraper(JetPhotosSink("", storage=storage))
        local = tmp_path / "B-1234_full_1772277610.jpg"
        local.write_bytes(b"\xff\xd8jpeg")

        key = scraper._handle_upload(str(local))

        assert key == f"{PREFIX}/B-1234_full_1772277610.jpg"
        assert storage.download_bytes(key) == b"\xff\xd8jpeg"
