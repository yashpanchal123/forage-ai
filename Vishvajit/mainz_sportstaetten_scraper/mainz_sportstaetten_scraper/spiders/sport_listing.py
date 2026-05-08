"""
Listing spider for Mainz Sportstättenverzeichnis
(https://www.mainz.de/angebote-entdecken/sport/Sportstaetten).

Uses the same GraphQL search API as the address directory, but this hub is driven
only by sport categories (no search-text queries): category ids come from the
same ``<select name="category-tree-key-*">`` as in the browser (typically
Sporthallen + Sportplätze), else from ``fil-3`` on the embedded SearchInput.
Refinement uses ``fil-4``; ``fil-2`` keeps the Sportstätten Solr group scope.

Detail pages (``sport_details``) split **venue** location (``Anschrift``) from the
**office / contact** postal block under Kontakt → Adresse; this spider only emits
listing fields for those URLs.
"""

from __future__ import annotations

import copy
import html
import json
import re
from typing import Any

import scrapy
from scrapy.http import JsonRequest

from mainz_sportstaetten_scraper.graphql_query import (
    LISTING_SEARCH_QUERY_CONTACT,
    LISTING_SEARCH_QUERY_MINIMAL,
)
from mainz_sportstaetten_scraper.items import SportListingItem
from mainz_sportstaetten_scraper.search_input import (
    absolute_detail_url,
    build_sport_search_input,
    prepare_search_input_template,
    sport_fil3_scope_category_ids,
)


class SportListingSpider(scrapy.Spider):
    name = "sport_listing"
    allowed_domains = ["www.mainz.de"]

    gql_url = "https://www.mainz.de/api/graphql/"

    custom_settings = {
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "FEEDS": {
            "sportstaetten_listings.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": [
                    "resource_id",
                    "title",
                    "detail_url",
                    "email",
                    "phone",
                    "object_type",
                    "filter_fil_2",
                ],
            }
        },
    }

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider.listing_page_url = crawler.settings.get(
            "MAINZ_SPORT_LISTING_PAGE",
            "https://www.mainz.de/angebote-entdecken/sport/Sportstaetten",
        )
        spider.baseline_only = crawler.settings.getbool("MAINZ_BASELINE_ONLY", False)
        mc = crawler.settings.getint("MAINZ_MAX_CATEGORIES", 0)
        spider.max_categories = None if mc <= 0 else mc
        spider.page_size = crawler.settings.getint("MAINZ_PAGE_SIZE", 100)
        return spider

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_rid_category: set[tuple[str, str]] = set()
        self.template_si_core: dict[str, Any] = {}
        self.base_filters: list[dict[str, Any]] = []
        self.base_url = "https://www.mainz.de"
        self.lang_prefix = ""
        self._category_ids: list[str | None] = []
        self.category_labels: dict[str, str] = {}

    def start_requests(self):
        yield scrapy.Request(self.listing_page_url, callback=self.parse_bootstrap)

    def parse_bootstrap(self, response: scrapy.http.Response):
        m = re.search(r'data-sp-domain-search="([^"]+)"', response.text)
        if not m:
            self.logger.error(
                "data-sp-domain-search not found on Sportstätten page; stopping spider"
            )
            return
        try:
            cfg = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError as e:
            self.logger.error("Failed to parse data-sp-domain-search JSON: %s", e)
            return

        self.base_url = (cfg.get("baseUrl") or "https://www.mainz.de").rstrip("/")
        lang = (cfg.get("searchInput") or {}).get("lang")
        self.lang_prefix = "en" if lang == "en" else ""

        self.template_si_core = prepare_search_input_template(
            cfg, page_size=self.page_size
        )
        self.base_filters = list(self.template_si_core.get("filter") or [])

        if self.baseline_only:
            self._category_ids = [None]
        else:
            ids, labels = self._category_ids_from_select(response)
            source = "select"
            if not ids:
                ids = sport_fil3_scope_category_ids(self.template_si_core)
                labels = {
                    "111220": "Sporthallen",
                    "111237": "Sportplaetze",
                }
                source = "fil-3"
            if not ids:
                self.logger.error(
                    "No Sportstätten categories (empty <select> options and no fil-3); "
                    "stopping spider"
                )
                return
            if self.max_categories is not None:
                ids = ids[: self.max_categories]
            self._category_ids = ids
            self.category_labels = labels
            self.logger.info(
                "Sportstätten categories from %s: %s (%s requests)",
                source,
                ids,
                len(ids),
            )

        for cid in self._category_ids:
            yield self._page_minimal_request(cid, page_offset=0)

    def _search_input(self, category_id: str | None, offset: int) -> dict[str, Any]:
        tmpl = copy.deepcopy(self.template_si_core)
        if not isinstance(tmpl.get("text"), str):
            tmpl["text"] = ""
        return build_sport_search_input(tmpl, self.base_filters, category_id, offset)

    def _page_minimal_request(
        self,
        category_id: str | None,
        page_offset: int,
    ) -> JsonRequest:
        si = self._search_input(category_id, page_offset)
        return JsonRequest(
            url=self.gql_url,
            data={
                "query": LISTING_SEARCH_QUERY_MINIMAL,
                "variables": {"searchInput": si},
            },
            callback=self.parse_page_minimal,
            dont_filter=True,
            meta={
                "category_id": category_id,
                "page_offset": page_offset,
            },
        )

    def parse_page_minimal(self, response):
        category_id: str | None = response.meta["category_id"]
        page_offset: int = response.meta["page_offset"]
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as e:
            self.logger.error("Invalid JSON from GraphQL: %s", e)
            return

        if payload.get("errors"):
            for err in payload["errors"]:
                self.logger.error("GraphQL minimal: %s", err.get("message", err))
            return

        search = (payload.get("data") or {}).get("search")
        if not search:
            return

        total = int(search.get("total") or 0)
        minimal_rows: list[dict[str, Any]] = list(search.get("results") or [])
        n = len(minimal_rows)
        if n == 0:
            return

        if page_offset == 0 and n == 1:
            yield from self._yield_items_from_page(
                minimal_rows, [None], category_id
            )
            yield from self._yield_next_page_gen(
                category_id, page_offset, n, total
            )
            return

        if page_offset == 0 and n > 1:
            si_contact = self._search_input(category_id, 1)
            si_contact["limit"] = n - 1
            yield JsonRequest(
                url=self.gql_url,
                data={
                    "query": LISTING_SEARCH_QUERY_CONTACT,
                    "variables": {"searchInput": si_contact},
                },
                callback=self.parse_page_contact,
                dont_filter=True,
                meta={
                    "category_id": category_id,
                    "page_offset": page_offset,
                    "total": total,
                    "minimal_results": minimal_rows,
                    "contact_strategy": "skip_first",
                },
            )
            return

        si_contact = self._search_input(category_id, page_offset)
        si_contact["limit"] = n
        yield JsonRequest(
            url=self.gql_url,
            data={
                "query": LISTING_SEARCH_QUERY_CONTACT,
                "variables": {"searchInput": si_contact},
            },
            callback=self.parse_page_contact,
            dont_filter=True,
            meta={
                "category_id": category_id,
                "page_offset": page_offset,
                "total": total,
                "minimal_results": minimal_rows,
                "contact_strategy": "parallel",
            },
        )

    def parse_page_contact(self, response):
        category_id = response.meta["category_id"]
        page_offset = response.meta["page_offset"]
        total = response.meta["total"]
        minimal_results: list[dict[str, Any]] = response.meta["minimal_results"]
        strategy = response.meta["contact_strategy"]

        teasers: list[Any]
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as e:
            self.logger.error("Invalid JSON from GraphQL (contact): %s", e)
            teasers = [None] * len(minimal_results)
        else:
            if payload.get("errors"):
                for err in payload["errors"]:
                    self.logger.warning(
                        "GraphQL contact: %s", err.get("message", err)
                    )
            contact_search = (payload.get("data") or {}).get("search")
            contact_rows = (
                list(contact_search.get("results") or [])
                if contact_search
                else []
            )

            if strategy == "skip_first":
                teasers = [None]
                for r in contact_rows:
                    teasers.append(r.get("teaser"))
            else:
                teasers = [r.get("teaser") for r in contact_rows]

            while len(teasers) < len(minimal_results):
                teasers.append(None)
            teasers = teasers[: len(minimal_results)]

        yield from self._yield_items_from_page(
            minimal_results, teasers, category_id
        )
        yield from self._yield_next_page_gen(
            category_id, page_offset, len(minimal_results), total
        )

    def _yield_next_page_gen(
        self,
        category_id: str | None,
        page_offset: int,
        n_results: int,
        total: int,
    ):
        next_off = page_offset + n_results
        if next_off < total:
            yield self._page_minimal_request(category_id, next_off)

    def _yield_items_from_page(
        self,
        minimal_rows: list[dict[str, Any]],
        teasers: list[Any],
        category_id: str | None,
    ):
        cat_key = category_id if category_id is not None else ""
        for row, teaser in zip(minimal_rows, teasers):
            rid = str(row.get("id") or "")
            dedupe_key = (rid, cat_key)
            if not rid or dedupe_key in self._seen_rid_category:
                continue
            self._seen_rid_category.add(dedupe_key)

            loc = row.get("location") or ""
            url = absolute_detail_url(self.base_url, self.lang_prefix, loc)

            merged = dict(row)
            merged["teaser"] = teaser

            item = SportListingItem()
            item["resource_id"] = rid
            item["title"] = row.get("name") or ""
            item["detail_url"] = url
            item["email"], item["phone"] = self._contact_from_row(merged)
            item["object_type"] = row.get("objectType") or ""
            item["filter_fil_2"] = self.category_labels.get(cat_key, cat_key)
            yield item

    @staticmethod
    def _category_ids_from_select(
        response: scrapy.http.Response,
    ) -> tuple[list[str], dict[str, str]]:
        """Match the visible Sportstätten dropdown (same pattern as Adressverzeichnis)."""
        options = response.xpath(
            '//select[starts-with(@name,"category-tree-key-")]/option[@value!=""]'
        )
        out: list[str] = []
        seen: set[str] = set()
        labels: dict[str, str] = {}
        for opt in options:
            cid = (opt.xpath("@value").get() or "").strip()
            label = (opt.xpath("normalize-space(text())").get() or "").strip()
            label = re.sub(r"\s*\(\d+\)\s*$", "", label).strip()
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
                if label:
                    labels[cid] = label
        return out, labels

    @staticmethod
    def _contact_from_row(row: dict[str, Any]) -> tuple[str, str]:
        teaser = row.get("teaser") or {}
        if teaser.get("__typename") != "ContactTeaser":
            return "", ""
        cd = teaser.get("contactData") or {}
        emails_raw = cd.get("emails") or []
        emails: list[str] = []
        for e in emails_raw:
            em = (e or {}).get("email")
            if em:
                emails.append(str(em))
        email_str = ";".join(dict.fromkeys(emails))

        phones_out: list[str] = []
        for p in cd.get("phones") or []:
            if not isinstance(p, dict):
                continue
            for key in ("nationalNumber", "internationalNumber", "uri"):
                v = p.get(key)
                if v:
                    phones_out.append(str(v))
                    break
        phone_str = ";".join(dict.fromkeys(phones_out))
        return email_str, phone_str
