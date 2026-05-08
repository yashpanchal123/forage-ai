import re
import json
import scrapy
from scrapy.crawler import CrawlerProcess


class CityEventsSpider(scrapy.Spider):
    name = "city_events"
    allowed_domains = ["10times.com"]

    # ✅ Proxy Config
    proxy_host = "proxy.zyte.com"
    proxy_port = "8011"
    proxy_user = "7916eb9714394ae9a160c862c3e3da93"
    proxy_pass = ""

    MAX_PAGES = 50

    # Cities to scrape (provided by user)
    # `city_path` must match the 10times city URL path.
    CITIES = [
        {
            "city": "Atlanta",
            "state": "GA",
            "market_type": "Tegna Market",
            "city_path": "atlanta-us",
        },
        {
            "city": "Austin",
            "state": "TX",
            "market_type": "Tegna Market",
            "city_path": "austin-us",
        },
        {
            "city": "Orlando",
            "state": "FL",
            "market_type": "Non-Tegna Market",
            "city_path": "orlando-us",
        },
        {
            "city": "Nashville",
            "state": "TN",
            "market_type": "Non-Tegna Market",
            "city_path": "nashville-us",
        },
    ]

    custom_settings = {
        # ✅ Save to CSV automatically
        "FEEDS": {
            "event_urls.csv": {
                "format": "csv",
                "fields": ["city", "state", "market_type", "event_url"],
                "overwrite": True,
            }
        },

        # ✅ Proxy Middleware
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware": 110,
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": 550,
        },

        # ✅ Retry settings (replaces `for i in range(3)`)
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],

        # ✅ Delay settings (replaces `time.sleep(2)`)
        "DOWNLOAD_DELAY": 2,
        "RANDOMIZE_DOWNLOAD_DELAY": True,

        # ✅ One request at a time (replaces sequential while loop)
        "CONCURRENT_REQUESTS": 1,

        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": False,

        # Use Scrapy default ScrapyClientContextFactory (does not verify peer certs).
        # BrowserLikeContextFactory uses platformTrust() and causes
        # "certificate verify failed" when using Zyte / some proxies.
        "DOWNLOADER_CLIENT_TLS_METHOD": "TLS",

        "LOG_LEVEL": "INFO",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Global dedupe across all cities/pages for this crawl run.
        self._seen_event_urls = set()
        self._duplicate_urls_skipped = 0

    def _proxy_url(self):
        """Build proxy URL (replaces proxies dict in requests)"""
        return f"http://{self.proxy_user}:{self.proxy_pass}@{self.proxy_host}:{self.proxy_port}"

    def _build_listing_url(self, *, city_path: str, page: int) -> str:
        return (
            f"https://10times.com/{city_path}"
            f"?ajax=1&page={page}&popular=1&groupBy=concurrent&listing_pagination=1"
        )

    def _build_request(self, *, city_cfg: dict, page: int):
        """Build a single request for a given city + page number"""
        city_path = city_cfg["city_path"]
        url = self._build_listing_url(city_path=city_path, page=page)
        referer = f"https://10times.com/{city_path}"
        return scrapy.Request(
            url=url,
            callback=self.parse,
            errback=self.handle_error,
            dont_filter=True,
            meta={
                "city": city_cfg["city"],
                "state": city_cfg["state"],
                "market_type": city_cfg["market_type"],
                "city_path": city_path,
                "page": page,
                "proxy": self._proxy_url(),
                "download_timeout": 20,
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": referer,
            },
        )

    def start_requests(self):
        """Replaces: page = 1 ... while True"""
        for city_cfg in self.CITIES:
            yield self._build_request(city_cfg=city_cfg, page=1)

    def parse(self, response):
        page = response.meta["page"]
        city = response.meta.get("city", "?")
        state = response.meta.get("state", "?")
        market_type = response.meta.get("market_type", "?")
        city_path = response.meta.get("city_path", "?")
        self.logger.info(
            f"{city}, {state} ({market_type}) | Page {page} | Status {response.status}"
        )

        # ✅ Parse JSON → get HTML fragment
        # Replaces: data = json.loads(response.text) / html_fragment = data.get("html", "")
        try:
            data = json.loads(response.text)
            html_fragment = data.get("html", "")
        except Exception:
            self.logger.warning(
                f"{city}, {state} | Page {page}: Not JSON — using raw text"
            )
            html_fragment = response.text

        # 🔴 Stop if empty (replaces: page = 9999 / break)
        if not html_fragment.strip():
            self.logger.warning(
                f"{city}, {state} | Page {page}: Empty response — stopping pagination"
            )
            return

        # Prefer DOM parsing over raw regex (the HTML shape changes on later pages).
        sel = scrapy.Selector(text=html_fragment)

        urls: list[str] = []

        # 1) onclick="window.open('...')" on listing rows/cells
        for onclick in sel.xpath(
            '//*[@onclick and contains(@onclick,"window.open")]/@onclick'
        ).getall():
            m = re.search(r"window\.open\((?:'|\")([^'\"]+)", onclick)
            if m:
                urls.append(response.urljoin(m.group(1).strip()))

        # 2) fallback: direct anchors inside the listing table (some pages drop onclick)
        if not urls:
            for href in sel.xpath(
                '//table[@id="listing-events"]//a[@href]/@href'
            ).getall():
                h = (href or "").strip()
                if not h:
                    continue
                full = response.urljoin(h)
                if "10times.com" in full and "?ajax=1" not in full:
                    urls.append(full)

        # Final fallback: regex scan for absolute event URLs in fragment
        if not urls:
            # Simple safe pattern for absolute URLs inside fragment
            urls.extend(re.findall(r"https?://10times\\.com/[A-Za-z0-9_./-]+", html_fragment))

        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                deduped.append(u)

        urls_found = 0
        for event_url in deduped:
            if event_url in self._seen_event_urls:
                self._duplicate_urls_skipped += 1
                continue
            self._seen_event_urls.add(event_url)
            yield {
                "city": city,
                "state": state,
                "market_type": market_type,
                "event_url": event_url,
            }
            urls_found += 1

        self.logger.info(
            f"{city}, {state} | Page {page} | {urls_found} URLs extracted"
        )

        if urls_found == 0:
            self.logger.warning(
                f"{city}, {state} | Page {page}: 0 URLs — stopping pagination for this city"
            )
            return

        # ✅ Go to next page (replaces: page += 1)
        next_page = page + 1
        if next_page <= self.MAX_PAGES:
            yield self._build_request(
                city_cfg={
                    "city": city,
                    "state": state,
                    "market_type": market_type,
                    "city_path": city_path,
                },
                page=next_page,
            )

    def handle_error(self, failure):
        """Replaces: except Exception as e / Failed page X stopping"""
        page = failure.request.meta.get("page", "?")
        city = failure.request.meta.get("city", "?")
        state = failure.request.meta.get("state", "?")
        self.logger.error(
            f"{city}, {state} | Page {page} | Request failed: {failure.request.url} | {failure.value}"
        )

    def closed(self, reason):
        self.logger.info(
            "Run summary | reason=%s | unique_urls=%s | duplicates_skipped=%s",
            reason,
            len(self._seen_event_urls),
            self._duplicate_urls_skipped,
        )


# ✅ Run spider directly
if __name__ == "__main__":
    process = CrawlerProcess()
    process.crawl(CityEventsSpider)
    process.start()