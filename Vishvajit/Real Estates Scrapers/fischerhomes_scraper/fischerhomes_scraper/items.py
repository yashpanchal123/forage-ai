import scrapy


class FischerHomesListingItem(scrapy.Item):
    """Row from listing spider for move-in-ready (QMI) inventory."""

    state = scrapy.Field()
    community_url = scrapy.Field()
    house_url = scrapy.Field()
    card_id = scrapy.Field()
