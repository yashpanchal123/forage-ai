from __future__ import annotations

import re
from urllib.parse import urldefrag, urlparse
import xml.etree.ElementTree as ET

import scrapy

from ..items import EastwoodHomesListingItem

BASE = "https://www.eastwoodhomes.com"
SITEMAP_URL = f"{BASE}/sitemap.xml"
DEFAULT_STATES = {"nc", "sc", "va", "ga"}
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class EastwoodHomesListingSpider(scrapy.Spider):
    name = "eastwoodhomes_listing"
    allowed_domains = ["eastwoodhomes.com", "www.eastwoodhomes.com"]

    custom_settings = {
        "FEEDS": {
            "eastwoodhomes_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["state", "community_url", "house_url"],
            }
        }
    }

    def __init__(self, states=None, sitemap_url=SITEMAP_URL, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.states = self._normalize_states(states)
        self.sitemap_url = str(sitemap_url or SITEMAP_URL).strip()
        self._seen_house_urls: set[str] = set()

    def start_requests(self):
        yield scrapy.Request(
            self.sitemap_url,
            callback=self.parse_sitemap,
            headers=REQUEST_HEADERS,
            dont_filter=True,
        )

    def parse_sitemap(self, response):
        if response.status != 200:
            self.logger.error("Unable to fetch sitemap HTTP %s %s", response.status, response.url)
            return

        sitemap_xml = response.text or ""

        # User requirement:
        # - only URLs with <priority>0.7</priority>
        # - exclude anything ending with -floor-plan
        # - keep only actual home URLs
        for loc, priority in self._iter_sitemap_entries(sitemap_xml):
            if priority != "0.7":
                continue
            url = self._normalize_url(loc)
            if not self._is_house_url(url):
                continue
            if url.lower().endswith("-floor-plan"):
                continue
            if url in self._seen_house_urls:
                continue

            state = self._state_from_url(url)
            if state and state not in self.states:
                continue

            self._seen_house_urls.add(url)
            yield EastwoodHomesListingItem(
                state=state,
                community_url=self._community_url_from_house(url),
                house_url=url,
            )

    def _normalize_states(self, states_arg) -> set[str]:
        if not states_arg:
            return set(DEFAULT_STATES)
        out = set()
        for raw in str(states_arg).split(","):
            token = raw.strip().lower().replace("_", "-")
            if not token:
                continue
            if token in {"north-carolina", "north carolina"}:
                out.add("nc")
            elif token in {"south-carolina", "south carolina"}:
                out.add("sc")
            elif token == "virginia":
                out.add("va")
            elif token == "georgia":
                out.add("ga")
            elif token in {"nc", "sc", "va", "ga"}:
                out.add(token)
        return out or set(DEFAULT_STATES)

    @staticmethod
    def _normalize_url(url: str) -> str:
        raw = str(url or "").strip()
        if not raw:
            return ""
        clean, _ = urldefrag(raw)
        clean = clean.split("?")[0].strip()
        if clean.startswith("http://") or clean.startswith("https://"):
            return clean.rstrip("/")
        if clean.startswith("/"):
            return f"{BASE}{clean}".rstrip("/")
        return f"{BASE}/{clean}".rstrip("/")

    @staticmethod
    def _is_house_url(url: str) -> bool:
        low = str(url or "").lower()
        path = urlparse(low).path
        return "-home-" in path and not path.endswith("-floor-plan") and "/thank-you" not in path

    @staticmethod
    def _community_url_from_house(house_url: str) -> str:
        parsed = urlparse(house_url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 4:
            return ""
        community_parts = parts[:3]
        return f"{BASE}/" + "/".join(community_parts)

    @staticmethod
    def _state_from_url(url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return ""
        first = parts[0].lower()
        if first in {"nc", "sc", "va", "ga"}:
            return first
        return ""

    @staticmethod
    def _iter_sitemap_entries(sitemap_xml: str):
        try:
            root = ET.fromstring(sitemap_xml)
        except ET.ParseError:
            return []

        entries = []
        for node in root.findall(".//{*}url"):
            loc = (node.findtext("{*}loc") or "").strip()
            priority = (node.findtext("{*}priority") or "").strip()
            if loc:
                entries.append((loc, priority))
        return entries
