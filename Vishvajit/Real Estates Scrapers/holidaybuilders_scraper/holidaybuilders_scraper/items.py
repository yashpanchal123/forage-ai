import scrapy


class HolidayBuildersListingItem(scrapy.Item):
    house_url = scrapy.Field()
    lastmod = scrapy.Field()
