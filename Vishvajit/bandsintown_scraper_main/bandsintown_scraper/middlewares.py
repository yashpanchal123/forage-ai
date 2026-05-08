"""
Downloader middleware: fetch HTML via cloudscraper (blocking I/O in a thread pool).

Uses ``async def process_request`` + ``asyncio.to_thread`` (Scrapy 2.14 asyncio
reactor). Returning a Twisted ``Deferred`` from ``process_request`` is deprecated
and can leave the crawl stuck at 0 pages.

``trust_env=False`` on the session avoids Windows ``HTTP_PROXY`` / ``HTTPS_PROXY``
from hijacking or stalling every request unless Geonode credentials are set.

When :setting:`GEONODE_ONLY` is True (default), credentials come from
``settings.GEONODE_PROXY_USER`` / ``GEONODE_PROXY_PASSWORD`` (defaults in
``settings.py``; override via env or ``-s``). Fetches use Geonode only; failed
proxy fetches return an empty HTML response so the spider can retry instead of
opening a direct connection to the target site.
"""
import asyncio
import logging
import random
import threading
from urllib.parse import quote

import cloudscraper
from scrapy.http import HtmlResponse


logger = logging.getLogger(__name__)

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) "
    "Gecko/20100101 Firefox/123.0",

    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",

    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

_local = threading.local()


def _get_scraper():
    if not hasattr(_local, "scraper"):
        _local.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        # Do not merge with OS proxy env unless we pass explicit ``proxies=`` below.
        _local.scraper.trust_env = False
    return _local.scraper


def _geonode_proxies(user: str, password: str, host: str) -> dict[str, str] | None:
    user, password, host = user.strip(), password.strip(), host.strip()
    if not user or not password or not host:
        return None
    user_q, pass_q = quote(user, safe=""), quote(password, safe="")
    base = f"http://{user_q}:{pass_q}@{host}"
    return {"http": base, "https": base}


def _empty_html_response(request) -> HtmlResponse:
    """Avoid Scrapy’s default downloader (direct IP) when Geonode fetch failed."""
    return HtmlResponse(
        url=request.url,
        status=200,
        body=b"<html><head></head><body></body></html>",
        encoding="utf-8",
        request=request,
    )


class BandsintownMiddleware:

    def __init__(
        self,
        *,
        proxies: dict[str, str] | None,
        http_timeout: float,
        geonode_only: bool,
    ):
        self._proxies = proxies
        self._http_timeout = http_timeout
        self._geonode_only = geonode_only

    @classmethod
    def from_crawler(cls, crawler):
        s = crawler.settings
        geonode_only = s.getbool("GEONODE_ONLY", True)
        host = (s.get("GEONODE_PROXY_HOST") or "proxy.geonode.io:9000").strip()
        user = (s.get("GEONODE_PROXY_USER") or "").strip()
        password = (s.get("GEONODE_PROXY_PASSWORD") or "").strip()
        timeout = float(s.getfloat("GEONODE_HTTP_TIMEOUT", 35.0))
        proxies = _geonode_proxies(user, password, host)
        if geonode_only:
            if not proxies:
                raise ValueError(
                    "GEONODE_ONLY is True but Geonode credentials are missing. "
                    "Set GEONODE_PROXY_USER / GEONODE_PROXY_PASSWORD in settings.py, "
                    "or export them (they override defaults), or run with -s GEONODE_ONLY=False."
                )
            logger.info(
                "BandsintownMiddleware: Geonode-only mode (host=%s, timeout=%ss)",
                host,
                timeout,
            )
            return cls(proxies=proxies, http_timeout=timeout, geonode_only=True)
        if proxies:
            logger.info(
                "BandsintownMiddleware: Geonode optional (host=%s, timeout=%ss)",
                host,
                timeout,
            )
        return cls(
            proxies=proxies,
            http_timeout=timeout if proxies else 25.0,
            geonode_only=False,
        )

    async def process_request(self, request, spider=None):
        return await asyncio.to_thread(self._fetch, request)

    def _fetch(self, request):
        ua = random.choice(UA_POOL)
        return self._try_fetch(request, ua)

    def _try_fetch(self, request, ua: str):
        logger.debug("cloudscraper request: %s", request.url)
        kwargs = {
            "timeout": self._http_timeout,
            "headers": {"User-Agent": ua},
        }
        if self._proxies:
            kwargs["proxies"] = self._proxies
        try:
            resp = _get_scraper().get(request.url, **kwargs)
            if resp.status_code == 200:
                logger.debug("cloudscraper 200: %s", request.url)
                return HtmlResponse(
                    url=request.url,
                    body=resp.content,
                    encoding="utf-8",
                    request=request,
                )
            if resp.status_code in (403, 404, 410, 416):
                logger.debug("cloudscraper HTTP %s: %s", resp.status_code, request.url)
            else:
                logger.warning("cloudscraper HTTP %s: %s", resp.status_code, request.url)
        except Exception as exc:
            logger.warning("cloudscraper %s: %s (%s)", exc.__class__.__name__, exc, request.url)
        if self._geonode_only:
            return _empty_html_response(request)
        return None
