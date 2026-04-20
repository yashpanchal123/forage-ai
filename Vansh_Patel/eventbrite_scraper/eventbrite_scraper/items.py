import scrapy


class EventbriteEventItem(scrapy.Item):
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
    isonline = scrapy.Field()
    metadata = scrapy.Field()


class ListingUrlItem(scrapy.Item):
    url = scrapy.Field()
    category = scrapy.Field()
    location = scrapy.Field()
