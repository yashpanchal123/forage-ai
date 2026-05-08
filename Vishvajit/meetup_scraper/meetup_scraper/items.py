# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class MeetupScraperItem(scrapy.Item):
    event_url = scrapy.Field()
    city = scrapy.Field()


class MeetupEventDetailsItem(scrapy.Item):
    # Matches your target output schema (nested objects are stored as dicts).
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
    nearBy = scrapy.Field()
    metadata = scrapy.Field()
