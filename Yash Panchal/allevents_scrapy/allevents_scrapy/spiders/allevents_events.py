"""
AllEvents: POST paginated list API → CSV rows + GET event pages → Forage JSON.

Run from this project directory (where scrapy.cfg lives):
  scrapy crawl allevents_events
  scrapy crawl allevents_events -a max_pages=2 -a detail_limit=10
  scrapy crawl allevents_events -a list_only=1
"""

from __future__ import annotations

import json
from typing import Any

import scrapy
from scrapy.http import Request, Response

from allevents_scrapy.allevents_config import BASE, LIST_URL, LOCATIONS

from allevents_scrapy.event_utils import (  # noqa: E402
    extract_current_event_share_fields,
    extract_event_json_ld,
    is_sports_event,
    listing_meta_to_row,
    row_to_record,
    utc_now_iso,
)
from allevents_scrapy.items import (  # noqa: E402
    ForageEventItem,
    ListingLinkItem,
    ScrapeErrorItem,
)


def list_post_headers(city_slug: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": BASE,
        "Referer": f"{BASE}/{city_slug}/all",
    }


def list_payload(
    city_slug: str,
    page: int,
    rows: int,
    country: str,
    popular: bool,
) -> dict[str, Any]:
    return {
        "city": city_slug,
        "country": country,
        "page": page,
        "rows": rows,
        "popular": popular,
        "venue": [],
        "keywords": "",
        "type": "",
        "ids": [],
        "sdate": "",
        "edate": "",
    }


def organizer_name(raw: dict[str, Any]) -> str:
    org = raw.get("organizer")
    if isinstance(org, dict):
        return str(org.get("name") or "")
    return ""


class AllEventsSpider(scrapy.Spider):
    name = "allevents_events"
    allowed_domains = []

    custom_settings = {
        "DOWNLOAD_DELAY": 0.35,
    }

    def __init__(
        self,
        max_pages: str | None = None,
        page_size: str | None = None,
        list_only: str | None = None,
        detail_limit: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._page_size_arg = page_size
        self._list_only_arg = list_only
        self._detail_limit_arg = detail_limit
        self.max_pages = int(max_pages) if max_pages else None
        self.page_size = 0
        self.list_only = False
        self.detail_limit: int | None = None
        self.country = ""
        self.popular = True
        self.og_title_xpath = ""
        self.twitter_title_xpath = ""
        self.description_xpath = ""
        self.description_fallback_xpath = ""
        self._detail_scheduled = 0
        self._now = utc_now_iso()

    @classmethod
    def from_crawler(cls, crawler, *args: Any, **kwargs: Any):
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings
        spider.allowed_domains = list(settings.getlist("ALLEVENTS_ALLOWED_DOMAINS"))
        spider.country = str(settings.get("ALLEVENTS_COUNTRY"))
        spider.popular = bool(settings.getbool("ALLEVENTS_POPULAR"))
        spider.page_size = int(
            spider._page_size_arg or settings.getint("ALLEVENTS_LIST_PAGE_SIZE_DEFAULT")
        )
        list_only_raw = spider._list_only_arg or str(
            settings.get("ALLEVENTS_LIST_ONLY_DEFAULT")
        )
        spider.list_only = list_only_raw.lower() in ("1", "true", "yes")
        detail_limit_raw = spider._detail_limit_arg or str(
            settings.getint("ALLEVENTS_DETAIL_LIMIT_DEFAULT")
        )
        spider.detail_limit = int(detail_limit_raw) if detail_limit_raw else None
        spider.og_title_xpath = str(settings.get("ALLEVENTS_OG_TITLE_XPATH"))
        spider.twitter_title_xpath = str(settings.get("ALLEVENTS_TWITTER_TITLE_XPATH"))
        spider.description_xpath = str(settings.get("ALLEVENTS_DESCRIPTION_XPATH"))
        spider.description_fallback_xpath = str(
            settings.get("ALLEVENTS_DESCRIPTION_FALLBACK_XPATH")
        )
        spider.logger.info(
            "Initialized spider: max_pages=%s page_size=%s list_only=%s detail_limit=%s",
            spider.max_pages,
            spider.page_size,
            spider.list_only,
            spider.detail_limit,
        )
        return spider

    def start_requests(self) -> Any:
        self.logger.info("Starting listing requests for %s locations", len(LOCATIONS))
        for loc in LOCATIONS:
            slug = loc["city_slug"]
            meta = {
                "loc": loc,
                "page": 0,
            }
            body = json.dumps(
                list_payload(
                    slug,
                    page=0,
                    rows=self.page_size,
                    country=self.country,
                    popular=self.popular,
                )
            ).encode("utf-8")
            self.logger.debug("Queue listing request for city=%s page=0", slug)
            yield Request(
                url=LIST_URL,
                method="POST",
                body=body,
                headers=list_post_headers(slug),
                callback=self.parse_list,
                meta=meta,
                dont_filter=True,
            )

    def parse_list(self, response: Response) -> Any:
        loc = response.meta["loc"]
        page = response.meta["page"]
        slug = loc["city_slug"]

        try:
            body = json.loads(response.text)
        except json.JSONDecodeError as e:
            self.logger.error("List JSON error for %s page %s: %s", slug, page, e)
            return

        if body.get("error"):
            self.logger.error("List API error for %s: %s", slug, body)
            return

        data = body.get("data") or []
        if not isinstance(data, list):
            self.logger.error("Unexpected list data type for %s", slug)
            return
        self.logger.info("List page fetched city=%s page=%s items=%s", slug, page, len(data))

        for e in data:
            url = (e.get("event_url") or "").strip()
            eid = e.get("event_id")
            if not url or eid is None:
                continue

            cats = e.get("categories") or []
            tags = e.get("tags") or []
            if not isinstance(cats, list):
                cats = []
            if not isinstance(tags, list):
                tags = []
            if is_sports_event(listing=e, title=str(e.get("eventname_raw") or e.get("eventname") or "")):
                continue

            link_item = ListingLinkItem(
                event_url=url,
                event_id=str(eid),
                eventname_raw=str(e.get("eventname_raw") or ""),
                eventname=str(e.get("eventname") or ""),
                listing_json=json.dumps(e, ensure_ascii=False),
                city_slug=slug,
                market_name=loc["name"],
                state=loc["state"],
                tegna_market=str(loc["tegna_market"]).lower(),
                tegna_label=(
                    "Tegna Market"
                    if loc["tegna_market"]
                    else "Non - Tegna Market"
                ),
                event_tz=str(loc.get("timezone") or ""),
                organizer_name=organizer_name(e),
                categories_json=json.dumps(cats, ensure_ascii=False),
                tags_json=json.dumps(tags, ensure_ascii=False),
            )
            yield link_item

            if self.list_only:
                continue
            if self.detail_limit is not None and self._detail_scheduled >= self.detail_limit:
                continue

            self._detail_scheduled += 1
            yield Request(
                url=url,
                callback=self.parse_event,
                meta={
                    "event_url": url,
                    "event_id": str(eid),
                    "eventname_raw": str(e.get("eventname_raw") or ""),
                    "eventname": str(e.get("eventname") or ""),
                    "listing_json": json.dumps(e, ensure_ascii=False),
                    "city_slug": slug,
                    "market_name": loc["name"],
                    "state": loc["state"],
                    "tegna_market": str(loc["tegna_market"]).lower(),
                    "tegna_label": (
                        "Tegna Market"
                        if loc["tegna_market"]
                        else "Non - Tegna Market"
                    ),
                    "event_tz": str(loc.get("timezone") or ""),
                    "organizer_name": organizer_name(e),
                    "categories_json": json.dumps(cats, ensure_ascii=False),
                    "tags_json": json.dumps(tags, ensure_ascii=False),
                },
            )

        if len(data) < self.page_size:
            return
        if not self.list_only and self.detail_limit is not None:
            if self._detail_scheduled >= self.detail_limit:
                return
        next_page = page + 1
        if self.max_pages is not None and next_page >= self.max_pages:
            return

        body = json.dumps(
            list_payload(
                slug,
                page=next_page,
                rows=self.page_size,
                country=self.country,
                popular=self.popular,
            )
        ).encode("utf-8")
        self.logger.debug("Queue next listing page city=%s page=%s", slug, next_page)
        yield Request(
            url=LIST_URL,
            method="POST",
            body=body,
            headers=list_post_headers(slug),
            callback=self.parse_list,
            meta={"loc": loc, "page": next_page},
            dont_filter=True,
        )

    def parse_event(self, response: Response) -> Any:
        url = response.meta["event_url"]
        row = listing_meta_to_row(response.meta)
        ld = extract_event_json_ld(response.text)
        current_share = extract_current_event_share_fields(response.text)
        listing = {}
        listing_raw = row.get("listing_json") or ""
        if listing_raw:
            try:
                parsed = json.loads(listing_raw)
                if isinstance(parsed, dict):
                    listing = parsed
            except Exception:
                listing = {}

        html_title = response.xpath(self.og_title_xpath).get()
        if not html_title:
            html_title = response.xpath(self.twitter_title_xpath).get()
        if html_title:
            html_title = html_title.strip()
        if is_sports_event(listing=listing, ld=ld, title=html_title or row.get("eventname_raw")):
            return

        desc_nodes = response.xpath(self.description_xpath).getall()
        if not desc_nodes:
            desc_nodes = response.xpath(self.description_fallback_xpath).getall()
        html_description = " ".join(t.strip() for t in desc_nodes if t and t.strip())

        try:
            record = row_to_record(
                response,
                row,
                ld,
                now=self._now,
                html_title=html_title,
                html_description=html_description,
                current_share=current_share,
            )
        except Exception as ex:  # noqa: BLE001
            yield ScrapeErrorItem(event_url=url, error=str(ex))
            return

        if ld is None:
            yield ScrapeErrorItem(event_url=url, error="no_json_ld_event")
            self.logger.debug("No JSON-LD for event_url=%s (fallback data used)", url)
        yield ForageEventItem(record=record, event_url=url)
