"""Downloader middlewares for eastwoodhomes_scraper."""


class ProxyInjectionMiddleware:
    """Sets ``request.meta['proxy']`` from ``PROXY_URL``."""

    def __init__(self, proxy_url: str):
        self.proxy_url = (proxy_url or "").strip()

    @classmethod
    def from_crawler(cls, crawler):
        return cls(proxy_url=crawler.settings.get("PROXY_URL") or "")

    def process_request(self, request):
        if self.proxy_url and "proxy" not in request.meta:
            request.meta["proxy"] = self.proxy_url
        return None
