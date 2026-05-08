from pathlib import Path

BOT_NAME = "gritt_scraper"

SPIDER_MODULES = ["gritt_scraper.spiders"]
NEWSPIDER_MODULE = "gritt_scraper.spiders"

# ---------------------------------------------------------------------------
# Crawl behaviour
# ---------------------------------------------------------------------------
ROBOTSTXT_OBEY = False

CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 16
# API is slow; keep delay off and rely on retries/backoff
DOWNLOAD_DELAY = 0.0
RANDOMIZE_DOWNLOAD_DELAY = False
DOWNLOAD_TIMEOUT = 90

# Retry on transient errors
RETRY_ENABLED = True
RETRY_TIMES = 8
RETRY_HTTP_CODES = [500, 502, 503, 504, 429]
RETRY_PRIORITY_ADJUST = -1

# ---------------------------------------------------------------------------
# Headers  (mirrors the original requests script)
# ---------------------------------------------------------------------------
DEFAULT_REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.gritt.io/",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------
ITEM_PIPELINES = {
    "gritt_scraper.pipelines.DuplicateFilterPipeline": 100,
    "gritt_scraper.pipelines.CsvInvestorsPipeline": 200,
}

# Output CSV path (fixed inside gritt_scraper/gritt_scraper)
INVESTORS_CSV_PATH = str((Path(__file__).resolve().parent / "gritt_scraper/investors.csv").resolve())

# ---------------------------------------------------------------------------
# Middlewares  (defaults kept; uncomment to customise)
# ---------------------------------------------------------------------------
# SPIDER_MIDDLEWARES = {
#     "gritt_scraper.middlewares.GrittScraperSpiderMiddleware": 543,
# }
# DOWNLOADER_MIDDLEWARES = {
#     "gritt_scraper.middlewares.GrittScraperDownloaderMiddleware": 543,
# }

# ---------------------------------------------------------------------------
# Output / feeds  (optional – pipelines above already write files)
# ---------------------------------------------------------------------------
# FEEDS = {
#     "output/investors_feed.csv": {"format": "csv"},
# }

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = "INFO"

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"