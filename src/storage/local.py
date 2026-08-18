"""Local filesystem implementation of :class:`~src.storage.base.ObjectStorage`.

Used by the ``local`` deployment target, and by the test suite so that
storage-dependent code can be exercised without touching a cloud provider.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from src.core.exceptions import ObjectNotFoundError, StorageError
from src.storage.base import ObjectStorage

logger = logging.getLogger("storage.local")

__all__ = ["LocalStorage"]

# The working directory, not "data/": object keys already carry their "data/"
# prefix on the cloud targets, so anchoring at the working directory keeps a
# stored key resolving identically under every provider.
DEFAULT_ROOT = "."


class LocalStorage(ObjectStorage):
    """Object storage backed by a directory tree.

    Keys map to paths relative to ``root``. Keys that would escape the root
    are rejected, so a hostile stored path cannot be used to read arbitrary
    files.

    Args:
        root: Directory that keys resolve against, defaulting to the process
            working directory. Relative paths are taken as relative to it.
        public_base_url: Base URL objects are served from; normally empty so
            the Flask app serves them itself.
    """

    def __init__(self, root: str | Path | None = None, public_base_url: str | None = None) -> None:
        super().__init__(public_base_url=public_base_url)
        self.root = Path(root or DEFAULT_ROOT).expanduser().resolve()
        logger.debug("LocalStorage initialised (root=%s)", self.root)

    def download_bytes(self, key: str) -> bytes:
        """Read a file. See :meth:`ObjectStorage.download_bytes`."""
        path = self._resolve(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as e:
            raise ObjectNotFoundError(f"Local object not found: {key}", key=key) from e
        except OSError as e:
            raise StorageError(f"Failed to read {path}: {e}") from e

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Write a file, creating parent directories.

        ``content_type`` and ``cache_control`` are accepted for interface
        compatibility and ignored: the filesystem records no HTTP metadata.

        See :meth:`ObjectStorage.upload_bytes`.
        """
        path = self._resolve(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as e:
            raise StorageError(f"Failed to write {path}: {e}") from e

    def exists(self, key: str) -> bool:
        """Check for a file. See :meth:`ObjectStorage.exists`."""
        return self._resolve(key).is_file()

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Walk files under a prefix. See :meth:`ObjectStorage.list_keys`.

        The prefix is treated as a key prefix rather than strictly as a
        directory, matching S3 and GCS semantics.
        """
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if not prefix or key.startswith(prefix):
                yield key

    def _resolve(self, key: str) -> Path:
        """Map a key to an absolute path inside the root.

        Args:
            key: Object key.

        Returns:
            The resolved absolute path.

        Raises:
            StorageError: If the key resolves outside the storage root.
        """
        candidate = (self.root / key.lstrip("/")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageError(f"Object key escapes the storage root: {key}")
        return candidate
