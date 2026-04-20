from urllib.parse import quote, urlencode

import scrapy

from ..items import GrittInvestorItem

BASE_API = "https://x8zx-3roh-u9yx.e2.xano.io/api:lVInP5FG/searchAll"
INVESTOR_BASE = "https://www.gritt.io/investor/"


def build_url(page: int) -> str:
    params = {
        "vc": "false",
        "tag": "",
        "page": page,
        "year": "0",
        "stage": "",
        "country": "",
        "founder": "false",
    }
    return f"{BASE_API}?{urlencode(params)}"


class GrittListingSpider(scrapy.Spider):
    name = "gritt_listing"
    allowed_domains = ["x8zx-3roh-u9yx.e2.xano.io"]

    # Spider-level dedup set (backup – DuplicateFilterPipeline is the primary guard)
    _seen_urls: set = set()

    def start_requests(self):
        """Kick off with page 1."""
        yield scrapy.Request(
            url=build_url(1),
            callback=self.parse,
            cb_kwargs={"page": 1},
        )

    # ------------------------------------------------------------------
    def parse(self, response, page: int):
        try:
            data = response.json()
        except Exception as exc:
            self.logger.error(f"Page {page}: JSON parse error – {exc}")
            return

        # API returns an empty body / empty list when pages are exhausted
        if not data:
            self.logger.info(f"Page {page}: empty response – stopping pagination.")
            return

        items = data.get("result_1", {}).get("items", [])
        if not items:
            self.logger.info(f"Page {page}: no items – stopping pagination.")
            return

        yielded = 0
        for item in items:
            public_id = (
                item.get("_getprofilepic", {}).get("publicId", "") or ""
            ).strip()

            if not public_id:
                continue

            encoded_id = quote(public_id, safe="")
            investor_url = f"{INVESTOR_BASE}{encoded_id}/"

            if investor_url in self._seen_urls:
                continue
            self._seen_urls.add(investor_url)

            out = GrittInvestorItem(
                public_id=public_id,
                investor_url=investor_url,
                page=page,
            )
            self.logger.info(f"  [{page}] #{len(self._seen_urls)}  {investor_url}")
            yield out
            yielded += 1

        # Follow next page
        next_page = page + 1
        self.logger.info(f"Page {page}: yielded={yielded}. Fetching page {next_page} …")
        yield scrapy.Request(
            url=build_url(next_page),
            callback=self.parse,
            cb_kwargs={"page": next_page},
        )
