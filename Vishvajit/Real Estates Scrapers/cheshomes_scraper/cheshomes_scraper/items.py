import scrapy


class CheshomesListingItem(scrapy.Item):
    house_url = scrapy.Field()
    community_url = scrapy.Field()
