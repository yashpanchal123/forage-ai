import scrapy


class MarondaHomesListingItem(scrapy.Item):
    state = scrapy.Field()
    community_url = scrapy.Field()
    house_url = scrapy.Field()
