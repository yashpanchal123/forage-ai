import json
import re
import hashlib
from urllib.parse import urlencode, quote_plus

import scrapy
from bandsintown_scraper.items import EventURLItem

BASE_URL = "https://www.bandsintown.com"
CITY_SUGGEST_URL = BASE_URL + "/citySuggestions?string={query}"
EVENTS_FETCH_URL = BASE_URL + "/all-dates/fetch-next/upcomingEvents"

EVENT_PATH_RE = re.compile(r"^/e/\d+")
EVENT_ID_RE = re.compile(r"/e/(\d+[^\"'\s?#&,>)]*)")

TARGET_CITIES = [
    "Atlanta, GA",
    "Austin, TX",
    "Orlando, FL",
    "Nashville, TN",
]


class ListingSpider(scrapy.Spider):
    name = "bandsintown_listing"
    custom_settings = {
        # Listing is light; details spider uses project default GEONODE_ONLY=True.
        "GEONODE_ONLY": False,
        "FEEDS": {
            "bandsintown_event_urls.csv": {
                "format": "csv",
                "overwrite": True,
                "encoding": "utf-8",
            }
        }
    }

    def __init__(self, start_page: int = 1, max_page: int = 0, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_page = int(start_page)
        self.max_page = int(max_page)
        self.last_response_hash = {}
        self.last_urls = {}
        self.total_urls_emitted = 0
        self.total_pages_processed = 0

    async def start(self):
        self.logger.info(
            "Starting listing crawl for %s target cities (start_page=%s, max_page=%s)",
            len(TARGET_CITIES),
            self.start_page,
            self.max_page or "unbounded",
        )
        for city in TARGET_CITIES:
            yield scrapy.Request(
                url=CITY_SUGGEST_URL.format(query=quote_plus(city)),
                callback=self.parse_city,
                headers={"Accept": "application/json"},
                meta={"city_label": city},
                dont_filter=True,
            )

    def parse_city(self, response):
        label = response.meta["city_label"]

        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.logger.error("[%s] citySuggestions returned invalid JSON", label)
            return

        cities = data.get("cities") or []
        if not cities:
            self.logger.warning("[%s] No city match found; skipping", label)
            return

        city = cities[0]
        city_id = city["id"]
        lat = city["latitude"]
        lon = city["longitude"]

        self.logger.info("[%s] Resolved city_id=%s", label, city_id)

        yield self._make_events_request(city_id, lat, lon, self.start_page, label)

    def _make_events_request(self, city_id, lat, lon, page, label):
        params = urlencode({
            "city_id": city_id,
            "page": page,
            "latitude": lat,
            "longitude": lon,
        })

        return scrapy.Request(
            url=f"{EVENTS_FETCH_URL}?{params}",
            callback=self.parse_events,
            headers={"Accept": "application/json, text/html, */*"},
            meta={
                "city_id": city_id,
                "latitude": lat,
                "longitude": lon,
                "page": page,
                "city_label": label,
            },
            dont_filter=True,
        )

    def parse_events(self, response):
        city_id = response.meta["city_id"]
        lat = response.meta["latitude"]
        lon = response.meta["longitude"]
        page = response.meta["page"]
        label = response.meta["city_label"]

        self.total_pages_processed += 1
        current_hash = hashlib.md5(response.text.encode()).hexdigest()
        if self.last_response_hash.get(label) == current_hash:
            self.logger.info("[%s] Duplicate response on page %s; stopping", label, page)
            return
        self.last_response_hash[label] = current_hash

        urls = set()
        has_more = False

        try:
            data = json.loads(response.text)

            # ✅ City-filtered URLs
            urls = self._urls_from_json(data, label)

            has_more = self._json_has_more(data, urls)

        except (json.JSONDecodeError, ValueError):
            urls = self._extract_event_urls(response.text)
            has_more = bool(urls)

        if self.last_urls.get(label) == urls:
            self.logger.info("[%s] Same URLs as previous page; stopping", label)
            return
        self.last_urls[label] = urls

        self.logger.info(
            "[%s] Page %s: %s URLs (has_more=%s)",
            label,
            page,
            len(urls),
            has_more,
        )

        for url in urls:
            yield EventURLItem(event_url=url)
        self.total_urls_emitted += len(urls)

        at_limit = self.max_page and page >= self.max_page

        if has_more and not at_limit:
            yield self._make_events_request(city_id, lat, lon, page + 1, label)
        elif at_limit:
            self.logger.info("[%s] Reached max_page=%s", label, self.max_page)

    def _urls_from_json(self, data, city_label: str) -> set[str]:
        urls = set()

        events = data.get("events") or []

        target_city = city_label.split(",")[0].strip().lower()

        for event in events:
            location_text = (event.get("locationText") or "").strip().lower()

            if target_city in location_text:
                event_url = event.get("eventUrl")
                if event_url:
                    urls.add(event_url)

        return urls

    def _json_has_more(self, data, found_urls: set) -> bool:
        if isinstance(data, dict):
            if "has_more" in data:
                return bool(data["has_more"])
            if "next_page" in data and data["next_page"]:
                return True

            events = data.get("events") or []
            if isinstance(events, list):
                return len(events) > 0

        return bool(found_urls)

    def closed(self, reason):
        self.logger.info(
            "Listing crawl finished (reason=%s, pages=%s, emitted_urls=%s)",
            reason,
            self.total_pages_processed,
            self.total_urls_emitted,
        )

    def _extract_event_urls(self, html: str) -> set[str]:
        urls: set[str] = set()
        urls.update(self._urls_from_hrefs(html))
        urls.update(self._urls_from_next_data(html))
        urls.update(self._urls_from_raw_scan(html))
        return urls

    def _urls_from_hrefs(self, html: str) -> set[str]:
        urls: set[str] = set()
        for href in re.findall(r'''href=["']([^"']+)["']''', html):
            if "bandsintown.com/e/" in href:
                m = EVENT_ID_RE.search(href)
                if m:
                    urls.add(f"{BASE_URL}/e/{m.group(1)}")
            elif EVENT_PATH_RE.match(href):
                urls.add(BASE_URL + href.split("?")[0].split("#")[0])
        return urls

    def _urls_from_next_data(self, html: str) -> set[str]:
        urls: set[str] = set()
        nd_match = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if not nd_match:
            return urls
        try:
            text = json.dumps(json.loads(nd_match.group(1)))
            for slug in re.findall(r'/e/(\d+[^"\\]+)', text):
                clean = slug.split("?")[0].split("\\")[0].rstrip("/")
                if clean:
                    urls.add(f"{BASE_URL}/e/{clean}")
        except json.JSONDecodeError:
            pass
        return urls

    def _urls_from_raw_scan(self, html: str) -> set[str]:
        urls: set[str] = set()
        for slug in EVENT_ID_RE.findall(html):
            clean = slug.split("?")[0].rstrip("/")
            if clean:
                urls.add(f"{BASE_URL}/e/{clean}")
        return urls
