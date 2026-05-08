from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import scrapy
from scrapy.http import Request, Response

from scorestream_scrapy.items import ListingLinkItem, ScrapeErrorItem

MARKET_LABEL_BY_STATE_SLUG: dict[str, str] = {
    "georgia": "Atlanta GA (Georgia)",
    "texas": "Austin TX (Texas)",
    "florida": "Orlando FL (Florida)",
    "tennessee": "Nashville TN (Tennessee)",
}


def dumps_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _utc_date_range_strings(days_back: int) -> tuple[str, str]:
    """
    ScoreStream API expects `YYYY-MM-DD HH:mm:ss` (local server-side).
    We supply UTC timestamps; API accepts them as strings.
    """
    now = datetime.now(timezone.utc)
    after = now - timedelta(days=days_back)
    return (
        after.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _extract_state_slugs(response: Response) -> list[str]:
    slugs: set[str] = set()
    for href in response.css("a::attr(href)").getall():
        if not href:
            continue
        m = re.match(r"^/explore/r/([a-z0-9-]+)$", href.strip())
        if not m:
            continue
        slug = m.group(1)
        if slug in ("us", "all"):
            continue
        slugs.add(slug)
    return sorted(slugs)


def _extract_gdata(response_text: str) -> dict[str, Any] | None:
    m = re.search(r"var\s+gData\s*=\s*(\{.*?\})\s*\|\|\s*\{\};", response_text, re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _sports_from_high_school_page(response: Response) -> list[str]:
    """
    On `/high-school` page we get links like:
      /explore/r/texas/high-school/football/scores
      /explore/r/texas/high-school/boys-basketball/scores
    """
    out: set[str] = set()
    for href in response.css("a::attr(href)").getall():
        if not href:
            continue
        m = re.match(r"^/explore/r/[^/]+/high-school/([a-z0-9-]+)/scores$", href.strip())
        if m:
            out.add(m.group(1))
    return sorted(out)


class ScoreStreamListingSpider(scrapy.Spider):
    """
    Crawl ScoreStream explore pages to generate a CSV of game links.

    By default, only the configured target *states*:
    Georgia, Texas, Florida, Tennessee — matching Atlanta, Austin, Orlando, Nashville
    at the state level (ScoreStream has no city Explore URLs).

    Run from `scorestream/` (where scrapy.cfg lives):
      scrapy crawl scorestream_listing
      scrapy crawl scorestream_listing -a days_back=180
      scrapy crawl scorestream_listing -a state_slug=georgia
      scrapy crawl scorestream_listing -a state_slugs=georgia,texas
      scrapy crawl scorestream_listing -a all_states=1
    """

    name = "scorestream_listing"
    allowed_domains: list[str] = []

    custom_settings = {
        "DOWNLOAD_DELAY": 0.25,
        "SCORESTREAM_LISTING_CSV_PATH": "scorestream_game_links.csv",
        "ITEM_PIPELINES": {
            "scorestream_scrapy.pipelines.ListingCsvPipeline": 300,
        },
    }

    def __init__(
        self,
        days_back: str | None = None,
        state_slug: str | None = None,
        state_slugs: str | None = None,
        all_states: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._days_back_arg = days_back
        self.state_slug = state_slug.strip().lower() if state_slug else None
        self.state_slugs_arg = state_slugs.strip() if state_slugs else None
        self.all_states = (
            str(all_states).strip().lower() in ("1", "true", "yes")
            if all_states
            else False
        )
        self.days_back = 120
        self._after = ""
        self._before = ""

        self._sport_slugs_default: list[str] = []
        self._default_state_slugs: list[str] = []
        self._api_url = ""
        self._base = ""

    @classmethod
    def from_crawler(cls, crawler, *args: Any, **kwargs: Any):
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings
        spider.allowed_domains = list(settings.getlist("SCORESTREAM_ALLOWED_DOMAINS"))
        spider._api_url = str(settings.get("SCORESTREAM_API_URL"))
        spider._base = str(settings.get("SCORESTREAM_BASE"))
        spider._sport_slugs_default = list(
            settings.getlist("SCORESTREAM_SPORT_SLUGS_DEFAULT")
        )
        spider._default_state_slugs = list(
            settings.getlist("SCORESTREAM_DEFAULT_STATE_SLUGS")
        )
        spider.days_back = (
            int(spider._days_back_arg)
            if spider._days_back_arg
            else int(settings.getint("SCORESTREAM_LISTING_DAYS_BACK_DEFAULT"))
        )
        spider._after, spider._before = _utc_date_range_strings(spider.days_back)
        return spider

    def _resolve_state_slugs(self) -> list[str]:
        if self.state_slug:
            return [self.state_slug]
        if self.state_slugs_arg:
            return [
                s.strip().lower()
                for s in self.state_slugs_arg.split(",")
                if s.strip()
            ]
        return list(self._default_state_slugs)

    def _market_label(self, state_slug: str) -> str:
        return MARKET_LABEL_BY_STATE_SLUG.get(state_slug, state_slug)

    def start_requests(self) -> Iterable[Request]:
        if self.all_states:
            yield Request(url=f"{self._base}/explore/r/us", callback=self.parse_us)
            return
        for slug in self._resolve_state_slugs():
            yield Request(
                url=f"{self._base}/explore/r/{slug}/high-school",
                callback=self.parse_high_school,
                meta={
                    "state_slug": slug,
                    "market_label": self._market_label(slug),
                },
            )

    def parse_us(self, response: Response) -> Iterable[Request]:
        state_slugs = _extract_state_slugs(response)
        if self.state_slug:
            state_slugs = [s for s in state_slugs if s == self.state_slug]

        for slug in state_slugs:
            yield Request(
                url=f"{self._base}/explore/r/{slug}/high-school",
                callback=self.parse_high_school,
                meta={
                    "state_slug": slug,
                    "market_label": self._market_label(slug),
                },
            )

    def parse_high_school(self, response: Response) -> Iterable[Request]:
        state_slug = str(response.meta["state_slug"])
        market_label = str(response.meta.get("market_label") or self._market_label(state_slug))
        available = _sports_from_high_school_page(response)

        # Normalize into the canonical list user asked for, but allow gender variants.
        wanted = set(self._sport_slugs_default)
        allowed = []
        for s in available:
            base = s
            if s.startswith("boys-"):
                base = s.removeprefix("boys-")
            elif s.startswith("girls-"):
                base = s.removeprefix("girls-")
            if base in wanted:
                allowed.append(s)

        for sport_slug in sorted(set(allowed)):
            yield Request(
                url=f"{self._base}/explore/r/{state_slug}/high-school/{sport_slug}/scores",
                callback=self.parse_scores_page,
                meta={
                    "state_slug": state_slug,
                    "sport_slug": sport_slug,
                    "market_label": market_label,
                },
            )

    def parse_scores_page(self, response: Response) -> Iterable[Request]:
        state_slug = str(response.meta["state_slug"])
        sport_slug = str(response.meta["sport_slug"])
        gdata = _extract_gdata(response.text)
        if not gdata:
            yield ScrapeErrorItem(
                game_url=response.url, error="missing_gData_on_scores_page"
            )
            return

        dp = gdata.get("dataPoints") or {}
        if not isinstance(dp, dict):
            yield ScrapeErrorItem(game_url=response.url, error="bad_gData_dataPoints")
            return

        params = {
            "isExploreSearch": True,
            "aboveConfidenceGrade": 30,
            "afterDateTime": self._after,
            "beforeDateTime": self._before,
            "country": dp.get("country") or "US",
            "state": dp.get("state"),
            "sportNames": dp.get("sportNames"),
            "squadIds": dp.get("squadIds"),
        }

        # The API paginates with count/offset.
        meta_base = {
            "state_slug": state_slug,
            "sport_slug": sport_slug,
            "market_label": str(response.meta.get("market_label") or ""),
            "organization_id": dp.get("organizationId"),
            "state_code": dp.get("state"),
            "params": params,
        }
        yield from self._queue_games_search(meta_base, offset=0, count=200)

    def _queue_games_search(
        self, meta_base: dict[str, Any], *, offset: int, count: int
    ) -> Iterable[Request]:
        params = dict(meta_base["params"])
        params["offset"] = offset
        params["count"] = count
        body = {"api": "scoreStream", "method": "games.search", "params": params}
        yield scrapy.Request(
            url=self._api_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(body).encode("utf-8"),
            callback=self.parse_games_search,
            meta={**meta_base, "offset": offset, "count": count},
            dont_filter=True,
        )

    def parse_games_search(self, response: Response) -> Iterable[Any]:
        meta = response.meta
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            yield ScrapeErrorItem(game_url=response.url, error="games_search_bad_json")
            return

        result = data.get("result") or {}
        if not isinstance(result, dict):
            yield ScrapeErrorItem(game_url=response.url, error="games_search_no_result")
            return

        total = int(result.get("total") or 0)
        collections = result.get("collections") or {}
        games = ((collections.get("gameCollection") or {}).get("list")) or []
        if not isinstance(games, list):
            games = []

        for g in games:
            if not isinstance(g, dict):
                continue
            gid = g.get("gameId")
            game_url = g.get("url") or g.get("minUrl") or ""
            if gid is None or not game_url:
                continue
            yield ListingLinkItem(
                market_label=str(meta.get("market_label") or ""),
                game_id=str(gid),
                game_url=str(game_url),
                min_url=str(g.get("minUrl") or ""),
                state_slug=str(meta.get("state_slug") or ""),
                state_code=str(meta.get("state_code") or ""),
                sport_name=str(g.get("sportName") or meta.get("sport_slug") or ""),
                organization_id=str(meta.get("organization_id") or ""),
                squad_ids_json=dumps_compact(meta.get("params", {}).get("squadIds")),
                listing_json=dumps_compact(g),
            )

        offset = int(meta.get("offset") or 0)
        count = int(meta.get("count") or 0)
        next_offset = offset + count
        if next_offset >= total:
            return
        yield from self._queue_games_search(meta, offset=next_offset, count=count)

