# Scrapy settings for mainz_adress_scraper project
#
# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://docs.scrapy.org/en/latest/topics/settings.html
#     https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://docs.scrapy.org/en/latest/topics/spider-middleware.html

BOT_NAME = "mainz_adress_scraper"

SPIDER_MODULES = ["mainz_adress_scraper.spiders"]
NEWSPIDER_MODULE = "mainz_adress_scraper.spiders"

ADDONS = {}


# Crawl responsibly by identifying yourself (and your website) on the user-agent
#USER_AGENT = "mainz_adress_scraper (+http://www.yourdomain.com)"

# Obey robots.txt rules
ROBOTSTXT_OBEY = True

# Mainz address directory spider (see spiders/adress_listing.py)
# Use the same listing URL as in the browser (text:mainz) so embedded config matches UI state.
MAINZ_LISTING_PAGE = (
    "https://www.mainz.de/en/verzeichnisse/adressverzeichnis/"
    "?b85e3fd0-1f6e-4984-81a3-4339fb0f0f49=%28text%3Amainz%29"
)
# Legacy single term if MAINZ_SEARCH_TEXTS is empty. Sent as SearchInput.text.
MAINZ_SEARCH_TEXT = "mainz"
# Each term runs the full category crawl; listing CSV column ``search_text``.
# ``mainz's`` omitted: Solr/UI overlap with ``mainz``.
MAINZ_SEARCH_TEXTS = (
    "mainz",
    "mainz.de",
    "mainzer",
    "mainz.kulinarisch",
    "mainz.regency",
)
# True = single unfiltered search only (fast smoke test).
MAINZ_BASELINE_ONLY = False
# 0 = no cap. With MAINZ_FILTER_MODE=tree, this caps address-directory branches only.
MAINZ_MAX_COMBINATIONS = 0
MAINZ_PAGE_SIZE = 100
# "tree" = Address Directory uiConfig only (fil-2); "cartesian" = legacy full facet cross-product.
MAINZ_FILTER_MODE = "tree"

# Concurrency and throttling settings
CONCURRENT_REQUESTS = 50
CONCURRENT_REQUESTS_PER_DOMAIN = 50
DOWNLOAD_DELAY = 1

# Disable cookies (enabled by default)
#COOKIES_ENABLED = False

# Disable Telnet Console (enabled by default)
#TELNETCONSOLE_ENABLED = False

# Override the default request headers:
#DEFAULT_REQUEST_HEADERS = {
#    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
#    "Accept-Language": "en",
#}

# Enable or disable spider middlewares
# See https://docs.scrapy.org/en/latest/topics/spider-middleware.html
#SPIDER_MIDDLEWARES = {
#    "mainz_adress_scraper.middlewares.MainzAdressScraperSpiderMiddleware": 543,
#}

# Enable or disable downloader middlewares
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#DOWNLOADER_MIDDLEWARES = {
#    "mainz_adress_scraper.middlewares.MainzAdressScraperDownloaderMiddleware": 543,
#}

# Enable or disable extensions
# See https://docs.scrapy.org/en/latest/topics/extensions.html
#EXTENSIONS = {
#    "scrapy.extensions.telnet.TelnetConsole": None,
#}

# Configure item pipelines
# See https://docs.scrapy.org/en/latest/topics/item-pipeline.html
#ITEM_PIPELINES = {
#    "mainz_adress_scraper.pipelines.MainzAdressScraperPipeline": 300,
#}

# Enable and configure the AutoThrottle extension (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/autothrottle.html
#AUTOTHROTTLE_ENABLED = True
# The initial download delay
#AUTOTHROTTLE_START_DELAY = 5
# The maximum download delay to be set in case of high latencies
#AUTOTHROTTLE_MAX_DELAY = 60
# The average number of requests Scrapy should be sending in parallel to
# each remote server
#AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
# Enable showing throttling stats for every response received:
#AUTOTHROTTLE_DEBUG = False

# Enable and configure HTTP caching (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#httpcache-middleware-settings
#HTTPCACHE_ENABLED = True
#HTTPCACHE_EXPIRATION_SECS = 0
#HTTPCACHE_DIR = "httpcache"
#HTTPCACHE_IGNORE_HTTP_CODES = []
#HTTPCACHE_STORAGE = "scrapy.extensions.httpcache.FilesystemCacheStorage"

# Set settings whose default value is deprecated to a future-proof value
FEED_EXPORT_ENCODING = "utf-8"
