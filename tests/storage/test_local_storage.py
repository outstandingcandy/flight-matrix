"""Tests for `LocalStorage` and the shared `ObjectStorage` behaviour.

`LocalStorage` is the reference implementation for the interface contract, so
these tests double as the contract tests: any provider that diverges from them
will break call sites when the deployment target changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import ObjectNotFoundError, StorageError
from src.storage.local import LocalStorage


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(root=tmp_path)


def test_roundtrip(storage: LocalStorage) -> None:
    storage.upload_bytes("images/a/b.jpg", b"payload")
    assert storage.download_bytes("images/a/b.jpg") == b"payload"


def test_upload_creates_parent_directories(storage: LocalStorage, tmp_path: Path) -> None:
    storage.upload_bytes("deep/nested/path/file.txt", b"x")
    assert (tmp_path / "deep" / "nested" / "path" / "file.txt").is_file()


def test_upload_overwrites(storage: LocalStorage) -> None:
    storage.upload_bytes("k", b"first")
    storage.upload_bytes("k", b"second")
    assert storage.download_bytes("k") == b"second"


def test_download_missing_key_raises_object_not_found(storage: LocalStorage) -> None:
    with pytest.raises(ObjectNotFoundError) as excinfo:
        storage.download_bytes("nope.jpg")
    assert excinfo.value.key == "nope.jpg"


def test_exists(storage: LocalStorage) -> None:
    assert storage.exists("k") is False
    storage.upload_bytes("k", b"x")
    assert storage.exists("k") is True


def test_exists_is_false_for_a_directory(storage: LocalStorage) -> None:
    """Directories are not objects; a prefix must not look like a stored key."""
    storage.upload_bytes("dir/file", b"x")
    assert storage.exists("dir") is False


def test_list_keys_returns_posix_keys_not_paths(storage: LocalStorage) -> None:
    storage.upload_bytes("a/b/c.txt", b"x")
    assert list(storage.list_keys()) == ["a/b/c.txt"]


def test_list_keys_filters_by_prefix(storage: LocalStorage) -> None:
    for key in ("images/1.jpg", "images/2.jpg", "html/1.html"):
        storage.upload_bytes(key, b"x")

    assert list(storage.list_keys("images/")) == ["images/1.jpg", "images/2.jpg"]
    assert list(storage.list_keys("html")) == ["html/1.html"]


def test_list_keys_treats_prefix_as_key_prefix_not_directory(storage: LocalStorage) -> None:
    """Matches S3/GCS semantics: a partial filename is a valid prefix."""
    storage.upload_bytes("images/N703PA-1.jpg", b"x")
    storage.upload_bytes("images/N912XY-1.jpg", b"x")
    assert list(storage.list_keys("images/N703")) == ["images/N703PA-1.jpg"]


def test_list_keys_on_empty_root(storage: LocalStorage) -> None:
    assert list(storage.list_keys()) == []


@pytest.mark.parametrize("key", ["../escape.txt", "a/../../escape.txt", "/../escape.txt"])
def test_keys_cannot_escape_the_root(storage: LocalStorage, key: str) -> None:
    with pytest.raises(StorageError):
        storage.download_bytes(key)


def test_leading_slash_is_stripped(storage: LocalStorage) -> None:
    storage.upload_bytes("/k", b"x")
    assert storage.download_bytes("k") == b"x"


# ---------------------------------------------------------------------------
# public_url
# ---------------------------------------------------------------------------


def test_public_url_is_root_relative_without_a_base_url(storage: LocalStorage) -> None:
    assert storage.public_url("data/images/a.jpg") == "/data/images/a.jpg"


def test_public_url_uses_the_configured_base_url(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path, public_base_url="https://cdn.example.com/")
    assert storage.public_url("data/a.jpg") == "https://cdn.example.com/data/a.jpg"


def test_public_url_normalises_a_leading_slash(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path, public_base_url="https://cdn.example.com")
    assert storage.public_url("/data/a.jpg") == "https://cdn.example.com/data/a.jpg"


# ---------------------------------------------------------------------------
# strip_public_prefix — must recognise both clouds regardless of active target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Virtual-hosted S3, with and without a region
        ("https://my-bucket.s3.amazonaws.com/data/images/a.jpg", "data/images/a.jpg"),
        ("https://my-bucket.s3.us-east-1.amazonaws.com/data/a.jpg", "data/a.jpg"),
        ("https://my-bucket.s3-us-west-2.amazonaws.com/data/a.jpg", "data/a.jpg"),
        ("http://my-bucket.s3.amazonaws.com/data/a.jpg", "data/a.jpg"),
        # Path-style S3
        ("https://s3.amazonaws.com/my-bucket/data/a.jpg", "data/a.jpg"),
        ("https://s3.eu-west-1.amazonaws.com/my-bucket/data/a.jpg", "data/a.jpg"),
        # GCS
        ("https://storage.googleapis.com/my-bucket/data/a.jpg", "data/a.jpg"),
        # Legacy rows stored without a scheme
        ("my-bucket.s3.amazonaws.com/data/a.jpg", "data/a.jpg"),
    ],
)
def test_strip_public_prefix_recognises_both_clouds(url: str, expected: str) -> None:
    assert LocalStorage.strip_public_prefix(url) == expected


@pytest.mark.parametrize(
    "value",
    ["", "data/images/a.jpg", "/data/images/a.jpg", "https://cdn.example.com/data/a.jpg"],
)
def test_strip_public_prefix_leaves_unrecognised_values_alone(value: str) -> None:
    assert LocalStorage.strip_public_prefix(value) == value
