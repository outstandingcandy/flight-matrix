"""
ReExtractor for extracting fields from saved HTML files.

Enables batch re-extraction of fields from HTML files held in object storage
(S3, GCS or the local filesystem depending on `DEPLOY_TARGET`), useful when
extraction logic changes or new fields are added.
"""

import gzip
import logging
from collections.abc import Iterator
from typing import Any

from resilient_scraper.extractors.base import BaseExtractor
from resilient_scraper.scrapers.aviation.airport_data.extractor import AirportDataExtractor
from resilient_scraper.scrapers.aviation.jetphotos.extractor import JetPhotosExtractor

from src.core.exceptions import ObjectNotFoundError, StorageError
from src.storage.base import ObjectStorage

logger = logging.getLogger("scraper.reextractor")


class ReExtractor:
    """Re-extract fields from saved HTML files in object storage.

    Supports batch processing of stored HTML files, enabling re-extraction
    when extraction logic changes or new fields are added.

    Example:
        ```python
        from src.storage import StorageFactory

        reextractor = ReExtractor(storage=StorageFactory.create(config))

        # Single file re-extraction
        fields = reextractor.reextract(
            source="jetphotos",
            html_path="data/jetphotos_images/html/12345678.html",
            context={"source_url": "https://www.jetphotos.com/photo/12345678"}
        )

        # Batch re-extraction
        for result in reextractor.batch_reextract("jetphotos", html_paths):
            print(f"Extracted: {result}")
        ```
    """

    # Mapping of source names to extractor classes
    EXTRACTORS: dict[str, type[BaseExtractor]] = {
        "jetphotos": JetPhotosExtractor,
        "airport_data": AirportDataExtractor,
    }

    def __init__(self, storage: ObjectStorage) -> None:
        """Initialize the ReExtractor.

        Args:
            storage: Object storage holding the saved HTML files.
        """
        self.storage = storage

        # Initialize extractors
        self.extractors: dict[str, BaseExtractor] = {
            name: extractor_cls() for name, extractor_cls in self.EXTRACTORS.items()
        }

    def get_extractor(self, source: str) -> BaseExtractor:
        """Get the extractor for a given source.

        Args:
            source: Data source type ('jetphotos', 'airport_data').

        Returns:
            The extractor instance for the source.

        Raises:
            ValueError: If source is not supported.
        """
        if source not in self.extractors:
            raise ValueError(f"Unknown source: {source}. Supported: {list(self.extractors.keys())}")
        return self.extractors[source]

    def download_html(self, key: str) -> str:
        """Download HTML content from object storage.

        Args:
            key: Object key (path within the bucket or storage root).

        Returns:
            HTML content as string.

        Raises:
            FileNotFoundError: If the object does not exist.
            RuntimeError: If download fails.
        """
        try:
            content = self.storage.download_bytes(key)
        except ObjectNotFoundError as e:
            raise FileNotFoundError(f"HTML file not found: {key}") from e
        except StorageError as e:
            raise RuntimeError(f"Failed to download HTML: {e}") from e

        # Handle gzip-compressed content
        if key.endswith(".gz"):
            content = gzip.decompress(content)

        return content.decode("utf-8")

    def reextract(
        self,
        source: str,
        html_path: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Re-extract fields from a single HTML file.

        Args:
            source: Data source type ('jetphotos', 'airport_data').
            html_path: S3 path to the HTML file (key within bucket).
            context: Optional context dictionary for extraction.

        Returns:
            Dictionary of extracted fields.

        Raises:
            ValueError: If source is not supported.
            FileNotFoundError: If HTML file does not exist.
        """
        extractor = self.get_extractor(source)
        html = self.download_html(html_path)
        return extractor.extract(html, context)

    def reextract_safe(
        self,
        source: str,
        html_path: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Re-extract fields with error handling.

        Args:
            source: Data source type ('jetphotos', 'airport_data').
            html_path: S3 path to the HTML file.
            context: Optional context dictionary.

        Returns:
            Tuple of (extracted_fields, error_messages).
        """
        errors: list[str] = []
        try:
            extractor = self.get_extractor(source)
            html = self.download_html(html_path)
            return extractor.extract_safe(html, context)
        except FileNotFoundError as e:
            errors.append(f"FileNotFoundError: {e}")
            return {}, errors
        except Exception as e:
            errors.append(f"Error: {type(e).__name__}: {e}")
            return {}, errors

    def batch_reextract(
        self,
        source: str,
        html_paths: list[str],
        contexts: list[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Batch re-extract fields from multiple HTML files.

        Yields results as they are processed, allowing for streaming
        processing of large batches.

        Args:
            source: Data source type ('jetphotos', 'airport_data').
            html_paths: List of S3 paths to HTML files.
            contexts: Optional list of context dictionaries (one per path).

        Yields:
            Dictionary with extracted fields and metadata:
            {
                "html_path": str,
                "success": bool,
                "fields": dict[str, Any],
                "errors": list[str],
            }
        """
        extractor = self.get_extractor(source)
        contexts = contexts or [{}] * len(html_paths)

        for i, html_path in enumerate(html_paths):
            context = contexts[i] if i < len(contexts) else {}
            result = {
                "html_path": html_path,
                "success": False,
                "fields": {},
                "errors": [],
            }

            try:
                html = self.download_html(html_path)
                fields, errors = extractor.extract_safe(html, context)
                result["fields"] = fields
                result["errors"] = errors
                result["success"] = not errors
            except FileNotFoundError:
                error_list: list[str] = result["errors"]  # type: ignore[assignment]
                error_list.append(f"File not found: {html_path}")
            except Exception as e:
                error_list: list[str] = result["errors"]  # type: ignore[assignment]
                error_list.append(f"Error: {type(e).__name__}: {e}")

            yield result

    def list_html_files(
        self,
        prefix: str,
        max_files: int = 1000,
    ) -> list[str]:
        """List HTML files in object storage under a given prefix.

        Args:
            prefix: Key prefix to search under.
            max_files: Maximum number of files to return.

        Returns:
            List of object keys for HTML files.
        """
        html_files: list[str] = []

        for key in self.storage.list_keys(prefix):
            if key.endswith((".html", ".html.gz")):
                html_files.append(key)
                if len(html_files) >= max_files:
                    return html_files

        return html_files

    def get_version_info(self) -> dict[str, dict[str, str]]:
        """Get version information for all extractors.

        Returns:
            Dictionary mapping source names to version info.
        """
        return {
            source: extractor.get_version_info() for source, extractor in self.extractors.items()
        }
