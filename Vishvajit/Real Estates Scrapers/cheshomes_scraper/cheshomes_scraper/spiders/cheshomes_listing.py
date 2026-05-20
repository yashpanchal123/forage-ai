import re
from urllib.parse import urlparse

import scrapy

from ..items import CheshomesListingItem

SITEMAP_URL = "https://www.cheshomes.com/sitemap.xml"


class CheshomesListingSpider(scrapy.Spider):
    """
    Reads cheshomes.com sitemap and emits inventory home URLs.

    URL shapes (path segments after hostname):
    - Community hub: /new-homes/{state}/{city}/{community-slug}/{numeric-id}/  (5 segments)
    - Floorplan:     /new-homes/{state}/{city}/{community}/{plan-slug}/{plan-id}/  (6 segments;
      community id position holds a non-numeric slug)
    - Inventory:     /new-homes/{state}/{city}/{community-slug}/{community-id}/
      {homesite-slug}/{listing-id}/  (7 segments; community id & listing id are numeric)
    """

    name = "cheshomes_listing"
    allowed_domains = ["cheshomes.com", "www.cheshomes.com"]

    custom_settings = {
        "FEEDS": {
            "cheshomes_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["house_url", "community_url"],
            }
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen: set[str] = set()

    async def start(self):
        yield scrapy.Request(SITEMAP_URL, callback=self.parse_sitemap)

    def parse_sitemap(self, response):
        locs = re.findall(r"<loc>(.*?)</loc>", response.text or "", flags=re.I)
        emitted = 0
        for raw in locs:
            url = self._ensure_trailing_slash(self._normalize_url((raw or "").strip()))
            if not self._is_inventory_home_url(url):
                continue
            if url in self._seen:
                continue
            self._seen.add(url)
            emitted += 1
            yield CheshomesListingItem(
                house_url=url,
                community_url=self._community_url_from_house(url),
            )
        self.logger.info("inventory_homes_emitted=%s", emitted)

    @staticmethod
    def _normalize_url(url: str) -> str:
        clean = (url or "").strip().split("#")[0].split("?")[0].rstrip("/")
        if clean.startswith("//"):
            clean = "https:" + clean
        low = clean.lower()
        if "cheshomes.com" in low and low.startswith("http://"):
            clean = "https://" + clean[len("http://") :]
        return clean

    @staticmethod
    def _ensure_trailing_slash(url: str) -> str:
        u = (url or "").strip()
        return u if u.endswith("/") else (u + "/")

    @staticmethod
    def _path_parts(url: str) -> list[str]:
        return [p for p in urlparse(url or "").path.split("/") if p]

    @classmethod
    def _is_inventory_home_url(cls, url: str) -> bool:
        if not url or "cheshomes.com" not in url.lower():
            return False
        parts = cls._path_parts(url)
        if len(parts) != 7:
            return False
        if parts[0] != "new-homes":
            return False
        if not parts[4].isdigit() or not parts[6].isdigit():
            return False
        if parts[5].isdigit():
            return False
        return True

    @classmethod
    def _community_url_from_house(cls, house_url: str) -> str:
        parts = cls._path_parts(house_url)
        if len(parts) >= 5 and parts[0] == "new-homes":
            return cls._ensure_trailing_slash(
                "https://www.cheshomes.com/" + "/".join(parts[:5]) + "/"
            )
        return ""
