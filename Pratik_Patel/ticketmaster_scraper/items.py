from scrapy import Item, Field


class TicketmasterUrlItem(Item):
    """Discovered Ticketmaster event URL."""

    url = Field()