# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class DrHortonListingItem(scrapy.Item):
    """
    Same listing schema as cheshomes / nealcommunities:
      house_url, community_url

    `house_url` is any listing detail URL (QMI inventory or floor-plan page).
    """

    house_url = scrapy.Field()
    community_url = scrapy.Field()
