"""Tests for the object storage `scraper_main` hands the JetPhotos sink.

The thumbnail step is only as vendor-neutral as its wiring: whatever provider
`DEPLOY_TARGET` resolves to has to arrive at the sink, and a target with no
usable provider has to leave the scraper running rather than aborting it.

`_build_sinks_and_augment_configs` takes the config as an optional third
argument so the older two-argument call sites keep working; that also means a
caller that forgets it silently loses thumbnails, which is what
`test_a_call_without_config_still_builds_the_sink` pins down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.scraper_main import (
    _build_scraper_configs,
    _build_sinks_and_augment_configs,
    _build_storage,
)
from src.storage.local import LocalStorage
from src.storage.s3 import S3Storage


def _configs(db_url: str, **jetphotos: Any) -> dict[str, tuple[type, dict[str, Any]]]:
    return _build_scraper_configs(
        {"scraper": {"scrapers": {"jetphotos": {"enabled": True, **jetphotos}}}},
        database_url=db_url,
        local_mode=False,
        no_db=False,
        max_notes=None,
        max_comments=None,
        max_replies=None,
    )


def _config(provider: str, **storage: Any) -> dict[str, Any]:
    return {"storage": {"provider": provider, **storage}}


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'jetphotos.db'}"


class TestBuildStorage:
    def test_the_configured_provider_wins_over_the_target(self, tmp_path: Path) -> None:
        storage = _build_storage(_config("local", local={"root": str(tmp_path)}))

        assert isinstance(storage, LocalStorage)
        assert storage.root == tmp_path.resolve()

    def test_s3_is_built_from_the_bucket_in_config(self) -> None:
        storage = _build_storage(_config("s3", s3={"bucket": "flight-matrix-assets"}))

        assert isinstance(storage, S3Storage)
        assert storage.bucket == "flight-matrix-assets"

    def test_a_provider_that_cannot_be_configured_disables_thumbnails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An `s3` target with no bucket anywhere. Returning None costs the
        thumbnails; raising would cost the scrape."""
        monkeypatch.delenv("S3_BUCKET_NAME", raising=False)

        assert _build_storage(_config("s3")) is None

    def test_an_unknown_provider_disables_thumbnails(self) -> None:
        assert _build_storage(_config("dropbox")) is None

    def test_no_config_means_no_storage(self) -> None:
        assert _build_storage(None) is None


class TestSinkWiring:
    def test_the_sink_receives_storage_and_can_write_thumbnails(
        self, db_url: str, tmp_path: Path
    ) -> None:
        sinks = _build_sinks_and_augment_configs(
            _configs(db_url),
            db_url,
            _config("local", local={"root": str(tmp_path)}),
        )

        assert sinks["jetphotos"].thumbnails is not None

    def test_the_scrapers_download_directory_is_passed_through(
        self, db_url: str, tmp_path: Path
    ) -> None:
        """The source image is read from disk on the ingestion path, so the sink
        has to look where the scraper was told to download."""
        sinks = _build_sinks_and_augment_configs(
            _configs(db_url, images_dir="/srv/downloads"),
            db_url,
            _config("local", local={"root": str(tmp_path)}),
        )

        assert sinks["jetphotos"].thumbnails.local_dirs == ("/srv/downloads",)

    def test_a_call_without_config_still_builds_the_sink(self, db_url: str) -> None:
        sinks = _build_sinks_and_augment_configs(_configs(db_url), db_url)

        assert sinks["jetphotos"].thumbnails is None

    def test_the_persist_callback_is_still_wired(self, db_url: str, tmp_path: Path) -> None:
        configs = _configs(db_url)
        sinks = _build_sinks_and_augment_configs(
            configs, db_url, _config("local", local={"root": str(tmp_path)})
        )

        assert (
            configs["jetphotos"][1]["persist_images_callback"] == sinks["jetphotos"].persist_images
        )

    def test_the_upload_callback_is_wired_when_a_provider_exists(
        self, db_url: str, tmp_path: Path
    ) -> None:
        """Without this the scraper falls back to boto3, which stores nothing on
        the `gcp` and `local` targets."""
        configs = _configs(db_url)
        sinks = _build_sinks_and_augment_configs(
            configs, db_url, _config("local", local={"root": str(tmp_path)})
        )

        assert configs["jetphotos"][1]["upload_callback"] == sinks["jetphotos"].store_object

    def test_no_provider_leaves_the_scraper_its_own_upload_path(self, db_url: str) -> None:
        """An `aws` deployment whose provider could not be built still has a
        working boto3 client; routing uploads into a sink with nowhere to put
        them would take that away."""
        configs = _configs(db_url)
        _build_sinks_and_augment_configs(configs, db_url)

        assert "upload_callback" not in configs["jetphotos"][1]
