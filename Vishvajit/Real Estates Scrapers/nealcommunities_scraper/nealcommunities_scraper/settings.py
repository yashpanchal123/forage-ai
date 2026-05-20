BOT_NAME = "nealcommunities_scraper"

SPIDER_MODULES = ["nealcommunities_scraper.spiders"]
NEWSPIDER_MODULE = "nealcommunities_scraper.spiders"

ROBOTSTXT_OBEY = False
CONCURRENT_REQUESTS = 8
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DOWNLOAD_DELAY = 1

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
