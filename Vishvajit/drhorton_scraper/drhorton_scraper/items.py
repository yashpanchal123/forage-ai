# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class DrHortonListingItem(scrapy.Item):
    state = scrapy.Field()
    community_url = scrapy.Field()
    house_url = scrapy.Field()
