import scrapy


class EpconCommunitiesListingItem(scrapy.Item):
    state = scrapy.Field()
    house_url = scrapy.Field()
    inventory_id = scrapy.Field()
