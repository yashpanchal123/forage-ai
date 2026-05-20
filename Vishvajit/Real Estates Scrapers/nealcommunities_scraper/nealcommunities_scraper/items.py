import scrapy


class NealCommunitiesListingItem(scrapy.Item):
    house_url = scrapy.Field()
    community_url = scrapy.Field()
