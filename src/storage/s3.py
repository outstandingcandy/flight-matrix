"""Amazon S3 implementation of :class:`~src.storage.base.ObjectStorage`."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from src.core.exceptions import ObjectNotFoundError, StorageError
from src.storage.base import ObjectStorage

logger = logging.getLogger("storage.s3")

__all__ = ["S3Storage"]

_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


class S3Storage(ObjectStorage):
    """Object storage backed by an S3 bucket.

    Args:
        bucket: Bucket name.
        region: Optional AWS region; falls back to the boto3 default chain.
        client: Optional pre-built boto3 S3 client, primarily for tests and
            for call sites that already hold a client configured with
            explicit credentials.
        public_base_url: Base URL objects are served from (usually the
            CloudFront domain).

    Raises:
        StorageError: If no bucket name is supplied.
    """

    def __init__(
        self,
        bucket: str,
        region: str | None = None,
        client: Any = None,
        public_base_url: str | None = None,
    ) -> None:
        super().__init__(public_base_url=public_base_url)
        if not bucket:
            raise StorageError("S3Storage requires a bucket name")

        self.bucket = bucket
        self._client = client if client is not None else self._build_client(region)

        logger.debug("S3Storage initialised (bucket=%s, region=%s)", bucket, region)

    @staticmethod
    def _build_client(region: str | None) -> Any:
        """Create a boto3 S3 client from the ambient credential chain.

        Args:
            region: Optional region override.

        Returns:
            A boto3 S3 client.
        """
        import boto3

        return boto3.client("s3", region_name=region) if region else boto3.client("s3")

    def download_bytes(self, key: str) -> bytes:
        """Read an object from S3. See :meth:`ObjectStorage.download_bytes`."""
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            data: bytes = response["Body"].read()
            return data
        except ClientError as e:
            if _is_not_found(e):
                raise ObjectNotFoundError(f"S3 object not found: {key}", key=key) from e
            raise StorageError(f"Failed to read s3://{self.bucket}/{key}: {e}") from e

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Write an object to S3. See :meth:`ObjectStorage.upload_bytes`."""
        from botocore.exceptions import ClientError

        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        if cache_control:
            kwargs["CacheControl"] = cache_control

        try:
            self._client.put_object(**kwargs)
        except ClientError as e:
            raise StorageError(f"Failed to write s3://{self.bucket}/{key}: {e}") from e

    def exists(self, key: str) -> bool:
        """Check for an object in S3. See :meth:`ObjectStorage.exists`."""
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if _is_not_found(e):
                return False
            raise StorageError(f"Failed to stat s3://{self.bucket}/{key}: {e}") from e

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Iterate S3 keys under a prefix. See :meth:`ObjectStorage.list_keys`."""
        from botocore.exceptions import ClientError

        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    yield str(obj["Key"])
        except ClientError as e:
            raise StorageError(f"Failed to list s3://{self.bucket}/{prefix}: {e}") from e


def _is_not_found(error: Any) -> bool:
    """Return True when a boto3 ClientError represents a missing object.

    Args:
        error: The raised ``ClientError``.

    Returns:
        True when the error code or HTTP status indicates absence.
    """
    response = getattr(error, "response", {}) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = str(response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code in _NOT_FOUND_CODES or status == "404"
