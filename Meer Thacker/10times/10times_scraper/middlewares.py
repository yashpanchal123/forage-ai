import base64
import os
from urllib.parse import quote

from scrapy import signals
from scrapy.utils.python import to_bytes


class TentimesScraperSpiderMiddleware:
    @classmethod
    def from_crawler(cls, crawler):
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_spider_output(self, response, result, spider):
        for i in result:
            yield i

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)


class TentimesScraperDownloaderMiddleware:
    @classmethod
    def from_crawler(cls, crawler):
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_request(self, request, spider):
        return None

    def process_response(self, request, response, spider):
        return response

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)


class ZyteProxyMiddleware:
    """Injects Zyte Smart Proxy (proxy.zyte.com:8011) into requests.

    407 Proxy Authentication Required means Zyte rejected the API key (wrong key,
    expired, or account moved to Zyte API). Set a valid key via environment:
    ``ZYTE_SMARTPROXY_APIKEY`` (optional password in ``ZYTE_SMARTPROXY_PASS``).

    Skip proxy for a request with ``meta["no_proxy"]=True`` or spider ``no_proxy=True``.
    """

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls()
        mw.settings = crawler.settings
        return mw

    def process_request(self, request, spider=None):
        if request.meta.get("no_proxy") or getattr(spider, "no_proxy", False):
            return None

        env_key = (
            os.environ.get("ZYTE_SMARTPROXY_APIKEY", "").strip()
            or os.environ.get("ZYTE_API_KEY", "").strip()
        )
        if env_key:
            user = env_key
            pwd = (
                os.environ.get("ZYTE_SMARTPROXY_PASS", "").strip()
                or os.environ.get("ZYTE_SMARTPROXY_PASSWORD", "").strip()
                or os.environ.get("ZYTE_PROXY_PASS", "").strip()
            )
        else:
            user = getattr(spider, "proxy_user", None) if spider else None
            pwd = getattr(spider, "proxy_pass", None) if spider else None
            if user is None:
                user = self.settings.get("PROXY_USER", "") or ""
            if pwd is None:
                pwd = self.settings.get("PROXY_PASS", "") or ""

        host = getattr(spider, "proxy_host", None) if spider else None
        port = getattr(spider, "proxy_port", None) if spider else None
        if host is None:
            host = self.settings.get("PROXY_HOST", "proxy.zyte.com")
        if port is None:
            port = self.settings.get("PROXY_PORT", "8011")

        if not user:
            return None

        # HTTPS uses CONNECT; embed auth in URL so HttpProxyMiddleware (750) can
        # strip it and set Proxy-Authorization bytes correctly.
        enc_user = quote(user, safe="")
        enc_pwd = quote(pwd or "", safe="")
        request.meta["proxy"] = f"http://{enc_user}:{enc_pwd}@{host}:{port}"

        user_pass = to_bytes(f"{user}:{pwd or ''}", encoding="latin-1")
        request.headers[b"Proxy-Authorization"] = b"Basic " + base64.b64encode(user_pass)

