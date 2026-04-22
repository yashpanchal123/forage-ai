import scrapy


class TollbrothersListingSpider(scrapy.Spider):
    name = "tollbrothers_listing"
    allowed_domains = ["tollbrothers.com"]
    start_urls = ["https://www.tollbrothers.com/sitemap.xml"]
    custom_settings = {
        "FEEDS": {
            "tollbrothers_luxury_homes_urls.csv": {
                "format": "csv",
                "overwrite": True,
            }
        }
    }
    target_path = "/Quick-Move-In/"

    def parse(self, response):
        # Works for both sitemap index and urlset, regardless of XML namespace.
        sitemap_locs = response.xpath("//*[local-name()='loc']/text()").getall()

        for loc in sitemap_locs:
            url = loc.strip()
            if not url:
                continue

            # Recursively follow nested sitemap files.
            if url.endswith(".xml"):
                yield response.follow(url, callback=self.parse, dont_filter=True)
                continue

            if self.target_path in url:
                yield {"url": url}
