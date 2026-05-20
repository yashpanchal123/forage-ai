"""
Fischer Homes listing spider: move-in-ready (QMI) URLs per metro region via public API.

Seed: homepage embeds `var regions = [...]` with region ids and state codes.
Inventory: GET /api/region-revamped/homes/{regionId}?page=N (same as the site Angular app).
"""

from __future__ import annotations

import json
from urllib.parse import urldefrag

import scrapy

from ..items import FischerHomesListingItem

BASE = "https://www.fischerhomes.com"

# First-crawl default: five states (2-letter codes or full names).
DEFAULT_STATE_CODES = ("GA", "OH", "KY", "IN", "NC")

STATE_CODE_TO_SLUG = {
    "GA": "georgia",
    "OH": "ohio",
    "KY": "kentucky",
    "IN": "indiana",
    "NC": "north-carolina",
    "MO": "missouri",
    "FL": "florida",
}

STATE_NAME_TO_CODE = {
    "georgia": "GA",
    "ohio": "OH",
    "kentucky": "KY",
    "indiana": "IN",
    "north carolina": "NC",
    "north-carolina": "NC",
    "missouri": "MO",
    "florida": "FL",
}
# Aliases users may pass with -a states=
for _slug, _code in list(STATE_NAME_TO_CODE.items()):
    STATE_NAME_TO_CODE.setdefault(_slug.replace("-", " "), _code)


class FischerHomesListingSpider(scrapy.Spider):
    name = "fischerhomes_listing"
    allowed_domains = ["fischerhomes.com", "www.fischerhomes.com"]

    custom_settings = {
        "FEEDS": {
            "fischerhomes_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["state", "community_url", "house_url", "card_id"],
            }
        }
    }

    def __init__(self, states=None, seed_url=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seed_url = (seed_url or "").strip() or f"{BASE}/"
        if states:
            self.state_codes = self._normalize_state_codes(
                [s.strip() for s in str(states).split(",") if s.strip()]
            )
        else:
            self.state_codes = set(DEFAULT_STATE_CODES)
        self._seen_house_urls: set[str] = set()

    def start_requests(self):
        yield scrapy.Request(self.seed_url, callback=self.parse_regions_seed)

    def parse_regions_seed(self, response):
        regions = self._extract_regions_json(response.text or "")
        if not regions:
            self.logger.error("Could not parse var regions from %s", response.url)
            return

        metro_ids = self._metro_region_ids_for_states(regions, self.state_codes)
        self.logger.info(
            "states=%s matched_region_ids=%s",
            ",".join(sorted(self.state_codes)),
            ",".join(str(i) for i in sorted(metro_ids)),
        )

        for rid in sorted(metro_ids):
            yield scrapy.Request(
                f"{BASE}/api/region-revamped/homes/{rid}?page=1",
                callback=self.parse_homes_page,
                meta={
                    "region_id": rid,
                    "page": 1,
                    "state_lookup": self._region_id_to_state(regions),
                },
            )

    def parse_homes_page(self, response):
        region_id = response.meta["region_id"]
        page = int(response.meta.get("page") or 1)
        state_lookup = response.meta.get("state_lookup") or {}

        try:
            payload = response.json()
        except Exception:
            self.logger.warning("Non-JSON homes response region=%s page=%s", region_id, page)
            return

        bucket = payload.get("move-in-ready") or {}
        rows = bucket.get("data") or []
        last_page = int(bucket.get("last_page") or 1)
        state_code = state_lookup.get(region_id, "")

        for row in rows:
            if not isinstance(row, dict):
                continue
            path = str(row.get("url") or "").strip()
            card_id = row.get("id")
            if not path:
                continue
            house_url = self._normalize_url(path)
            if not house_url or house_url in self._seen_house_urls:
                continue
            self._seen_house_urls.add(house_url)

            cid = ""
            try:
                cid = str(int(card_id)) if card_id is not None else ""
            except (TypeError, ValueError):
                cid = str(card_id).strip() if card_id is not None else ""

            state_slug = STATE_CODE_TO_SLUG.get(state_code, (state_code or "").lower())
            yield FischerHomesListingItem(
                state=state_slug,
                community_url="",
                house_url=house_url,
                card_id=cid,
            )

        if page < last_page:
            next_page = page + 1
            yield scrapy.Request(
                f"{BASE}/api/region-revamped/homes/{region_id}?page={next_page}",
                callback=self.parse_homes_page,
                meta={
                    "region_id": region_id,
                    "page": next_page,
                    "state_lookup": state_lookup,
                },
            )

    @staticmethod
    def _extract_regions_json(html: str):
        marker = "var regions = "
        idx = html.find(marker)
        if idx == -1:
            return []
        i = idx + len(marker)
        while i < len(html) and html[i] in " \t\r\n":
            i += 1
        if i >= len(html) or html[i] != "[":
            return []
        depth = 0
        start = i
        for j in range(i, len(html)):
            c = html[j]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start : j + 1])
                    except json.JSONDecodeError:
                        return []
        return []

    def _metro_region_ids_for_states(self, regions, wanted_codes: set[str]) -> set[int]:
        wanted = {c.upper() for c in wanted_codes}
        out_set: set[int] = set()
        for region in regions:
            if not isinstance(region, dict):
                continue
            code = str(region.get("state") or "").strip().upper()
            if code in wanted:
                rid = region.get("id")
                try:
                    out_set.add(int(rid))
                except (TypeError, ValueError):
                    continue
        return out_set

    @staticmethod
    def _region_id_to_state(regions):
        m = {}
        for region in regions:
            if not isinstance(region, dict):
                continue
            try:
                rid = int(region.get("id"))
            except (TypeError, ValueError):
                continue
            code = str(region.get("state") or "").strip().upper()
            if code:
                m[rid] = code
        return m

    def _normalize_state_codes(self, parts: list[str]) -> set[str]:
        out: set[str] = set()
        for raw in parts:
            p = raw.strip().lower().replace("_", "-")
            if len(p) == 2 and p.isalpha():
                out.add(p.upper())
                continue
            if p in STATE_NAME_TO_CODE:
                out.add(STATE_NAME_TO_CODE[p])
                continue
            # e.g. "north carolina"
            plain = raw.strip().lower()
            if plain in STATE_NAME_TO_CODE:
                out.add(STATE_NAME_TO_CODE[plain])
        return out if out else set(DEFAULT_STATE_CODES)

    @staticmethod
    def _normalize_url(path_or_url: str) -> str:
        raw = str(path_or_url or "").strip()
        if not raw:
            return ""
        clean, _ = urldefrag(raw)
        clean = clean.split("?")[0].strip()
        if clean.startswith("http://") or clean.startswith("https://"):
            return clean.rstrip("/")
        return f"{BASE}{clean if clean.startswith('/') else '/' + clean}".rstrip("/")
