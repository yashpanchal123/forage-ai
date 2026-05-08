BOT_NAME = "khov_scraper"

SPIDER_MODULES = ["khov_scraper.spiders"]
NEWSPIDER_MODULE = "khov_scraper.spiders"

ROBOTSTXT_OBEY = False

CONCURRENT_REQUESTS = 16
DOWNLOAD_DELAY = 0.2

FEED_EXPORT_ENCODING = "utf-8"

ITEM_PIPELINES = {
    "khov_scraper.pipelines.KhovScraperPipeline": 300,
}