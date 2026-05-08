"""
Tavily Search client for web searches.

Provides a clean interface to the Tavily AI Search API.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("tavily_search")

# Configuration constants
DEFAULT_MAX_RESULTS = 5
MAX_CONTENT_LENGTH = 1000
EXCLUDED_DOMAINS = [
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
]


class TavilySearchClient:
    """Tavily AI Search API client.

    Provides methods for searching web content using the Tavily API.
    Supports both basic and advanced search depths.
    """

    def __init__(self, api_key: str | None = None):
        """Initialize Tavily Search client.

        Args:
            api_key: Tavily API key. Falls back to TAVILY_API_KEY env var.

        Raises:
            ValueError: If no API key is provided or found
            ImportError: If tavily-python package is not installed
        """
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        if not self.api_key:
            raise ValueError("Tavily API key is required. Set TAVILY_API_KEY environment variable.")

        try:
            from tavily import TavilyClient

            self.client = TavilyClient(api_key=self.api_key)
            logger.info("Tavily Search client initialized")
        except ImportError:
            raise ImportError(
                "tavily-python package required. Install with: pip install tavily-python"
            )

    def search(
        self,
        query: str,
        search_depth: str = "basic",
        max_results: int = DEFAULT_MAX_RESULTS,
        include_answer: bool = True,
        include_raw_content: bool = False,
    ) -> dict:
        """Execute a search query.

        Args:
            query: Search query string
            search_depth: "basic" or "advanced"
            max_results: Maximum number of results
            include_answer: Include AI-generated answer
            include_raw_content: Include raw page content

        Returns:
            Dictionary containing search results
        """
        logger.info(f"Searching Tavily ({search_depth}): {query}")

        try:
            response = self.client.search(
                query=query,
                search_depth=search_depth,
                include_images=False,
                include_answer=include_answer,
                include_raw_content=include_raw_content,
                max_results=max_results,
                exclude_domains=EXCLUDED_DOMAINS,
            )

            result_count = len(response.get("results", []))
            logger.info(f"Tavily search completed, found {result_count} results")
            return response

        except Exception as e:
            logger.error(f"Tavily search failed: {e}")
            return {"results": [], "answer": f"Search failed: {e!s}"}

    def search_and_format(
        self,
        query: str,
        context: str = "",
        search_depth: str = "basic",
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> str:
        """Search and return formatted results.

        Args:
            query: Search query string
            context: Context label for the results
            search_depth: "basic" or "advanced"
            max_results: Maximum number of results

        Returns:
            Formatted search results as string
        """
        response = self.search(query=query, search_depth=search_depth, max_results=max_results)
        return self.format_results(response, context or query)

    def format_results(self, response: dict, context: str) -> str:
        """Format search results into readable text.

        Args:
            response: Tavily API response
            context: Context label for the results

        Returns:
            Formatted string with search results
        """
        lines = [f"=== Search Results: {context} ===\n"]

        # Include AI-generated answer
        if response.get("answer"):
            lines.append(f"Summary:\n{response['answer']}\n")

        # Include individual results
        results = response.get("results", [])
        if results:
            lines.append("Sources:")
            for i, result in enumerate(results, 1):
                title = result.get("title", "No title")
                content = result.get("content", "")
                url = result.get("url", "")

                # Truncate long content
                if len(content) > MAX_CONTENT_LENGTH:
                    content = content[:MAX_CONTENT_LENGTH] + "..."

                lines.append(f"\n{i}. {title}")
                if content:
                    lines.append(f"   {content}")
                if url:
                    lines.append(f"   Source: {url}")
        else:
            lines.append("No results found.")

        return "\n".join(lines)

    def test_connection(self) -> bool:
        """Test the Tavily API connection.

        Returns:
            True if connection is working
        """
        try:
            response = self.search("test query", max_results=1)
            return bool(response.get("results") or response.get("answer"))
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False
