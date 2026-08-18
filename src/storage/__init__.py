"""Object-storage providers for the aws, gcp and local deployment targets.

Import :class:`ObjectStorage` for type annotations and
:class:`StorageFactory` (or :func:`get_storage`) to obtain an instance; avoid
importing the concrete provider modules outside of the factory so call sites
stay target-agnostic.
"""

from src.storage.base import ObjectStorage
from src.storage.factory import (
    StorageFactory,
    get_storage,
    reset_storage,
    resolve_media_base_url,
    resolve_static_base_url,
)
from src.storage.gcs import GCSStorage
from src.storage.local import LocalStorage
from src.storage.s3 import S3Storage

__all__ = [
    "GCSStorage",
    "LocalStorage",
    "ObjectStorage",
    "S3Storage",
    "StorageFactory",
    "get_storage",
    "reset_storage",
    "resolve_media_base_url",
    "resolve_static_base_url",
]
