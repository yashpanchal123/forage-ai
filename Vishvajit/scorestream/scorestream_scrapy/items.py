import scrapy


class ListingLinkItem(scrapy.Item):
    """One row for scorestream_game_links.csv (listing pass)."""

    market_label = scrapy.Field()
    game_id = scrapy.Field()
    game_url = scrapy.Field()
    min_url = scrapy.Field()
    state_slug = scrapy.Field()
    state_code = scrapy.Field()
    sport_name = scrapy.Field()
    organization_id = scrapy.Field()
    squad_ids_json = scrapy.Field()
    listing_json = scrapy.Field()


class ForageEventItem(scrapy.Item):
    """Final Forage-shaped event (sport_foramt.json)."""

    record = scrapy.Field()
    game_url = scrapy.Field()


class ScrapeErrorItem(scrapy.Item):
    game_url = scrapy.Field()
    error = scrapy.Field()

