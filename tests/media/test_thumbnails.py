"""Tests for `src.media.thumbnails`.

Two things here are contracts with code that cannot be imported, so they are
asserted rather than assumed:

* The key mapping. Templates and JavaScript derive the thumbnail URL in the
  browser with `.replace('/jetphotos_images/', '/jetphotos_thumbnails/')
  .replace('_full_', '_thumb_')` (`web_static/js/app.js`,
  `web_static/js/aircraft_detail.js`, `web_templates/flight_schedules.html`).
  A thumbnail written under any other key is an object nothing ever requests.
* The stored metadata. `Cache-Control` is what keeps the CDN from re-fetching
  ~800k thumbnails, and the content type is what makes the browser render one.

`LocalStorage` rooted at a tmp dir stands in as a real `ObjectStorage`, so these
exercise the provider contract rather than a mock's idea of it. The one
subclass below only records the metadata `LocalStorage` has nowhere to put.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from src.core.exceptions import StorageError
from src.media.thumbnails import (
    SOURCE_PREFIX,
    THUMB_CACHE_CONTROL,
    THUMB_PREFIX,
    THUMB_SIZE,
    ThumbnailService,
    render_thumbnail,
    source_name_from_thumbnail_key,
    thumbnail_key,
)
from src.storage.local import LocalStorage

SOURCE_KEY = f"{SOURCE_PREFIX}B-1234_full_1772277610.jpg"
THUMB_KEY = f"{THUMB_PREFIX}B-1234_thumb_1772277610.jpg"


def _jpeg(width: int = 1600, height: int = 1067, mode: str = "RGB") -> bytes:
    """Encoded test image of a given size, in the format the sources come in."""
    buffer = io.BytesIO()
    fmt = "PNG" if mode in ("RGBA", "P", "LA") else "JPEG"
    Image.new(mode, (width, height), "red" if mode != "P" else 0).save(buffer, format=fmt)
    return buffer.getvalue()


class RecordingStorage(LocalStorage):
    """`LocalStorage` that also remembers the metadata passed to uploads."""

    def __init__(self, root: Path) -> None:
        super().__init__(root=root)
        self.uploads: list[dict[str, Any]] = []

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        self.uploads.append(
            {
                "key": key,
                "size": len(data),
                "content_type": content_type,
                "cache_control": cache_control,
            }
        )
        super().upload_bytes(key, data, content_type, cache_control)


@pytest.fixture
def storage(tmp_path: Path) -> RecordingStorage:
    return RecordingStorage(tmp_path / "bucket")


class TestKeyMapping:
    def test_full_becomes_thumb_under_the_thumbnail_prefix(self) -> None:
        assert thumbnail_key(SOURCE_KEY) == THUMB_KEY

    def test_it_matches_what_the_frontend_computes(self) -> None:
        """The JavaScript rewrite, transcribed. If these ever disagree, every
        gallery on the site shows a broken image."""
        frontend = SOURCE_KEY.replace("/jetphotos_images/", "/jetphotos_thumbnails/").replace(
            "_full_", "_thumb_"
        )

        assert thumbnail_key(SOURCE_KEY) == frontend

    def test_a_key_outside_the_image_prefix_has_no_thumbnail(self) -> None:
        """Xiaohongshu images and the scraped HTML stored under the image prefix
        share the bucket; neither has a thumbnail URL anything asks for."""
        assert thumbnail_key("data/xiaohongshu_images/note_1.jpg") is None
        assert thumbnail_key(f"{SOURCE_PREFIX}html/12345.html") is None

    def test_a_stored_url_or_absolute_path_still_yields_a_key(self) -> None:
        """Rows outlive the provider that wrote them, and a CloudFront host is
        not a shape `strip_public_prefix` knows."""
        assert thumbnail_key(f"https://d111.cloudfront.net/{SOURCE_KEY}") == THUMB_KEY
        assert thumbnail_key(f"/srv/flight-matrix/{SOURCE_KEY}") == THUMB_KEY

    def test_a_filename_without_full_keeps_its_name(self) -> None:
        """Pre-timestamp filenames. Both sides leave them alone, so both agree."""
        assert thumbnail_key(f"{SOURCE_PREFIX}B-1234_001.jpg") == f"{THUMB_PREFIX}B-1234_001.jpg"

    def test_the_inverse_recovers_the_source_filename(self) -> None:
        assert source_name_from_thumbnail_key(THUMB_KEY) == SOURCE_KEY.replace(SOURCE_PREFIX, "")


class TestRender:
    def test_the_result_fits_the_box_and_keeps_its_aspect_ratio(self) -> None:
        img = Image.open(io.BytesIO(render_thumbnail(_jpeg(1600, 1067))))

        assert img.width <= THUMB_SIZE[0]
        assert img.height <= THUMB_SIZE[1]
        assert img.width == THUMB_SIZE[0], "a 3:2 photo should be width-bound"
        assert abs(img.width / img.height - 1600 / 1067) < 0.01

    def test_the_result_is_always_jpeg(self) -> None:
        """The derived key keeps the source extension, so a PNG source still has
        to come back as the JPEG the Content-Type promises."""
        img = Image.open(io.BytesIO(render_thumbnail(_jpeg(800, 600, mode="RGBA"))))

        assert img.format == "JPEG"
        assert img.mode == "RGB"

    def test_a_palette_image_is_converted(self) -> None:
        assert render_thumbnail(_jpeg(800, 600, mode="P"))

    def test_a_greyscale_image_stays_greyscale(self) -> None:
        """Converting it to RGB would triple the bytes for no visible gain."""
        img = Image.open(io.BytesIO(render_thumbnail(_jpeg(800, 600, mode="L"))))

        assert img.mode == "L"

    def test_an_image_smaller_than_the_box_is_not_upscaled(self) -> None:
        img = Image.open(io.BytesIO(render_thumbnail(_jpeg(200, 150))))

        assert (img.width, img.height) == (200, 150)

    def test_undecodable_bytes_raise(self) -> None:
        """The service catches this; the renderer must not silently return
        something that looks like a thumbnail."""
        with pytest.raises((OSError, ValueError)):
            render_thumbnail(b"not an image")


class TestEnsureThumbnail:
    def test_it_writes_the_thumbnail_next_to_the_source(
        self, storage: RecordingStorage, tmp_path: Path
    ) -> None:
        storage.upload_bytes(SOURCE_KEY, _jpeg())
        service = ThumbnailService(storage)

        assert service.ensure_thumbnail(SOURCE_KEY) == THUMB_KEY
        assert storage.exists(THUMB_KEY)
        assert Image.open(io.BytesIO(storage.download_bytes(THUMB_KEY))).width == THUMB_SIZE[0]

    def test_the_upload_carries_the_cdn_metadata(self, storage: RecordingStorage) -> None:
        storage.upload_bytes(SOURCE_KEY, _jpeg())
        storage.uploads.clear()

        ThumbnailService(storage).ensure_thumbnail(SOURCE_KEY)

        assert storage.uploads == [
            {
                "key": THUMB_KEY,
                "size": storage.uploads[0]["size"],
                "content_type": "image/jpeg",
                "cache_control": THUMB_CACHE_CONTROL,
            }
        ]

    def test_the_thumbnail_is_much_smaller_than_the_source(self, storage: RecordingStorage) -> None:
        source = _jpeg()
        storage.upload_bytes(SOURCE_KEY, source)

        ThumbnailService(storage).ensure_thumbnail(SOURCE_KEY)

        assert len(storage.download_bytes(THUMB_KEY)) < len(source)

    def test_an_existing_thumbnail_is_left_alone(self, storage: RecordingStorage) -> None:
        storage.upload_bytes(SOURCE_KEY, _jpeg())
        storage.upload_bytes(THUMB_KEY, b"already-here")
        storage.uploads.clear()

        assert ThumbnailService(storage).ensure_thumbnail(SOURCE_KEY) == THUMB_KEY
        assert storage.uploads == []
        assert storage.download_bytes(THUMB_KEY) == b"already-here"

    def test_skip_existing_off_regenerates(self, storage: RecordingStorage) -> None:
        """What the bulk backfill uses, having already diffed the two prefixes."""
        storage.upload_bytes(SOURCE_KEY, _jpeg())
        storage.upload_bytes(THUMB_KEY, b"stale")

        ThumbnailService(storage).ensure_thumbnail(SOURCE_KEY, skip_existing=False)

        assert storage.download_bytes(THUMB_KEY) != b"stale"

    def test_a_missing_source_returns_none_without_raising(self, storage: RecordingStorage) -> None:
        """A scrape must not fail because one photo went missing."""
        assert ThumbnailService(storage).ensure_thumbnail(SOURCE_KEY) is None
        assert storage.uploads == []

    def test_an_unreadable_source_returns_none_and_writes_nothing(
        self, storage: RecordingStorage
    ) -> None:
        storage.upload_bytes(SOURCE_KEY, b"truncated garbage")
        storage.uploads.clear()

        assert ThumbnailService(storage).ensure_thumbnail(SOURCE_KEY) is None
        assert not storage.exists(THUMB_KEY)

    def test_a_key_with_no_thumbnail_convention_is_skipped(self, storage: RecordingStorage) -> None:
        storage.upload_bytes("data/xiaohongshu_images/note_1.jpg", _jpeg())
        storage.uploads.clear()

        assert ThumbnailService(storage).ensure_thumbnail("data/xiaohongshu_images/note_1.jpg") is (
            None
        )
        assert storage.uploads == []

    def test_a_public_url_is_reduced_to_its_key(self, storage: RecordingStorage) -> None:
        """Rows written under a previous provider hold a full URL, and both the
        read and the thumbnail still have to land on a key."""
        storage.upload_bytes(SOURCE_KEY, _jpeg())

        result = ThumbnailService(storage).ensure_thumbnail(f"https://cdn.example.com/{SOURCE_KEY}")

        assert result == THUMB_KEY
        assert storage.exists(THUMB_KEY)

    def test_an_upload_failure_is_reported_not_raised(
        self, storage: RecordingStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage.upload_bytes(SOURCE_KEY, _jpeg())

        def boom(*args: Any, **kwargs: Any) -> None:
            raise StorageError("bucket is on fire")

        monkeypatch.setattr(storage, "upload_bytes", boom)

        assert ThumbnailService(storage).ensure_thumbnail(SOURCE_KEY) is None


class TestLocalSource:
    """The ingestion path: the scraper has just written the file to disk."""

    def test_it_reads_the_local_file_without_touching_storage(
        self, storage: RecordingStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On gcp the scraper's own boto3 upload does nothing, so the source is
        on disk and nowhere else. The thumbnail still has to be written."""
        monkeypatch.chdir(tmp_path)
        local = tmp_path / SOURCE_KEY
        local.parent.mkdir(parents=True)
        local.write_bytes(_jpeg())

        service = ThumbnailService(storage, prefer_local=True)

        assert service.ensure_thumbnail(SOURCE_KEY) == THUMB_KEY
        assert storage.exists(THUMB_KEY)

    def test_images_dir_is_searched_for_the_basename(
        self, storage: RecordingStorage, tmp_path: Path
    ) -> None:
        """`images_dir` is configurable, so the stored key's directory and the
        directory the file is actually in need not match."""
        images_dir = tmp_path / "somewhere-else"
        images_dir.mkdir()
        (images_dir / "B-1234_full_1772277610.jpg").write_bytes(_jpeg())

        service = ThumbnailService(storage, local_dirs=(str(images_dir),), prefer_local=True)

        assert service.ensure_thumbnail(SOURCE_KEY) == THUMB_KEY

    def test_it_falls_back_to_storage_when_the_file_is_gone(
        self, storage: RecordingStorage
    ) -> None:
        """`delete_local_after_upload` is a supported setting."""
        storage.upload_bytes(SOURCE_KEY, _jpeg())

        service = ThumbnailService(storage, prefer_local=True)

        assert service.ensure_thumbnail(SOURCE_KEY) == THUMB_KEY


class TestEnsureThumbnails:
    def test_it_counts_written_and_failed(self, storage: RecordingStorage) -> None:
        good = [f"{SOURCE_PREFIX}A-{i}_full_1.jpg" for i in range(3)]
        for key in good:
            storage.upload_bytes(key, _jpeg(400, 300))
        missing = f"{SOURCE_PREFIX}GONE_full_1.jpg"

        written, failed = ThumbnailService(storage).ensure_thumbnails([*good, missing], workers=2)

        assert (written, failed) == (3, 1)
        for key in good:
            assert storage.exists(thumbnail_key(key) or "")

    def test_an_empty_batch_is_a_no_op(self, storage: RecordingStorage) -> None:
        assert ThumbnailService(storage).ensure_thumbnails([]) == (0, 0)
