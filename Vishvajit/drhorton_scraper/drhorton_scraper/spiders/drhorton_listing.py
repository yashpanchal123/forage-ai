"""
D.R. Horton listing spider: community + house (QMI) URLs (no Playwright).

Primary source:
  /api/comms/direct/{state}
This endpoint returns state-level community cards (same dataset used by site search cards).
"""

import re
from urllib.parse import urldefrag, urlparse

import scrapy

from ..items import DrHortonListingItem

BASE = "https://www.drhorton.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class DrHortonListingSpider(scrapy.Spider):
    name = "drhorton_listing"
    allowed_domains = ["drhorton.com", "www.drhorton.com"]

    states = ["alabama", "tennessee"]

    custom_settings = {
        "ROBOTSTXT_OBEY": False,
        "DOWNLOAD_DELAY": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DEFAULT_REQUEST_HEADERS": {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "FEEDS": {
            "drhorton_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["state", "community_url", "house_url"],
            }
        },
    }

    max_inventory_page = 500
    max_empty_pages_in_row = 3

    metro_second_segment_deny = frozenset(
        {
            "inventory",
            "privacy-policy",
            "terms-of-use",
            "legal",
            "accessibility",
            "contact",
            "careers",
            "investor-relations",
            "warranty",
            "manage-cookies",
            "licensing-and-state-notices",
            "your-privacy-choices",
            "legal-notices",
            "epa-consent-decree",
            "who-we-are",
            "smart-home",
            "services",
            "customer-care",
        }
    )

    def __init__(self, states=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_house_urls = set()
        self._seen_community_urls = set()
        if states:
            self.states = [s.strip().lower() for s in str(states).split(",") if s.strip()]
        self.start_urls = [f"{BASE}/{state}" for state in self.states]

    async def start(self):
        for url in self.start_urls:
            state = self._first_path_segment(url)
            if state not in self.states:
                continue
            api_url = f"{BASE}/api/comms/direct/{state}"
            yield scrapy.Request(
                url=api_url,
                callback=self.parse_state_api,
                meta={"state": state},
            )

    def parse_state_api(self, response):
        state = response.meta["state"]
        try:
            payload = response.json()
        except Exception:
            payload = {}

        community_rows = payload.get("CommunityData") or []
        community_count = 0
        for row in community_rows:
            href = (row or {}).get("commPageLink")
            if not href:
                continue
            href = str(href).strip()
            if href.startswith("http://") or href.startswith("https://"):
                raw_url = href
            else:
                raw_url = f"{BASE}/{href.lstrip('/')}"
            url = raw_url.split("?")[0].split("#")[0].rstrip("/")
            parts = [p for p in urlparse(url).path.split("/") if p]
            if len(parts) != 4 or parts[0].lower() != state:
                continue
            if url in self._seen_community_urls:
                continue
            self._seen_community_urls.add(url)
            community_count += 1
            yield scrapy.Request(
                url=url,
                callback=self.parse_community_for_houses,
                meta={"state": state, "community_url": url},
            )

        self.logger.info(
            "State %s API communities=%s scheduled_communities=%s unique_houses=%s",
            state,
            len(community_rows),
            community_count,
            len(self._seen_house_urls),
        )

        # Keep HTML fallback enabled to capture any links missing in API payload.
        yield scrapy.Request(
            url=f"{BASE}/{state}",
            callback=self.parse_state_html_fallback,
            meta={"state": state},
            dont_filter=True,
        )

    def parse_state_html_fallback(self, response):
        state = response.meta["state"]
        metros = self._metro_slugs_from_html(response.text, state)
        self.logger.info("Fallback state %s: found %d metro slugs", state, len(metros))
        for metro in sorted(metros):
            inv_url = f"{BASE}/{state}/{metro}/inventory?page=1"
            yield scrapy.Request(
                url=inv_url,
                callback=self.parse_metro_inventory,
                meta={"state": state, "metro": metro, "page": 1, "empty_streak": 0},
                dont_filter=True,
            )

    def parse_community_for_houses(self, response):
        state = response.meta["state"]
        community_url = response.meta["community_url"]

        hrefs = response.xpath('//a[contains(@href,"/qmis/")]/@href').getall()
        text = (response.text or "").replace("\\/", "/")
        # Embedded QMI model includes "Url":"/{state}/.../qmis/..."
        hrefs.extend(re.findall(rf'"/{re.escape(state)}/[^"]+/qmis/[^"]+"', text, flags=re.I))

        house_urls = set()
        for href in hrefs:
            href = str(href).strip().strip('"')
            if not href:
                continue
            if "/qmis/" not in href.lower():
                continue
            full = response.urljoin(href) if href.startswith("/") else href
            if not full.startswith("http"):
                full = f"{BASE}/{href.lstrip('/')}"
            clean = full.split("?")[0].split("#")[0].rstrip("/")
            if clean:
                house_urls.add(clean)

        for house_url in sorted(house_urls):
            if house_url in self._seen_house_urls:
                continue
            self._seen_house_urls.add(house_url)
            yield DrHortonListingItem(state=state, community_url=community_url, house_url=house_url)

    def parse_metro_inventory(self, response):
        state = response.meta["state"]
        metro = response.meta["metro"]
        page = int(response.meta.get("page") or 1)
        empty_streak = int(response.meta.get("empty_streak") or 0)

        if response.status != 200:
            self.logger.warning("Skip %s status=%s", response.url, response.status)
            return

        found = self._extract_community_urls(response.text, state)
        new_communities = 0
        for community_url in sorted(found):
            if community_url in self._seen_community_urls:
                continue
            self._seen_community_urls.add(community_url)
            new_communities += 1
            yield scrapy.Request(
                url=community_url,
                callback=self.parse_community_for_houses,
                meta={"state": state, "community_url": community_url},
                dont_filter=True,
            )

        self.logger.info(
            "Metro %s/%s page=%s communities_found=%s communities_scheduled=%s unique_houses=%s",
            state,
            metro,
            page,
            len(found),
            new_communities,
            len(self._seen_house_urls),
        )

        if new_communities == 0:
            empty_streak += 1
        else:
            empty_streak = 0

        if empty_streak >= self.max_empty_pages_in_row:
            return
        if page >= self.max_inventory_page:
            return

        next_page = page + 1
        next_url = f"{BASE}/{state}/{metro}/inventory?page={next_page}"
        yield scrapy.Request(
            url=next_url,
            callback=self.parse_metro_inventory,
            meta={"state": state, "metro": metro, "page": next_page, "empty_streak": empty_streak},
            dont_filter=True,
        )

    def _metro_slugs_from_html(self, html, state):
        if not html:
            return set()
        text = html.replace("\\/", "/")
        metros = set()
        for m in re.finditer(rf"/{re.escape(state)}/([a-z0-9-]+)(?:[\"'/?#]|$)", text, re.I):
            slug = m.group(1).lower()
            if slug in self.metro_second_segment_deny:
                continue
            metros.add(slug)
        return metros

    def _extract_community_urls(self, html, state):
        if not html:
            return set()
        text = html.replace("\\/", "/")
        out = set()

        # Direct community links.
        community = re.compile(rf"/{re.escape(state)}/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+", re.I)
        for m in community.finditer(text):
            full = m.group(0).rstrip("/")
            parts = [p for p in full.split("/") if p]
            if len(parts) != 4 or parts[0].lower() != state:
                continue
            if "/qmis/" in full or "/floor-plans/" in full:
                continue
            out.add(f"{BASE}{full}")

        # Fallback: infer community from QMI paths.
        qmi = re.compile(rf"/{re.escape(state)}/(?:[a-z0-9-]+/)+qmis/[a-z0-9_-]+", re.I)
        for m in qmi.finditer(text):
            full = m.group(0).rstrip("/")
            base = full.split("/qmis/")[0]
            parts = [p for p in base.split("/") if p]
            if len(parts) != 4 or parts[0].lower() != state:
                continue
            out.add(f"{BASE}{base}")

        return out

    @staticmethod
    def _first_path_segment(url):
        clean, _ = urldefrag(url)
        path = urlparse(clean).path.strip("/")
        if not path:
            return None
        return path.split("/")[0].lower()
