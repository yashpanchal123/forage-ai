import scrapy


class EventItem(scrapy.Item):
    event_url = scrapy.Field()

import scrapy


class EventDetailItem(scrapy.Item):
    # Identity
    event_url        = scrapy.Field()
    event_id         = scrapy.Field()
    event_name       = scrapy.Field()
    edition_id       = scrapy.Field()

    # Dates
    start_date       = scrapy.Field()
    end_date         = scrapy.Field()
    date_display     = scrapy.Field()
    frequency        = scrapy.Field()
    status           = scrapy.Field()

    # Location
    venue_name       = scrapy.Field()
    city             = scrapy.Field()
    country          = scrapy.Field()
    full_address     = scrapy.Field()

    # Details
    description      = scrapy.Field()
    event_type       = scrapy.Field()       # Tradeshow / Conference / Workshop …
    industries       = scrapy.Field()       # comma-separated tags
    website          = scrapy.Field()
    organizer        = scrapy.Field()

    # Social / stats
    rating           = scrapy.Field()
    visitor_count    = scrapy.Field()
    interested_count = scrapy.Field()

    # Social links
    twitter_url      = scrapy.Field()
    linkedin_url     = scrapy.Field()
    facebook_url     = scrapy.Field()
    instagram_url    = scrapy.Field()
    youtube_url      = scrapy.Field()
