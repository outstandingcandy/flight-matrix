"""Tests for `MediaService`'s aircraft-image retrieval.

The regression these cover: `aircraft_images.image_path` holds an *object
storage key*, not a local path. On any cloud target the file is not on the
report host's disk, so the old `os.path.exists()` filter dropped every image
and the email went out without photos — no error, no warning.

`LocalStorage` rooted at a tmp dir stands in for S3/GCS: it is a real
`ObjectStorage`, so a key that resolves through it and *not* through the
process working directory reproduces the cloud situation exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from src.data.db_manager import DatabaseManager
from src.media.service import MediaService
from src.storage.local import LocalStorage

IMAGE_KEY = "data/jetphotos_images/B-1234_001.jpg"
IMAGE_BYTES = b"\xff\xd8\xff\xe0stored-in-object-storage-only"


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    """Storage holding one image, under a root nothing else knows about."""
    store = LocalStorage(root=tmp_path / "bucket")
    store.upload_bytes(IMAGE_KEY, IMAGE_BYTES)
    return store


@pytest.fixture
def db_with_image(db_manager: DatabaseManager) -> DatabaseManager:
    """A DB whose `aircraft_images` row points at a key that is not on disk."""
    with db_manager.get_session() as session:
        session.execute(
            text(
                "INSERT INTO aircraft_images (id, registration, image_path, display_order) "
                "VALUES (1, :reg, :path, 0)"
            ),
            {"reg": "B-1234", "path": IMAGE_KEY},
        )
        session.commit()
    return db_manager


def test_database_image_is_fetched_from_storage(
    db_with_image: DatabaseManager,
    storage: LocalStorage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The headline regression: a storage-only key must still reach the email.

    `chdir` to an empty directory so the key cannot possibly resolve on the
    local filesystem — exactly the situation on a Lambda or a GCE VM.
    """
    monkeypatch.chdir(tmp_path / "empty" if (tmp_path / "empty").is_dir() else tmp_path)

    service = MediaService(
        enable_maps=False,
        enable_aircraft_images=True,
        database_manager=db_with_image,
        storage=storage,
    )

    paths = service.get_aircraft_images("B-1234")

    assert len(paths) == 1, "storage-backed image was dropped"
    assert Path(paths[0]).read_bytes() == IMAGE_BYTES


def test_local_file_is_used_when_storage_lacks_the_key(
    db_with_image: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-existing local-disk path keeps working with no storage at all."""
    monkeypatch.chdir(tmp_path)
    local = tmp_path / IMAGE_KEY
    local.parent.mkdir(parents=True)
    local.write_bytes(b"on-disk")

    service = MediaService(
        enable_maps=False,
        enable_aircraft_images=True,
        database_manager=db_with_image,
        storage=None,
    )

    paths = service.get_aircraft_images("B-1234")

    assert len(paths) == 1
    assert Path(paths[0]).read_bytes() == b"on-disk"


def test_unfetchable_image_is_skipped_not_raised(
    db_with_image: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key present in neither place degrades to no photos, not a failed report."""
    monkeypatch.chdir(tmp_path)

    service = MediaService(
        enable_maps=False,
        enable_aircraft_images=True,
        database_manager=db_with_image,
        storage=LocalStorage(root=tmp_path / "empty-bucket"),
    )

    assert service.get_aircraft_images("B-1234") == []


def test_temp_dir_does_not_accumulate_across_calls(
    db_with_image: DatabaseManager,
    storage: LocalStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ReportService` is a long-running process that builds one `MediaService`.

    Materialised files must be cleared per call, or a `run_forever` loop grows
    a temp directory without bound.
    """
    monkeypatch.chdir(tmp_path)

    service = MediaService(
        enable_maps=False,
        enable_aircraft_images=True,
        database_manager=db_with_image,
        storage=storage,
    )

    first = service.get_aircraft_images("B-1234")
    second = service.get_aircraft_images("B-1234")

    assert first and second
    holding_dir = Path(second[0]).parent
    assert len(list(holding_dir.iterdir())) == 1, "previous call's files were left behind"
