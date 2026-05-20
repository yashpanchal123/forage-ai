"""
D.R. Horton listing spider: community + listing URLs (no Playwright).

Emits one CSV row per listing URL (same `house_url` column):
  - QMI (inventory): …/qmis/{slug}
  - Floor plan: …/floor-plans/{slug}

Primary source:
  /api/comms/direct/{state}
This endpoint returns state-level community cards (same dataset used by site search cards).
"""

import json
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
                "fields": ["house_url", "community_url"],
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
        self._seen_listing_urls = set()
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
            "State %s API communities=%s scheduled_communities=%s unique_listing_urls=%s",
            state,
            len(community_rows),
            community_count,
            len(self._seen_listing_urls),
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

        qmi_urls = set()
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
            if clean and self._is_qmi_listing_url(clean, state):
                qmi_urls.add(clean)

        floor_urls = self._collect_floor_plan_urls(response, state)

        for house_url in sorted(qmi_urls):
            yield from self._emit_listing_row(community_url, house_url)

        for fp_url in sorted(floor_urls):
            yield from self._emit_listing_row(community_url, fp_url)

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
            "Metro %s/%s page=%s communities_found=%s communities_scheduled=%s unique_listing_urls=%s",
            state,
            metro,
            page,
            len(found),
            new_communities,
            len(self._seen_listing_urls),
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

        # Fallback: infer community from floor-plan paths.
        fp = re.compile(rf"/{re.escape(state)}/(?:[a-z0-9-]+/)+floor-plans/[a-z0-9_-]+", re.I)
        for m in fp.finditer(text):
            full = m.group(0).rstrip("/")
            base = full.split("/floor-plans/")[0]
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

    def _emit_listing_row(self, community_url, house_url):
        if house_url in self._seen_listing_urls:
            return
        self._seen_listing_urls.add(house_url)
        yield DrHortonListingItem(
            house_url=house_url,
            community_url=community_url,
        )

    @staticmethod
    def _path_parts(url):
        return [p for p in urlparse(url or "").path.strip("/").split("/") if p]

    def _is_qmi_listing_url(self, url, state):
        parts = self._path_parts(url)
        if not parts or parts[0].lower() != (state or "").lower():
            return False
        return "qmis" in [p.lower() for p in parts]

    def _is_floor_plan_listing_url(self, url, state):
        parts = self._path_parts(url)
        if not parts or parts[0].lower() != (state or "").lower():
            return False
        low = [p.lower() for p in parts]
        if "floor-plans" not in low:
            return False
        idx = low.index("floor-plans")
        return idx >= 4 and idx + 1 < len(parts)

    def _collect_floor_plan_urls(self, response, state):
        out = set()
        text = (response.text or "").replace("\\/", "/")

        for href in response.xpath('//a[contains(@href,"/floor-plans/")]/@href').getall():
            clean = self._absolute_listing_url(response, href, state)
            if clean and self._is_floor_plan_listing_url(clean, state):
                out.add(clean)

        for m in re.finditer(rf'"/{re.escape(state)}/[^"]+/floor-plans/[^"]+"', text, flags=re.I):
            clean = self._absolute_listing_url(response, m.group(0).strip('"'), state)
            if clean and self._is_floor_plan_listing_url(clean, state):
                out.add(clean)

        plans_model = self._extract_embedded_model(text, "SortPlans")
        for plan in plans_model.get("Items") or []:
            if not isinstance(plan, dict):
                continue
            url_val = plan.get("Url") or plan.get("url")
            if not url_val:
                continue
            clean = self._absolute_listing_url(response, str(url_val).strip(), state)
            if clean and self._is_floor_plan_listing_url(clean, state):
                out.add(clean)

        return out

    def _absolute_listing_url(self, response, href, state):
        href = str(href or "").strip().strip('"')
        if not href:
            return ""
        if href.startswith(("http://", "https://")):
            full = href
        elif href.startswith("/"):
            full = response.urljoin(href)
        else:
            full = f"{BASE}/{href.lstrip('/')}"
        clean = full.split("?")[0].split("#")[0].rstrip("/")
        if "drhorton.com" not in (urlparse(clean).netloc or "").lower():
            return ""
        parts = self._path_parts(clean)
        if not parts or parts[0].lower() != (state or "").lower():
            return ""
        return clean

    @staticmethod
    def _extract_embedded_model(text, function_name):
        marker = f"function {function_name}(clickedElement)"
        start_idx = text.find(marker)
        if start_idx == -1:
            return {}
        var_idx = text.find("var model =", start_idx)
        if var_idx == -1:
            return {}
        brace_idx = text.find("{", var_idx)
        if brace_idx == -1:
            return {}

        depth = 0
        in_string = False
        escape = False
        for pos in range(brace_idx, len(text)):
            char = text[pos]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[brace_idx : pos + 1])
                        except Exception:
                            return {}
        return {}
