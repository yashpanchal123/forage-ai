import scrapy
from urllib.parse import urljoin, urlencode
from patch_calendar.items import EventURLItem


class MultiCityListingSpider(scrapy.Spider):
    """
    Crawls calendar events from Patch.com for multiple cities.
    - Resolves each city's patch_id via the metadata API
    - Fetches paginated events and yields unique URLs only
    """

    name = "multi_city_listing"
    allowed_domains = ["patch.com"]

    custom_settings = {
        "FEEDS": {
            "event_urls.csv": {
                "format": "csv",
                "overwrite": True,
                "item_filter": "patch_calendar.pipelines.ListingItemFilter",
            }
        }
    }

    METADATA_API = "https://patch.com/api_v2/metadata/search"
    CALENDAR_API = "https://patch.com/api_v2/calendar_r/events"

    CITY_MAP = {
        "Atlanta":   "/georgia/atlanta",
        "Austin":    "/texas/downtownaustin",
        "Orlando":   "/florida/orlando",
        "Nashville": "/tennessee/nashville",
    }

    def __init__(self, start_page: str = "1", max_pages: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_page = int(start_page)
        self.max_pages = int(max_pages) if max_pages not in (None, "") else None
        self.seen_urls: set[str] = set()  # ✅ Tracks seen URLs across all cities
        self.scraped_urls_count = 0

    def closed(self, reason):
        self.logger.info(
            "Listing spider finished (reason=%s). Unique event URLs scraped: %d",
            reason,
            self.scraped_urls_count,
        )

    # ── Step 1: Kick off metadata requests for each city ──────────────────────

    def start_requests(self):
        self.logger.info(
            "Starting listing crawl for %d cities (start_page=%s, max_pages=%s)",
            len(self.CITY_MAP),
            self.start_page,
            self.max_pages,
        )
        for city, alias in self.CITY_MAP.items():
            self.logger.info("[%s] Requesting metadata for alias: %s", city, alias)
            yield scrapy.Request(
                url=f"{self.METADATA_API}?limit=10&types=community&query={city}",
                callback=self.parse_metadata,
                cb_kwargs={"city": city, "expected_alias": alias},
            )

    # ── Step 2: Extract patch_id from metadata response ───────────────────────

    def parse_metadata(self, response, city: str, expected_alias: str):
        match = next(
            (item for item in response.json() if item.get("alias") == expected_alias),
            None,
        )

        if not match:
            self.logger.warning(f"[{city}] patch_id not found for alias '{expected_alias}'")
            return

        patch_id = str(match["id"])
        self.logger.info(f"[{city}] Resolved patch_id={patch_id}")
        yield from self._build_calendar_request(city, patch_id, page=self.start_page, pages_fetched=0)

    # ── Step 3: Build a calendar API request for a given page ─────────────────

    def _build_calendar_request(self, city: str, patch_id: str, page: int, pages_fetched: int):
        params = {
            "patchId": patch_id,
            "groupByDate": "true",
            "sortPromotedFirst": "true",
            "pageNumber": str(page),
        }
        yield scrapy.Request(
            url=f"{self.CALENDAR_API}?{urlencode(params)}",
            headers={"referer": f"https://patch.com{self.CITY_MAP[city]}/calendar"},
            callback=self.parse_calendar,
            dont_filter=True,
            cb_kwargs={"city": city, "patch_id": patch_id, "pages_fetched": pages_fetched},
        )

    # ── Step 4: Extract event URLs, skip duplicates, follow next page ─────────

    def parse_calendar(self, response, city: str, patch_id: str, pages_fetched: int):
        payload = response.json()
        results = payload.get("results") or {}
        self.logger.info(
            "[%s] Parsing calendar page (patch_id=%s, pages_fetched=%s)",
            city,
            patch_id,
            pages_fetched,
        )

        # results is a dict keyed by date when groupByDate=true
        for events_on_date in results.values():
            if not isinstance(events_on_date, list):
                continue

            for event in events_on_date:
                url = self._extract_url(event)

                if not url:
                    continue

                if url in self.seen_urls:
                    self.logger.debug(f"[{city}] Skipping duplicate: {url}")
                    continue

                self.seen_urls.add(url)
                self.scraped_urls_count += 1
                yield EventURLItem(city=city, event_url=url)

        # ── Pagination: follow next page if allowed ────────────────────────────
        next_page_url = payload.get("nextPageUrl")
        if not next_page_url:
            self.logger.info("[%s] No more pages.", city)
            return

        pages_fetched += 1
        if self.max_pages is not None and pages_fetched >= self.max_pages:
            return

        full_url = (
            f"{self.CALENDAR_API}{next_page_url}"
            if next_page_url.startswith("?")
            else next_page_url
        )

        yield scrapy.Request(
            url=full_url,
            callback=self.parse_calendar,
            dont_filter=True,
            cb_kwargs={"city": city, "patch_id": patch_id, "pages_fetched": pages_fetched},
        )

    # ── Helper: safely extract canonical URL from an event dict ───────────────

    def _extract_url(self, event: dict) -> str | None:
        if not isinstance(event, dict):
            return None
        canonical_path = event.get("canonicalUrl")
        return urljoin("https://patch.com", canonical_path) if canonical_path else None