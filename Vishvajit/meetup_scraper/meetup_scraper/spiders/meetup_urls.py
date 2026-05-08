from ..items import MeetupScraperItem
import scrapy
import json
from datetime import date

GQL_URL = "https://www.meetup.com/gql2"

# recommendedEventsWithSeries (listing)
RECOMMENDED_EVENTS_HASH = "3f7480361301be1b3208df0cd724930a22f7741d3c24666ab5b37a381ff4e0e8"

# getEventSeriesById — extra occurrences beyond embedded series.events window
GET_EVENT_SERIES_HASH = "4c016453a9384eb53d91aa4627635cfa3687eeea6eae4da3b2e1a2cb55f9ec0d"
SERIES_EXTRA_NUMBER_OF_EVENTS = 50


def _meetup_event_url(node, parent_node=None):
    """Canonical event URL from node, or built from group urlname + id."""
    u = (node or {}).get("eventUrl")
    if u and str(u).strip():
        return str(u).strip()
    gid = (node or {}).get("id")
    g = (node or {}).get("group") or ((parent_node or {}).get("group") or {})
    urlname = (g.get("urlname") or "").strip()
    if urlname and gid:
        return f"https://www.meetup.com/{urlname}/events/{gid}/"
    return None


def _series_start_date_iso(node):
    dt = (node or {}).get("dateTime")
    if isinstance(dt, str) and len(dt) >= 10:
        return dt[:10]
    return date.today().isoformat()


class MeetupUrlsSpider(scrapy.Spider):
    name = "meetup_urls"
    allowed_domains = ["www.meetup.com"]

    custom_settings = {
        "FEEDS": {
            "meetup_urls.csv": {
                "format": "csv",
                "overwrite": True,
                "encoding": "utf-8",
            }
        }
    }

    locations = [
        {"name": "Atlanta", "param": "us--ga--Atlanta"},
        {"name": "Austin", "param": "us--tx--Austin"},
        {"name": "Orlando", "param": "us--fl--Orlando"},
        {"name": "Nashville", "param": "us--tn--Nashville"},
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.all_seen_event_urls = set()
        # One getEventSeriesById per recurring template (avoid duplicate gql for same series).
        self._series_api_keys = set()
        self.logger.info(
            "Spider initialized with %d locations: %s",
            len(self.locations),
            [l["name"] for l in self.locations],
        )

    def _emit_url(self, city, event_url):
        if not event_url:
            return None
        if event_url in self.all_seen_event_urls:
            return None
        self.all_seen_event_urls.add(event_url)
        item = MeetupScraperItem()
        item["event_url"] = event_url
        item["city"] = city
        return item

    def _emit_series_embedded(self, city, parent_node):
        """Yield URLs from listing payload series.events.edges (often missing eventUrl on children)."""
        series = parent_node.get("series")
        if not isinstance(series, dict):
            return
        events = series.get("events")
        if not isinstance(events, dict):
            return
        edges = events.get("edges")
        if not isinstance(edges, list):
            return
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            child = edge.get("node")
            if not isinstance(child, dict):
                continue
            url = _meetup_event_url(child, parent_node)
            item = self._emit_url(city, url)
            if item:
                self.logger.debug("[%s] Series (embedded) URL: %s", city, url)
                yield item

    def _series_request(self, city, parent_node):
        series = parent_node.get("series")
        if not isinstance(series, dict):
            return
        if not series.get("events"):
            return
        group = parent_node.get("group") or {}
        urlname = (group.get("urlname") or "").strip()
        desc = (series.get("description") or "").strip()
        key = (urlname, desc or parent_node.get("id") or "")
        if not key[0]:
            return
        if key in self._series_api_keys:
            return
        self._series_api_keys.add(key)

        event_id = str(parent_node.get("id") or "").strip()
        if not event_id:
            return

        payload = {
            "operationName": "getEventSeriesById",
            "variables": {
                "eventId": event_id,
                "numberOfEvents": SERIES_EXTRA_NUMBER_OF_EVENTS,
                "seriesStartDate": _series_start_date_iso(parent_node),
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": GET_EVENT_SERIES_HASH,
                }
            },
        }
        self.logger.info(
            "[%s] getEventSeriesById for eventId=%s start=%s",
            city,
            event_id,
            payload["variables"]["seriesStartDate"],
        )
        yield scrapy.Request(
            url=GQL_URL,
            method="POST",
            body=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
                "Origin": "https://www.meetup.com",
            },
            callback=self.parse_series,
            meta={
                "city": city,
                "parent_node": parent_node,
            },
        )

    def start_requests(self):
        self.logger.info("Starting requests for %d locations", len(self.locations))
        for loc in self.locations:
            url = f"https://www.meetup.com/find/?location={loc['param']}&source=EVENTS"
            self.logger.info("Queueing location page for %s: %s", loc["name"], url)

            yield scrapy.Request(
                url=url,
                method="GET",
                callback=self.parse_location,
                headers={"User-Agent": "Mozilla/5.0"},
                cb_kwargs={"loc": loc},
                dont_filter=True,
            )

    def parse_location(self, response, loc):
        self.logger.info(
            "[%s] Parsing location page (status=%s): %s",
            loc["name"],
            response.status,
            response.url,
        )
        raw = response.xpath("//script[@id='__NEXT_DATA__']/text()").get()

        if not raw:
            self.logger.error("[%s] No JSON found in __NEXT_DATA__ script tag", loc["name"])
            return

        self.logger.debug("[%s] Found __NEXT_DATA__ JSON, parsing...", loc["name"])
        data = json.loads(raw)

        page_props = data.get("props", {}).get("pageProps", {})
        user_loc = page_props.get("userLocation") or {}
        lat = user_loc.get("lat")
        lon = user_loc.get("lon")
        if lat is None or lon is None:
            self.logger.error("[%s] Missing lat/lon in pageProps", loc["name"])
            return

        self.logger.info("%s → %s, %s", loc["name"], lat, lon)
        self.logger.info("[%s] Sending GraphQL recommendedEventsWithSeries", loc["name"])

        payload = {
            "operationName": "recommendedEventsWithSeries",
            "variables": {
                "first": 200,
                "lat": lat,
                "lon": lon,
                "after": None,
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": RECOMMENDED_EVENTS_HASH,
                }
            },
        }

        yield scrapy.Request(
            url=GQL_URL,
            method="POST",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            callback=self.parse_api,
            meta={
                "payload": payload,
                "lat": lat,
                "lon": lon,
                "city": loc["name"],
            },
        )

    def parse_series(self, response):
        city = response.meta.get("city", "")
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.logger.error("[%s] getEventSeriesById: invalid JSON", city)
            return

        if data.get("errors"):
            self.logger.warning("[%s] getEventSeriesById GraphQL errors: %s", city, data["errors"])

        parent = response.meta.get("parent_node") or {}
        event = (data.get("data") or {}).get("event") or {}
        serie = event.get("series") or {}
        events_conn = serie.get("events") or {}
        edges = events_conn.get("edges") if isinstance(events_conn, dict) else []

        extra = 0
        for edge in edges or []:
            if not isinstance(edge, dict):
                continue
            child = edge.get("node")
            if not isinstance(child, dict):
                continue
            url = _meetup_event_url(child, parent)
            item = self._emit_url(city, url)
            if item:
                extra += 1
                self.logger.debug("[%s] Series (API) URL: %s", city, url)
                yield item

        self.logger.info("[%s] getEventSeriesById added %d new URLs", city, extra)

    def parse_api(self, response):
        city = response.meta["city"]
        self.logger.info("[%s] Received API response (status=%s)", city, response.status)
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.logger.error("[%s] Invalid JSON from gql2", city)
            return

        if data.get("errors"):
            self.logger.warning("[%s] recommendedEventsWithSeries errors: %s", city, data["errors"])

        result = (data.get("data") or {}).get("result") or {}
        edges = result.get("edges")
        if not isinstance(edges, list):
            self.logger.error("[%s] Missing or invalid result.edges", city)
            return

        self.logger.info("[%s] Found %d edges in this page", city, len(edges))

        new_items = 0
        skipped_duplicates = 0
        skipped_no_url = 0

        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if not isinstance(node, dict):
                continue

            event_url = _meetup_event_url(node)
            if not event_url:
                self.logger.debug("[%s] Skipping edge with no resolvable URL", city)
                skipped_no_url += 1
                continue
            if event_url in self.all_seen_event_urls:
                self.logger.debug("[%s] Skipping duplicate event URL: %s", city, event_url)
                skipped_duplicates += 1
            else:
                self.all_seen_event_urls.add(event_url)
                item = MeetupScraperItem()
                item["event_url"] = event_url
                item["city"] = city
                self.logger.debug("[%s] Yielding new event URL: %s", city, event_url)
                new_items += 1
                yield item

            # Same recurring series: nested occurrences often lack eventUrl on each node.
            yield from self._emit_series_embedded(city, node)

            # Extra occurrences Meetup may omit from the embedded window.
            yield from self._series_request(city, node)

        self.logger.info(
            "[%s] Page summary — new top-level: %s, duplicates skipped: %s, no-url skipped: %s",
            city,
            new_items,
            skipped_duplicates,
            skipped_no_url,
        )

        page_info = result.get("pageInfo") or {}
        self.logger.debug(
            "[%s] pageInfo: hasNextPage=%s, endCursor=%s",
            city,
            page_info.get("hasNextPage"),
            page_info.get("endCursor"),
        )

        if page_info.get("hasNextPage"):
            next_cursor = page_info.get("endCursor")
            self.logger.info("[%s] Paginating with cursor: %s", city, next_cursor)

            payload = response.meta["payload"]
            payload["variables"]["after"] = next_cursor

            yield scrapy.Request(
                url=GQL_URL,
                method="POST",
                body=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                callback=self.parse_api,
                meta=response.meta,
            )
        else:
            self.logger.info(
                "[%s] No more pages. Total unique URLs: %d",
                city,
                len(self.all_seen_event_urls),
            )
