import scrapy


class SportListingItem(scrapy.Item):
    """Sportstättenverzeichnis listing row (category-driven; feeds ``sport_details``)."""

    resource_id = scrapy.Field()
    title = scrapy.Field()
    detail_url = scrapy.Field()
    email = scrapy.Field()
    phone = scrapy.Field()
    object_type = scrapy.Field()
    # Sport facet ``fac-1`` category id (stored as ``fil-4`` in GraphQL filters).
    filter_fil_2 = scrapy.Field()


class SportDetailItem(scrapy.Item):
    resource_id = scrapy.Field()
    url = scrapy.Field()
    title = scrapy.Field()
    category_label = scrapy.Field()
    district = scrapy.Field()
    venue_address = scrapy.Field()
    large_playing_fields = scrapy.Field()
    small_playing_fields = scrapy.Field()
    other_facilities = scrapy.Field()
    parking_spaces = scrapy.Field()
    dimensions_area = scrapy.Field()
    hall_division = scrapy.Field()
    flooring = scrapy.Field()
    changing_rooms = scrapy.Field()
    accessible_for_people_with_disabilities = scrapy.Field()
    bus_lines = scrapy.Field()
    predominant_sports = scrapy.Field()
    description = scrapy.Field()
    address = scrapy.Field()
    mobile = scrapy.Field()
    fax = scrapy.Field()
    email = scrapy.Field()
    website = scrapy.Field()
    way_us = scrapy.Field()
