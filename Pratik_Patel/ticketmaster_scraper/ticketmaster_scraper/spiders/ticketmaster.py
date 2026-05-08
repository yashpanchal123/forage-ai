import scrapy
from urllib.parse import urlencode


class TicketmasterSpider(scrapy.Spider):
    name = "ticketmaster"
    allowed_domains = ["ticketmaster.com"]
    MAX_PAGES = 50

    custom_settings = {
        # Global settings.py uses LOG_LEVEL=ERROR; this spider needs visible progress.
        "LOG_LEVEL": "INFO",
        "FEEDS": {
            "all_urls.csv": {
                "format": "csv",
                "overwrite": True,
                "fields": ["City", "State", "URL"],
            }
        },
        "CONCURRENT_REQUESTS": 4,
        "DOWNLOAD_DELAY": 1,
    }

    HEADERS = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://www.ticketmaster.com/search",
        "user-agent": "Mozilla/5.0",
        "x-tmclient-app": "marketplace_fe",
        "x-tmlangcode": "en-us",
        "x-tmplatform": "global",
        "x-tmregion": "200",
    }

    CITY_LIST = [
        "Atlanta, GA",
        "Austin, TX",
        "Orlando, FL",
        "Nashville, TN",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_urls = set()

    @staticmethod
    def _city_state_from_label(label: str) -> tuple[str, str]:
        """'Atlanta, GA' -> ('Atlanta', 'GA')."""
        s = (label or "").strip()
        if "," not in s:
            return s, ""
        left, right = s.rsplit(",", 1)
        city = " ".join(left.split()).strip()
        st = "".join(c for c in right if c.isalpha()).upper()[:2]
        return city, st

    def start_requests(self):
        base_url = "https://www.ticketmaster.com/api/discovery/location/city"

        for city in self.CITY_LIST:
            url = f"{base_url}?{urlencode({'city': city})}"
            yield scrapy.Request(
                url=url,
                headers=self.HEADERS,
                callback=self.parse_city,
                cb_kwargs={"city": city},
                errback=self.errback,
            )

    def parse_city(self, response, city):
        try:
            data = response.json()
            city_name = data[0]["name"]
            city_lat = data[0]["latitude"]
            city_long = data[0]["longitude"]
            self.logger.info(f"Fetched city info: {city_name} ({city_lat}, {city_long})")
        except (KeyError, IndexError, ValueError) as e:
            self.logger.error(f"Failed to parse city data for '{city}': {e}")
            return

        city_display, state_code = self._city_state_from_label(city)
        yield from self.fetch_events(
            city_name, city_lat, city_long, page=1,
            city_display=city_display, state_code=state_code,
        )

    def fetch_events(self, city_name, lat, long, page, city_display, state_code):
        params = urlencode({
            "q": "",
            "region": "200",
            "sort": "relevance",
            "page": page,
            "latitude": lat,
            "longitude": long,
            "distance": "6214",
            "distanceUnit": "miles",
        })
        url = f"https://www.ticketmaster.com/api/search/events?{params}"

        yield scrapy.Request(
            url=url,
            headers=self.HEADERS,
            callback=self.parse_events,
            cb_kwargs={
                "city_name": city_name,
                "lat": lat,
                "long": long,
                "page": page,
                "city_display": city_display,
                "state_code": state_code,
            },
            errback=self.errback,
        )

    def parse_events(
            self, response, city_name, lat, long, page,
            city_display, state_code,
    ):
        try:
            data = response.json()
        except ValueError:
            self.logger.error(f"Invalid JSON on page {page} for {city_name}")
            return

        # Stop pagination if empty response
        if not data:
            self.logger.info(f"No more data for {city_name} at page {page}. Stopping.")
            return

        events = data.get("events", [])

        # Stop pagination if no events returned
        if not events:
            self.logger.info(f"No events on page {page} for {city_name}. Stopping.")
            return

        for event in events:
            event_url = event.get("url", "")
            if not event_url or event_url in self._seen_urls:
                continue
            self._seen_urls.add(event_url)
            print(event_url)
            yield {
                "City": city_display,
                "State": state_code,
                "URL": event_url,
            }

        self.logger.info(f"{city_name} — scraped page {page} ({len(events)} events)")

        if page >= self.MAX_PAGES:
            self.logger.info(f"{city_name} reached MAX_PAGES={self.MAX_PAGES}")
            return

        # Follow next page
        yield from self.fetch_events(
            city_name, lat, long, page + 1,
            city_display=city_display, state_code=state_code,
        )

    def errback(self, failure):
        self.logger.error(f"Request failed: {failure.request.url} — {repr(failure)}")