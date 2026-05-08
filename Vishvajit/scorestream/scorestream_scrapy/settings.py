"""Scrapy settings for ScoreStream listing + detail crawl."""

from __future__ import annotations

import sys
from pathlib import Path

_BOTPKG = Path(__file__).resolve().parent.parent
if str(_BOTPKG) not in sys.path:
    sys.path.insert(0, str(_BOTPKG))

# Inlined defaults (no external config dependency).
DEFAULT_STATE_SLUGS: list[str] = [
    "georgia",
    "texas",
    "florida",
    "tennessee",
]
HIGH_SCHOOL_SPORT_BASE_SLUGS: list[str] = [
    "baseball",
    "basketball",
    "football",
    "soccer",
    "softball",
    "volleyball",
]

BOT_NAME = "scorestream_scrapy"
SPIDER_MODULES = ["scorestream_scrapy.spiders"]
NEWSPIDER_MODULE = "scorestream_scrapy.spiders"

ROBOTSTXT_OBEY = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CONCURRENT_REQUESTS = 8
DOWNLOAD_DELAY = 0.3
CONCURRENT_REQUESTS_PER_DOMAIN = 4

DEFAULT_REQUEST_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}

FEED_EXPORT_ENCODING = "utf-8"

SCORESTREAM_ALLOWED_DOMAINS = ["scorestream.com", "cdn-scorestream-com.s3.amazonaws.com"]
SCORESTREAM_BASE = "https://scorestream.com"
SCORESTREAM_API_URL = "https://scorestream.com/api"

# Listing: only these Explore state slugs by default.
SCORESTREAM_DEFAULT_STATE_SLUGS = DEFAULT_STATE_SLUGS

# High-school sports (base slugs; boys-/girls- variants matched in spider).
SCORESTREAM_SPORT_SLUGS_DEFAULT = HIGH_SCHOOL_SPORT_BASE_SLUGS

# Wider window helps when breaks / off-season yield few recent games.
SCORESTREAM_LISTING_DAYS_BACK_DEFAULT = 120

SCORESTREAM_SITE_ID = 124
SCORESTREAM_PROVIDER = "forage"
SCORESTREAM_MODULE = "sports"
SCORESTREAM_SOURCE_ID = "scorestream"
SCORESTREAM_SOURCE_NAME = "ScoreStream"
