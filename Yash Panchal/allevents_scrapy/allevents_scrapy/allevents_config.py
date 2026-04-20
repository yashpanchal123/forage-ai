"""Shared settings for AllEvents listing + detail scrapers."""

from __future__ import annotations

from typing import Any

BASE = "https://allevents.in"
LIST_URL = f"{BASE}/api/events/list"

# Output shape (see format.json)
PROVIDER = "forage"
MODULE = "events"
SOURCE_NAME = "AllEvents"
SOURCE_ID = "allevents"
SOURCE_HOME = "https://allevents.in"

# City slug must match allevents.in URLs (e.g. allevents.in/atlanta).
# siteId / nearBy are inferred from scraped ``location.address`` in ``row_to_record``.
LOCATIONS: list[dict[str, Any]] = [
    {
        "name": "Atlanta, GA",
        "city_slug": "atlanta",
        "state": "GA",
        "tegna_market": True,
        "timezone": "America/New_York",
    },
    {
        "name": "Austin, TX",
        "city_slug": "austin",
        "state": "TX",
        "tegna_market": True,
        "timezone": "America/Chicago",
    },
    {
        "name": "Orlando, FL",
        "city_slug": "orlando",
        "state": "FL",
        "tegna_market": False,
        "timezone": "America/New_York",
    },
    {
        "name": "Nashville, TN",
        "city_slug": "nashville",
        "state": "TN",
        "tegna_market": False,
        "timezone": "America/Chicago",
    },
]

DEFAULT_LISTING_CSV = "event_links.csv"
DEFAULT_OUTPUT_JSON = "forage_events.json"
