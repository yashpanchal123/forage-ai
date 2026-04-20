# import json
# from itemadapter import ItemAdapter
# from scrapy.exporters import CsvItemExporter
#
#
# class PatchCalendarPipeline:
#     """Pass-through pipeline for the listing spider (CSV feed handles output)."""
#
#     def process_item(self, item, spider):
#         return item
#
#
# class EventDetailsPipeline:
#     """
#     Collects all event dicts from the event_details spider and writes them
#     as a single JSON array to patch_events_full_data.json when the spider closes.
#     Only activates for the event_details spider.
#     """
#
#     OUTPUT_FILE = "patch_events_full_data.json"
#
#     def open_spider(self, spider):
#         if spider.name != "event_details":
#             return
#         self.items = []
#
#     def process_item(self, item, spider):
#         if spider.name != "event_details":
#             return item
#         self.items.append(dict(ItemAdapter(item)))
#         return item
#
#     def close_spider(self, spider):
#         if spider.name != "event_details":
#             return
#         with open(self.OUTPUT_FILE, "w", encoding="utf-8") as f:
#             json.dump(self.items, f, indent=2, ensure_ascii=False)
#         spider.logger.info(
#             f"EventDetailsPipeline: {len(self.items)} events → {self.OUTPUT_FILE}"
#         )

import json
from itemadapter import ItemAdapter


class ListingItemFilter:
    """
    Filter for FEEDS export (used by multi_city_listing spider).
    """

    def __init__(self, feed_options):
        # Scrapy passes feed_options automatically
        self.feed_options = feed_options

    def accepts(self, item):
        # Allow all items (you can customize later)
        return True


class PatchCalendarPipeline:
    def process_item(self, item, spider):
        return item


class EventDetailsPipeline:
    OUTPUT_FILE = "patch_fulldata_04_02_2026.json"

    def open_spider(self, spider):
        if spider.name != "event_details":
            return
        self.items = []

    def process_item(self, item, spider):
        if spider.name != "event_details":
            return item
        self.items.append(dict(ItemAdapter(item)))
        return item

    def close_spider(self, spider):
        if spider.name != "event_details":
            return
        with open(self.OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=2, ensure_ascii=False)
        spider.logger.info(
            f"EventDetailsPipeline: {len(self.items)} events → {self.OUTPUT_FILE}"
        )