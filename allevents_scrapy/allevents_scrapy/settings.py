"""Scrapy settings for AllEvents listing + detail crawl."""

from __future__ import annotations

import sys
from pathlib import Path

_BOTPKG = Path(__file__).resolve().parent.parent
if str(_BOTPKG) not in sys.path:
    sys.path.insert(0, str(_BOTPKG))

BOT_NAME = "allevents_scrapy"
SPIDER_MODULES = ["allevents_scrapy.spiders"]
NEWSPIDER_MODULE = "allevents_scrapy.spiders"

ROBOTSTXT_OBEY = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CONCURRENT_REQUESTS = 8
DOWNLOAD_DELAY = 0.35
CONCURRENT_REQUESTS_PER_DOMAIN = 4

DEFAULT_REQUEST_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}

FEED_EXPORT_ENCODING = "utf-8"
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# AllEvents spider static config
ALLEVENTS_ALLOWED_DOMAINS = [
    "allevents.in",
    "cdn2.allevents.in",
    "cdn5.allevents.in",
    "cdn-ip.allevents.in",
]
ALLEVENTS_COUNTRY = "united states"
ALLEVENTS_POPULAR = True
ALLEVENTS_LIST_PAGE_SIZE_DEFAULT = 100
ALLEVENTS_DETAIL_LIMIT_DEFAULT = 1000
ALLEVENTS_LIST_ONLY_DEFAULT = "0"
ALLEVENTS_CSV_PATH_DEFAULT = "event_links.csv"

ALLEVENTS_OG_TITLE_XPATH = '//meta[@property="og:title"]/@content'
ALLEVENTS_TWITTER_TITLE_XPATH = '//meta[@name="twitter:title"]/@content'
ALLEVENTS_DESCRIPTION_XPATH = '//div[@class="event-description-html"]//text()'
ALLEVENTS_DESCRIPTION_FALLBACK_XPATH = (
    '//div[contains(@class,"event-description-html")]//text()'
)

# Output paths (relative to cwd when you run scrapy, or absolute)
ALLEVENTS_CSV_PATH = "event_links.csv"
ALLEVENTS_JSON_PATH = "forage_events.json"
ALLEVENTS_ERRORS_JSON_PATH = "forage_events.errors.json"

ITEM_PIPELINES = {
    "allevents_scrapy.pipelines.ListingCsvPipeline": 300,
    "allevents_scrapy.pipelines.ForageJsonPipeline": 400,
}
