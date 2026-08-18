"""Google Cloud Storage implementation of :class:`~src.storage.base.ObjectStorage`."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from src.core.exceptions import ObjectNotFoundError, StorageError
from src.storage.base import ObjectStorage

logger = logging.getLogger("storage.gcs")

__all__ = ["GCSStorage"]

GCS_PUBLIC_HOST = "https://storage.googleapis.com"


class GCSStorage(ObjectStorage):
    """Object storage backed by a GCS bucket.

    Authentication uses Application Default Credentials — the attached
    service account on a GCE VM, or ``gcloud auth application-default login``
    locally. No key material is read from configuration.

    Args:
        bucket: Bucket name.
        project_id: Optional GCP project; falls back to the ADC project.
        client: Optional pre-built ``google.cloud.storage.Client``, for tests.
        public_base_url: Base URL objects are served from. Defaults to the
            bucket's public ``storage.googleapis.com`` URL.

    Raises:
        StorageError: If no bucket name is supplied.
    """

    def __init__(
        self,
        bucket: str,
        project_id: str | None = None,
        client: Any = None,
        public_base_url: str | None = None,
    ) -> None:
        if not bucket:
            raise StorageError("GCSStorage requires a bucket name")

        super().__init__(public_base_url=public_base_url or f"{GCS_PUBLIC_HOST}/{bucket}")
        self.bucket_name = bucket

        if client is not None:
            self._client = client
        else:
            from google.cloud import storage as gcs

            self._client = gcs.Client(project=project_id) if project_id else gcs.Client()

        self._bucket = self._client.bucket(bucket)
        logger.debug("GCSStorage initialised (bucket=%s, project=%s)", bucket, project_id)

    def download_bytes(self, key: str) -> bytes:
        """Read an object from GCS. See :meth:`ObjectStorage.download_bytes`."""
        from google.api_core.exceptions import GoogleAPICallError, NotFound

        try:
            data: bytes = self._bucket.blob(key).download_as_bytes()
            return data
        except NotFound as e:
            raise ObjectNotFoundError(f"GCS object not found: {key}", key=key) from e
        except GoogleAPICallError as e:
            raise StorageError(f"Failed to read gs://{self.bucket_name}/{key}: {e}") from e

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Write an object to GCS. See :meth:`ObjectStorage.upload_bytes`."""
        from google.api_core.exceptions import GoogleAPICallError

        blob = self._bucket.blob(key)
        if cache_control:
            blob.cache_control = cache_control

        try:
            blob.upload_from_string(data, content_type=content_type)
        except GoogleAPICallError as e:
            raise StorageError(f"Failed to write gs://{self.bucket_name}/{key}: {e}") from e

    def exists(self, key: str) -> bool:
        """Check for an object in GCS. See :meth:`ObjectStorage.exists`."""
        from google.api_core.exceptions import GoogleAPICallError

        try:
            return bool(self._bucket.blob(key).exists())
        except GoogleAPICallError as e:
            raise StorageError(f"Failed to stat gs://{self.bucket_name}/{key}: {e}") from e

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Iterate GCS keys under a prefix. See :meth:`ObjectStorage.list_keys`."""
        from google.api_core.exceptions import GoogleAPICallError

        try:
            for blob in self._client.list_blobs(self.bucket_name, prefix=prefix or None):
                yield str(blob.name)
        except GoogleAPICallError as e:
            raise StorageError(f"Failed to list gs://{self.bucket_name}/{prefix}: {e}") from e
