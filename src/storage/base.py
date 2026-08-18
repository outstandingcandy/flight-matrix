"""Object-storage abstraction shared by the aws, gcp and local targets.

The three deployment targets store aircraft images, scraped HTML and
thumbnails in S3, GCS and the local filesystem respectively. Callers depend
on this interface rather than on a cloud SDK, so a target switch is a factory
change instead of a code change.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator

__all__ = ["ObjectStorage"]

# Public URL shapes that may appear in stored image paths. Both S3 and GCS
# forms are recognised regardless of the active provider: after a target
# switch the same table legitimately holds paths written under either cloud.
_PUBLIC_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Virtual-hosted S3, with or without a region: bucket.s3[.region].amazonaws.com/key
    re.compile(r"^https?://[^/]+\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com/(?P<key>.+)$"),
    # Path-style S3: s3[.region].amazonaws.com/bucket/key
    re.compile(r"^https?://s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com/[^/]+/(?P<key>.+)$"),
    # GCS: storage.googleapis.com/bucket/key
    re.compile(r"^https?://storage\.googleapis\.com/[^/]+/(?P<key>.+)$"),
)

# Legacy substring form kept because existing rows store bare
# `bucket.s3.amazonaws.com/key` values without a scheme, which the anchored
# patterns above do not match.
_LEGACY_S3_MARKER = ".s3.amazonaws.com/"


class ObjectStorage(ABC):
    """Read/write access to a flat key-value object store.

    Args:
        public_base_url: Base URL that keys are publicly served from (a
            CloudFront domain, a GCS bucket URL, or empty for local
            development where the app serves the files itself).
    """

    def __init__(self, public_base_url: str | None = None) -> None:
        self._public_base_url = (public_base_url or "").rstrip("/")

    @abstractmethod
    def download_bytes(self, key: str) -> bytes:
        """Read an object's full contents.

        Args:
            key: Object key.

        Returns:
            The raw object bytes.

        Raises:
            ObjectNotFoundError: If the key does not exist.
            StorageError: If the read fails for any other reason.
        """

    @abstractmethod
    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Write an object, overwriting any existing value.

        Args:
            key: Object key.
            data: Raw bytes to store.
            content_type: Optional MIME type recorded with the object.
            cache_control: Optional ``Cache-Control`` header served with the
                object. Only meaningful for providers fronted by a CDN;
                filesystem storage ignores it.

        Raises:
            StorageError: If the write fails.
        """

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Report whether a key is present.

        Args:
            key: Object key.

        Returns:
            True when the object exists.

        Raises:
            StorageError: If the check fails for a reason other than absence.
        """

    @abstractmethod
    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Iterate over keys under a prefix.

        Implementations page lazily, so callers can stop early without paying
        for the full listing.

        Args:
            prefix: Key prefix to filter on; empty lists everything.

        Yields:
            Object keys in provider order.

        Raises:
            StorageError: If the listing fails.
        """

    def public_url(self, key: str) -> str:
        """Build the externally reachable URL for a key.

        Args:
            key: Object key.

        Returns:
            An absolute URL when a public base URL is configured, otherwise a
            root-relative path so local development works without a CDN.
        """
        normalised = key.lstrip("/")
        if self._public_base_url:
            return f"{self._public_base_url}/{normalised}"
        return f"/{normalised}"

    @staticmethod
    def strip_public_prefix(url: str) -> str:
        """Reduce a stored public URL back to its object key.

        Recognises both S3 and GCS URL shapes irrespective of which provider
        is currently active — stored paths outlive the target that wrote them.

        Args:
            url: A stored path, which may be a full public URL or already a
                bare key.

        Returns:
            The object key, or ``url`` unchanged when it matches no known
            public URL shape.
        """
        if not url:
            return url

        for pattern in _PUBLIC_URL_PATTERNS:
            match = pattern.match(url)
            if match:
                return match.group("key")

        if _LEGACY_S3_MARKER in url:
            return url.split(_LEGACY_S3_MARKER, 1)[1]

        return url
