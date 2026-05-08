"""
ReExtractor for extracting fields from saved HTML files.

Enables batch re-extraction of fields from HTML files stored in S3,
useful when extraction logic changes or new fields are added.
"""

import gzip
import logging
from collections.abc import Iterator
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.scraper.extractors import AirportDataExtractor, JetPhotosExtractor
from src.scraper.extractors.base import BaseExtractor

logger = logging.getLogger("scraper.reextractor")


class ReExtractor:
    """Re-extract fields from saved HTML files in S3.

    Supports batch processing of HTML files stored in S3, enabling
    re-extraction when extraction logic changes or new fields are added.

    Example:
        ```python
        reextractor = ReExtractor(s3_bucket="my-bucket")

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

    def __init__(
        self,
        s3_bucket: str,
        s3_client: Any = None,
    ) -> None:
        """Initialize the ReExtractor.

        Args:
            s3_bucket: S3 bucket name containing HTML files.
            s3_client: Optional boto3 S3 client (created if not provided).
        """
        self.s3_bucket = s3_bucket
        self.s3_client = s3_client or boto3.client("s3")

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

    def download_html(self, s3_key: str) -> str:
        """Download HTML content from S3.

        Args:
            s3_key: S3 object key (path within bucket).

        Returns:
            HTML content as string.

        Raises:
            FileNotFoundError: If the S3 object does not exist.
            RuntimeError: If download fails.
        """
        try:
            response = self.s3_client.get_object(Bucket=self.s3_bucket, Key=s3_key)
            content = response["Body"].read()

            # Handle gzip-compressed content
            if s3_key.endswith(".gz"):
                content = gzip.decompress(content)

            return content.decode("utf-8")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchKey":
                raise FileNotFoundError(
                    f"HTML file not found: s3://{self.s3_bucket}/{s3_key}"
                ) from e
            raise RuntimeError(f"Failed to download HTML: {e}") from e

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
        """List HTML files in S3 under a given prefix.

        Args:
            prefix: S3 key prefix to search under.
            max_files: Maximum number of files to return.

        Returns:
            List of S3 keys for HTML files.
        """
        html_files: list[str] = []
        paginator = self.s3_client.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=self.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".html") or key.endswith(".html.gz"):
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
