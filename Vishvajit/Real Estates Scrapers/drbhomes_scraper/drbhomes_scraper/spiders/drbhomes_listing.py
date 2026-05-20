import random
import re
from urllib.parse import urlencode, urlparse

import scrapy

from ..items import DrbHomesListingItem

SITEMAP_URL = "https://www.drbhomes.com/sitemap.xml"
API_ORIGIN = "https://www.drbhomes.com"
BY_NAME_URL = "https://api.drbhomes.com/api/v1/public/inventory/state/region/by-name"

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": API_ORIGIN,
    "referer": f"{API_ORIGIN}/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
BASE_PREFIX = "https://www.drbhomes.com/drbhomes/find-your-home/communities/"
# Sitemap also lists QMI *section* and *plan* URLs (not a specific homesite), e.g.
#   .../community/quick-move-in-homes
#   .../community/home-plans/burgess/quick-move-in-homes
# Only emit true inventory leaves: .../community/quick-move-in-homes/{address-slug}
AVAILABLE_STATES = [
    "alabama",
    "arizona",
    "colorado",
    "delaware",
    "florida",
    "georgia",
    "maryland",
    "north-carolina",
    "pennsylvania",
    "south-carolina",
    "tennessee",
    "texas",
    "virginia",
    "west-virginia",
]


class DrbHomesListingSpider(scrapy.Spider):
    name = "drbhomes_listing"
    allowed_domains = ["drbhomes.com", "www.drbhomes.com", "api.drbhomes.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [400, 404],
        "FEEDS": {
            "drbhomes_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["state", "house_url", "inventory_id"],
            }
        }
    }

    def __init__(self, states=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if states:
            self.states = [s.strip().lower() for s in str(states).split(",") if s.strip()]
        else:
            self.states = random.sample(AVAILABLE_STATES, 7)
        self._seen_inventory_ids: set[int] = set()

    def start_requests(self):   
        yield scrapy.Request(SITEMAP_URL, callback=self.parse_sitemap)

    def parse_sitemap(self, response):
        urls = re.findall(r"<loc>(.*?)</loc>", response.text or "", flags=re.I)
        scheduled = 0
        for state in self.states:
            state_urls = [u for u in urls if f"/communities/{state}/" in u and "/drbhomes/" in u]
            # Dedupe same physical homesite across duplicate metro URLs in sitemap.
            best_url_by_homesite = {}
            for url in state_urls:
                home_url = self._normalize_qmi_url(url)
                if not home_url or not self._is_inventory_leaf_qmi_url(home_url):
                    continue
                parts = [p for p in urlparse(home_url).path.split("/") if p]
                idx = parts.index("communities")
                homesite_key = (parts[idx + 1], parts[idx + 3], parts[idx + 5])
                prev = best_url_by_homesite.get(homesite_key)
                if prev is None or home_url < prev:
                    best_url_by_homesite[homesite_key] = home_url

            for house_url in sorted(best_url_by_homesite.values()):
                parsed = self._parse_quick_move_in_params(house_url)
                if not parsed:
                    self.logger.warning("Could not parse by-name params from %s", house_url)
                    continue
                by_name_full = f"{BY_NAME_URL}?{urlencode(parsed)}"
                scheduled += 1
                yield scrapy.Request(
                    by_name_full,
                    method="GET",
                    headers=API_HEADERS,
                    callback=self.parse_by_name_resolve_id,
                    meta={"state": state, "house_url": house_url},
                    dont_filter=True,
                )
        self.logger.info(
            "Selected states=%s scheduled_by_name_lookups=%s (rows emitted only when id resolves)",
            ",".join(self.states),
            scheduled,
        )

    def parse_by_name_resolve_id(self, response):
        state = (response.meta.get("state") or "").strip().lower()
        house_url = (response.meta.get("house_url") or "").strip().rstrip("/")

        if response.status != 200:
            self.logger.debug(
                "listing skip no id: by_name status=%s url=%s", response.status, house_url
            )
            return
        try:
            payload = response.json()
        except Exception:
            self.logger.warning("listing skip: by_name not JSON url=%s", house_url)
            return

        inv_id = payload.get("id")
        if inv_id is None:
            self.logger.debug("listing skip: by_name missing id url=%s", house_url)
            return
        try:
            inv_int = int(inv_id)
        except (TypeError, ValueError):
            self.logger.warning("listing skip: invalid id=%r url=%s", inv_id, house_url)
            return

        if inv_int in self._seen_inventory_ids:
            self.logger.debug(
                "listing skip duplicate inventory_id=%s url=%s", inv_int, house_url
            )
            return
        self._seen_inventory_ids.add(inv_int)

        # Only write CSV rows when by-name returned an id (no row for 400 / missing id / duplicates).
        yield DrbHomesListingItem(
            state=state,
            house_url=house_url,
            inventory_id=str(inv_int),
        )

    @staticmethod
    def _normalize_qmi_url(url):
        if not url or "quick-move-in-homes" not in url.lower():
            return ""
        clean = url.split("?")[0].strip()
        clean = re.sub(r"/(overview|map|floorplan)$", "", clean, flags=re.I).rstrip("/")
        return clean

    @staticmethod
    def _is_inventory_leaf_qmi_url(normalized_url):
        """True only for .../communities/{st}/{region}/{community}/quick-move-in-homes/{slug}."""
        if not normalized_url:
            return False
        low = normalized_url.lower()
        if "home-plans" in low:
            return False
        parts = [p for p in urlparse(normalized_url).path.split("/") if p]
        try:
            idx = parts.index("communities")
        except ValueError:
            return False
        # communities, state, region, community, quick-move-in-homes, address_slug
        if len(parts) != idx + 6:
            return False
        if parts[idx + 4] != "quick-move-in-homes":
            return False
        slug = parts[idx + 5]
        if not slug or slug.lower() in ("overview", "map", "floorplan", "quick-move-in-homes"):
            return False
        # Homesite slugs from DRB almost always include a lot or street number.
        if not re.search(r"\d", slug):
            return False
        return True

    @staticmethod
    def _parse_quick_move_in_params(url):
        """Return dict state, region, community, address for by-name API, or None."""
        if not url:
            return None
        parts = [p for p in urlparse(url).path.split("/") if p]
        try:
            idx = parts.index("communities")
        except ValueError:
            return None
        if idx + 5 >= len(parts) or parts[idx + 4] != "quick-move-in-homes":
            return None
        return {
            "state": parts[idx + 1],
            "region": parts[idx + 2],
            "community": parts[idx + 3],
            "address": parts[idx + 5],
        }
