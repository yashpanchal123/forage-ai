import re
from urllib.parse import urljoin, urlparse

import scrapy

from ..items import NealCommunitiesListingItem

SITEMAP_INDEX_URL = "https://www.nealcommunities.com/sitemap_index.xml"
FALLBACK_COMMUNITY_SITEMAP = "https://www.nealcommunities.com/community-sitemap.xml"


class NealCommunitiesListingSpider(scrapy.Spider):
    """
    1) Fetch sitemap_index.xml and locate community-sitemap.xml.
    2) From community-sitemap.xml, collect each community hub URL
       (/new-homes/{community}/).
    3) Crawl each community page and collect inventory home URLs
       (/new-homes/{community}/{plan}/{homesite-slug}/).
    """

    name = "nealcommunities_listing"
    allowed_domains = ["nealcommunities.com", "www.nealcommunities.com"]

    custom_settings = {
        "FEEDS": {
            "nealcommunities_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["house_url", "community_url"],
            }
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_house_urls: set[str] = set()

    # ------------------------------------------------------------------ #
    #  Entry point — async start() replaces deprecated start_requests()   #
    # ------------------------------------------------------------------ #
    async def start(self):
        yield scrapy.Request(SITEMAP_INDEX_URL, callback=self.parse_sitemap_index)

    # ------------------------------------------------------------------ #
    #  Sitemap index                                                       #
    # ------------------------------------------------------------------ #
    def parse_sitemap_index(self, response):
        locs = re.findall(r"<loc>(.*?)</loc>", response.text or "", flags=re.I)
        community_sitemap = ""
        for raw in locs:
            u = (raw or "").strip()
            if "community-sitemap.xml" in u:
                community_sitemap = self._normalize_url(u)
                break

        if not community_sitemap:
            self.logger.warning(
                "community-sitemap.xml not found in sitemap index; using fallback %s",
                FALLBACK_COMMUNITY_SITEMAP,
            )
            community_sitemap = FALLBACK_COMMUNITY_SITEMAP
        else:
            self.logger.info("Resolved community sitemap: %s", community_sitemap)

        yield scrapy.Request(community_sitemap, callback=self.parse_community_sitemap)

    # ------------------------------------------------------------------ #
    #  Community sitemap                                                   #
    # ------------------------------------------------------------------ #
    def parse_community_sitemap(self, response):
        locs = re.findall(r"<loc>(.*?)</loc>", response.text or "", flags=re.I)
        scheduled = 0
        for raw in locs:
            # Normalize first, then add trailing slash so we hit the
            # canonical URL directly and avoid the 301 redirect round-trip.
            url = self._ensure_trailing_slash(
                self._normalize_url((raw or "").strip())
            )
            # _is_community_hub_url works on path without trailing slash
            if not self._is_community_hub_url(url):
                continue
            scheduled += 1
            yield scrapy.Request(url, callback=self.parse_community_page)

        self.logger.info(
            "community_sitemap communities_scheduled=%s (source=%s)",
            scheduled,
            response.url,
        )

    # ------------------------------------------------------------------ #
    #  Community page                                                      #
    # ------------------------------------------------------------------ #
    def parse_community_page(self, response):
        emitted_here = 0
        candidates: set[str] = set()

        hub_parts = [p for p in urlparse(response.url).path.split("/") if p]
        hub_slug = hub_parts[1].lower() if len(hub_parts) >= 2 else ""

        # -- collect from <a href> attributes --
        for href in response.xpath("//@href").getall():
            if not href or href.startswith("#"):
                continue
            full = urljoin(response.url, href)
            if self._is_inventory_home_url(full):
                candidates.add(self._normalize_url(full))

        # -- collect from raw page text (JS / JSON blobs) --
        for m in re.finditer(
            r"https://www\.nealcommunities\.com/new-homes/[^\s\"'<>]+",
            response.text or "",
            flags=re.I,
        ):
            u = self._normalize_url(m.group(0))
            if self._is_inventory_home_url(u):
                candidates.add(u)

        for norm in sorted(candidates):
            norm = self._ensure_trailing_slash(self._normalize_url(norm))
            if not self._is_inventory_home_url(norm):
                continue
            row_slug = self._slugs_from_url(norm)[0].lower()
            if hub_slug and row_slug != hub_slug:
                continue
            # Guardrail: never emit community hub URL as house_url.
            if self._is_community_hub_url(norm):
                continue
            if norm in self._seen_house_urls:
                continue
            self._seen_house_urls.add(norm)
            yield NealCommunitiesListingItem(
                house_url=norm,
                community_url=self._ensure_trailing_slash(self._community_url(norm)),
            )
            emitted_here += 1

        self.logger.info(
            "community_page url=%s new_houses=%s total_unique=%s",
            response.url,
            emitted_here,
            len(self._seen_house_urls),
        )

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip fragment, query string, whitespace, and trailing slash."""
        clean = (url or "").split("#")[0].split("?")[0].strip().rstrip("/")
        if clean.startswith("//"):
            clean = "https:" + clean
        return clean

    @staticmethod
    def _ensure_trailing_slash(url: str) -> str:
        u = (url or "").strip()
        if not u:
            return ""
        return u if u.endswith("/") else (u + "/")

    @staticmethod
    def _is_community_hub_url(url: str) -> bool:
        """
        Matches /new-homes/{community}/ (with or without trailing slash).
        Exactly 2 non-empty path segments after stripping slashes.
        """
        if "nealcommunities.com" not in (url or "").lower():
            return False
        parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
        return len(parts) == 2 and parts[0] == "new-homes" and bool(parts[1])

    @staticmethod
    def _is_inventory_home_url(url: str) -> bool:
        """
        Matches /new-homes/{community}/{plan}/{homesite-slug}/.
        Exactly 4 non-empty path segments.
        """
        if "nealcommunities.com" not in (url or "").lower():
            return False
        parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
        return len(parts) == 4 and parts[0] == "new-homes"

    @staticmethod
    def _slugs_from_url(url: str) -> tuple[str, str]:
        """Return (community_slug, plan_slug) from an inventory home URL."""
        parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
        if len(parts) >= 4 and parts[0] == "new-homes":
            return parts[1], parts[2]
        return "", ""

    @staticmethod
    def _community_url(house_url: str) -> str:
        """Derive the community hub URL from a house URL."""
        parts = [p for p in urlparse(house_url).path.strip("/").split("/") if p]
        if len(parts) >= 2 and parts[0] == "new-homes":
            return f"https://www.nealcommunities.com/new-homes/{parts[1]}/"
        return ""