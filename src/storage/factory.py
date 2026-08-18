"""Object-storage factory.

Creates the :class:`~src.storage.base.ObjectStorage` implementation for the
active deployment target. Mirrors
:class:`~src.notifications.factory.EmailNotifierFactory` so both provider
factories read the same way.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from src.core.deploy_target import default_storage_provider, resolve_provider
from src.core.exceptions import StorageError
from src.storage.base import ObjectStorage
from src.storage.gcs import GCS_PUBLIC_HOST, GCSStorage
from src.storage.local import LocalStorage
from src.storage.s3 import S3Storage

if TYPE_CHECKING:
    from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("storage.factory")

__all__ = [
    "StorageFactory",
    "get_storage",
    "reset_storage",
    "resolve_media_base_url",
    "resolve_static_base_url",
]

_storage: ObjectStorage | None = None

# Media and static assets are resolved separately because they are different
# kinds of thing that only happen to share a host on aws.
#
# Media is data: the scraped aircraft images under `data/`, written by the
# scrapers, over a terabyte of it, and never shipped with the code. It has to
# come from object storage on any cloud target.
#
# Static assets are build artefacts of the deployed commit: four CSS and six JS
# files in `web_static/`, which must match the templates referencing them and
# which travel with the deploy.
#
# One resolver for both meant that configuring a bucket for the images also
# pointed the templates at that bucket, where nothing had published `web_static/`.
# The deploy still succeeded and `/` still returned 200 -- only a browser saw the
# unstyled page.


def resolve_media_base_url() -> str:
    """Return the base URL that objects in object storage are served from.

    Resolution order:

    1. ``MEDIA_BASE_URL`` — the target-neutral variable (a full URL).
    2. ``STATIC_BASE_URL`` — one variable used to cover both kinds of asset,
       so deployments configured that way keep working.
    3. ``CLOUDFRONT_DOMAIN`` — the existing AWS deployment, unchanged.
    4. ``GCS_ASSETS_BUCKET`` — derives the public GCS bucket URL.
    5. Empty string, meaning the app serves the files itself.

    Returns:
        A base URL without a trailing slash, or an empty string.
    """
    media_base = os.environ.get("MEDIA_BASE_URL", "").strip()
    if media_base:
        return media_base.rstrip("/")

    static_base = os.environ.get("STATIC_BASE_URL", "").strip()
    if static_base:
        return static_base.rstrip("/")

    cloudfront_domain = os.environ.get("CLOUDFRONT_DOMAIN", "").strip()
    if cloudfront_domain:
        return f"https://{cloudfront_domain}"

    gcs_bucket = os.environ.get("GCS_ASSETS_BUCKET", "").strip()
    if gcs_bucket:
        return f"{GCS_PUBLIC_HOST}/{gcs_bucket}"

    return ""


def resolve_static_base_url() -> str:
    """Return the base URL the application's own CSS and JavaScript come from.

    Resolution order:

    1. ``STATIC_BASE_URL`` — a full URL. Set this only when something actually
       publishes ``web_static/`` there.
    2. ``CLOUDFRONT_DOMAIN`` — the existing AWS deployment, where the CDK stack
       syncs ``web_static/`` to the bucket CloudFront fronts.
    3. Empty string, meaning Flask serves them from ``web_static/`` itself.

    ``GCS_ASSETS_BUCKET`` is deliberately not consulted: holding the scrapers'
    output says nothing about whether this commit's assets were published there.
    Falling back to serving them from the app is always correct, if not always
    the fastest.

    Returns:
        A base URL without a trailing slash, or an empty string.
    """
    static_base = os.environ.get("STATIC_BASE_URL", "").strip()
    if static_base:
        return static_base.rstrip("/")

    cloudfront_domain = os.environ.get("CLOUDFRONT_DOMAIN", "").strip()
    if cloudfront_domain:
        return f"https://{cloudfront_domain}"

    return ""


class StorageFactory:
    """Factory for creating object-storage instances."""

    @staticmethod
    def create(
        yaml_config: YAMLConfig,
        bucket: str | None = None,
        client: Any = None,
    ) -> ObjectStorage:
        """Create object storage from configuration.

        Args:
            yaml_config: YAML configuration manager.
            bucket: Overrides the configured bucket. For call sites that keep
                their own bucket key (for example ``image_download.s3.bucket``)
                rather than reading ``storage.*``.
            client: Overrides the provider SDK client. For call sites that
                build one with explicit credentials.

        Returns:
            Configured object-storage instance.

        Raises:
            StorageError: If the provider is unsupported or its required
                settings are missing.
        """
        configured = yaml_config.get("storage.provider", "")
        provider = resolve_provider(configured, default_storage_provider())
        public_base_url = yaml_config.get("storage.public_base_url", "") or resolve_media_base_url()

        if provider == "s3":
            return S3Storage(
                bucket=bucket
                or yaml_config.get("storage.s3.bucket", "")
                or os.environ.get("S3_BUCKET_NAME", ""),
                region=yaml_config.get("storage.s3.region", "") or None,
                client=client,
                public_base_url=public_base_url,
            )
        if provider == "gcs":
            return GCSStorage(
                bucket=bucket
                or yaml_config.get("storage.gcs.bucket", "")
                or os.environ.get("GCS_ASSETS_BUCKET", ""),
                project_id=yaml_config.get("storage.gcs.project_id", "") or None,
                client=client,
                public_base_url=public_base_url or None,
            )
        if provider == "local":
            return LocalStorage(
                root=yaml_config.get("storage.local.root", "") or None,
                public_base_url=public_base_url,
            )

        raise StorageError(f"Unsupported storage provider: {provider}")

    @staticmethod
    def create_from_dict(config: dict[str, Any]) -> ObjectStorage:
        """Create object storage directly from a dictionary.

        Useful for tests and for call sites that do not hold a ``YAMLConfig``.

        Args:
            config: Mapping with a ``provider`` key plus provider-specific
                settings (``bucket``, ``region``, ``project_id``, ``root``,
                ``client``, ``public_base_url``). An empty or absent
                ``provider`` resolves from the deployment target.

        Returns:
            Configured object-storage instance.

        Raises:
            StorageError: If the provider is unsupported.
        """
        provider = resolve_provider(config.get("provider"), default_storage_provider())
        public_base_url = config.get("public_base_url") or resolve_media_base_url()

        if provider == "s3":
            return S3Storage(
                bucket=config.get("bucket", ""),
                region=config.get("region"),
                client=config.get("client"),
                public_base_url=public_base_url,
            )
        if provider == "gcs":
            return GCSStorage(
                bucket=config.get("bucket", ""),
                project_id=config.get("project_id"),
                client=config.get("client"),
                public_base_url=config.get("public_base_url"),
            )
        if provider == "local":
            return LocalStorage(
                root=config.get("root"),
                public_base_url=public_base_url,
            )

        raise StorageError(f"Unsupported storage provider: {provider}")


def get_storage(yaml_config: YAMLConfig | None = None) -> ObjectStorage:
    """Return the process-wide object-storage instance, creating it on demand.

    Args:
        yaml_config: Configuration manager used on first call. When omitted, a
            :class:`~src.utils.yaml_config.YAMLConfig` is constructed.

    Returns:
        The shared object-storage instance.
    """
    global _storage

    if _storage is None:
        if yaml_config is None:
            from src.utils.yaml_config import YAMLConfig

            yaml_config = YAMLConfig()
        _storage = StorageFactory.create(yaml_config)
        logger.info("Object storage initialised: %s", type(_storage).__name__)

    return _storage


def reset_storage() -> None:
    """Discard the cached instance so the next call re-reads configuration.

    Intended for tests that switch ``DEPLOY_TARGET`` between cases.
    """
    global _storage
    _storage = None
