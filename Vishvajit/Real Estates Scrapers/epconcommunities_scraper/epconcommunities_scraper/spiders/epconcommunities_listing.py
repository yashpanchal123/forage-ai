import re
from urllib.parse import urlencode, urlparse

import scrapy

from ..items import EpconCommunitiesListingItem

# Resolves to the same document as www (302); using the final URL avoids an extra hop.
SITEMAP_URL = "https://api.epconcommunities.com/api/v1/sitemap.xml"
API_ORIGIN = "https://www.epconcommunities.com"
BY_NAME_URL = (
    "https://api.epconcommunities.com/api/v1/public-epcon/inventory/"
    "state/region/county/city/by-name"
)

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": API_ORIGIN,
    "referer": f"{API_ORIGIN}/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


class EpconCommunitiesListingSpider(scrapy.Spider):
    name = "epconcommunities_listing"
    allowed_domains = ["epconcommunities.com", "www.epconcommunities.com", "api.epconcommunities.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [400, 404],
        "FEEDS": {
            "epconcommunities_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["state", "house_url", "inventory_id"],
            }
        },
    }

    def __init__(self, states=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if states:
            self.state_filter = {
                s.strip().lower().replace("_", "-") for s in str(states).split(",") if s.strip()
            }
        else:
            self.state_filter = None
        self._seen_inventory_ids: set[int] = set()

    def start_requests(self):
        yield scrapy.Request(SITEMAP_URL, callback=self.parse_sitemap)

    def parse_sitemap(self, response):
        urls = re.findall(r"<loc>(.*?)</loc>", response.text or "", flags=re.I)
        scheduled = 0
        best_url_by_homesite: dict[tuple[str, ...], str] = {}

        for raw_url in urls:
            house_url = self._normalize_qmi_url(raw_url.strip())
            if not house_url or not self._is_inventory_leaf_qmi_url(house_url):
                continue
            parsed = self._parse_community_qmi_params(house_url)
            if not parsed:
                continue
            if self.state_filter is not None and parsed["state"].lower() not in self.state_filter:
                continue
            key = (
                parsed["state"].lower(),
                parsed["region"].lower(),
                parsed["county"].lower(),
                parsed["city"].lower(),
                parsed["community"].lower(),
                parsed["address"].lower(),
            )
            prev = best_url_by_homesite.get(key)
            if prev is None or house_url < prev:
                best_url_by_homesite[key] = house_url

        for house_url in sorted(best_url_by_homesite.values()):
            parsed = self._parse_community_qmi_params(house_url)
            if not parsed:
                self.logger.warning("Could not parse QMI params from %s", house_url)
                continue
            by_name_full = f"{BY_NAME_URL}?{urlencode(parsed)}"
            scheduled += 1
            yield scrapy.Request(
                by_name_full,
                method="GET",
                headers=API_HEADERS,
                callback=self.parse_by_name_resolve_id,
                meta={"state": parsed["state"], "house_url": house_url},
                dont_filter=True,
            )

        self.logger.info(
            "state_filter=%s scheduled_by_name_lookups=%s",
            ",".join(sorted(self.state_filter)) if self.state_filter else "(all)",
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

        yield EpconCommunitiesListingItem(
            state=state,
            house_url=house_url,
            inventory_id=str(inv_int),
        )

    @staticmethod
    def _normalize_qmi_url(url):
        if not url or "quick-move-in-homes" not in url.lower():
            return ""
        clean = url.split("?")[0].strip().rstrip("/")
        if clean.lower().endswith("/floorplan"):
            clean = clean[: -len("/floorplan")].rstrip("/")
        clean = re.sub(r"/(overview|map)$", "", clean, flags=re.I).rstrip("/")
        return clean

    @staticmethod
    def _is_inventory_leaf_qmi_url(normalized_url):
        """True for .../communities/.../quick-move-in-homes/{homesite-slug}."""
        if not normalized_url:
            return False
        parsed = EpconCommunitiesListingSpider._parse_community_qmi_params(normalized_url)
        if not parsed:
            return False
        slug = parsed["address"]
        if not slug or slug.lower() in ("floorplan", "quick-move-in-homes"):
            return False
        if not re.search(r"\d", slug):
            return False
        return True

    @staticmethod
    def _parse_community_qmi_params(url):
        """Return dict state, region, county, city, community, address or None."""
        if not url:
            return None
        parts = [p for p in urlparse(url).path.split("/") if p]
        try:
            idx = parts.index("communities")
        except ValueError:
            return None
        # communities, state, region, county, city, community, quick-move-in-homes, address_slug
        if len(parts) < idx + 8:
            return None
        if parts[idx + 6] != "quick-move-in-homes":
            return None
        address = parts[idx + 7]
        if not address:
            return None
        return {
            "state": parts[idx + 1],
            "region": parts[idx + 2],
            "county": parts[idx + 3],
            "city": parts[idx + 4],
            "community": parts[idx + 5],
            "address": address,
        }
