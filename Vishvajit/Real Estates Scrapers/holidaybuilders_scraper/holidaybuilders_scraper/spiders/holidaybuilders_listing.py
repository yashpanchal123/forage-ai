import re
from urllib.parse import urldefrag

import scrapy

from ..items import HolidayBuildersListingItem

SITEMAP_URL = "https://holidaybuilders.com/homes-sitemap.xml"


class HolidayBuildersListingSpider(scrapy.Spider):
    name = "holidaybuilders_listing"
    allowed_domains = ["holidaybuilders.com", "www.holidaybuilders.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [404, 500, 502, 503],
        "FEEDS": {
            "holidaybuilders_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["house_url", "lastmod"],
            }
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_urls: set[str] = set()

    def start_requests(self):
        yield scrapy.Request(SITEMAP_URL, callback=self.parse_sitemap_or_index, errback=self._err)

    def parse_sitemap_or_index(self, response):
        text = response.text or ""
        if re.search(r"<sitemapindex", text, flags=re.I):
            for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text, flags=re.I):
                loc = loc.strip()
                if loc:
                    yield scrapy.Request(loc, callback=self.parse_sitemap_or_index, errback=self._err)
            return

        for block in re.findall(
            r"<url>(.*?)</url>", text, flags=re.I | re.DOTALL
        ):
            loc_m = re.search(r"<loc>\s*([^<]+?)\s*</loc>", block, flags=re.I)
            if not loc_m:
                continue
            raw = loc_m.group(1).strip()
            clean, _ = urldefrag(raw)
            clean = clean.split("?")[0].strip().rstrip("/")
            if "/new-homes-for-sale/" not in clean:
                continue
            if clean in self._seen_urls:
                continue
            self._seen_urls.add(clean)

            lastmod = ""
            lm = re.search(r"<lastmod>\s*([^<]+?)\s*</lastmod>", block, flags=re.I)
            if lm:
                lastmod = lm.group(1).strip()

            yield HolidayBuildersListingItem(house_url=f"{clean}/", lastmod=lastmod)

        self.logger.info("listing_unique_new_homes_urls=%s", len(self._seen_urls))

    def _err(self, failure):
        self.logger.error("sitemap request failed: %s", failure.value)
