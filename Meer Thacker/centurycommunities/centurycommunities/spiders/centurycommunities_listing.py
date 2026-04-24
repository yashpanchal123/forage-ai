import scrapy
import csv
from pathlib import Path


class CenturycommunitiesListingSpider(scrapy.Spider):
    name = "centurycommunities_listing"
    allowed_domains = ["centurycommunities.com"]
    start_urls = ["https://www.centurycommunities.com/sitemap.xml"]
    csv_filename = "centurycommunities_lots_plans_urls.csv"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filtered_urls = set()

    def parse(self, response):
        # Sitemaps commonly use default XML namespaces; local-name() keeps selection robust.
        sitemap_urls = response.xpath(
            "//*[local-name()='sitemap']/*[local-name()='loc']/text()"
        ).getall()
        page_urls = response.xpath(
            "//*[local-name()='url']/*[local-name()='loc']/text()"
        ).getall()

        for sitemap_url in sitemap_urls:
            yield response.follow(sitemap_url.strip(), callback=self.parse)

        for page_url in page_urls:
            clean_url = page_url.strip()
            if "/lots/" in clean_url or "/plans/" in clean_url:
                self.filtered_urls.add(clean_url)

    def closed(self, reason):
        output_path = Path(self.csv_filename)
        with output_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["url"])
            for url in sorted(self.filtered_urls):
                writer.writerow([url])

        self.logger.info(
            "Saved %s filtered URLs to %s", len(self.filtered_urls), output_path.resolve()
        )
