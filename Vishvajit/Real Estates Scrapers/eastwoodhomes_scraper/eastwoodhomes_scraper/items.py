import scrapy


class EastwoodHomesListingItem(scrapy.Item):
    state = scrapy.Field()
    community_url = scrapy.Field()
    house_url = scrapy.Field()
