import scrapy
import re


class KhovListingSpider(scrapy.Spider):
    name = "khov_listing"
    allowed_domains = ["khov.com"]
    start_urls = ["https://www.khov.com/sitemap.xml"]
    custom_settings = {
        "FEEDS": {
            "khov_urls.csv": {
                "format": "csv",
                "overwrite": True,
                "encoding": "utf-8",
                "fields": ["listing_url"],
            }
        }
    }

    def parse(self, response):
        sitemap_urls = response.xpath('//*[local-name()="sitemap"]/*[local-name()="loc"]/text()').getall()
        if sitemap_urls:
            for sitemap_url in sitemap_urls:
                yield response.follow(sitemap_url, callback=self.parse)
            return

        urls = response.xpath('//*[local-name()="url"]/*[local-name()="loc"]/text()').getall()
        if not urls:
            self.logger.warning("No <loc> URLs found in sitemap: %s", response.url)

        matched = 0
        for url in urls:
            if self.is_listing_url(url):
                matched += 1
                yield {"listing_url": url}

        if urls and matched == 0:
            self.logger.warning(
                "Sitemap had %d URLs but 0 matched the strict filter. "
                "Falling back to all /new-construction-homes/ URLs.",
                len(urls),
            )
            for url in urls:
                if "/new-construction-homes/" in url:
                    yield {"listing_url": url}

    def is_listing_url(self, url):
        # must be under new construction homes
        if "/new-construction-homes/" not in url:
            return False

        last_part = url.rstrip("/").split("/")[-1]

        # Common KHov pattern is a slug ending with an ID (e.g. -0052),
        # but keep this permissive to avoid returning nothing if URL formats change.
        return bool(re.search(r"-\d+$", last_part)) or last_part.isdigit()