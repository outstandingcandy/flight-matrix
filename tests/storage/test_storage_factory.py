"""Tests for `StorageFactory` and the public-base-URL resolution order.

Only the `local` provider is instantiated for real; `s3` and `gcs` are asserted
on the class the factory *selects*, patched to avoid needing cloud credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.core.deploy_target import ENV_VAR
from src.core.exceptions import StorageError
from src.storage.factory import StorageFactory, resolve_public_base_url
from src.storage.gcs import GCSStorage
from src.storage.local import LocalStorage
from src.storage.s3 import S3Storage

_URL_ENV = ("STATIC_BASE_URL", "CLOUDFRONT_DOMAIN", "GCS_ASSETS_BUCKET")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear target and URL variables so a developer environment cannot leak in."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    for key in _URL_ENV:
        monkeypatch.delenv(key, raising=False)


class _FakeConfig:
    """Minimal stand-in for `YAMLConfig.get(key_path, default)`."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = values or {}

    def get(self, key_path: str, default: Any = None) -> Any:
        return self._values.get(key_path, default)


# ---------------------------------------------------------------------------
# resolve_public_base_url
# ---------------------------------------------------------------------------


def test_static_base_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATIC_BASE_URL", "https://cdn.example.com/")
    monkeypatch.setenv("CLOUDFRONT_DOMAIN", "d123.cloudfront.net")
    assert resolve_public_base_url() == "https://cdn.example.com"


def test_cloudfront_domain_is_still_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live AWS deployment sets only CLOUDFRONT_DOMAIN; it must keep working."""
    monkeypatch.setenv("CLOUDFRONT_DOMAIN", "d123.cloudfront.net")
    assert resolve_public_base_url() == "https://d123.cloudfront.net"


def test_gcs_bucket_derives_the_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCS_ASSETS_BUCKET", "proj-assets")
    assert resolve_public_base_url() == "https://storage.googleapis.com/proj-assets"


def test_no_base_url_configured() -> None:
    assert resolve_public_base_url() == ""


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def test_target_selects_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "aws")
    monkeypatch.setattr(S3Storage, "_build_client", lambda self, region: object())
    storage = StorageFactory.create(_FakeConfig({"storage.s3.bucket": "bkt"}))
    assert isinstance(storage, S3Storage)
    assert storage.bucket == "bkt"


def test_target_selects_gcs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "gcp")
    storage = StorageFactory.create_from_dict({"bucket": "bkt", "client": _FakeGcsClient()})
    assert isinstance(storage, GCSStorage)
    assert storage.public_url("a.jpg") == "https://storage.googleapis.com/bkt/a.jpg"


def test_target_selects_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_VAR, "local")
    storage = StorageFactory.create(_FakeConfig({"storage.local.root": str(tmp_path)}))
    assert isinstance(storage, LocalStorage)
    assert storage.root == tmp_path.resolve()


def test_explicit_provider_overrides_the_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running the local filesystem provider on the AWS target must be possible."""
    monkeypatch.setenv(ENV_VAR, "aws")
    storage = StorageFactory.create(
        _FakeConfig({"storage.provider": "local", "storage.local.root": str(tmp_path)})
    )
    assert isinstance(storage, LocalStorage)


def test_unsupported_provider_raises() -> None:
    with pytest.raises(StorageError, match="azure_blob"):
        StorageFactory.create_from_dict({"provider": "azure_blob"})


def test_s3_bucket_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "aws")
    monkeypatch.setenv("S3_BUCKET_NAME", "env-bucket")
    monkeypatch.setattr(S3Storage, "_build_client", lambda self, region: object())
    storage = StorageFactory.create(_FakeConfig())
    assert isinstance(storage, S3Storage)
    assert storage.bucket == "env-bucket"


def test_missing_bucket_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "aws")
    with pytest.raises(StorageError, match="bucket"):
        StorageFactory.create(_FakeConfig())


def test_public_base_url_reaches_the_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setenv("STATIC_BASE_URL", "https://cdn.example.com")
    storage = StorageFactory.create(_FakeConfig({"storage.local.root": str(tmp_path)}))
    assert storage.public_url("data/a.jpg") == "https://cdn.example.com/data/a.jpg"


class _FakeGcsClient:
    """Stands in for `google.cloud.storage.Client` so no ADC lookup happens."""

    def bucket(self, name: str) -> object:
        return object()
