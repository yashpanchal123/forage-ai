import os
from urllib.parse import quote

BOT_NAME = "eastwoodhomes_scraper"

SPIDER_MODULES = ["eastwoodhomes_scraper.spiders"]
NEWSPIDER_MODULE = "eastwoodhomes_scraper.spiders"

ROBOTSTXT_OBEY = False
CONCURRENT_REQUESTS = 12
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DOWNLOAD_DELAY = 1

ZYTE_API_KEY = os.environ.get("ZYTE_API_KEY", "7916eb9714394ae9a160c862c3e3da93").strip()
PROXY_URL = (
    f"http://{quote(ZYTE_API_KEY, safe='')}:@proxy.zyte.com:8011"
    if ZYTE_API_KEY
    else ""
)

DOWNLOADER_MIDDLEWARES = {
    "eastwoodhomes_scraper.middlewares.ProxyInjectionMiddleware": 350,
}

DEFAULT_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

FEED_EXPORT_ENCODING = "utf-8"
