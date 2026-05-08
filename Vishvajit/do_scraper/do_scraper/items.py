# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class DoUrlItem(scrapy.Item):
    city = scrapy.Field()
    event_url = scrapy.Field()


class DoEventDetailsItem(scrapy.Item):
    # Top-level contract only (nested keys live inside these dict fields).
    provider = scrapy.Field()
    module = scrapy.Field()
    groupId = scrapy.Field()
    id = scrapy.Field()
    createdAt = scrapy.Field()
    updatedAt = scrapy.Field()
    title = scrapy.Field()
    source = scrapy.Field()
    recordSource = scrapy.Field()
    location = scrapy.Field()
    siteId = scrapy.Field()
    metadata = scrapy.Field()
