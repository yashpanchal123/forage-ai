import re
from urllib.parse import urlparse

import scrapy

from ..items import TrueHomesListingItem

BASE = "https://www.truehomes.com"
SITEMAP_URL = f"{BASE}/sitemap.xml"


class TrueHomesListingSpider(scrapy.Spider):
    name = "truehomes_listing"
    allowed_domains = ["truehomes.com", "www.truehomes.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [400, 404, 500],
        "FEEDS": {
            "truehomes_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": [
                    "state",
                    "house_url",
                ],
            }
        },
    }

    def start_requests(self):
        yield scrapy.Request(SITEMAP_URL, callback=self.parse_sitemap, dont_filter=True)

    def parse_sitemap(self, response):
        # Some proxies return 5xx for sitemap. Let Scrapy retry; if it still fails, bail.
        if response.status != 200:
            self.logger.warning("Sitemap fetch failed status=%s url=%s", response.status, response.url)
            return

        urls = re.findall(r"<loc>(.*?)</loc>", response.text or "", flags=re.I)
        seen: set[str] = set()

        for raw in urls:
            url = (raw or "").strip()
            if not url.startswith("http"):
                continue
            url = url.split("#")[0].split("?")[0].rstrip("/")
            if not url.startswith(BASE):
                continue
            if url in seen:
                continue
            seen.add(url)

            url_type = self._classify_url(url)
            if not url_type:
                continue

            # House/QMI only. Community URLs intentionally skipped because we only scrape house details.
            if url_type != "qmi":
                continue

            state = self._infer_state_from_url(url)
            yield TrueHomesListingItem(
                state=state,
                house_url=url,
            )

    @staticmethod
    def _classify_url(url: str) -> str:
        path = urlparse(url).path.lower().strip("/")
        if not path:
            return ""
        if path.startswith("new-construction-homes/move-in-ready/"):
            return "qmi"
        return ""

    @staticmethod
    def _infer_state_from_url(url: str) -> str:
        """
        For QMI pages, slug typically contains "-nc-" or "-sc-".
        For other pages, state is usually only available in the embedded row data.
        """
        low = url.lower()
        if "-nc-" in low or low.endswith("-nc"):
            return "north-carolina"
        if "-sc-" in low or low.endswith("-sc"):
            return "south-carolina"
        return ""

