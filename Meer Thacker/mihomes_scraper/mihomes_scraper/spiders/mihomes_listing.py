import scrapy
import csv


class MihomesListingSpider(scrapy.Spider):
    name = "mihomes_listing"
    allowed_domains = ["mihomes.com"]
    start_urls = ["https://www.mihomes.com/Sitemap/sitemap.xml"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filtered_urls = set()

    def parse(self, response):
        sitemap_links = response.xpath(
            "//*[local-name()='sitemap']/*[local-name()='loc']/text()"
        ).getall()

        if sitemap_links:
            for sitemap_url in sitemap_links:
                yield response.follow(sitemap_url.strip(), callback=self.parse)
            return

        for url_node in response.xpath("//*[local-name()='url']"):
            loc = url_node.xpath("./*[local-name()='loc']/text()").get()
            priority = url_node.xpath("./*[local-name()='priority']/text()").get()

            if not loc or not priority:
                continue

            loc = loc.strip()
            priority = priority.strip()

            if "/new-homes/" in loc and priority == "0.7":
                self.filtered_urls.add(loc)

    def closed(self, reason):
        output_file = "mihomes_new_homes_urls.csv"
        with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["url"])
            for url in sorted(self.filtered_urls):
                writer.writerow([url])

        self.logger.info(
            "Saved %s unique URLs to %s",
            len(self.filtered_urls),
            output_file,
        )
