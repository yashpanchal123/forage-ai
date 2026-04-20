import scrapy


class ListingLinkItem(scrapy.Item):
    """One row for event_links.csv (listing pass)."""

    event_url = scrapy.Field()
    event_id = scrapy.Field()
    eventname_raw = scrapy.Field()
    eventname = scrapy.Field()
    # Full raw object from the listing API; used by the detail scraper for better media/pricing.
    listing_json = scrapy.Field()
    city_slug = scrapy.Field()
    market_name = scrapy.Field()
    state = scrapy.Field()
    tegna_market = scrapy.Field()
    tegna_label = scrapy.Field()
    event_tz = scrapy.Field()
    organizer_name = scrapy.Field()
    categories_json = scrapy.Field()
    tags_json = scrapy.Field()


class ForageEventItem(scrapy.Item):
    """Final Forage-shaped event (format.json)."""

    record = scrapy.Field()
    event_url = scrapy.Field()


class ScrapeErrorItem(scrapy.Item):
    event_url = scrapy.Field()
    error = scrapy.Field()
