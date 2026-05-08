import os

BOT_NAME = "bandsintown_scraper"

SPIDER_MODULES = ["bandsintown_scraper.spiders"]
NEWSPIDER_MODULE = "bandsintown_scraper.spiders"

# ── Concurrency ───────────────────────────────────────────────────────────────
# BandsintownMiddleware uses deferToThread; parallel fetches are capped by
# REACTOR_THREADPOOL_MAXSIZE unless raised here.
CONCURRENT_REQUESTS = 18
CONCURRENT_REQUESTS_PER_DOMAIN = 15
DOWNLOAD_DELAY = 0.5   # seconds between starts to same domain (tunable)
RANDOMIZE_DOWNLOAD_DELAY = True
REACTOR_THREADPOOL_MAXSIZE = 24

# ── Pipelines ─────────────────────────────────────────────────────────────────
# DB pipeline removed. Use per-spider FEEDS (CSV exports) instead.
ITEM_PIPELINES = {}

# ── Geonode proxy (BandsintownMiddleware / cloudscraper) ─────────────────────
# GEONODE_ONLY=True (default): Geonode-only fetches for details spider.
# Local/dev without proxy: scrapy crawl ... -s GEONODE_ONLY=False
# Env vars override the defaults below (use env on shared machines / CI).
GEONODE_ONLY = True
GEONODE_PROXY_HOST = os.environ.get("GEONODE_PROXY_HOST", "proxy.geonode.io:9000")
GEONODE_PROXY_USER = os.environ.get("GEONODE_PROXY_USER", "crawlmagic")
GEONODE_PROXY_PASSWORD = os.environ.get(
    "GEONODE_PROXY_PASSWORD",
    "e6e10ef0-41fa-4b5d-9288-3902c239347f",
)
# Residential proxies often need longer reads than direct; raise if you still see timeouts.
GEONODE_HTTP_TIMEOUT = float(os.environ.get("GEONODE_HTTP_TIMEOUT") or "55")

# ── Middleware ────────────────────────────────────────────────────────────────
DOWNLOADER_MIDDLEWARES = {
    "bandsintown_scraper.middlewares.BandsintownMiddleware": 543,
}

# ── HTTP / Robot settings ─────────────────────────────────────────────────────
ROBOTSTXT_OBEY = False
COOKIES_ENABLED = True
DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ── Retry ─────────────────────────────────────────────────────────────────────
RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [429, 500, 502, 503, 504]

# ── Timeouts ──────────────────────────────────────────────────────────────────
DOWNLOAD_TIMEOUT = 60

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# ── Feed exports (optional CSV / JSON side-output) ────────────────────────────
# Uncomment to also save a JSON file:
# FEEDS = {
#     "events_%(time)s.json": {"format": "json", "encoding": "utf-8"},
# }

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
