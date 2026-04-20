"""
Detail-only crawl: read rows from event_links.csv (from list_events.py or list phase).

  scrapy crawl allevents_from_csv -a csv_path=../event_links.csv
  scrapy crawl allevents_from_csv -a csv_path=../event_links.csv -a detail_limit=50
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import scrapy
from scrapy.http import Response

from allevents_scrapy.event_utils import (
    extract_current_event_share_fields,
    extract_event_json_ld,
    is_sports_event,
    listing_meta_to_row,
    row_to_record,
    utc_now_iso,
)
from allevents_scrapy.items import ForageEventItem, ScrapeErrorItem


class AllEventsFromCsvSpider(scrapy.Spider):
    name = "allevents_from_csv"
    allowed_domains = []

    def __init__(
        self,
        csv_path: str | None = None,
        detail_limit: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._csv_path_arg = csv_path
        self._detail_limit_arg = detail_limit
        self.csv_path = ""
        self.detail_limit: int | None = None
        self.og_title_xpath = ""
        self.twitter_title_xpath = ""
        self.description_xpath = ""
        self.description_fallback_xpath = ""
        self._now = utc_now_iso()
        self._count = 0

    @classmethod
    def from_crawler(cls, crawler, *args: Any, **kwargs: Any):
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings
        spider.allowed_domains = list(settings.getlist("ALLEVENTS_ALLOWED_DOMAINS"))
        spider.csv_path = spider._csv_path_arg or str(
            settings.get("ALLEVENTS_CSV_PATH_DEFAULT")
        )
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
            "Initialized CSV spider: csv_path=%s detail_limit=%s",
            spider.csv_path,
            spider.detail_limit,
        )
        return spider

    def start_requests(self) -> Any:
        path = Path(self.csv_path)
        if not path.is_file():
            path = Path.cwd() / self.csv_path
        if not path.is_file():
            self.logger.error("CSV not found: %s", self.csv_path)
            return
        self.logger.info("Reading CSV input from %s", path)

        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if self.detail_limit is not None and self._count >= self.detail_limit:
                    break
                url = (row.get("event_url") or "").strip()
                if not url:
                    continue
                self._count += 1
                if self._count % 100 == 0:
                    self.logger.info("Queued %s detail requests from CSV", self._count)
                yield scrapy.Request(
                    url=url,
                    callback=self.parse_event,
                    meta=dict(row),
                )

    def parse_event(self, response: Response) -> Any:
        url = response.meta.get("event_url") or response.url
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
