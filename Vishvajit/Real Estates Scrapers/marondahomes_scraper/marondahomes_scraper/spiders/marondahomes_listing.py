import re
from urllib.parse import urldefrag, urlparse

import scrapy

from ..items import MarondaHomesListingItem

BASE = "https://www.marondahomes.com"
QUICK_DELIVERY_URL = f"{BASE}/all-quick-deliveries/"

STATE_CODE_TO_NAME = {
    "al": "alabama",
    "fl": "florida",
    "ga": "georgia",
    "in": "indiana",
    "ky": "kentucky",
    "md": "maryland",
    "oh": "ohio",
    "pa": "pennsylvania",
    "sc": "south-carolina",
    "va": "virginia",
    "wv": "west-virginia",
}

AVAILABLE_STATES = sorted(set(STATE_CODE_TO_NAME.values()))


class MarondaHomesListingSpider(scrapy.Spider):
    name = "marondahomes_listing"
    allowed_domains = ["marondahomes.com", "www.marondahomes.com"]

    custom_settings = {
        "FEEDS": {
            "marondahomes_listings.json": {
                "format": "json",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": ["state", "community_url", "house_url"],
            }
        }
    }

    def __init__(self, states=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if states:
            requested = [s.strip().lower() for s in str(states).split(",") if s.strip()]
            self.states = self._normalize_states(requested)
        else:
            self.states = list(AVAILABLE_STATES)
        self._seen_house_urls = set()

    def start_requests(self):
        yield scrapy.Request(QUICK_DELIVERY_URL, callback=self.parse)

    def parse(self, response):
        hrefs = response.xpath('//a[contains(@href, "/job-")]/@href').getall()
        text = (response.text or "").replace("\\/", "/")
        hrefs.extend(re.findall(r'https?://www\.marondahomes\.com/[^"\s]+/job-[^"\s]+\.html', text, re.I))
        hrefs.extend(re.findall(r'/[a-z]{2}/[^"\s]+/[^"\s]+/job-[^"\s]+\.html', text, re.I))

        for href in hrefs:
            house_url = self._normalize_url(response.urljoin(href))
            if not house_url or house_url in self._seen_house_urls:
                continue
            state = self._state_from_house_url(house_url)
            if not state or state not in self.states:
                continue
            self._seen_house_urls.add(house_url)
            community_url = self._community_url_from_house_url(house_url)
            yield MarondaHomesListingItem(
                state=state,
                community_url=community_url,
                house_url=house_url,
            )

        self.logger.info(
            "Selected states=%s yielded_house_urls=%s",
            ",".join(self.states),
            len(self._seen_house_urls),
        )

    @staticmethod
    def _normalize_url(url):
        raw = str(url or "").strip()
        if not raw:
            return ""
        clean, _ = urldefrag(raw)
        clean = clean.split("?")[0].rstrip("/")
        if clean.startswith("http://") or clean.startswith("https://"):
            return clean
        return f"{BASE}/{clean.lstrip('/')}"

    @staticmethod
    def _community_url_from_house_url(house_url):
        if not house_url:
            return ""
        return re.sub(r"/job-[^/]+\.html$", ".html", house_url, flags=re.I)

    @staticmethod
    def _state_from_house_url(house_url):
        parts = [p for p in urlparse(house_url).path.split("/") if p]
        if len(parts) < 4:
            return ""
        return STATE_CODE_TO_NAME.get(parts[0].lower(), "")

    @staticmethod
    def _normalize_states(requested):
        state_names = set(AVAILABLE_STATES)
        out = []
        for value in requested:
            v = value.strip().lower().replace("_", "-")
            if v in state_names:
                out.append(v)
                continue
            if v in STATE_CODE_TO_NAME:
                out.append(STATE_CODE_TO_NAME[v])
                continue
            v2 = v.replace(" ", "-")
            if v2 in state_names:
                out.append(v2)
        out = list(dict.fromkeys(out))
        return out or list(AVAILABLE_STATES)
