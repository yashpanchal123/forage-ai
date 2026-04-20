from __future__ import annotations

import csv
import json
import logging
import threading
from pathlib import Path
from typing import Any

from scrapy import Spider

from allevents_scrapy.items import ForageEventItem, ListingLinkItem, ScrapeErrorItem

logger = logging.getLogger(__name__)


class ListingCsvPipeline:
    """Append listing rows to CSV (thread-safe)."""

    def open_spider(self, spider: Spider) -> None:
        if spider.name != "allevents_events":
            self._skip = True
            return
        self._skip = False
        self._lock = threading.Lock()
        path = Path(spider.settings["ALLEVENTS_CSV_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._file = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(
            self._file,
            fieldnames=[
                "event_url",
                "event_id",
                "eventname_raw",
                "eventname",
                "listing_json",
                "city_slug",
                "market_name",
                "state",
                "tegna_market",
                "tegna_label",
                "event_tz",
                "organizer_name",
                "categories_json",
                "tags_json",
            ],
        )
        self._writer.writeheader()
        logger.info("Listing CSV pipeline opened: %s", self._path)

    def close_spider(self, spider: Spider) -> None:
        if getattr(self, "_skip", False):
            return
        self._file.close()
        logger.info("Listing CSV pipeline closed: %s", self._path)

    def process_item(self, item: Any, spider: Spider) -> Any:
        if getattr(self, "_skip", False):
            return item
        if not isinstance(item, ListingLinkItem):
            return item
        row = {k: item.get(k) for k in self._writer.fieldnames}
        with self._lock:
            self._writer.writerow(row)
            self._file.flush()
        return item


class ForageJsonPipeline:
    """Collect ForageEventItem into a JSON array; log errors."""

    def open_spider(self, spider: Spider) -> None:
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._errors: list[dict[str, str]] = []
        self._json_path = Path(spider.settings["ALLEVENTS_JSON_PATH"])
        self._err_path = Path(spider.settings["ALLEVENTS_ERRORS_JSON_PATH"])
        logger.info(
            "Forage JSON pipeline opened: json=%s errors=%s",
            self._json_path,
            self._err_path,
        )

    def close_spider(self, spider: Spider) -> None:
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        with self._json_path.open("w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False, indent=2)
        if self._errors:
            with self._err_path.open("w", encoding="utf-8") as f:
                json.dump(self._errors, f, ensure_ascii=False, indent=2)
            spider.logger.info(
                "Logged %s scrape issues to %s",
                len(self._errors),
                self._err_path,
            )
        spider.logger.info("Wrote %s records to %s", len(self._records), self._json_path)

    def process_item(self, item: Any, spider: Spider) -> Any:
        if isinstance(item, ForageEventItem):
            with self._lock:
                self._records.append(item["record"])
            return item
        if isinstance(item, ScrapeErrorItem):
            with self._lock:
                self._errors.append(
                    {"event_url": item["event_url"], "error": item["error"]}
                )
            return item
        return item
