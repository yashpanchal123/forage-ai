BOT_NAME = 'ticketmaster_scraper'
SPIDER_MODULES = ['ticketmaster_scraper.spiders']
NEWSPIDER_MODULE = 'ticketmaster_scraper.spiders'

# Keep robots disabled unless you want strict compliance for your use case.
ROBOTSTXT_OBEY = False

# Reasonable defaults for scraping.
DOWNLOAD_DELAY = 1.0
RANDOMIZE_DOWNLOAD_DELAY = True
CONCURRENT_REQUESTS = 4
CONCURRENT_REQUESTS_PER_DOMAIN = 4

ITEM_PIPELINES = {
    'ticketmaster_scraper.pipelines.UrlLinesPipeline': 300,
}

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1
AUTOTHROTTLE_MAX_DELAY = 10
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0

# Quiet default (no Scrapy/Zyte banner spam). Use `scrapy crawl ... -L INFO` when debugging.
LOG_LEVEL = 'ERROR'

ADDONS = {}

FEED_EXPORT_ENCODING = "utf-8"

