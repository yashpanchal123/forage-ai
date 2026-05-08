import scrapy


class AdressListingItem(scrapy.Item):
    """Address directory listing row (aligned with vereine-style link exports)."""

    resource_id = scrapy.Field()
    title = scrapy.Field()
    detail_url = scrapy.Field()
    email = scrapy.Field()
    phone = scrapy.Field()
    object_type = scrapy.Field()
    # Active category filter for this request.
    filter_fil_2 = scrapy.Field()
    # SearchInput.text used for this hit (e.g. mainz, mainz.de).
    search_text = scrapy.Field()


class AdressDetailItem(scrapy.Item):
    resource_id = scrapy.Field()
    url = scrapy.Field()
    # SearchInput.text from listing scrape (same row as detail_url).
    search_text = scrapy.Field()
    title = scrapy.Field()
    category_label = scrapy.Field()
    description = scrapy.Field()
    address = scrapy.Field()
    mobile = scrapy.Field()
    fax = scrapy.Field()
    email = scrapy.Field()
    # Single primary website URL (string).
    website = scrapy.Field()
    way_us = scrapy.Field()
