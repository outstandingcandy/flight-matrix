"""
Base search client protocol and interfaces.

This module defines the abstract interface that all search providers
must implement, enabling consistent usage across different backends.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SearchResult:
    """Represents a single search result.

    Attributes:
        title: Title of the result
        url: URL of the source
        content: Text content or snippet
        score: Optional relevance score (0.0 to 1.0)
        raw_content: Optional full raw content
    """

    title: str
    url: str
    content: str
    score: float | None = None
    raw_content: str | None = None


@dataclass
class SearchResponse:
    """Represents a search response containing multiple results.

    Attributes:
        query: The original search query
        results: List of search results
        answer: Optional direct answer from search provider
        follow_up_questions: Optional suggested follow-up questions
    """

    query: str
    results: list[SearchResult]
    answer: str | None = None
    follow_up_questions: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert response to dictionary format."""
        return {
            "query": self.query,
            "results": [
                {"title": r.title, "url": r.url, "content": r.content, "score": r.score}
                for r in self.results
            ],
            "answer": self.answer,
            "follow_up_questions": self.follow_up_questions,
        }


class SearchClient(ABC):
    """Abstract base class for search clients.

    All search provider implementations must inherit from this class
    and implement the required methods.
    """

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        """Execute a general search query.

        Args:
            query: The search query string
            max_results: Maximum number of results to return

        Returns:
            SearchResponse containing the results

        Raises:
            SearchError: If the search fails
        """
        pass

    @abstractmethod
    def search_aircraft_info(
        self,
        query: str,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search for aircraft-specific information.

        Optimized search for aviation-related queries with optional
        domain filtering.

        Args:
            query: The search query (e.g., aircraft registration, type)
            max_results: Maximum number of results to return
            include_domains: Optional list of domains to prioritize
            exclude_domains: Optional list of domains to exclude

        Returns:
            Dictionary containing search results and metadata

        Raises:
            SearchError: If the search fails
        """
        pass

    @abstractmethod
    def test_connection(self) -> bool:
        """Test the connection to the search provider.

        Returns:
            True if connection is successful, False otherwise
        """
        pass

    def is_available(self) -> bool:
        """Check if the search client is available and configured.

        Returns:
            True if the client can be used, False otherwise
        """
        try:
            return self.test_connection()
        except Exception:
            return False


class NullSearchClient(SearchClient):
    """Null implementation of SearchClient.

    Used as a fallback when no search provider is configured.
    Returns empty results without errors.
    """

    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        """Return empty search response."""
        return SearchResponse(query=query, results=[])

    def search_aircraft_info(
        self,
        query: str,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return empty aircraft info response."""
        return {"query": query, "results": [], "answer": None}

    def test_connection(self) -> bool:
        """Always returns True for null client."""
        return True
