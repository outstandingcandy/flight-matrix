"""
Local task sources for different scraper types.

Each task source provides tasks for a specific scraper type in local mode.
"""

from src.scraper.sources.cli_source import CLITaskSource
from src.scraper.sources.fr24_airport_source import FR24AirportTaskSource
from src.scraper.sources.fr24_map_source import FR24MapTaskSource
from src.scraper.sources.jetphotos_source import JetPhotosTaskSource
from src.scraper.sources.queue_source import QueueTaskSource
from src.scraper.sources.xiaohongshu_source import (
    XiaohongshuAuthorSource,
    XiaohongshuRegistrationSource,
)

__all__ = [
    "CLITaskSource",
    "FR24AirportTaskSource",
    "FR24MapTaskSource",
    "JetPhotosTaskSource",
    "QueueTaskSource",
    "XiaohongshuAuthorSource",
    "XiaohongshuRegistrationSource",
]
