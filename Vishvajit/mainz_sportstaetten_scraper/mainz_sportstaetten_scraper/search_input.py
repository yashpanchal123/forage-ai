"""Build SearchInput payloads for https://www.mainz.de/api/graphql/ from page-embedded config."""

from __future__ import annotations

import copy
import html as html_lib
import itertools
import json
import re
from typing import Any, Iterator

# Facet keys in page JSON map to filter keys (dropdown → Solr filter tag).
FACET_KEYS = ("fac-1", "fac-2", "fac-3", "fac-4")
FILTER_KEYS = ("fil-2", "fil-3", "fil-4", "fil-5")


def address_directory_fil2_ids(cfg: dict[str, Any]) -> list[str]:
    """
    Selectable category ids for the Address Directory (+ subcategory) tree from uiConfig.

    The site uses a dynamicCategoryTree (not independent Cartesian facets). Other
    dropdowns often do not apply for a given branch; we only vary fil-2 here.
    """
    ui = cfg.get("uiConfig") or {}
    selections = ui.get("categorySelections") or []
    seen: set[str] = set()
    ordered: list[str] = []
    for sel in selections:
        if sel.get("type") != "dynamicCategoryTree":
            continue
        entry = sel.get("entryNodeId")
        entry_s = str(entry) if entry is not None else ""
        for node in sel.get("nodeList") or []:
            nid = node.get("id")
            if nid is not None:
                s = str(nid)
                if s and s != entry_s and s not in seen:
                    seen.add(s)
                    ordered.append(s)
            for sid in node.get("successorList") or []:
                s = str(sid)
                if s and s != entry_s and s not in seen:
                    seen.add(s)
                    ordered.append(s)
    return ordered


def sport_fil3_scope_category_ids(search_input: dict[str, Any]) -> list[str]:
    """
    Sportstätten hub scopes the directory with ``fil-3`` categories — on the live page
    this matches the dropdown (e.g. Sporthallen + Sportplätze only), not the long
    ``fac-1`` facet id list embedded for Solr.
    """
    for f in search_input.get("filter") or []:
        if f.get("key") != "fil-3":
            continue
        raw = f.get("categories")
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for cid in raw:
            if cid is None:
                continue
            s = str(cid)
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out
    return []


def sport_merge_filters(
    base_filters: list[dict[str, Any]],
    category_id: str | None,
) -> list[dict[str, Any]]:
    """
    Sportstätten search keeps base ``fil-2`` group scope and ``fil-3`` categories; the
    UI facet ``fac-1`` maps to Solr filter ``fil-4`` (see facet ``excludeFilter``).
    """
    out = copy.deepcopy(base_filters)
    if category_id is not None:
        out.append({"key": "fil-4", "categories": [str(category_id)]})
    return out


def parse_domain_search_config(html: str) -> dict[str, Any]:
    """Parse the `data-sp-domain-search` JSON from the address directory HTML."""
    m = re.search(r'data-sp-domain-search="([^"]+)"', html)
    if not m:
        raise ValueError("data-sp-domain-search not found on page")
    return json.loads(html_lib.unescape(m.group(1)))


def strip_type_discriminators(obj: Any) -> None:
    """Remove UI-only 'type' keys; GraphQL InputFilter/InputSort use a flat input shape."""
    if isinstance(obj, dict):
        obj.pop("type", None)
        for v in obj.values():
            strip_type_discriminators(v)
    elif isinstance(obj, list):
        for x in obj:
            strip_type_discriminators(x)


def prune_empty_filters(search_input: dict[str, Any]) -> None:
    """Drop filter rows that would trigger GraphQL/Solr errors (empty constraints)."""
    filters = search_input.get("filter") or []
    kept: list[dict[str, Any]] = []
    for f in filters:
        if f.get("key") == "teaserPropertyFilter":
            # Empty Solr fq for this tag breaks search on mainz.de; not needed for listings.
            continue
        cats = f.get("categories")
        grps = f.get("groups")
        q = f.get("query")
        has_cats = isinstance(cats, list) and len(cats) > 0
        has_grps = isinstance(grps, list) and len(grps) > 0
        has_q = isinstance(q, str) and q.strip() != ""
        if has_cats or has_grps or has_q:
            kept.append(f)
    search_input["filter"] = kept


def prepare_search_input_template(cfg: dict[str, Any], page_size: int = 100) -> dict[str, Any]:
    """Deep copy searchInput from widget config, normalized for GraphQL."""
    si = copy.deepcopy(cfg["searchInput"])
    strip_type_discriminators(si)
    prune_empty_filters(si)
    si["limit"] = page_size
    si["offset"] = 0
    return si


def facet_dimension_options(search_input: dict[str, Any]) -> list[list[str]]:
    """Category id lists for each UI facet (Address dir., Category, District, …)."""
    facet_map = {
        f["key"]: list(f.get("categories") or []) for f in search_input.get("facets") or []
    }
    return [facet_map.get(k, []) for k in FACET_KEYS]


def iter_filter_combinations(
    cfg: dict[str, Any],
    *,
    baseline_only: bool = False,
    max_combinations: int | None = None,
    mode: str = "tree",
) -> Iterator[tuple[str | None, str | None, str | None, str | None]]:
    """
    Valid filter tuples (fil-2 … fil-5).

    Default ``mode="tree"``: matches the Address Directory widget — baseline (no fil-2)
    plus one request per selectable tree node id (fil-2 only; fil-3/4/5 unset). This
    avoids the invalid full Cartesian product of all Solr facets.

    ``mode="cartesian"``: legacy cross-product of fac-1…4 category lists (very large).

    Order matches FILTER_KEYS: (fil-2, fil-3, fil-4, fil-5).
    """
    if baseline_only:
        yield (None, None, None, None)
        return

    search_input = cfg.get("searchInput") or {}

    if mode == "cartesian":
        dims = [[None] + ids for ids in facet_dimension_options(search_input)]
        prod = itertools.product(*dims)
        if max_combinations is None or max_combinations <= 0:
            yield from prod
        else:
            yield from itertools.islice(prod, max_combinations)
        return

    # tree: address directory (+ implicit subcategory ids as their own fil-2 values)
    combos: list[tuple[str | None, str | None, str | None, str | None]] = [
        (None, None, None, None)
    ]
    for cid in address_directory_fil2_ids(cfg):
        combos.append((cid, None, None, None))

    if max_combinations is not None and max_combinations > 0:
        combos = combos[: max_combinations]

    yield from combos


def merge_category_filters(
    base_filters: list[dict[str, Any]],
    combo: tuple[str | None, str | None, str | None, str | None],
) -> list[dict[str, Any]]:
    """Append fil-2 … fil-5 category filters for this combination."""
    out = copy.deepcopy(base_filters)
    for filter_key, raw_id in zip(FILTER_KEYS, combo):
        if raw_id is not None:
            out.append({"key": filter_key, "categories": [str(raw_id)]})
    return out


def build_search_input(
    template: dict[str, Any],
    base_filters: list[dict[str, Any]],
    combo: tuple[str | None, str | None, str | None, str | None],
    offset: int,
) -> dict[str, Any]:
    si = copy.deepcopy(template)
    si["offset"] = offset
    si["filter"] = merge_category_filters(base_filters, combo)
    return si


def build_sport_search_input(
    template: dict[str, Any],
    base_filters: list[dict[str, Any]],
    category_id: str | None,
    offset: int,
) -> dict[str, Any]:
    si = copy.deepcopy(template)
    si["offset"] = offset
    si["filter"] = sport_merge_filters(base_filters, category_id)
    return si


def absolute_detail_url(base_url: str, lang_prefix: str, location: str) -> str:
    """Detail URL: ``/en`` prefix only when ``lang_prefix`` is ``en`` (German pages omit it)."""
    base = base_url.rstrip("/")
    lang = lang_prefix.strip("/")
    loc = location if location.startswith("/") else f"/{location}"
    if lang:
        return f"{base}/{lang}{loc}"
    return f"{base}{loc}"
