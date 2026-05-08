# Scrapy settings for mainz_sportstaetten_scraper project

BOT_NAME = "mainz_sportstaetten_scraper"

SPIDER_MODULES = ["mainz_sportstaetten_scraper.spiders"]
NEWSPIDER_MODULE = "mainz_sportstaetten_scraper.spiders"

ADDONS = {}

ROBOTSTXT_OBEY = True

# Sportstättenverzeichnis (see spiders/sport_listing.py)
MAINZ_SPORT_LISTING_PAGE = (
    "https://www.mainz.de/angebote-entdecken/sport/Sportstaetten"
)
# True = single unfiltered search (no per-sport ``fil-4`` category), fast smoke test.
MAINZ_BASELINE_ONLY = False
# 0 = all facet categories (except the tree entry node). Otherwise cap after ordering.
MAINZ_MAX_CATEGORIES = 0
MAINZ_PAGE_SIZE = 100

CONCURRENT_REQUESTS = 50
CONCURRENT_REQUESTS_PER_DOMAIN = 50
DOWNLOAD_DELAY = 1

FEED_EXPORT_ENCODING = "utf-8"
