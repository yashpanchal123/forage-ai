"""
Listing spider for Mainz address directory (Adressverzeichnis).

Uses GraphQL search (POST /api/graphql/). ``SearchInput.text`` comes from
``MAINZ_SEARCH_TEXTS`` (or ``-a search_text=…`` / ``MAINZ_SEARCH_TEXT``). Each
request adds a single ``fil-2`` category (dropdown option id), same as the Sitepark UI.

Contact fields come from ``ContactTeaser.contactData`` on a second query per page. The first
global Solr row breaks ``teaser`` resolution; for offset 0 we fetch contacts from offset 1
and align rows 1..n (row 0 has empty email/phone).
"""

from __future__ import annotations

import copy
import html
import json
import re
from typing import Any

import scrapy
from scrapy.http import JsonRequest

from mainz_adress_scraper.graphql_query import (
    LISTING_SEARCH_QUERY_CONTACT,
    LISTING_SEARCH_QUERY_MINIMAL,
)
from mainz_adress_scraper.items import AdressListingItem
from mainz_adress_scraper.search_input import (
    absolute_detail_url,
    build_search_input,
    prepare_search_input_template,
)


class AdressListingSpider(scrapy.Spider):
    name = "adress_listing"
    allowed_domains = ["www.mainz.de"]

    gql_url = "https://www.mainz.de/api/graphql/"

    custom_settings = {
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "FEEDS": {
            "mainz_listings.csv": {
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
                    "search_text",
                ],
            }
        },
    }

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider.listing_page_url = crawler.settings.get(
            "MAINZ_LISTING_PAGE",
            "https://www.mainz.de/en/verzeichnisse/adressverzeichnis/"
            "?b85e3fd0-1f6e-4984-81a3-4339fb0f0f49=%28text%3Amainz%29",
        )
        if getattr(spider, "search_text", "").strip():
            spider._search_texts = [spider.search_text.strip()]
        else:
            raw = crawler.settings.get("MAINZ_SEARCH_TEXTS") or ()
            if isinstance(raw, str):
                texts = [t.strip() for t in raw.split(",") if t.strip()]
            else:
                texts = [str(t).strip() for t in raw if str(t).strip()]
            if not texts:
                fb = (crawler.settings.get("MAINZ_SEARCH_TEXT") or "mainz").strip()
                texts = [fb] if fb else ["mainz"]
            spider._search_texts = texts
        spider.baseline_only = crawler.settings.getbool("MAINZ_BASELINE_ONLY", False)
        mc = crawler.settings.getint("MAINZ_MAX_COMBINATIONS", 0)
        spider.max_combinations = None if mc <= 0 else mc
        spider.page_size = crawler.settings.getint("MAINZ_PAGE_SIZE", 100)
        spider.filter_mode = crawler.settings.get("MAINZ_FILTER_MODE", "tree").lower()
        return spider

    def __init__(self, *args, **kwargs):
        # -a search_text=... overrides settings MAINZ_SEARCH_TEXT (filled in from_crawler if empty)
        self.search_text = (kwargs.pop("search_text", None) or "").strip()
        super().__init__(*args, **kwargs)
        # One row per (resource_id, fil-2 category id, search text).
        self._seen_rid_fil2_search: set[tuple[str, str, str]] = set()
        self.template_si_core: dict[str, Any] = {}
        self.base_filters: list[dict[str, Any]] = []
        self.base_url = "https://www.mainz.de"
        self.lang_prefix = "en"
        self.filter_mode = "tree"
        self.combo_labels: dict[
            tuple[str | None, str | None, str | None, str | None], str
        ] = {}

    def start_requests(self):
        yield scrapy.Request(self.listing_page_url, callback=self.parse_bootstrap)

    def parse_bootstrap(self, response: scrapy.http.Response):
        m = re.search(r'data-sp-domain-search="([^"]+)"', response.text)
        if not m:
            self.logger.error("data-sp-domain-search not found on listing page; stopping spider")
            return
        try:
            cfg = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError as e:
            self.logger.error("Failed to parse data-sp-domain-search JSON: %s", e)
            return

        self.base_url = (cfg.get("baseUrl") or "https://www.mainz.de").rstrip("/")
        lang = (cfg.get("searchInput") or {}).get("lang") or "en"
        self.lang_prefix = "en" if lang == "en" else ""

        self.template_si_core = prepare_search_input_template(
            cfg, page_size=self.page_size
        )
        self.base_filters = list(self.template_si_core.get("filter") or [])

        # ---- Address Directory hierarchy (fil-2) from dynamicCategoryTree ----
        selections = (cfg.get("uiConfig") or {}).get("categorySelections") or []
        node_list: list[dict[str, Any]] = []
        entry_node = None
        if selections and isinstance(selections[0], dict):
            node_list = selections[0].get("nodeList") or []
            entry_node = selections[0].get("entryNodeId")
        else:
            self.logger.warning("uiConfig.categorySelections[0].nodeList missing")

        tree: dict[str, list[str]] = {}
        for node in node_list:
            if not isinstance(node, dict):
                continue
            nid = node.get("id")
            if nid is None:
                continue
            tree[str(nid)] = [str(x) for x in (node.get("successorList") or [])]
        # Build categories from the visible dropdown options on the page first.
        # This matches the exact request values users see in the UI.
        options = response.xpath(
            '//select[starts-with(@name,"category-tree-key-")]/option[@value!=""]'
        )
        category_pairs: list[tuple[str, str]] = []
        for opt in options:
            cid = (opt.xpath("@value").get() or "").strip()
            label = self._clean([opt.xpath("normalize-space(text())").get() or ""])
            # Strip trailing counts like "(104)" from labels.
            label = re.sub(r"\s*\(\d+\)\s*$", "", label).strip()
            if cid:
                category_pairs.append((cid, label))

        # Fallback if DOM options are unavailable.
        if not category_pairs:
            fil2_map = self._facet_label_map(cfg, "fac-1")
            category_ids: list[str] = []
            if entry_node is not None and str(entry_node) in tree:
                category_ids = tree.get(str(entry_node), []) or []
            else:
                self.logger.warning(
                    "entryNodeId missing in tree; falling back to all fac-1 labels"
                )
                category_ids = list(fil2_map.keys())
            category_pairs = [(cid, fil2_map.get(cid, "")) for cid in category_ids]

        seen_cat: set[str] = set()
        combos: list[tuple[str | None, str | None, str | None, str | None]] = []
        for cid, category_label in category_pairs:
            if cid in seen_cat:
                continue
            seen_cat.add(cid)
            combo = (cid, None, None, None)
            self.combo_labels[combo] = category_label
            combos.append(combo)

        for term in self._search_texts:
            self.logger.info(
                "Queued %s top-level category requests for SearchInput.text=%r",
                len(combos),
                term,
            )
            for combo in combos:
                yield self._page_minimal_request(combo, page_offset=0, search_text=term)

        

        

    def _search_input(
        self,
        combo: tuple[str | None, str | None, str | None, str | None],
        offset: int,
        *,
        search_text: str,
    ) -> dict[str, Any]:
        tmpl = copy.deepcopy(self.template_si_core)
        tmpl["text"] = search_text
        return build_search_input(tmpl, self.base_filters, combo, offset)

    def _page_minimal_request(
        self,
        combo: tuple[str | None, str | None, str | None, str | None],
        page_offset: int,
        *,
        search_text: str,
    ) -> JsonRequest:
        si = self._search_input(combo, page_offset, search_text=search_text)
        return JsonRequest(
            url=self.gql_url,
            data={
                "query": LISTING_SEARCH_QUERY_MINIMAL,
                "variables": {"searchInput": si},
            },
            callback=self.parse_page_minimal,
            dont_filter=True,
            meta={
                "combo": combo,
                "page_offset": page_offset,
                "search_text": search_text,
            },
        )

    def parse_page_minimal(self, response):
        combo: tuple[str | None, str | None, str | None, str | None] = response.meta[
            "combo"
        ]
        page_offset: int = response.meta["page_offset"]
        search_text: str = response.meta["search_text"]
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
                minimal_rows, [None], combo, search_text
            )
            yield from self._yield_next_page_gen(
                combo, page_offset, n, total, search_text
            )
            return

        if page_offset == 0 and n > 1:
            si_contact = self._search_input(combo, 1, search_text=search_text)
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
                    "combo": combo,
                    "page_offset": page_offset,
                    "total": total,
                    "minimal_results": minimal_rows,
                    "contact_strategy": "skip_first",
                    "search_text": search_text,
                },
            )
            return

        si_contact = self._search_input(combo, page_offset, search_text=search_text)
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
                "combo": combo,
                "page_offset": page_offset,
                "total": total,
                "minimal_results": minimal_rows,
                "contact_strategy": "parallel",
                "search_text": search_text,
            },
        )

    def parse_page_contact(self, response):
        combo = response.meta["combo"]
        page_offset = response.meta["page_offset"]
        total = response.meta["total"]
        minimal_results: list[dict[str, Any]] = response.meta["minimal_results"]
        strategy = response.meta["contact_strategy"]
        search_text: str = response.meta["search_text"]

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
            minimal_results, teasers, combo, search_text
        )
        yield from self._yield_next_page_gen(
            combo, page_offset, len(minimal_results), total, search_text
        )

    def _yield_next_page_gen(
        self,
        combo: tuple[str | None, str | None, str | None, str | None],
        page_offset: int,
        n_results: int,
        total: int,
        search_text: str,
    ):
        next_off = page_offset + n_results
        if next_off < total:
            yield self._page_minimal_request(
                combo, next_off, search_text=search_text
            )

    def _yield_items_from_page(
        self,
        minimal_rows: list[dict[str, Any]],
        teasers: list[Any],
        combo: tuple[str | None, str | None, str | None, str | None],
        search_text: str,
    ):
        for row, teaser in zip(minimal_rows, teasers):
            rid = str(row.get("id") or "")
            fil2_id = combo[0] if combo[0] is not None else ""
            dedupe_key = (rid, fil2_id, search_text)
            if not rid or dedupe_key in self._seen_rid_fil2_search:
                continue
            self._seen_rid_fil2_search.add(dedupe_key)

            loc = row.get("location") or ""
            url = absolute_detail_url(self.base_url, self.lang_prefix, loc)

            merged = dict(row)
            merged["teaser"] = teaser

            item = AdressListingItem()
            item["resource_id"] = rid
            item["title"] = row.get("name") or ""
            item["detail_url"] = url
            item["email"], item["phone"] = self._contact_from_row(merged)
            item["object_type"] = row.get("objectType") or ""
            item["filter_fil_2"] = self.combo_labels.get(combo, "")
            item["search_text"] = search_text
            yield item

    @staticmethod
    def _dfs_paths(tree: dict[str, list[str]], root: str | None) -> list[list[str]]:
        """Return root-to-leaf paths for uiConfig.categorySelections[0].nodeList tree."""
        if not tree:
            return []
        if not root or root not in tree:
            # fallback: start from nodes that are never children
            children = {c for v in tree.values() for c in v}
            roots = [n for n in tree.keys() if n not in children]
            if not roots:
                roots = list(tree.keys())
        else:
            roots = [root]

        out: list[list[str]] = []

        def walk(node: str, path: list[str]) -> None:
            nxt = tree.get(node, [])
            if not nxt:
                out.append(path.copy())
                return
            for c in nxt:
                walk(c, path + [c])

        for r in roots:
            walk(r, [r])
        return out

    @staticmethod
    def _facet_label_map(cfg: dict[str, Any], facet_key: str) -> dict[str, str]:
        """Best-effort map: facet id -> display label from labels.facets."""
        raw = None
        # Preferred source on this page: uiConfig.categorySelections[*].labels.facets
        for sel in (cfg.get("uiConfig") or {}).get("categorySelections") or []:
            if not isinstance(sel, dict):
                continue
            facet_keys = [str(x) for x in (sel.get("facetKeys") or [])]
            if facet_key in facet_keys:
                raw = ((sel.get("labels") or {}).get("facets") or None)
                break
        # Fallback source (often empty on this page)
        if raw is None:
            labels = (cfg.get("labels") or {}).get("facets") or {}
            raw = labels.get(facet_key)
        out: dict[str, str] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                out[str(k)] = str(v)
            return out
        if isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict):
                    continue
                rid = row.get("id") or row.get("key") or row.get("value")
                lab = row.get("label") or row.get("name") or row.get("title")
                if rid is not None and lab:
                    out[str(rid)] = str(lab)
        return out

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
