from __future__ import annotations

import os
from typing import Any

from itemadapter import ItemAdapter


class UrlLinesPipeline:
    """
    Writes unique event URLs to `output/event_urls.txt`.
    """

    def __init__(self) -> None:
        self._fp = None
        self._seen: set[str] = set()

    def open_spider(self, spider: Any) -> None:
        os.makedirs("output", exist_ok=True)
        self._fp = open("output/event_urls.txt", "w", encoding="utf-8")
        self._seen.clear()

    def close_spider(self, spider: Any) -> None:
        if self._fp:
            self._fp.close()
            self._fp = None

    def process_item(self, item: Any, spider: Any = None) -> Any:
        adapter = ItemAdapter(item)
        url = adapter.get("url") or adapter.get("URL")
        if not url:
            return item

        url = str(url).strip()
        if not url:
            return item

        if url not in self._seen:
            self._seen.add(url)
            assert self._fp is not None
            self._fp.write(url + "\n")

        return item