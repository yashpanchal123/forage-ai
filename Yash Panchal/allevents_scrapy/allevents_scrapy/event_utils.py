"""JSON-LD parsing and Forage-shaped record building (no HTTP)."""

from __future__ import annotations

import json
import html as html_lib
import csv
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from allevents_scrapy.allevents_config import MODULE, PROVIDER, SOURCE_HOME, SOURCE_ID, SOURCE_NAME

logger = logging.getLogger(__name__)


def clean_whitespace(s: str) -> str:
    """Collapse all whitespace (including \\r, \\n, tabs) to single spaces."""
    if s is None:
        return ""
    t = re.sub(r"[\r\n\t\v\f]+", " ", str(s))
    return " ".join(t.split()).strip()


def _is_placeholder_venue_name(name: str) -> bool:
    """Venue / location title is not a real place yet (omit address & coordinates)."""
    low = name.strip().lower()
    if not low:
        return False
    if "soon to be announced" in low or "soon to be announce" in low:
        return True
    if "venue to be announced" in low or "location to be announced" in low:
        return True
    if re.fullmatch(r"(tba|tbd|n/a|t\.b\.a\.|t\.b\.d\.|—|-)", low):
        return True
    return False


_US_STATE_ABBR = {
    "al",
    "ak",
    "az",
    "ar",
    "ca",
    "co",
    "ct",
    "de",
    "dc",
    "fl",
    "ga",
    "hi",
    "id",
    "il",
    "in",
    "ia",
    "ks",
    "ky",
    "la",
    "me",
    "md",
    "ma",
    "mi",
    "mn",
    "ms",
    "mo",
    "mt",
    "ne",
    "nv",
    "nh",
    "nj",
    "nm",
    "ny",
    "nc",
    "nd",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "vt",
    "va",
    "wa",
    "wv",
    "wi",
    "wy",
}

_US_STATE_NAMES = {
    # We only need a small subset for the markets we scrape, but keeping this permissive is ok.
    "georgia",
    "texas",
    "florida",
    "tennessee",
}


def _is_city_state_only_name(name: str) -> bool:
    """
    When JSON-LD/listing doesn't provide a venue, AllEvents often falls back to a city/state,
    e.g. \"Austin, Texas\" or \"Austin, TX\". That's not a venue name; omit it (null).
    """
    t = clean_whitespace(name)
    if not t or any(ch.isdigit() for ch in t):
        return False
    m = re.fullmatch(r"([A-Za-z .'-]+),\s*([A-Za-z]{2,})", t)
    if not m:
        return False
    left = clean_whitespace(m.group(1)).lower()
    state = m.group(2).strip().lower().rstrip(".")
    if state not in _US_STATE_ABBR and state not in _US_STATE_NAMES:
        return False
    # Only treat as city/state fallback when the "city" side is exactly a known market city.
    # Keep true venue names like "Paramount Theatre - Austin, TX".
    return left in {"atlanta", "austin", "orlando", "nashville"}


def _is_placeholder_address(address: str) -> bool:
    """Address line is TBA / to-be-announced style (omit it and coordinates)."""
    low = address.strip().lower()
    if not low:
        return False
    if "soon to be announced" in low:
        return True
    if re.match(r"^tba\s*,", low):
        return True
    return False


def _sanitize_location_placeholders(loc_block: dict[str, Any]) -> None:
    """Drop invalid venue names, and clear geo/address for explicit TBA placeholders."""
    name = loc_block.get("name")
    addr = loc_block.get("address")
    name_str = clean_whitespace(str(name)) if isinstance(name, str) else ""
    addr_str = clean_whitespace(str(addr)) if isinstance(addr, str) else ""
    wipe = (name_str and _is_placeholder_venue_name(name_str)) or (
        addr_str and _is_placeholder_address(addr_str)
    )
    if wipe:
        # Explicit placeholders should not appear as venue names.
        loc_block["name"] = None
        loc_block["address"] = ""
        loc_block["latitude"] = None
        loc_block["longitude"] = None
        return

    # City/state-only is not a venue (keep address/geo; just null-out the name).
    if name_str and _is_city_state_only_name(name_str):
        loc_block["name"] = None

    # Output rule: if venue name is missing, do not send address/coordinates.
    if not loc_block.get("name"):
        loc_block["address"] = ""
        loc_block["latitude"] = None
        loc_block["longitude"] = None

# Word match order: first hit wins on ``location.address`` (lowercased).
_MARKET_NEARBY_RULES: tuple[tuple[str, int | None], ...] = (
    ("atlanta", 85),
    ("austin", 269),
    ("orlando", None),
    ("nashville", None),
)


_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _normalize_state_code(state: str | None) -> str:
    s = clean_whitespace(state or "").lower().strip(".")
    if len(s) == 2 and s.isalpha():
        return s
    full_to_abbr = {
        "georgia": "ga",
        "texas": "tx",
        "florida": "fl",
        "tennessee": "tn",
    }
    return full_to_abbr.get(s, s[:2] if len(s) >= 2 else "")


def _state_in_text(state_code: str, text: str) -> bool:
    if not state_code:
        return False
    t = clean_whitespace(text).lower()
    if not t:
        return False
    full_map = {
        "ga": "georgia",
        "tx": "texas",
        "fl": "florida",
        "tn": "tennessee",
    }
    if re.search(rf"(?:\b|,\s*){re.escape(state_code)}(?:\b|,)", t):
        return True
    full = full_map.get(state_code)
    if full and re.search(rf"\b{re.escape(full)}\b", t):
        return True
    return False


def _market_key(text: str | None) -> str | None:
    t = clean_whitespace(text or "").lower()
    if "atlanta" in t:
        return "atlanta"
    if "austin" in t:
        return "austin"
    if "orlando" in t:
        return "orlando"
    if "nashville" in t:
        return "nashville"
    return None


def _load_zip_scope() -> dict[str, set[tuple[str, str]]]:
    """
    Map zip -> {(market_key, state_code), ...} from Tegna pilot scope CSV.
    """
    out: dict[str, set[tuple[str, str]]] = {}
    csv_path = Path(__file__).resolve().parents[1] / "Tegna - Pilot Scope Locations.csv"
    if not csv_path.is_file():
        logger.warning("ZIP scope file not found: %s", csv_path)
        return out
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                zip_raw = clean_whitespace(str(row.get("Zip") or ""))
                m = re.search(r"\d{5}", zip_raw)
                if not m:
                    continue
                zip5 = m.group(0)
                mk = _market_key(str(row.get("Market") or ""))
                st = _normalize_state_code(str(row.get("State") or ""))
                if not mk or not st:
                    continue
                out.setdefault(zip5, set()).add((mk, st))
    except Exception:
        logger.exception("Failed loading ZIP scope CSV: %s", csv_path)
        return {}
    logger.info("Loaded ZIP scope rows for %s unique ZIPs", len(out))
    return out


ZIP_SCOPE = _load_zip_scope()


def infer_nearby_and_site_id(
    *,
    address: str,
    market_name: str,
    state_name: str,
) -> tuple[bool | None, int | None]:
    """
    ZIP-first nearby logic:
      1) if ZIP in address and present in scope sheet -> nearby only when market+state match.
      2) if ZIP absent -> fallback to market/state combinations:
         (Atlanta, GA), (Austin, TX), (Orlando, FL), (Nashville, TN).
    """
    addr = clean_whitespace(address or "")
    mk = _market_key(market_name)
    st = _normalize_state_code(state_name)
    event_pair = (mk, st)

    address_market_key = _market_key(addr)
    address_has_state = _state_in_text(st, addr) if st else False
    # Per QA rule: if market or state text is missing in location.address, nearBy = null.
    if not address_market_key or not address_has_state:
        return None, None

    z = _ZIP_RE.search(addr)
    if z:
        zip5 = z.group(1)
        allowed = ZIP_SCOPE.get(zip5)
        if allowed is not None:
            ok = bool(mk and st and event_pair in allowed)
            if ok:
                sid = 85 if mk == "atlanta" else 269 if mk == "austin" else None
                return True, sid
            return False, None

    fallback_allowed = {
        ("atlanta", "ga"),
        ("austin", "tx"),
        ("orlando", "fl"),
        ("nashville", "tn"),
    }
    # No ZIP: require both market + state to be present in the location address text.
    if (
        mk
        and st
        and event_pair in fallback_allowed
        and address_market_key == mk
        and _state_in_text(st, addr)
    ):
        sid = 85 if mk == "atlanta" else 269 if mk == "austin" else None
        return True, sid
    return False, None


def extract_current_event_share_fields(html_text: str) -> dict[str, Any]:
    """
    Extract a few reliable fields from:
      var current_event_share = { ... };
    We avoid fully parsing the JS object and instead regex only the keys we need.
    """

    def get_str(key: str) -> str | None:
        # value is sometimes double-quoted, sometimes single-quoted (e.g. latitude/'33.7')
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', html_text, flags=re.S)
        if m:
            return html_lib.unescape(m.group(1))
        m = re.search(rf'"{re.escape(key)}"\s*:\s*\'([^\']*)\'', html_text, flags=re.S)
        if m:
            return m.group(1)
        return None

    def get_float(key: str) -> float | None:
        v = get_str(key)
        if v is None:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    return {
        "safetitle": get_str("safetitle"),
        "short_desc": get_str("short_desc"),
        "full_address": get_str("full_address"),
        "start_date": get_str("start_date"),
        "end_date": get_str("end_date"),
        "currency": get_str("currency"),
        "latitude": get_float("latitude"),
        "longitude": get_float("longitude"),
    }


def triple_id(*parts: str) -> str:
    u = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts))
    h = f"{u.int:032d}"
    return f"{int(h[0:4])}-{int(h[4:10])}-{int(h[10:18])}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_zone(timezone_name: str | None) -> ZoneInfo | timezone:
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            pass
    return timezone.utc


def parse_json_ld_schedule_start_end(
    value: str | None,
    *,
    timezone_name: str | None = None,
) -> tuple[str | None, bool]:
    """
    Parse JSON-LD startDate/endDate for output aligned with schema:
    - Date-only (YYYY-MM-DD): returns (\"YYYY-MM-DD\", True) for all-day / date precision.
    - Datetime: returns local wall-clock \"YYYY-MM-DDTHH:MM:SS\" without Z — no UTC suffix.
      Z/offset inputs are converted to the event location timezone for display only.
    - Naive datetimes are treated as wall time in the event timezone (or UTC if unknown).
    """
    if not value or not isinstance(value, str):
        return None, False
    s = value.strip()
    if not s:
        return None, False

    # Date-only (YYYY-MM-DD)
    if "T" not in s and len(s) <= 10:
        try:
            d = datetime.fromisoformat(s).date()
            return d.isoformat(), True
        except ValueError:
            return None, False

    tz = _local_zone(timezone_name)

    # Datetime with explicit Z or offset: convert to local wall time, no UTC Z in output.
    if s.endswith("Z"):
        s_norm = s[:-1] + "+00:00"
    elif re.search(r"[+-]\d{2}:\d{2}$", s):
        s_norm = s
    else:
        s_norm = ""

    if s_norm:
        try:
            dt = datetime.fromisoformat(s_norm)
        except ValueError:
            return None, False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(tz)
        return local.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S"), False

    # Naive datetime: wall clock in event local zone (no conversion to UTC).
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, False
    if dt.tzinfo is not None:
        local = dt.astimezone(tz)
        return local.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S"), False
    # Truly naive: use as literal wall clock (no UTC conversion).
    return dt.strftime("%Y-%m-%dT%H:%M:%S"), False


def format_epoch_local_no_z(epoch: float, timezone_name: str | None) -> str:
    tz = _local_zone(timezone_name)
    return datetime.fromtimestamp(float(epoch), tz=tz).replace(tzinfo=None).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


# Visible schedule on event pages, e.g. "Fri, 08 May • 07:00 PM (CDT)".
_UI_TIME_LABEL_RE = re.compile(
    r"(?P<dow>[A-Za-z]{3}),\s*(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3})"
    r"(?:,\s*(?P<year>\d{4}))?"
    r"\s*[•\u2022.]\s*"
    r"(?P<h12>\d{1,2}):(?P<min>\d{2})\s*(?P<ap>AM|PM)",
    re.I,
)

# "Date & location" / multi-date header, e.g.
# "Fri, 22 May, 2026 at 09:00 PM to 11:00 PM (CST)"
_UI_AT_TIME_RANGE_RE = re.compile(
    r"(?P<dow>[A-Za-z]{3}),\s*(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3}),\s*(?P<year>\d{4})\s+at\s+"
    r"(?P<h1>\d{1,2}):(?P<m1>\d{2})\s*(?P<ap1>AM|PM)"
    r"(?:\s+to\s+(?P<h2>\d{1,2}):(?P<m2>\d{2})\s*(?P<ap2>AM|PM))?",
    re.I,
)

# Same without year: "Fri, 22 May at 09:00 PM to 11:00 PM"
_UI_AT_TIME_RANGE_NO_YEAR_RE = re.compile(
    r"(?P<dow>[A-Za-z]{3}),\s*(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3})\s+at\s+"
    r"(?P<h1>\d{1,2}):(?P<m1>\d{2})\s*(?P<ap1>AM|PM)"
    r"(?:\s+to\s+(?P<h2>\d{1,2}):(?P<m2>\d{2})\s*(?P<ap2>AM|PM))?",
    re.I,
)

_MONTH_ABBR_TO_NUM = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _h12_to_hour_24(h12: int, ap: str) -> int:
    apu = ap.upper()
    if apu == "PM" and h12 != 12:
        return h12 + 12
    if apu == "AM" and h12 == 12:
        return 0
    return h12


def _iter_event_time_label_paragraphs(response) -> list[str]:
    """One string per <p.event-time-label> so 'Multiple Dates' does not merge with the real line."""
    texts: list[str] = []
    for p in response.xpath(
        '//p[contains(concat(" ", normalize-space(@class), " "), " event-time-label ")]'
    ):
        raw = clean_whitespace(" ".join(p.xpath(".//text()").getall()))
        if raw:
            texts.append(raw)
    if not texts:
        for p in response.xpath('//p[@class="event-time-label"]'):
            raw = clean_whitespace(" ".join(p.xpath(".//text()").getall()))
            if raw:
                texts.append(raw)
    return texts


def _parse_schedule_line_raw(
    raw: str,
    *,
    year_hint: int | None,
) -> tuple[str | None, str | None]:
    """
    Parse one time-label line. Returns (start_iso, end_iso_or_none) wall times, no Z.
    """
    m = _UI_AT_TIME_RANGE_RE.search(raw)
    if not m:
        m = _UI_AT_TIME_RANGE_NO_YEAR_RE.search(raw)
    if m:
        mon_key = m.group("mon").lower()[:3]
        month = _MONTH_ABBR_TO_NUM.get(mon_key)
        if not month:
            return None, None
        day = int(m.group("day"))
        yg = m.groupdict().get("year")
        if yg:
            year = int(yg)
        elif year_hint is not None:
            year = year_hint
        else:
            year = datetime.now(timezone.utc).year
        h1 = int(m.group("h1"))
        m1 = int(m.group("m1"))
        ap1 = m.group("ap1")
        try:
            start_dt = datetime(
                year,
                month,
                day,
                _h12_to_hour_24(h1, ap1),
                m1,
                0,
            )
        except ValueError:
            return None, None
        start_s = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        h2g = m.groupdict().get("h2")
        if h2g is not None:
            h2 = int(h2g)
            m2 = int(m.group("m2"))
            ap2 = m.group("ap2")
            try:
                end_dt = datetime(
                    year,
                    month,
                    day,
                    _h12_to_hour_24(h2, ap2),
                    m2,
                    0,
                )
            except ValueError:
                return start_s, None
            if end_dt <= start_dt:
                return start_s, None
            return start_s, end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        return start_s, None

    m = _UI_TIME_LABEL_RE.search(raw)
    if not m:
        return None, None
    mon_key = m.group("mon").lower()[:3]
    month = _MONTH_ABBR_TO_NUM.get(mon_key)
    if not month:
        return None, None
    day = int(m.group("day"))
    yg = m.group("year")
    if yg:
        year = int(yg)
    elif year_hint is not None:
        year = year_hint
    else:
        year = datetime.now(timezone.utc).year
    h12 = int(m.group("h12"))
    minute = int(m.group("min"))
    ap = m.group("ap")
    try:
        dt = datetime(
            year,
            month,
            day,
            _h12_to_hour_24(h12, ap),
            minute,
            0,
        )
    except ValueError:
        return None, None
    return dt.strftime("%Y-%m-%dT%H:%M:%S"), None


def parse_ui_event_schedule_display(
    response,
    *,
    year_hint: int | None = None,
) -> tuple[str | None, str | None]:
    """
    Parse visible AllEvents schedule from <p class=\"event-time-label\"> nodes.
    Handles bullet times and \"Fri, 22 May, 2026 at 09:00 PM to 11:00 PM (CST)\".
    Returns (start, end) local wall-clock strings; end may be None.
    """
    for raw in _iter_event_time_label_paragraphs(response):
        start, end = _parse_schedule_line_raw(raw, year_hint=year_hint)
        if start:
            return start, end
    return None, None


def extract_event_json_ld(html: str) -> dict[str, Any] | None:
    for m in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Event":
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Event":
                    return item
    return None


def _float_or_none(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def pricing_is_free_zero(lo: float | None, hi: float | None) -> bool:
    """Single effective price of 0 (point or both ends zero), for ``eventPricing.kind`` free."""
    if lo is None and hi is None:
        return False
    if lo is not None and hi is not None and lo != hi:
        return False
    v = lo if lo is not None else hi
    if v is None:
        return False
    return float(v) == 0.0


def pick_price_range(offers: Any) -> tuple[float | None, float | None, str | None]:
    if not isinstance(offers, list):
        return None, None, None
    lows: list[float] = []
    highs: list[float] = []
    currency: str | None = None
    for o in offers:
        if not isinstance(o, dict):
            continue
        c = o.get("priceCurrency")
        if isinstance(c, str) and c:
            currency = c
        lp = _float_or_none(o.get("lowPrice"))
        hp = _float_or_none(o.get("highPrice"))
        pr = _float_or_none(o.get("price"))
        if lp is not None:
            lows.append(lp)
        if hp is not None:
            highs.append(hp)
        elif pr is not None:
            lows.append(pr)
            highs.append(pr)
    if not lows:
        return None, None, currency
    return min(lows), max(highs) if highs else max(lows), currency


def map_event_status(url_status: str | None) -> str:
    """Schema: tbd | scheduled | postponed | cancelled."""
    if not url_status:
        return "scheduled"
    if "EventCancelled" in url_status:
        return "cancelled"
    if "EventPostponed" in url_status:
        return "postponed"
    if "EventScheduled" in url_status:
        return "scheduled"
    return "scheduled"


def map_availability(offers: Any) -> str | None:
    if not isinstance(offers, list):
        return None
    for o in offers:
        if not isinstance(o, dict):
            continue
        av = o.get("availability")
        if isinstance(av, str):
            if "InStock" in av or "LimitedAvailability" in av:
                return "onSale"
            if "SoldOut" in av:
                return "soldOut"
    return None


def map_availability_from_listing_tickets(tickets: Any, ld_offers: Any) -> str | None:
    """
    JSON-LD offers first; then listing tickets. Omit when unknown (schema: null).
    """
    ld_av = map_availability(ld_offers)
    if ld_av in ("onSale", "soldOut"):
        return ld_av

    if isinstance(tickets, dict):
        ht = tickets.get("has_tickets")
        if ht is True or ht == 1 or str(ht).lower() == "true":
            return "onSale"
        if ht is False or ht == 0 or str(ht).lower() == "false":
            return "soldOut"
    return None


def is_sports_event(
    *,
    listing: dict[str, Any] | None = None,
    ld: dict[str, Any] | None = None,
    title: str | None = None,
) -> bool:
    """
    Sports events are out of scope for this feed.
    """
    if isinstance(ld, dict):
        ld_type = ld.get("@type")
        if isinstance(ld_type, str) and "sports" in ld_type.lower():
            return True
        if isinstance(ld_type, list):
            for t in ld_type:
                if isinstance(t, str) and "sports" in t.lower():
                    return True

    if isinstance(listing, dict):
        cats = listing.get("categories")
        if isinstance(cats, list):
            for c in cats:
                if "sport" in str(c).lower():
                    return True
        tags = listing.get("tags")
        if isinstance(tags, list):
            for t in tags:
                if "sport" in str(t).lower():
                    return True

    if title:
        t = title.lower()
        if " vs " in t or " at " in t:
            return True
    return False


def pick_purchase_url(ld: dict[str, Any]) -> str | None:
    offers = ld.get("offers")
    if isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict) and o.get("url"):
                return str(o["url"])
    u = ld.get("url")
    return str(u) if u else None


def build_location_block(ld: dict[str, Any], location: str) -> dict[str, Any]:
    loc = ld.get("location")
    name = ""
    address_line = ""
    lat: float | None = None
    lon: float | None = None
    if isinstance(loc, dict):
        name = clean_whitespace(str(loc.get("name") or ""))
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress"),
                addr.get("addressLocality"),
                addr.get("addressRegion"),
                addr.get("postalCode"),
                addr.get("addressCountry"),
            ]
            address_line = ", ".join(
                clean_whitespace(str(p)) for p in parts if p and str(p).strip()
            )
        elif isinstance(addr, str) and addr.strip():
            address_line = clean_whitespace(addr)
        geo = loc.get("geo")
        if isinstance(geo, dict):
            lat = _float_or_none(geo.get("latitude"))
            lon = _float_or_none(geo.get("longitude"))
    display_name = name or (address_line.split(",")[0] if address_line else "")
    out_name = clean_whitespace(str(location or display_name or ""))
    return {
        "name": out_name,
        "address": clean_whitespace(address_line),
        "latitude": lat,
        "longitude": lon,
    }


def organizers_from_ld(ld: dict[str, Any]) -> list[dict[str, Any]]:
    """Schema: { \"type\": \"organizer\", \"name\": \"...\" } (and similar roles)."""
    out: list[dict[str, Any]] = []
    org = ld.get("organizer")
    if isinstance(org, list):
        for o in org:
            if isinstance(o, dict) and o.get("name"):
                out.append(
                    {"type": "organizer", "name": clean_whitespace(str(o["name"]))}
                )
    elif isinstance(org, dict) and org.get("name"):
        out.append(
            {"type": "organizer", "name": clean_whitespace(str(org["name"]))}
        )
    return out


def _extract_image_id_from_url(image_url: str) -> str | None:
    """
    AllEvents CDN images use ``?v=<timestamp>`` (e.g. ``...jpg?v=1771812131``).
    Use that value for ``metadata.event.media[].source.id``; otherwise null.
    """
    try:
        parsed = urlparse(image_url)
        q = parse_qs(parsed.query, keep_blank_values=False)
        for key in ("v", "V"):
            vals = q.get(key)
            if vals and str(vals[0]).strip():
                return str(vals[0]).strip()
        return None
    except Exception:
        return None


def _format_media_id(i: int) -> str:
    return f"media-{i:03d}"


def _build_media_entries(image_urls: list[str], title: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, img_url in enumerate(image_urls, start=1):
        if not isinstance(img_url, str) or not img_url.strip():
            continue
        img = img_url.strip()
        ext = Path(urlparse(img).path).suffix.lower().lstrip(".") or "jpeg"
        mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
        source_id = _extract_image_id_from_url(img)

        out.append(
            {
                "id": _format_media_id(i),
                "type": "image",
                "title": (title or "")[:120],
                "shortDescription": "",
                "source": {"name": SOURCE_NAME, "id": source_id, "url": img},
                "thumbnail": {"url": img, "width": 150, "height": 150},
                "variants": [],
            }
        )
    return out


def build_media(ld: dict[str, Any], title: str) -> list[dict[str, Any]]:
    img = ld.get("image")
    image_urls: list[str] = []
    if isinstance(img, list):
        image_urls = [str(x) for x in img if isinstance(x, str) and x.strip()]
    elif isinstance(img, str):
        if img.strip():
            image_urls = [img.strip()]
    elif isinstance(img, dict):
        for key in ("url", "@url", "@id"):
            v = img.get(key)
            if isinstance(v, str) and v.strip():
                image_urls = [v.strip()]
                break

    if not image_urls:
        return []
    return _build_media_entries(image_urls, title)


def build_media_from_image(image_url: str, title: str) -> list[dict[str, Any]]:
    if not image_url:
        return []
    return _build_media_entries([image_url], title)


def listing_meta_to_row(meta: dict[str, Any]) -> dict[str, str]:
    return {
        "event_url": meta.get("event_url") or "",
        "event_id": meta.get("event_id") or "",
        "eventname_raw": meta.get("eventname_raw") or "",
        "eventname": meta.get("eventname") or "",
        "listing_json": meta.get("listing_json") or "",
        "city_slug": meta.get("city_slug") or "",
        "market_name": meta.get("market_name") or "",
        "state": meta.get("state") or "",
        "tegna_market": meta.get("tegna_market") or "",
        "tegna_label": meta.get("tegna_label") or "",
        "event_tz": meta.get("event_tz") or "",
        "organizer_name": meta.get("organizer_name") or "",
        "categories_json": meta.get("categories_json") or "[]",
        "tags_json": meta.get("tags_json") or "[]",
    }


def row_to_record(
    response,
    row: dict[str, str],
    ld: dict[str, Any] | None,
    *,
    now: str,
    html_title: str | None = None,
    html_description: str | None = None,
    current_share: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_id = row.get("event_id") or ""
    url = row.get("event_url") or ""
    event_tz = row.get("event_tz") or ""
    share = current_share or {}
    listing: dict[str, Any] = {}
    listing_raw = row.get("listing_json") or ""

    if listing_raw:
        try:
            parsed = json.loads(listing_raw)
            if isinstance(parsed, dict):
                listing = parsed
        except json.JSONDecodeError:
            listing = {}
    location = listing.get("location")
    label = listing.get("label")
    title = clean_whitespace(html_title or "") if html_title else ""
    description = clean_whitespace(html_description or "") if html_description else ""
    purchase_url = response.xpath('//button[@data-track="event-tickets|ticket-container-v4|tickets-click"]/@href').get()
    if purchase_url:
        purchase_url = re.sub(r"[\r\n\t]+", "", str(purchase_url)).strip()

    start_val: str | None = None
    end_val: str | None = None
    start_is_date = False
    end_is_date = False
    all_day = False

    year_hint: int | None = None
    try:
        st_hint = listing.get("start_time")
        if st_hint is not None:
            year_hint = datetime.fromtimestamp(float(st_hint), tz=timezone.utc).year
    except Exception:
        pass

    ui_start, ui_end = parse_ui_event_schedule_display(
        response, year_hint=year_hint
    )
    if ui_start:
        start_val = ui_start
        start_is_date = False
        all_day = False
        end_val = ui_end
        end_is_date = False

    if ld:
        if not title:
            title = clean_whitespace(str(ld.get("name") or ""))
        if not description:
            description = clean_whitespace(str(ld.get("description") or ""))
        if not ui_start:
            sv, sd_flag = parse_json_ld_schedule_start_end(
                ld.get("startDate"), timezone_name=event_tz
            )
            ev, ed_flag = parse_json_ld_schedule_start_end(
                ld.get("endDate"), timezone_name=event_tz
            )
            start_val, start_is_date = sv, sd_flag
            end_val, end_is_date = ev, ed_flag
            sd = ld.get("startDate")
            if isinstance(sd, str) and "T" not in sd and len(sd) <= 10:
                all_day = True
            if start_is_date:
                all_day = True

    # Fallbacks for title/description when HTML XPaths fail.
    if not title:
        title = clean_whitespace(
            str(
                share.get("safetitle")
                or row.get("eventname_raw")
                or row.get("eventname")
                or ""
            )
        )
    if not description:
        description = clean_whitespace(str(share.get("short_desc") or ""))

    # If JSON-LD missed dates, use listing start_time/end_time (epoch seconds).
    if not ui_start and (start_val is None or end_val is None) and listing:
        try:
            st = listing.get("start_time")
            if start_val is None and st is not None:
                start_val = format_epoch_local_no_z(float(st), event_tz)
                start_is_date = False
        except Exception:
            pass
        try:
            et = listing.get("end_time")
            if end_val is None and et is not None:
                end_val = format_epoch_local_no_z(float(et), event_tz)
                end_is_date = False
        except Exception:
            pass

    # If JSON-LD/listing missed dates but we have share dates, use calendar dates (all-day).
    if not ui_start and (start_val is None or end_val is None) and (
        share.get("start_date") or share.get("end_date")
    ):
        try:
            sd_str = str(share.get("start_date") or "").strip()
            ed_str = str(share.get("end_date") or "").strip()
            if sd_str:
                sd_dt = datetime.strptime(sd_str, "%m/%d/%Y").date()
                start_val = sd_dt.isoformat()
                start_is_date = True
                all_day = True
            if ed_str:
                ed_dt = datetime.strptime(ed_str, "%m/%d/%Y").date()
                end_val = ed_dt.isoformat()
                end_is_date = True
        except Exception:
            pass


    min_p, max_p, cur = pick_price_range(ld.get("offers") if ld else None)
    if cur is None:
        cur = str(share.get("currency") or "")

    tickets = listing.get("tickets")
    if isinstance(tickets, dict):
        if cur is None:
            ticket_currency = tickets.get("ticket_currency")
            if ticket_currency:
                cur = str(ticket_currency)
        if min_p is None and tickets.get("min_ticket_price") is not None:
            try:
                min_p = float(str(tickets.get("min_ticket_price")).strip())
            except Exception:
                pass
        if max_p is None and tickets.get("max_ticket_price") is not None:
            try:
                max_p = float(str(tickets.get("max_ticket_price")).strip())
            except Exception:
                pass

    # Do not populate currency when no numeric price is available.
    if min_p is None and max_p is None:
        cur = None

    loc_block = (
        build_location_block(ld, location)
        if ld
        else {
            "name": clean_whitespace(str(location or "")),
            "address": "",
            "latitude": None,
            "longitude": None,
        }
    )
    # Use current_event_share as a fallback for basic geo fields.
    if share:
        if not loc_block.get("address") and share.get("full_address"):
            loc_block["address"] = clean_whitespace(str(share["full_address"]))
        if loc_block.get("latitude") is None and share.get("latitude") is not None:
            loc_block["latitude"] = share["latitude"]
        if loc_block.get("longitude") is None and share.get("longitude") is not None:
            loc_block["longitude"] = share["longitude"]

    # If JSON-LD missed geo fields, use listing venue/address.
    if listing and (loc_block.get("address") == "" or loc_block.get("address") is None):
        venue = listing.get("venue")
        if isinstance(venue, dict):
            full_address = venue.get("full_address") or ""
            if full_address:
                loc_block["address"] = clean_whitespace(str(full_address))
            if loc_block.get("latitude") is None and venue.get("latitude") is not None:
                try:
                    loc_block["latitude"] = float(str(venue.get("latitude")).strip())
                except Exception:
                    pass
            if loc_block.get("longitude") is None and venue.get("longitude") is not None:
                try:
                    loc_block["longitude"] = float(str(venue.get("longitude")).strip())
                except Exception:
                    pass
            if not loc_block.get("name") and venue.get("city"):
                loc_block["name"] = str(venue.get("city") or "")
        elif listing.get("location_raw"):
            loc_block["address"] = clean_whitespace(
                str(listing.get("location_raw") or "")
            )

    # QA: sometimes `full_address` contains the venue name prefix.
    # Remove `<name> ...` from `location.address` when it appears to be embedded.
    addr_val = loc_block.get("address")
    name_val = loc_block.get("name")
    if isinstance(addr_val, str) and addr_val.strip() and isinstance(name_val, str) and name_val.strip():
        cleaned = re.sub(
            rf"^{re.escape(name_val)}\s*[,|-–—]?\s*",
            "",
            addr_val,
            flags=re.I,
        )
        if cleaned and cleaned.strip() and cleaned != addr_val:
            loc_block["address"] = cleaned.strip()

    _sanitize_location_placeholders(loc_block)
    if isinstance(loc_block.get("name"), str):
        loc_block["name"] = clean_whitespace(loc_block["name"])
    if isinstance(loc_block.get("address"), str):
        loc_block["address"] = clean_whitespace(loc_block["address"])

    org_roles = organizers_from_ld(ld) if ld else []

    tags = response.xpath('//div[@class="eps-event-tags-container"]/a/text()').getall()
    tags = [clean_whitespace(str(t)) for t in tags if str(t).strip()]
    if not tags:
        try:
            tags = json.loads(row.get("tags_json") or "[]")
        except json.JSONDecodeError:
            tags = []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t) for t in tags]

    if start_val and end_val and end_val <= start_val:
        end_val = None

    # End only when same calendar day as start (UTC date prefix); multi-day → no end.
    if start_val and end_val:

        def _iso_date_prefix(s: str) -> str | None:
            t = s.strip()
            if len(t) >= 10 and t[4] == "-" and t[7] == "-":
                return t[:10]
            return None

        ds = _iso_date_prefix(start_val)
        de = _iso_date_prefix(end_val)
        if ds and de and ds != de:
            end_val = None

    def _to_schedule_date_value(v: str | None) -> str | None:
        if not v:
            return None
        return v[:10] if "T" in v else v

    schedule: dict[str, Any]
    if not start_val:
        schedule = {"kind": "tbd"}
    else:
        use_date_precision = all_day or (
            start_is_date and (not end_val or end_is_date)
        )
        if use_date_precision and start_val:
            all_day = True
            sv = _to_schedule_date_value(start_val) or start_val
            ev = _to_schedule_date_value(end_val) if end_val else None
        else:
            sv = start_val
            ev = end_val

        has_end = bool(ev) and ev != sv
        kind = "span" if has_end else "point"
        prec = "date" if use_date_precision and sv else "dateTime"
        start_type: str = "date" if prec == "date" else "dateTime"
        schedule = {
            "kind": kind,
            "precision": prec,
            "allDay": bool(all_day and prec == "date"),
            "start": {"type": start_type, "value": sv},
        }
        if has_end and ev is not None:
            schedule["end"] = {
                "type": "date" if prec == "date" else "dateTime",
                "value": ev,
            }

    if min_p is None and max_p is None:
        pricing = {"kind": "tbd"}
    elif pricing_is_free_zero(min_p, max_p):
        pricing = {"kind": "free"}
    elif cur and min_p is not None and max_p is not None and min_p != max_p:
        pricing = {
            "kind": "span",
            "currency": cur,
            "minAmount": min_p,
            "maxAmount": max_p,
        }
    elif cur and min_p is not None and (max_p is None or max_p == min_p):
        pricing = {"kind": "point", "currency": cur, "amount": min_p}
    elif cur and max_p is not None:
        pricing = {"kind": "point", "currency": cur, "amount": max_p}
    else:
        pricing = {"kind": "tbd"}

    media = build_media(ld, title) if ld else []
    if not media and listing:
        img = (
            listing.get("thumb_url_large")
            or listing.get("thumb_url")
            or listing.get("banner_url")
        )
        if isinstance(img, str) and img.strip():
            media = build_media_from_image(img.strip(), title)

    nearby, site_id = infer_nearby_and_site_id(
        address=str(loc_block.get("address") or ""),
        market_name=str(row.get("market_name") or ""),
        state_name=str(row.get("state") or ""),
    )

    location_out: dict[str, Any] = {"nearBy": nearby}
    for lk, lv in loc_block.items():
        if lv is None:
            continue
        if lk in ("address", "name") and lv == "":
            continue
        location_out[lk] = lv

    purchase_combined = (
        (
            listing.get("tickets", {}).get("ticket_url")
            if isinstance(listing.get("tickets"), dict)
            else None
        )
        or purchase_url
    )
    if purchase_combined:
        purchase_combined = re.sub(r"[\r\n\t]+", "", str(purchase_combined)).strip()
    event_meta: dict[str, Any] = {
        "eventSchedule": schedule,
        "status": map_event_status(ld.get("eventStatus") if ld else None),
        "eventPricing": pricing,
        "availability": None,
        "audiences": [],
        "categories": [],
    }
    if description:
        event_meta["description"] = description
    if org_roles:
        event_meta["eventRoles"] = org_roles
    if purchase_combined:
        event_meta["purchaseUrl"] = purchase_combined
    if tags:
        event_meta["tags"] = tags
    if media:
        event_meta["media"] = media

    return {
        "provider": PROVIDER,
        "module": MODULE,
        "groupId": triple_id("group", PROVIDER, event_id, url),
        "id": triple_id("id", PROVIDER, event_id, url),
        "createdAt": now,
        "updatedAt": now,
        "title": title,
        "source": {
            "name": SOURCE_NAME,
            "id": SOURCE_ID,
            "url": SOURCE_HOME,
        },
        "recordSource": {
            "id": event_id,
            "url": url,
        },
        "location": location_out,
        "siteId": site_id,
        "metadata": {
            "event": event_meta,
        },
    }
