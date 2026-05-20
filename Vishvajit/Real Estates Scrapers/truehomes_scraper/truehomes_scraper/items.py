import scrapy


class TrueHomesListingItem(scrapy.Item):
    state = scrapy.Field()
    house_url = scrapy.Field()

