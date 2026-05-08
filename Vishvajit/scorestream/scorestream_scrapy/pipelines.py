from __future__ import annotations

import csv
import json
import threading
from pathlib import Path
from typing import Any

from scrapy import Spider

from scorestream_scrapy.items import ForageEventItem, ListingLinkItem, ScrapeErrorItem


def baseline_dedupe_key(record: dict[str, Any]) -> str:
    title = str(record.get("title") or "").strip().lower()
    venue = str((record.get("location") or {}).get("name") or "").strip().lower()
    start = (
        (((record.get("metadata") or {}).get("sport") or {}).get("eventSchedule") or {})
        .get("start", {})
        .get("value")
    )
    date = ""
    if isinstance(start, str) and len(start) >= 10:
        date = start[:10]
    return f"{title}|{venue}|{date}"


class ListingCsvPipeline:
    """Write listing rows to CSV (thread-safe)."""

    def open_spider(self, spider: Spider) -> None:
        if spider.name != "scorestream_listing":
            self._skip = True
            return
        self._skip = False
        self._lock = threading.Lock()
        self._seen_game_ids: set[str] = set()
        path = Path(spider.settings["SCORESTREAM_LISTING_CSV_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._file = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(
            self._file,
            fieldnames=[
                "market_label",
                "game_id",
                "game_url",
                "min_url",
                "state_slug",
                "state_code",
                "sport_name",
                "organization_id",
                "squad_ids_json",
                "listing_json",
            ],
        )
        self._writer.writeheader()

    def close_spider(self, spider: Spider) -> None:
        if getattr(self, "_skip", False):
            return
        self._file.close()

    def process_item(self, item: Any, spider: Spider) -> Any:
        if getattr(self, "_skip", False):
            return item
        if not isinstance(item, ListingLinkItem):
            return item
        game_id = str(item.get("game_id") or "").strip()
        if not game_id:
            return item
        row = {k: item.get(k) for k in self._writer.fieldnames}
        with self._lock:
            if game_id in self._seen_game_ids:
                return item
            self._seen_game_ids.add(game_id)
            self._writer.writerow(row)
            self._file.flush()
        return item


class ForageJsonPipeline:
    """Stream-write ForageEventItem JSON array; log errors; dedupe baseline key."""

    def open_spider(self, spider: Spider) -> None:
        if spider.name != "scorestream_scrape":
            self._skip = True
            return
        self._skip = False
        self._lock = threading.Lock()
        self._errors: list[dict[str, str]] = []
        self._dedupe: set[str] = set()
        self._written_records = 0
        self._first_record = True
        self._json_path = Path(spider.settings["SCORESTREAM_JSON_PATH"])
        self._err_path = Path(spider.settings["SCORESTREAM_ERRORS_JSON_PATH"])
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        self._json_file = self._json_path.open("w", encoding="utf-8")
        self._json_file.write("[\n")
        self._json_file.flush()

    def close_spider(self, spider: Spider) -> None:
        if getattr(self, "_skip", False):
            return
        with self._lock:
            self._json_file.write("\n]\n")
            self._json_file.flush()
            self._json_file.close()
        if self._errors:
            with self._err_path.open("w", encoding="utf-8") as f:
                json.dump(self._errors, f, ensure_ascii=False, indent=2)
            spider.logger.info(
                "Logged %s scrape issues to %s",
                len(self._errors),
                self._err_path,
            )
        spider.logger.info("Wrote %s records to %s", self._written_records, self._json_path)

    def process_item(self, item: Any, spider: Spider) -> Any:
        if getattr(self, "_skip", False):
            return item
        if isinstance(item, ForageEventItem):
            record = item["record"]
            key = baseline_dedupe_key(record)
            with self._lock:
                if key in self._dedupe:
                    return item
                self._dedupe.add(key)
                if not self._first_record:
                    self._json_file.write(",\n")
                self._json_file.write(json.dumps(record, ensure_ascii=False, indent=2))
                self._json_file.flush()
                self._first_record = False
                self._written_records += 1
            return item
        if isinstance(item, ScrapeErrorItem):
            with self._lock:
                self._errors.append(
                    {"game_url": item["game_url"], "error": item["error"]}
                )
            return item
        return item

