import scrapy


class EventURLItem(scrapy.Item):
    """Item yielded by the listing spider — just the event URL."""
    event_url = scrapy.Field()


class EventItem(scrapy.Item):
    """Full event details scraped from the details page."""
    # ── identifiers ───────────────────────────────────────────────────────────
    event_id    = scrapy.Field()   # numeric ID from URL
    url         = scrapy.Field()   # Event URL

    # ── event info ────────────────────────────────────────────────────────────
    event_name  = scrapy.Field()   # Full event title
    artist_name = scrapy.Field()   # Headlining artist name
    datetime    = scrapy.Field()   # Event Date / Time (ISO-8601)
    end_datetime = scrapy.Field()  # Event end Date / Time (ISO-8601)
    description = scrapy.Field()   # Event description

    # ── venue / address ───────────────────────────────────────────────────────
    venue_name      = scrapy.Field()   # Venue
    venue_location  = scrapy.Field()   # Address (street, city, region, country)
    venue_latitude  = scrapy.Field()
    venue_longitude = scrapy.Field()

    # ── ticketing ─────────────────────────────────────────────────────────────
    cost            = scrapy.Field()   # e.g. "USD 25.00" or "Free"
    availability    = scrapy.Field()   # "Available" / "Sold Out" / "Pre-Sale" / "Limited" / "Unknown"

    # ── organiser ─────────────────────────────────────────────────────────────
    promoter = scrapy.Field()   # Event Sponsor / Promoter

    # ── schema support extras ─────────────────────────────────────────────────
    status       = scrapy.Field()   # scheduled/cancelled/postponed/tbd
    event_roles  = scrapy.Field()   # list[{"organizer": "..."}]
    purchase_url = scrapy.Field()
    tags         = scrapy.Field()   # list[str]
    media_urls   = scrapy.Field()   # list[str]
    category           = scrapy.Field()
    target_demographic = scrapy.Field()


class BandsintownEventDetailsItem(scrapy.Item):
    """
    Nested output schema aligned with the "Crawlmagic Data Feedback" spreadsheet
    and the meetup_details output format.
    """
    provider = scrapy.Field()
    module = scrapy.Field()
    groupId = scrapy.Field()
    id = scrapy.Field()
    createdAt = scrapy.Field()
    updatedAt = scrapy.Field()
    title = scrapy.Field()
    source = scrapy.Field()
    recordSource = scrapy.Field()
    location = scrapy.Field()
    siteId = scrapy.Field()
    nearBy = scrapy.Field()
    metadata = scrapy.Field()
