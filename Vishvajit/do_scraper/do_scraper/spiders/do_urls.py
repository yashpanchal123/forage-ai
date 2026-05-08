import scrapy
from datetime import datetime, timedelta
from ..items import DoUrlItem


class DoUrlsSpider(scrapy.Spider):
    name = "do_urls"

    cities = ["Atlanta", "Austin", "Orlando", "Nashville"]


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_urls = set()
        self._pages_seen = 0
        self._events_emitted = 0

    custom_settings = {
        "FEEDS": {
            "do_urls.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["city", "event_url"],
            }
        }
    }
    # -------------------------------
    # Generate next 30 days
    # -------------------------------
    def get_dates(self):
        base = datetime.today()

        return [base + timedelta(days=i) for i in range(30)]

    def start_requests(self):
        self.logger.info("Starting do_urls spider for %d cities and %d days", len(self.cities), len(self.get_dates()))

        yield scrapy.Request(
            url="https://do303.com/other_cities",
            callback=self.parse_cities
        )

    # -------------------------------
    # Get city URLs
    # -------------------------------
    def parse_cities(self, response):
        for city in self.cities:
            city_url = response.xpath(
                f'//span[contains(text(),"{city}")]/parent::a/@href'
            ).get()

            if not city_url:
                self.logger.warning("City not found in other_cities page: %s", city)
                continue

            base_url = response.urljoin(city_url)
            self.logger.info("Resolved city base URL: city=%s base=%s", city, base_url)

            # loop over 30 days
            for dt in self.get_dates():
                date_path = dt.strftime("/events/%Y/%m/%d")
                full_url = base_url.rstrip("/") + date_path

                yield scrapy.Request(
                    url=full_url,
                    callback=self.parse_events,
                    meta={
                        "city": city,
                        "date": dt.strftime("%Y-%m-%d")
                    }
                )

    # -------------------------------
    # Extract event URLs
    # -------------------------------
    def parse_events(self, response):
        self._pages_seen += 1
        city = response.meta["city"]
        date = response.meta["date"]

        event_links = response.xpath(
            '//div[@itemprop="event"]/a/@href'
        ).getall()
        self.logger.debug("Parsed events page: city=%s date=%s events_found=%d url=%s", city, date, len(event_links), response.url)

        for link in event_links:
            full_url = response.urljoin(link)

            if full_url in self.seen_urls:
                continue

            self.seen_urls.add(full_url)
            self._events_emitted += 1

            yield DoUrlItem(
                city=city,
                event_url=full_url
            )

        # -------------------------------
        # Pagination
        # -------------------------------
        next_page = response.xpath(
            '//a[contains(@class,"ds-next-page")]/@href'
        ).get()

        if next_page:
            yield scrapy.Request(
                url=response.urljoin(next_page),
                callback=self.parse_events,
                meta=response.meta
            )

    def closed(self, reason):
        self.logger.info(
            "do_urls finished: reason=%s pages_seen=%d unique_events=%d",
            reason,
            self._pages_seen,
            self._events_emitted,
        )