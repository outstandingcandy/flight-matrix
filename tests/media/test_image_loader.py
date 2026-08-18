"""Tests for `src.media.image_loader.load_image_bytes`.

`LocalStorage` rooted at a tmp dir is used as a real `ObjectStorage`, so these
exercise the actual provider contract rather than a mock's idea of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import StorageError
from src.media.image_loader import load_image_bytes
from src.storage.base import ObjectStorage
from src.storage.local import LocalStorage

KEY = "data/jetphotos_images/B-1234_001.jpg"


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(root=tmp_path / "bucket")


def test_reads_from_object_storage(storage: LocalStorage) -> None:
    storage.upload_bytes(KEY, b"remote")
    assert load_image_bytes(KEY, storage) == b"remote"


def test_storage_wins_over_local(
    storage: LocalStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-named local file is a stale leftover, so storage takes priority."""
    monkeypatch.chdir(tmp_path)
    local = tmp_path / KEY
    local.parent.mkdir(parents=True)
    local.write_bytes(b"stale-local")
    storage.upload_bytes(KEY, b"remote")

    assert load_image_bytes(KEY, storage) == b"remote"


def test_falls_back_to_local_when_key_absent(
    storage: LocalStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locally-scraped image that was never uploaded must still load."""
    monkeypatch.chdir(tmp_path)
    local = tmp_path / KEY
    local.parent.mkdir(parents=True)
    local.write_bytes(b"local-only")

    assert load_image_bytes(KEY, storage) == b"local-only"


def test_local_only_when_no_storage_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    local = tmp_path / KEY
    local.parent.mkdir(parents=True)
    local.write_bytes(b"local-only")

    assert load_image_bytes(KEY, None) == b"local-only"


def test_basename_found_under_local_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Paths stored before the `data/`-prefixed key convention still resolve."""
    monkeypatch.chdir(tmp_path)
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "B-1234_001.jpg").write_bytes(b"by-basename")

    assert load_image_bytes(KEY, None, local_dirs=[str(images_dir)]) == b"by-basename"


def test_missing_everywhere_returns_none(
    storage: LocalStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_image_bytes(KEY, storage) is None


def test_empty_path_returns_none(storage: LocalStorage) -> None:
    assert load_image_bytes("", storage) is None


def test_full_public_url_is_reduced_to_a_key(storage: LocalStorage) -> None:
    """Rows outlive target switches, so a stored S3 URL must still resolve."""
    storage.upload_bytes(KEY, b"remote")
    url = f"https://my-bucket.s3.us-east-1.amazonaws.com/{KEY}"

    assert load_image_bytes(url, storage) == b"remote"


def test_broken_storage_does_not_propagate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A credentials or network failure degrades to local, then to None.

    A missing photo must not fail the report that contains it.
    """
    monkeypatch.chdir(tmp_path)

    class BrokenStorage(ObjectStorage):
        def download_bytes(self, key: str) -> bytes:
            raise StorageError("credentials expired")

        def upload_bytes(
            self,
            key: str,
            data: bytes,
            content_type: str | None = None,
            cache_control: str | None = None,
        ) -> None:
            raise StorageError("credentials expired")

        def exists(self, key: str) -> bool:
            raise StorageError("credentials expired")

        def list_keys(self, prefix: str = ""):
            raise StorageError("credentials expired")

    assert load_image_bytes(KEY, BrokenStorage()) is None
