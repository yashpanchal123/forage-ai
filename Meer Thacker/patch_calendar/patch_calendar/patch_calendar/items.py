import scrapy


class EventURLItem(scrapy.Item):
    """Yielded by multi_city_listing spider → written to event_urls.csv."""
    city = scrapy.Field()
    event_url = scrapy.Field()
