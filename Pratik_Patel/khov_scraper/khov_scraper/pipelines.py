from scrapy.exceptions import DropItem


class KhovScraperPipeline:

    def __init__(self):
        self.seen = set()

    def process_item(self, item, spider):
        url = item.get("listing_url")

        # ❌ drop empty
        if not url:
            raise DropItem("Empty URL")

        # ❌ drop duplicates
        if url in self.seen:
            raise DropItem(f"Duplicate URL: {url}")

        self.seen.add(url)

        return item