"""Aviation-scraper result sinks.

Each sink owns the DB-write side of one scraper type. Sinks are bound to
their scrapers in ``src/scraper_main.py`` at worker startup via:

- ``scraper.on_success`` / ``on_failure`` for post-scrape side effects, and
- specific callback config keys (``persist_aircraft_callback``,
  ``persist_images_callback``, ``add_task_callback``, ...) for flow-control
  hooks that the scraper invokes mid-run.

Sinks stay in flight-matrix so the submodule never imports application tables.
"""

from src.scraper.sinks.airport_data_sink import AirportDataSink
from src.scraper.sinks.base import Sink, bind_sink
from src.scraper.sinks.fr24_aircraft_sink import FR24AircraftSink
from src.scraper.sinks.fr24_airport_sink import FR24AirportSink
from src.scraper.sinks.fr24_map_sink import FR24MapSink
from src.scraper.sinks.jetphotos_sink import JetPhotosSink

__all__ = [
    "AirportDataSink",
    "FR24AircraftSink",
    "FR24AirportSink",
    "FR24MapSink",
    "JetPhotosSink",
    "Sink",
    "bind_sink",
]
