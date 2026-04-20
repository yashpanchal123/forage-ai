import scrapy
from urllib.parse import urlsplit, urlunsplit
from ..items import ListingUrlItem


class EventbriteListingSpider(scrapy.Spider):
    name = "eventbrite_listing"
    allowed_domains = ["eventbrite.com"]

    start_urls = [
        "https://www.eventbrite.com/d/tx--austin/all-events/",
        "https://www.eventbrite.com/d/ga--atlanta/all-events/",
        "https://www.eventbrite.com/d/fl--orlando/all-events/",
        "https://www.eventbrite.com/d/tn--nashville/all-events/",
    ]

    default_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "priority": "u=0, i",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }

    custom_settings = {
        "DEFAULT_REQUEST_HEADERS": default_headers,
        "FEEDS": {
            "eventbrite_listing_urls.csv": {
                "format": "csv",
                "overwrite": True,
                "fields": ["url"],
            },
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_event_urls = set()

    def start_requests(self):
        self.logger.info("Starting crawl: %d listing(s)", len(self.start_urls))
        for base in self.start_urls:
            listing_base = base.rstrip("/")
            url = self._listing_page_url(listing_base, 1)
            self.logger.info("Queue listing %r — page 1: %s", listing_base, url)
            yield scrapy.Request(
                url,
                callback=self.parse,
                cb_kwargs={"listing_base": listing_base, "page": 1},
            )

    def closed(self, reason):
        self.logger.info("Crawl finished (%s)", reason)

    @staticmethod
    def _listing_page_url(listing_base: str, page: int) -> str:
        return f"{listing_base}/?page={page}"

    @staticmethod
    def _normalize_event_url(url: str) -> str:
        """
        Normalize Eventbrite event URLs for dedupe.
        Removes query string and fragment so tracking params do not create duplicates.
        """
        parsed = urlsplit(url)
        normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        return normalized

    def parse(self, response, listing_base: str, page: int):
        cards = response.xpath('//div[contains(@class,"event_card")]')

        if not cards:
            self.logger.info(
                "No event cards on page %d for %r — pagination done for this listing (%s)",
                page,
                listing_base,
                response.url,
            )
            return

        n_cards = len(cards)
        self.logger.info(
            "Page %d for %r: %d card(s) — %s",
            page,
            listing_base,
            n_cards,
            response.url,
        )

        yielded = 0
        skipped_duplicates = 0
        for card in cards:
            href = card.xpath(
                './/section[contains(@class,"vertical-event-card")]'
                '//div[contains(@class,"event-card-image")]/parent::a/@href'
            ).get()
            if not href:
                continue

            absolute_url = response.urljoin(href)
            normalized_url = self._normalize_event_url(absolute_url)
            if normalized_url in self._seen_event_urls:
                skipped_duplicates += 1
                continue
            self._seen_event_urls.add(normalized_url)

            item = ListingUrlItem()
            item["url"] = normalized_url
            yielded += 1
            yield item

        self.logger.info(
            "Page %d for %r: yielded %d URL item(s), skipped %d duplicate(s); next page %d",
            page,
            listing_base,
            yielded,
            skipped_duplicates,
            page + 1,
        )

        next_page = page + 1
        yield scrapy.Request(
            self._listing_page_url(listing_base, next_page),
            callback=self.parse,
            cb_kwargs={"listing_base": listing_base, "page": next_page},
        )