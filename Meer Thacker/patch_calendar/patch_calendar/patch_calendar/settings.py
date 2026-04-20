# Scrapy settings for patch_calendar project
# https://docs.scrapy.org/en/latest/topics/settings.html

BOT_NAME = "patch_calendar"

SPIDER_MODULES = ["patch_calendar.spiders"]
NEWSPIDER_MODULE = "patch_calendar.spiders"

# ── Identity ──────────────────────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

ROBOTSTXT_OBEY = False

# ── Concurrency & throttling ──────────────────────────────────────────────────

CONCURRENT_REQUESTS = 5
CONCURRENT_REQUESTS_PER_DOMAIN = 15
DOWNLOAD_DELAY = 0

# ── Retry ─────────────────────────────────────────────────────────────────────

RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 429]

# ── Pipelines ─────────────────────────────────────────────────────────────────
# PatchCalendarPipeline  → used by multi_city_listing spider
# EventDetailsPipeline   → used by event_details spider

ITEM_PIPELINES = {
    "patch_calendar.pipelines.PatchCalendarPipeline": 200,
}

# ── Feed / output ─────────────────────────────────────────────────────────────

FEED_EXPORT_ENCODING = "utf-8"

# Feed outputs (reruns overwrite files)
FEEDS = {
    # Details spider output
    "patch_events_full_data.json": {
        "format": "json",
        "encoding": "utf-8",
        "indent": 2,
        "overwrite": True,
    },
    # Optional: listing spider output (uncomment if you want Scrapy to write it)
    # "event_urls.csv": {
    #     "format": "csv",
    #     "overwrite": True,
    #     "item_filter": "patch_calendar.pipelines.ListingItemFilter",
    # },
}

# ── Request headers ───────────────────────────────────────────────────────────

DEFAULT_REQUEST_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
