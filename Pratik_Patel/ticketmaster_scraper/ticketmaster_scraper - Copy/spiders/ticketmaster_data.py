import json
import logging
import math
import os
import re
import threading
import time
import uuid
import warnings
import csv
import random

import scrapy
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.internet import defer, error as twisted_error
from twisted.internet.task import deferLater
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from importlib import import_module
from importlib.util import find_spec
from datetime import date, datetime, time as dt_time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from lxml import html as lxml_html
if find_spec("openpyxl") is not None:
    load_workbook = import_module("openpyxl").load_workbook
else:
    load_workbook = None


warnings.filterwarnings("ignore")

_zip_dma_logger = logging.getLogger(__name__)

# Nominatim: https://operations.osmfoundation.org/policies/nominatim/ — max ~1 req/s, valid User-Agent required.
_GEOCODE_LOCK = threading.Lock()
_LAST_GEOCODE_MONO = 0.0
_NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_UA = "forage-ticketmaster-scraper/1.0"


def _geocode_address_nominatim(address: str | None) -> tuple[float | None, float | None]:
    """Resolve a free-text address to WGS84 coordinates (blocking, rate-limited)."""
    global _LAST_GEOCODE_MONO
    q = clean_str(address)
    if not q or len(q) < 4:
        return None, None
    ql = q.lower()
    if "online" in ql and ("event" in ql or "stream" in ql):
        return None, None
    params = urlencode({"q": q, "format": "json", "limit": "1"})
    url = f"{_NOMINATIM_SEARCH}?{params}"
    with _GEOCODE_LOCK:
        now = time.monotonic()
        wait = 1.15 - (now - _LAST_GEOCODE_MONO)
        if wait > 0:
            time.sleep(wait)
        req = Request(url, headers={"User-Agent": _NOMINATIM_UA, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            _LAST_GEOCODE_MONO = time.monotonic()
        except Exception:
            _LAST_GEOCODE_MONO = time.monotonic()
            return None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, list) or not data:
        return None, None
    first = data[0]
    if not isinstance(first, dict):
        return None, None
    return safe_float(first.get("lat")), safe_float(first.get("lon"))


def _geocode_query_candidates(location_name, location_address) -> list[str]:
    """
    Build ordered free-text queries for Nominatim. Venue name alone is often omitted
    when only location_address was used; combining name + city improves hit rate.
    """
    nm = clean_str(location_name) or ""
    addr = clean_str(location_address) or ""
    if not nm and not addr:
        return []
    cands: list[str] = []
    if nm and addr:
        if addr.lower().startswith(nm.lower()):
            cands.append(addr.strip().strip(","))
        else:
            cands.append(f"{nm}, {addr}".strip().strip(","))
            a = addr.strip().strip(",")
            if a.lower() != cands[0].lower():
                cands.append(a)
    elif addr:
        cands.append(addr.strip().strip(","))
    elif nm:
        cands.append(nm.strip())
    seen: set[str] = set()
    out: list[str] = []
    for q in cands:
        qn = q.strip().strip(",")
        if len(qn) < 4:
            continue
        k = qn.lower()
        if k not in seen:
            seen.add(k)
            out.append(qn)
    return out


# ======================================================================
#  HELPERS
# ======================================================================

def triple_id(*parts: str) -> str:
    u = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts))
    h = f"{u.int:032d}"
    return f"{int(h[0:4])}-{int(h[4:10])}-{int(h[10:18])}"


def safe_float(val):
    if val is None:
        return None
    if isinstance(val, str) and not val.strip():
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def clean_str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

_DATE_ONLY_SCHEDULE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Google / generic map URLs: @lat,lng and query params (HTML href/src + body fallback).
_HTML_MAP_COORD_RES = (
    re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)(?=[,/]|%7C|\?|#|\"|'|$|\s)"),
    re.compile(r"[?&]q=(-?\d+\.\d+)(?:%2[Cc]|,)(-?\d+\.\d+)\b"),
    re.compile(r"[?&]center=(-?\d+\.\d+),(-?\d+\.\d+)\b", re.I),
    re.compile(r"[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)\b", re.I),
)

_US_STATE_APPX_IANA = {
    "GA": "America/New_York",
    "FL": "America/New_York",
    "TX": "America/Chicago",
    "TN": "America/Chicago",
}


def ticketmaster_image_id_from_img(img: dict, img_url: str | None) -> str | None:
    if isinstance(img, dict):
        v = img.get("id") or img.get("imageId") or img.get("image_id")
        if v is not None and str(v).strip():
            return str(v).strip()
    if img_url:
        m = _UUID_RE.search(str(img_url))
        if m:
            return m.group(0)
    return None


BLOCKED_URL_SUBSTRINGS = ("https://am.ticketmaster.com/",)
ALLOWED_URL_PREFIXES = ("https://www.ticketmaster.com/",)


def is_blocked_url(u: str | None) -> bool:
    if not u:
        return True
    s = str(u).strip()
    if not s:
        return True
    sl = s.lower()
    if any(b in sl for b in BLOCKED_URL_SUBSTRINGS):
        return True
    try:
        host = (urlparse(s).hostname or "").lower()
    except Exception:
        host = ""
    if host == "am.ticketmaster.com":
        return True
    return False


def is_allowed_url(u: str | None) -> bool:
    if not u:
        return False
    s = str(u).strip()
    if not s:
        return False
    sl = s.lower()
    if not any(sl.startswith(p) for p in ALLOWED_URL_PREFIXES):
        return False
    if is_blocked_url(sl):
        return False
    return True


_META_DESC_RE_1 = re.compile(
    r"\btickets at the\s+(?P<venue>.+?)\s+in\s+(?P<city>[^,]+?),\s*(?P<state>[A-Z]{2})\s+for\s+"
    r"(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)

_META_DESC_RE_2 = re.compile(
    r"\bat\s+the\s+(?P<venue>.+?)\s+in\s+(?P<city>[^,]+?),\s*(?P<state>[A-Z]{2})\s+(?:for|on)\s+"
    r"(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)


def parse_meta_description(description: str | None):
    if not description:
        return None, None, None, None
    text = " ".join(str(description).split())
    for rx in (_META_DESC_RE_1, _META_DESC_RE_2):
        m = rx.search(text)
        if not m:
            continue
        venue = (m.group("venue") or "").strip()
        city = (m.group("city") or "").strip()
        state = (m.group("state") or "").strip()
        mon = (m.group("mon") or "").strip().title()
        day = (m.group("day") or "").strip()
        year = (m.group("year") or "").strip()
        start_date = None
        try:
            dt = datetime.strptime(f"{mon} {day} {year}", "%b %d %Y")
            start_date = dt.strftime("%Y-%m-%d")
        except Exception:
            start_date = None
        return venue, city, state, start_date
    return None, None, None, None


STATUS_MAP = {
    "https://schema.org/EventScheduled": "scheduled",
    "http://schema.org/EventScheduled": "scheduled",
    "https://schema.org/EventCancelled": "cancelled",
    "http://schema.org/EventCancelled": "cancelled",
    "https://schema.org/EventPostponed": "postponed",
    "http://schema.org/EventPostponed": "postponed",
    "https://schema.org/EventRescheduled": "postponed",
    "http://schema.org/EventRescheduled": "postponed",
    "https://schema.org/EventMovedOnline": "movedOnline",
    "http://schema.org/EventMovedOnline": "movedOnline",
}

_STATUS_EVENT_TAIL = {
    "EventScheduled": "scheduled",
    "EventCancelled": "cancelled",
    "EventPostponed": "postponed",
    "EventRescheduled": "postponed",
    "EventMovedOnline": "movedOnline",
}


def _map_raw_event_status_to_enum(raw) -> str | None:
    """Map schema.org / API eventStatus to our enum, or None if unmapped."""
    if raw is None:
        return None
    if isinstance(raw, list):
        raw = next((x for x in raw if x is not None and str(x).strip()), None)
        if raw is None:
            return None
    s = str(raw).strip()
    if not s:
        return None
    sl = s.lower()
    if sl == "rescheduled":
        return "postponed"
    if sl in ("scheduled", "cancelled", "postponed"):
        return sl
    if sl == "movedonline":
        return "movedOnline"
    if s in STATUS_MAP:
        return STATUS_MAP[s]
    s_https = s.replace("http://schema.org/", "https://schema.org/")
    if s_https in STATUS_MAP:
        return STATUS_MAP[s_https]
    s_http = s.replace("https://schema.org/", "http://schema.org/")
    if s_http in STATUS_MAP:
        return STATUS_MAP[s_http]
    tail = s.rstrip("/").split("/")[-1]
    for k, v in _STATUS_EVENT_TAIL.items():
        if tail.lower() == k.lower():
            return v
    return None


def _status_from_schema_event_status(schema) -> str | None:
    if not isinstance(schema, dict):
        return None
    return _map_raw_event_status_to_enum(schema.get("eventStatus"))


def status_from_event_and_schema(event=None, schema=None, *, html_tree=None):
    """
    Status for metadata.event: prefer frontend (eventschema JSON-LD, then page
    application/ld+json), then backend flags and API eventStatus only if nothing mapped.
    """
    schema = schema or {}
    event = event or {}

    fe = _status_from_schema_event_status(schema)
    if fe is not None:
        return fe

    if html_tree is not None:
        he = _status_from_html_json_ld_tree(html_tree)
        if he is not None:
            return he

    if event.get("isCanceled"):
        return "cancelled"
    if event.get("isPostponed"):
        return "postponed"
    if event.get("isRescheduled"):
        return "postponed"

    be = _map_raw_event_status_to_enum(event.get("eventStatus"))
    if be is not None:
        return be

    return "scheduled"


# QA metadata.event.status (ticketing / lifecycle enum for downstream).
_EVENT_SALE_STATUS_ENUM = frozenset(
    {
        "notApplicable",
        "unknown",
        "comingSoon",
        "presale",
        "onSale",
        "soldOut",
        "waitList",
        "closed",
        "postponed",
    },
)

_SOLD_OUT_COPY_RE = re.compile(r"\bsold\s*[- ]?\s*out\b", re.IGNORECASE)
_WAIT_LIST_COPY_RE = re.compile(r"\bwait\s*list\b|\bwaitlist\b", re.IGNORECASE)
_COMING_SOON_COPY_RE = re.compile(r"\bcoming\s+soon\b", re.IGNORECASE)
_PRE_SALE_COPY_RE = re.compile(r"\bpre[- ]?sale\b|\bpresale\b", re.IGNORECASE)


def normalize_metadata_event_status(
        status,
        *,
        description: str | None = None,
        title: str | None = None,
) -> str:
    """
    Final metadata.event.status — QA sale-state enum.
    Internal lifecycle values (scheduled, cancelled, …) map here; default sale state is onSale.
    """
    s = clean_str(status) or ""
    sl = s.lower()
    if sl == "rescheduled":
        s = "postponed"
        sl = "postponed"
    hay = f"{description or ''} {title or ''}"

    if sl == "postponed":
        return "postponed"
    if sl == "cancelled":
        return "closed"
    if sl == "unknown":
        return "unknown"
    if re.sub(r"\s+", "", sl) == "notapplicable":
        return "notApplicable"

    _internal = frozenset({"scheduled", "cancelled", "postponed", "movedOnline"})
    for canon in _EVENT_SALE_STATUS_ENUM:
        if canon.lower() == sl and canon not in _internal:
            return canon

    if not sl or sl in ("scheduled", "movedonline"):
        if _SOLD_OUT_COPY_RE.search(hay):
            return "soldOut"
        if _WAIT_LIST_COPY_RE.search(hay):
            return "waitList"
        if _PRE_SALE_COPY_RE.search(hay):
            return "presale"
        if _COMING_SOON_COPY_RE.search(hay):
            return "comingSoon"
        return "onSale"

    return "unknown"


def normalize_metadata_event_availability(
        availability,
        status: str,
        *,
        description: str | None = None,
        title: str | None = None,
        purchase_url: str | None = None,
) -> str:
    """
    metadata.event.availability — single pass with status dependency (postponed ≠ onSale).
    No purchaseUrl (missing/empty) → closed.
    """
    a = clean_str(availability) or "onSale"
    if (a or "").lower() == "offsale":
        a = "closed"
    if status == "postponed":
        if a == "waitList":
            out = "waitList"
        else:
            hay = f"{description or ''} {title or ''}".lower()
            if _WAIT_LIST_COPY_RE.search(hay):
                out = "waitList"
            else:
                out = "closed"
    else:
        out = a
    if not clean_str(purchase_url):
        return "closed"
    return out


def _contract_event_schedule_for_qa(schedule: dict | None) -> dict:
    """
    Remove partial start/end objects; use JSON null (None) when no valid instant.
    Never emit empty {} for start/end.
    """
    if not isinstance(schedule, dict):
        return {"kind": "tbd", "allDay": False, "start": None, "end": None}
    out = dict(schedule)
    kind = out.get("kind")

    def _segment_or_null(seg):
        if seg is None:
            return None
        if isinstance(seg, dict) and not seg:
            return None
        if not isinstance(seg, dict):
            return None
        v = _sanitize_event_schedule_value(seg.get("value"))
        if not v:
            return None
        typ = seg.get("type") or ("date" if len(v) == 10 and v[4:5] == "-" else "dateTime")
        return {"type": typ, "value": v}

    start_c = _segment_or_null(out.get("start"))
    end_c = _segment_or_null(out.get("end"))
    out["start"] = start_c
    out["end"] = end_c

    if kind == "point":
        if start_c is None:
            out["kind"] = "tbd"
            out.pop("precision", None)
            out["end"] = None
    elif kind == "span":
        if start_c is None or end_c is None:
            out["kind"] = "tbd"
            out["start"] = None
            out["end"] = None
            out.pop("precision", None)
    elif kind == "tbd":
        out.pop("precision", None)

    if out.get("kind") == "tbd":
        out.setdefault("allDay", False)
        if "start" not in out:
            out["start"] = None
        if "end" not in out:
            out["end"] = None

    return out


_DATETIME_TBA_PHRASES_RE = re.compile(
    r"date\s*&\s*time\s*tba|date\s+and\s+time\s+tba",
    re.IGNORECASE,
)

_MON_DAY_YEAR_IN_TEXT_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),\s*(\d{4})\b",
    re.IGNORECASE,
)


def _parse_month_day_year_to_iso(month: str, day: str, year: str) -> str | None:
    mon = (month or "").strip()[:3].title()
    try:
        d_int = int(day)
        y_int = int(year)
    except (TypeError, ValueError):
        return None
    for fmt in ("%b %d %Y",):
        try:
            return datetime.strptime(f"{mon} {d_int} {y_int}", fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _extract_first_calendar_date_iso_from_text(text: str | None) -> str | None:
    """First Mon DD, YYYY / Month DD, YYYY in combined copy → YYYY-MM-DD."""
    t = clean_str(text)
    if not t:
        return None
    m = _MON_DAY_YEAR_IN_TEXT_RE.search(t)
    if not m:
        return None
    return _parse_month_day_year_to_iso(m.group(1), m.group(2), m.group(3))


def _finalize_event_schedule_for_output(
        event_schedule,
        status: str,
        description: str | None,
        title: str | None,
) -> dict:
    """Postponed / explicit Date&Time TBA → tbd + nulls; else sanitize; recover date from copy if still tbd."""
    hay = f"{description or ''} {title or ''}"
    if status == "postponed":
        return {
            "kind": "tbd",
            "allDay": metadata_event_schedule_all_day(description, title),
            "start": None,
            "end": None,
        }
    if _DATETIME_TBA_PHRASES_RE.search(hay):
        return {
            "kind": "tbd",
            "allDay": metadata_event_schedule_all_day(description, title),
            "start": None,
            "end": None,
        }
    if not isinstance(event_schedule, dict):
        base = {"kind": "tbd", "allDay": metadata_event_schedule_all_day(description, title)}
    else:
        base = {**event_schedule}
        base["allDay"] = metadata_event_schedule_all_day(description, title)
        base = sanitize_event_schedule_start_end_values(base)
    contracted = _contract_event_schedule_for_qa(base)
    if (
            contracted.get("kind") == "tbd"
            and contracted.get("start") is None
            and contracted.get("end") is None
            and not _DATETIME_TBA_PHRASES_RE.search(hay)
    ):
        iso = _extract_first_calendar_date_iso_from_text(hay)
        if iso:
            contracted = {
                "kind": "point",
                "precision": "date",
                "allDay": metadata_event_schedule_all_day(description, title),
                "start": {"type": "date", "value": iso},
                "end": None,
            }
    return contracted


def _finalize_display_title(title: str | None, record_url: str | None) -> str:
    """Keep frontend title; fallback only to URL-derived title hint."""
    t = clean_str(title)
    if t:
        return t
    return clean_str(_title_hint_from_ticketmaster_url(record_url)) or ""


def _finalize_event_roles_for_output(event_roles) -> list[dict]:
    """Drop breadcrumb-like organizer values; keep only valid organizer names or omit organizer role."""
    if not isinstance(event_roles, list):
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for role in event_roles:
        if not isinstance(role, dict):
            continue
        rtype = clean_str(role.get("type")) or ""
        name = clean_str(role.get("name")) or ""
        if not rtype or not name:
            continue
        rtype_l = rtype.lower()
        if rtype_l == "organizer" and _is_navigation_breadcrumb_organizer_name(name):
            continue
        key = (rtype_l, name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": rtype, "name": name})
    return out


def finalize_ticketmaster_event_record(record: dict) -> dict:
    """
    Single QA normalization layer before JSON: status, availability, schedule, title.
    Mutates a shallow copy of the top-level record and nested metadata.event.
    """
    if not isinstance(record, dict):
        return record
    out = dict(record)
    meta = out.get("metadata")
    if not isinstance(meta, dict):
        return out
    ev = meta.get("event")
    if not isinstance(ev, dict):
        return out
    ev = dict(ev)
    meta = dict(meta)
    meta["event"] = ev
    out["metadata"] = meta

    description = ev.get("description")
    title_src = clean_str(out.get("title")) or ""

    status = normalize_metadata_event_status(
        ev.get("status"), description=description, title=title_src,
    )
    ev["status"] = status

    ev["availability"] = normalize_metadata_event_availability(
        ev.get("availability"),
        status,
        description=description,
        title=title_src,
        purchase_url=ev.get("purchaseUrl"),
    )

    ev["eventSchedule"] = _finalize_event_schedule_for_output(
        ev.get("eventSchedule"), status, description, title_src,
    )
    ev["eventRoles"] = _finalize_event_roles_for_output(ev.get("eventRoles"))

    record_url = clean_str(((out.get("recordSource") or {}).get("url")))
    out["title"] = _finalize_display_title(title_src, record_url)

    return out


def _party_to_roles(party, rtype, add_role):
    if party is None:
        return
    if isinstance(party, list):
        for p in party:
            _party_to_roles(p, rtype, add_role)
        return
    if isinstance(party, dict):
        name = clean_str(party.get("name")) or clean_str(party.get("legalName"))
        if name:
            add_role(rtype, name)
        return
    if isinstance(party, str) and party.strip():
        add_role(rtype, party.strip())


CSV_PATH = "all_urls.csv"

def _resolve_output_json_path() -> str:
    """
    Reuse an existing output JSON file if present (no new file), otherwise
    default to project-root `forafge_eventys_ticketmaster_update.json`.
    """
    spiders_dir = Path(__file__).resolve().parent
    package_dir = spiders_dir.parent
    project_root = package_dir.parent
    candidates = [
        project_root / "forafge_eventys_ticketmaster_update.json",
        package_dir / "forafge_eventys_ticketmaster_update.json",
        spiders_dir / "forafge_eventys_ticketmaster_update.json",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return str(project_root / "forafge_eventys_ticketmaster_update.json")


OUTPUT_PATH = _resolve_output_json_path()
OVERWRITE_OUTPUT_JSON = True

_pipeline_log = logging.getLogger(__name__)


def _format_eta_seconds(sec: float | None) -> str:
    if sec is None:
        return "—"
    s = max(0, int(round(sec)))
    if s < 60:
        return f"~{s}s"
    m, r = divmod(s, 60)
    if m < 60:
        return f"~{m}m {r}s"
    h, m2 = divmod(m, 60)
    return f"~{h}h {m2}m"


def _resolve_all_urls_csv_path() -> Path | None:
    name = CSV_PATH
    cwd_p = Path(os.getcwd()).resolve() / name
    if cwd_p.is_file():
        return cwd_p
    project_p = _PROJECT_ROOT / name
    if project_p.is_file():
        return project_p
    return None


def _record_source_url(record: dict) -> str:
    if not isinstance(record, dict):
        return ""
    rs = record.get("recordSource")
    if isinstance(rs, dict):
        return clean_str(rs.get("url")) or ""
    return ""


def _normalize_tm_event_url(url: str | None) -> str:
    """Match CSV URLs to JSON recordSource URLs (ignore query + trailing slash + case)."""
    u = clean_str(url) or ""
    if not u:
        return ""
    return u.rstrip("/").split("?", 1)[0].lower()


def _ticketmaster_record_is_complete(record: dict) -> bool:
    """
    True when an existing JSON row already has enough fields — skip re-fetching that URL.
    Offline venues: title + (location name or address) + latitude + longitude.
    Online events: title + online flag / Online Event location (coords may be absent).
    """
    if not isinstance(record, dict):
        return False
    url = _record_source_url(record)
    if not url or not is_allowed_url(url):
        return False
    if not clean_str(record.get("title")):
        return False
    loc = record.get("location")
    if not isinstance(loc, dict):
        return False
    meta = record.get("metadata")
    ev = (meta.get("event") if isinstance(meta, dict) else None) or {}
    ev = ev if isinstance(ev, dict) else {}
    online = bool(ev.get("online"))
    name = clean_str(loc.get("name"))
    addr = clean_str(loc.get("address"))
    nm_l = (name or "").lower()
    if online or nm_l == "online event" or "online event" in nm_l:
        return True
    if not name and not addr:
        return False
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        return False
    try:
        la = float(lat)
        lo = float(lon)
    except (TypeError, ValueError):
        return False
    if math.isnan(la) or math.isnan(lo) or math.isinf(la) or math.isinf(lo):
        return False
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return False
    return True


def _url_speed_priority(url: str) -> tuple[int, int]:
    """
    Lower tuple => higher priority (scrape earlier).
    Heuristic:
    - URLs with canonical /event/ path and no query are usually faster.
    - Query-heavy / non-canonical URLs often redirect or take longer.
    """
    u = clean_str(url) or ""
    ul = u.lower()
    has_event_path = "/event/" in ul
    has_query = "?" in u
    # 0 = fast-likely, 1 = medium, 2 = slow-likely
    if has_event_path and not has_query:
        bucket = 0
    elif has_event_path:
        bucket = 1
    else:
        bucket = 2
    # Shorter URLs often involve fewer redirect hops.
    return (bucket, len(u))


def _load_existing_records_from_output(path: Path) -> list[dict]:
    """
    Best-effort loader: handles valid JSON arrays and partially corrupted files by
    decoding dicts one by one from raw text.
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not text.strip():
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        pass

    out: list[dict] = []
    dec = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, end = dec.raw_decode(text, start)
        except Exception:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        idx = end
    return out


def _sanitize_for_json(obj):
    """Deep-copy to JSON-safe values (no NaN/Inf, odd keys, datetime/Decimal/bytes)."""
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, int) and not isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[str(k)] = _sanitize_for_json(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dt_time):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        try:
            f = float(obj)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        except (TypeError, ValueError, OverflowError):
            return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


# Top-level record keys that must stay in JSON as explicit null when unset.
_STRIP_NULLS_KEEP_NONE_KEYS = frozenset({"nearBy", "siteId"})


def _strip_nulls(obj):
    """Remove None values so JSON has no null, except selected keys (explicit JSON null)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        out = {}
        preserve_schedule_nulls = obj.get("kind") == "tbd"
        for k, v in obj.items():
            sk = str(k)
            if v is None:
                if sk in _STRIP_NULLS_KEEP_NONE_KEYS or (
                        preserve_schedule_nulls and sk in ("start", "end")
                ):
                    out[sk] = None
                continue
            inner = _strip_nulls(v)
            if inner is None:
                continue
            out[sk] = inner
        return out
    if isinstance(obj, (list, tuple)):
        return [
            x
            for x in (_strip_nulls(el) for el in obj)
            if x is not None
        ]
    return obj


ZYTE_API_KEY_ENV = os.environ.get("ZYTE_API_KEY", "7916eb9714394ae9a160c862c3e3da93").strip()
HAS_ZYTE_API = find_spec("scrapy_zyte_api") is not None and bool(ZYTE_API_KEY_ENV)

_ZIP_MARKET_CSV_FILENAMES = (
    "Tegna - Pilot Scope Locations.csv",
)

# Project root: directory that contains scrapy.cfg (same as Eventbrite reference: parents[2] from spiders/).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_zip_market_csv_path() -> Path | None:
    """Prefer CSV next to scrapy.cfg, then inner package folder."""
    spiders_dir = Path(__file__).resolve().parent
    package_dir = spiders_dir.parent
    project_root = package_dir.parent
    for name in _ZIP_MARKET_CSV_FILENAMES:
        for p in (project_root / name, package_dir / name):
            if p.is_file():
                return p
    return None


# Target metros for DMA workbook matching: ZIP in address → workbook row → compare to this list.
CITY_LIST = [
    "Atlanta, GA",
    "Austin, TX",
    "Orlando, FL",
    "Nashville, TN",
]


def _city_list_to_dma_keywords(entries: list[str] | tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Parse 'City, ST' → (CITY_UPPER, ST) for DMA name / workbook matching."""
    pairs: list[tuple[str, str]] = []
    for raw in entries:
        s = raw.strip()
        if "," not in s:
            continue
        city_part, st_part = s.rsplit(",", 1)
        city_key = " ".join(city_part.split()).upper()
        st = "".join(c for c in st_part if c.isalpha()).upper()[:2]
        if len(city_key) >= 2 and len(st) == 2:
            pairs.append((city_key, st))
    return tuple(pairs)


# DMA name must contain this keyword (substring, case-insensitive); St Abv must match workbook row.
_TARGET_DMA_KEYWORDS = _city_list_to_dma_keywords(CITY_LIST)

# Full state / territory names (lowercase) -> USPS abbreviation (Nielsen workbook sometimes uses full names).
_US_STATE_NAME_TO_ABBR: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "american samoa": "AS",
    "guam": "GU",
    "northern mariana islands": "MP",
    "puerto rico": "PR",
    "us virgin islands": "VI",
    "virgin islands": "VI",
}

# Ticketmaster URL slugs sometimes misspell state names (e.g. "tennessee" for Tennessee).
_TM_SLUG_STATE_TYPOS: dict[str, str] = {
    "tennessee": "TN",
    "tennesee": "TN",
}

_TM_EVENT_ID_HEX_RE = re.compile(r"^[0-9A-Fa-f]{10,24}$")

_ZIP_HEADER_EXACT = frozenset(
    {
        "zip",
        "zipcode",
        "zip code",
        "zip5",
        "zip 5",
        "postal code",
        "postal",
    }
)

# None = not loaded yet; after load, always a dict (possibly empty).
_DMA_ZIP_LOOKUP: dict[str, tuple[str, str]] | None = None


def _normalize_state_to_abbr(value) -> str:
    """Return two-letter USPS abbreviation, or empty if unknown."""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    letters_only = re.sub(r"[^A-Za-z]", "", raw).upper()
    if len(letters_only) == 2:
        return letters_only
    key = " ".join(raw.lower().split())
    return _US_STATE_NAME_TO_ABBR.get(key, "")


def _address_matches_city_list(text: str) -> bool:
    """True if text contains a CITY_LIST metro: city as whole word + state (abbr or full name)."""
    if not text or not text.strip():
        return False
    a_fold = " ".join(text.lower().split())
    for entry in CITY_LIST:
        s = entry.strip()
        if "," not in s:
            continue
        city_raw, st_raw = s.rsplit(",", 1)
        city = " ".join(city_raw.split()).lower()
        st_abbr = "".join(c for c in st_raw if c.isalpha()).upper()[:2]
        if len(city) < 2 or len(st_abbr) != 2:
            continue
        if not re.search(rf"\b{re.escape(city)}\b", a_fold, re.IGNORECASE):
            continue
        if re.search(rf"\b{re.escape(st_abbr.lower())}\b", a_fold):
            return True
        for full_name, abbr in _US_STATE_NAME_TO_ABBR.items():
            if abbr == st_abbr and re.search(rf"\b{re.escape(full_name)}\b", a_fold):
                return True
    return False


def _dma_header_indices(header: tuple | list) -> tuple[int | None, int | None, int | None]:
    """Detect ZIP, DMA name, and state columns from a header row (Nielsen layouts vary by sheet)."""
    if not header:
        return None, None, None
    idx_zip = idx_dma = idx_st = None
    for i, cell in enumerate(header):
        if cell is None:
            continue
        hn = " ".join(str(cell).strip().lower().split())

        if idx_zip is None:
            if hn in _ZIP_HEADER_EXACT:
                idx_zip = i
            elif "zip" in hn and "code" in hn and "dma" not in hn:
                idx_zip = i
            elif hn.startswith("zip") and len(hn) <= 12 and "dma" not in hn:
                idx_zip = i

        if idx_dma is None:
            hn_dma = hn.replace(":", " ").strip()
            if hn_dma in ("dma", "dma name", "dma region", "dma market"):
                idx_dma = i
            elif "dma" in hn and any(
                x in hn for x in ("name", "region", "market", "description", "title")
            ):
                idx_dma = i

        if idx_st is None:
            if hn in ("st", "st.", "st abv", "st. abv", "state", "state abv", "state code"):
                idx_st = i
            elif "state" in hn and any(
                x in hn for x in ("abbr", "abbrev", "abbreviation", "code")
            ):
                idx_st = i
    return idx_zip, idx_dma, idx_st


def _normalize_header_name(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _csv_header_indices(header: list[str]) -> tuple[int | None, int | None, int | None]:
    """Detect Zip/Market/State columns from Tegna CSV headers."""
    idx_zip = idx_dma = idx_st = None
    for i, raw in enumerate(header):
        hn = _normalize_header_name(raw)
        if idx_zip is None and hn in ("zip", "zip code", "zipcode"):
            idx_zip = i
        if idx_dma is None and hn in ("market", "dma", "dma name", "dma market"):
            idx_dma = i
        if idx_st is None and hn in ("state", "st", "state abv", "state code"):
            idx_st = i
    return idx_zip, idx_dma, idx_st


def _load_dma_excel(path: Path) -> dict[str, tuple[str, str]]:
    """Build zip (5 chars) -> (dma_name, st_abbr) from Nielsen DMA workbook / .xlsm."""
    result: dict[str, tuple[str, str]] = {}
    if load_workbook is None:
        _zip_dma_logger.warning("openpyxl not installed; nearBy cannot use DMA workbook.")
        return result
    if not path.exists():
        _zip_dma_logger.warning(
            "DMA workbook not found at %s; nearBy will be false for physical events with ZIP.",
            path,
        )
        return result

    # read_only=True allows only one forward pass per sheet; scan header + data in a single iteration.
    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        found_any_table = False
        for ws in wb.worksheets:
            iz = idm = ist = None
            header_logged = False
            for row in ws.iter_rows(values_only=True):
                if iz is None:
                    if not row:
                        continue
                    a, b, c = _dma_header_indices(row)
                    if a is None or b is None or c is None:
                        continue
                    iz, idm, ist = a, b, c
                    found_any_table = True
                    if not header_logged:
                        _zip_dma_logger.info(
                            "DMA table: sheet=%r zip_col=%s dma_col=%s st_col=%s",
                            ws.title,
                            iz,
                            idm,
                            ist,
                        )
                        header_logged = True
                    continue
                if not row or iz >= len(row):
                    continue
                z = _normalize_zip_cell(row[iz])
                if not z:
                    continue
                dma = row[idm] if idm < len(row) else None
                st = row[ist] if ist < len(row) else None
                if dma is None and st is None:
                    continue
                dma_s = str(dma).strip() if dma is not None else ""
                st_s = _normalize_state_to_abbr(st)
                if dma_s or st_s:
                    result[z] = (dma_s, st_s)

        if not found_any_table:
            _zip_dma_logger.warning(
                "Could not detect ZIP / DMA / State columns in any sheet of %s. "
                "Expected headers like 'Zip Code', 'DMA Name', 'St Abv'.",
                path,
            )
    finally:
        wb.close()
    _zip_dma_logger.info("Loaded %d ZIP rows from DMA workbook %s", len(result), path.name)
    return result


def _load_dma_csv(path: Path) -> dict[str, tuple[str, str]]:
    """Build zip (5 chars) -> (market, state_abbr) from Tegna pilot scope CSV."""
    result: dict[str, tuple[str, str]] = {}
    if not path.exists():
        _zip_dma_logger.warning("CSV not found at %s; nearBy ZIP lookup disabled.", path)
        return result
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                _zip_dma_logger.warning("CSV %s has no header row.", path)
                return result
            iz, idm, ist = _csv_header_indices(header)
            if iz is None or idm is None or ist is None:
                _zip_dma_logger.warning(
                    "Could not detect required columns in %s. Expected headers like 'Zip', 'Market', 'State'.",
                    path,
                )
                return result
            for row in reader:
                if not row or iz >= len(row):
                    continue
                z = _normalize_zip_cell(row[iz])
                if not z:
                    continue
                market = row[idm] if idm < len(row) else None
                st = row[ist] if ist < len(row) else None
                market_s = str(market).strip() if market is not None else ""
                st_s = _normalize_state_to_abbr(st)
                if market_s or st_s:
                    result[z] = (market_s, st_s)
    except OSError as exc:
        _zip_dma_logger.warning("CSV read failed (%s): %s", path, exc)
        return {}
    _zip_dma_logger.info("Loaded %d ZIP rows from market CSV %s", len(result), path.name)
    return result


def _normalize_zip_cell(value) -> str | None:
    """Normalize Excel ZIP values; pad with leading zeros when Excel dropped them (e.g. 2108 -> 02108)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if isinstance(value, float) and value == int(value):
            s = str(int(value))
        elif s.replace(".", "", 1).isdigit() and "." in s:
            s = str(int(float(s)))
    except (TypeError, ValueError):
        pass
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 5:
        return digits[:5]
    if 3 <= len(digits) <= 4:
        return digits.zfill(5)
    return None


def _extract_us_zip_from_address(address: str) -> str | None:
    """Extract 5-digit US ZIP; align with workbook keys (leading zeros preserved)."""
    if not address:
        return None
    text = address.strip()
    m = re.search(r"\b([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\b", text)
    if m:
        z = m.group(2)
        return _normalize_zip_cell(z) or z
    m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", text)
    if m:
        z = m.group(1)
        return _normalize_zip_cell(z) or z
    matches = list(re.finditer(r"\b(\d{5})(?:-\d{4})?\b", text))
    if matches:
        z = matches[-1].group(1)
        return _normalize_zip_cell(z) or z
    m = re.search(r"(?<![0-9])(\d{5})(?![0-9])", text)
    if m:
        z = m.group(1)
        return _normalize_zip_cell(z) or z
    return None


def _dma_matches_target_market(dma_name: str, st_abv: str) -> bool:
    if not dma_name:
        return False
    st = _normalize_state_to_abbr(st_abv)
    if len(st) != 2:
        return False
    dma_upper = re.sub(r"\s+", " ", str(dma_name).strip().upper())
    for keyword, state_key in _TARGET_DMA_KEYWORDS:
        if st != state_key:
            continue
        if keyword in dma_upper:
            return True
    return False


def _get_dma_zip_lookup() -> dict[str, tuple[str, str]]:
    global _DMA_ZIP_LOOKUP
    if _DMA_ZIP_LOOKUP is not None:
        return _DMA_ZIP_LOOKUP
    path = _resolve_zip_market_csv_path()
    if path is None or not path.is_file():
        _zip_dma_logger.warning(
            "Market CSV missing. Place one of %s under %s or %s",
            _ZIP_MARKET_CSV_FILENAMES,
            _PROJECT_ROOT,
            Path(__file__).resolve().parent.parent,
        )
        _DMA_ZIP_LOOKUP = {}
        return _DMA_ZIP_LOOKUP
    _DMA_ZIP_LOOKUP = _load_dma_csv(path)
    return _DMA_ZIP_LOOKUP

def parse_address(address: str):
    """
    Extract city, state, zip from address
    Example: 'State Farm Arena, Atlanta, GA 30303'
    """
    if not address:
        return None, None, None

    # ZIP
    zip_match = re.search(r'\b(\d{5})(?:-\d{4})?\b', address)
    zip_code = zip_match.group(1) if zip_match else None

    # STATE (2 letter)
    state_match = re.search(r'\b([A-Z]{2})\b\s*\d{5}', address)
    state = state_match.group(1) if state_match else None

    # CITY (before state)
    city_match = re.search(r',\s*([^,]+),\s*[A-Z]{2}\s*\d{5}', address)
    city = city_match.group(1).strip() if city_match else None

    return city, state, zip_code


def resolve_site_id(location_address: str) -> int | None:
    """Map venue address text to site id; Nashville/Orlando return None until IDs are defined."""
    if not location_address:
        return None
    city_text = re.sub(r"[^a-z]+", " ", str(location_address).lower()).strip()
    if "atlanta" in city_text:
        return 85
    if "austin" in city_text:
        return 269
    if "orlando" in city_text:
        return None
    if "nashville" in city_text:
        return None
    return None


def event_schedule_to_utc(schedule: dict, tz_name: str | None) -> dict:
    return _event_schedule_apply_utc_values(schedule, tz_name)


def extract_event_timezone(event: dict | None) -> str | None:
    event = event or {}
    dates = event.get("dates")
    dates = dates if isinstance(dates, dict) else {}
    for key in ("timezone", "timeZone", "ianaTimezone", "ianaTimeZone"):
        v = dates.get(key)
        if v and str(v).strip():
            return str(v).strip()
    for key in ("timezone", "timeZone"):
        v = event.get(key)
        if v and str(v).strip():
            return str(v).strip()
    venue = event.get("venue")
    if isinstance(venue, dict):
        for key in ("timezone", "timeZone"):
            v = venue.get(key)
            if v and str(v).strip():
                return str(v).strip()
    return None


def extract_event_timezone_with_fallback(event: dict | None, venue_state: str | None = None) -> str | None:
    tz = extract_event_timezone(event)
    if tz:
        return tz
    if venue_state:
        state_upper = str(venue_state).strip().upper()
        inferred = _US_STATE_APPX_IANA.get(state_upper)
        if inferred:
            return inferred
    return None


def event_is_online(event: dict | None, schema: dict | None = None, event_json: dict | None = None) -> bool:
    event = event or {}
    schema = schema or {}

    def _truthy(v) -> bool:
        if v is True:
            return True
        if isinstance(v, (int, float)) and v != 0:
            return v == 1
        if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes", "online"):
            return True
        return False

    for key in (
            "isOnlineEvent", "isOnlineOnly", "onlineEvent", "isVirtualEvent",
            "virtualEvent", "isStream", "streamingEvent",
    ):
        if _truthy(event.get(key)):
            return True

    venue = event.get("venue")
    if isinstance(venue, dict):
        vn = clean_str(venue.get("name"))
        if vn and "online" in vn.lower():
            return True
        pid = str(venue.get("placeId") or venue.get("id") or "").lower()
        if "virtual" in pid or "online" in pid:
            return True

    for k in ("placeType", "eventType"):
        t = event.get(k)
        if isinstance(t, str) and "online" in t.lower():
            return True

    loc = schema.get("location")
    if isinstance(loc, dict):
        ot = loc.get("@type") or loc.get("type")
        types = ot if isinstance(ot, list) else ([ot] if ot is not None else [])
        for t in types:
            if t is not None and "virtual" in str(t).lower():
                return True
        nm = clean_str(loc.get("name"))
        if nm and "online" in nm.lower():
            return True

    eam = schema.get("eventAttendanceMode")
    if eam:
        es = str(eam)
        if "OnlineEventAttendanceMode" in es or "onlineeventattendancemode" in es.lower().replace(" ", ""):
            return True

    try:
        pp = (event_json or {}).get("props", {}).get("pageProps", {}) or {}
        ei = pp.get("eventInfo")
        if isinstance(ei, dict):
            for key in ("isOnlineEvent", "onlineEvent", "isVirtual", "virtualEvent", "isOnlineOnly"):
                if _truthy(ei.get(key)):
                    return True
            v = ei.get("venue")
            if isinstance(v, dict):
                n = clean_str(v.get("name"))
                if n and "online" in n.lower():
                    return True
    except Exception:
        pass

    return False


def infer_event_schedule(start_time, end_time, event_timezone: str | None = None):
    st = (str(start_time).strip() if start_time else "") or None
    en = (str(end_time).strip() if end_time else "") or None
    if not st and not en:
        return event_schedule_to_utc({"kind": "tbd"}, event_timezone)

    def classify(s):
        if not s:
            return None, None, False
        s = s.strip()
        if len(s) > 10 and s[4] == "-" and s[7] == "-" and s[10] == " ":
            s = s[:10] + "T" + s[11:]
        if "T" in s:
            return "dateTime", s, False
        val = s[:10] if len(s) >= 10 else s
        return "date", val, True

    if st and en:
        if st == en:
            typ, val, all_day = classify(st)
            if not val:
                return event_schedule_to_utc({"kind": "tbd"}, event_timezone)
            out = {"kind": "point", "precision": typ, "allDay": all_day,
                   "start": {"type": typ, "value": val}}
            return event_schedule_to_utc(out, event_timezone)
        st_typ, st_val, _ = classify(st)
        en_typ, en_val, _ = classify(en)
        if not st_val and not en_val:
            return event_schedule_to_utc({"kind": "tbd"}, event_timezone)
        prec = "date" if st_typ == en_typ == "date" else "dateTime"
        all_day = True if st_typ == en_typ == "date" else False
        out = {
            "kind": "span", "precision": prec, "allDay": all_day,
            "start": {"type": st_typ or prec, "value": st_val or st},
            "end": {"type": en_typ or prec, "value": en_val or en},
        }
        return event_schedule_to_utc(out, event_timezone)

    single = st or en
    typ, val, all_day = classify(single)
    if not val:
        return event_schedule_to_utc({"kind": "tbd"}, event_timezone)
    out = {"kind": "point", "precision": typ, "allDay": all_day,
           "start": {"type": typ, "value": val}}
    return event_schedule_to_utc(out, event_timezone)


# Human-visible times (e.g. og:description), not ISO dates like "May 10, 2026".
_LISTED_TIME_12H_RE = re.compile(
    r"\b(?:1[0-2]|0?[1-9]):[0-5][0-9]\s*(?:a\.?m\.?|p\.?m\.?)\b"
    r"|\b(?:1[0-2]|0?[1-9])\s*(?:a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)

_ALL_DAY_LABEL_RE = re.compile(r"\ball[-\s]*day\b", re.IGNORECASE)


def metadata_event_schedule_all_day(description: str | None, title: str | None) -> bool:
    """
    metadata.event.eventSchedule.allDay — only true when copy explicitly says all day;
    any listed clock time (e.g. 1:00 PM) forces false.
    """
    text = f"{description or ''} {title or ''}"
    if not str(text).strip():
        return False
    if _LISTED_TIME_12H_RE.search(text):
        return False
    if _ALL_DAY_LABEL_RE.search(text):
        return True
    return False


_EVENT_SCHEDULE_INVALID_LITERALS = frozenset(
    {"tba", "date tba", "time tba", "tbd"}
)


def _sanitize_event_schedule_value(value) -> str | None:
    """
    metadata.event.eventSchedule.(start|end).value — only strict YYYY-MM-DD or
    ISO-8601 datetime (with 'T'); placeholders and junk → None (JSON null).
    """
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return None
    s = str(value).strip()
    if not s:
        return None
    lit = " ".join(s.lower().split())
    if lit in _EVENT_SCHEDULE_INVALID_LITERALS:
        return None
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            date.fromisoformat(s)
            return s
        except ValueError:
            return None
    if "T" not in s:
        return None
    try:
        to_parse = s[:-1] + "+00:00" if s.endswith("Z") else s
        datetime.fromisoformat(to_parse)
        return s
    except ValueError:
        return None


def sanitize_event_schedule_start_end_values(schedule: dict) -> dict:
    """Copy schedule; null invalid start/end values; never keep kind=point/span without a valid start."""
    if not isinstance(schedule, dict):
        return schedule
    out = dict(schedule)
    if out.get("kind") not in ("point", "span"):
        return out
    for key in ("start", "end"):
        if key not in out:
            continue
        seg = out.get(key)
        if not isinstance(seg, dict):
            continue
        new_seg = dict(seg)
        if "value" in new_seg:
            new_seg["value"] = _sanitize_event_schedule_value(new_seg.get("value"))
        out[key] = new_seg

    start_o = out.get("start")
    start_val = None
    if isinstance(start_o, dict):
        start_val = start_o.get("value")
    if not start_val:
        out["kind"] = "tbd"
        out["start"] = None
        out.pop("precision", None)

    return out


def _dates_segment_to_iso(segment, norm):
    if segment is None:
        return None
    if isinstance(segment, str):
        return norm(segment)
    if not isinstance(segment, dict):
        return None
    ld = norm(segment.get("localDate"))
    lt = norm(segment.get("localTime"))
    if ld and lt:
        return f"{ld}T{lt}"
    dt = norm(segment.get("dateTime") or segment.get("datetime"))
    if dt:
        return dt
    return ld


def extract_tm_start_end(event: dict | None, schema: dict | None):
    event = event or {}
    schema = schema or {}

    def norm(v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    start_val = None
    end_val = None

    dates = event.get("dates") or {}
    if isinstance(dates, dict):
        start_val = _dates_segment_to_iso(dates.get("start"), norm)
        end_val = _dates_segment_to_iso(dates.get("end"), norm)
        start_val = start_val or norm(dates.get("startDate"))
        end_val = end_val or norm(dates.get("endDate"))

    start_val = start_val or norm(event.get("startDate"))
    end_val = end_val or norm(event.get("endDate"))

    s_schema = norm(schema.get("startDate"))
    e_schema = norm(schema.get("endDate"))
    if not start_val:
        start_val = s_schema
    if not end_val:
        end_val = e_schema

    if start_val or end_val:
        return start_val, end_val
    return None, None


def _truthy_tm_flag(v) -> bool:
    if v is True:
        return True
    if isinstance(v, (int, float)) and v != 0:
        return v == 1
    if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def _pageprops_event_info(event_json) -> dict:
    try:
        ei = (event_json or {}).get("props", {}).get("pageProps", {}).get("eventInfo")
        return ei if isinstance(ei, dict) else {}
    except Exception:
        return {}


def _edp_context_event_from_next_data(event_json) -> dict:
    try:
        ctx = (event_json or {}).get("props", {}).get("pageProps", {}).get("edpData", {}).get("context")
        ev = ctx.get("event") if isinstance(ctx, dict) else None
        return ev if isinstance(ev, dict) else {}
    except Exception:
        return {}


def _event_date_from_ticketmaster_url_slug(url: str | None) -> date | None:
    """
    Many TM URLs end with ...-MM-DD-YYYY/event/<id> (US-style month-day-year in the slug).
    Returns None if the pattern is missing or invalid.
    """
    if not url:
        return None
    try:
        path = urlparse(str(url).strip()).path
        segs = [s for s in path.split("/") if s]
        try:
            ei = next(i for i, s in enumerate(segs) if s.lower() == "event")
        except StopIteration:
            return None
        if ei < 1:
            return None
        slug = segs[ei - 1]
    except Exception:
        return None
    m = re.search(r"-(\d{1,2})-(\d{1,2})-(\d{4})$", slug)
    if not m:
        return None
    try:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31 and 1990 <= year <= 2100):
            return None
        return date(year, month, day)
    except ValueError:
        return None


def _url_slug_event_date_is_past(url: str | None, now: datetime | None = None) -> bool:
    d = _event_date_from_ticketmaster_url_slug(url)
    if d is None:
        return False
    now = now or datetime.now(timezone.utc)
    return d < now.date()


def _next_data_is_past_event_flags(event, event_json) -> bool:
    """Ticketmaster sets isPastEvent on edp event and/or eventInfo when the show is over."""
    for src in (
        event or {},
        _pageprops_event_info(event_json),
        _edp_context_event_from_next_data(event_json),
    ):
        if isinstance(src, dict) and _truthy_tm_flag(src.get("isPastEvent")):
            return True
    return False


def _merge_event_dates_from_next_data_eventinfo(event, event_json):
    """Merge `props.pageProps.eventInfo` date fields into the edp event (same __NEXT_DATA__ tree)."""
    out = dict(event or {})
    ei = _pageprops_event_info(event_json)
    if not isinstance(ei, dict):
        return out
    ei_dates = ei.get("dates")
    if isinstance(ei_dates, dict):
        ev_dates = out.get("dates")
        if not isinstance(ev_dates, dict):
            out["dates"] = dict(ei_dates)
        else:
            merged = dict(ev_dates)
            for k, v in ei_dates.items():
                merged.setdefault(k, v)
            out["dates"] = merged
    for k in ("startDate", "endDate", "eventDate"):
        v = ei.get(k)
        if v is not None and str(v).strip() and not out.get(k):
            out[k] = v
    return out


def _event_datetime_string_is_tbd(val) -> bool:
    """True when value is missing or explicitly TBD/TBA/unannounced."""
    if val is None:
        return True
    t = str(val).strip()
    if not t:
        return True
    tl = t.lower()
    if tl in ("tbd", "tba", "null", "none", "undefined"):
        return True
    for token in ("to be determined", "to be announced", "date tbd", "time tbd", "tbd."):
        if token in tl:
            return True
    return False


def _primary_event_datetime_string_from_next_data(event, schema, event_json) -> str | None:
    """
    Single instant for completion: endDate if present, else startDate (extract_tm_start_end + eventInfo).
    None if missing/TBD/unusable → caller may treat as unknown / TBA.
    """
    merged = _merge_event_dates_from_next_data_eventinfo(event, event_json)
    start_s, end_s = extract_tm_start_end(merged, schema)
    primary = end_s or start_s
    if primary is None or _event_datetime_string_is_tbd(primary):
        return None
    return str(primary).strip()


def _parse_iso_to_utc_aware(iso_s: str | None) -> datetime | None:
    if not iso_s or not str(iso_s).strip():
        return None
    s = str(iso_s).strip()
    if len(s) >= 10 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_schedule_time_is_before_now(iso_s: str | None, now: datetime | None = None) -> bool:
    """True if date-only is strictly before today (UTC) or full datetime is before now (UTC)."""
    now = now or datetime.now(timezone.utc)
    if not iso_s or not str(iso_s).strip():
        return False
    s = str(iso_s).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            return d < now.date()
        except ValueError:
            pass
    dt = _parse_iso_to_utc_aware(s)
    return bool(dt and dt < now)


def tm_site_event_is_finished(event, schema, event_json=None, page_url: str | None = None) -> bool:
    """
    Skip when the show is clearly over:
    1) URL slug ends with -MM-DD-YYYY before /event/ and that calendar date is before today (UTC).
    2) __NEXT_DATA__ has isPastEvent on edp event, eventInfo, or embedded event blob.
    3) Parsable end/start from merged event + schema is strictly before now; missing/TBD → not skipped here.
    """
    event = event or {}
    schema = schema or {}
    if page_url and _url_slug_event_date_is_past(page_url):
        return True
    if _next_data_is_past_event_flags(event, event_json):
        return True
    primary = _primary_event_datetime_string_from_next_data(event, schema, event_json)
    if primary is None:
        return False
    return _event_schedule_time_is_before_now(primary, datetime.now(timezone.utc))


def _dates_object_signals_tba(dates) -> bool:
    if not isinstance(dates, dict):
        return False
    for key in ("spanType", "type", "status", "state"):
        v = dates.get(key)
        if isinstance(v, str):
            vl = v.lower()
            if any(
                x in vl
                for x in (
                    "tbd",
                    "tba",
                    "to be announced",
                    "to be determined",
                    "not yet scheduled",
                )
            ):
                return True
    for flag in (
        "dateTimeTBA",
        "dateTimeTba",
        "localDateTba",
        "timeTba",
        "isDateTBA",
        "isDateTba",
        "dateTBD",
        "timeTBD",
    ):
        if _truthy_tm_flag(dates.get(flag)):
            return True
    return False


def _event_dict_datetime_tba_flags(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    for flag in (
        "dateTimeTBA",
        "dateTimeTba",
        "localDateTba",
        "timeTba",
        "isDateTBA",
        "isDateTba",
    ):
        if _truthy_tm_flag(d.get(flag)):
            return True
    return False


def tm_site_event_is_datetime_tba(
    event, schema, event_json=None, page_url: str | None = None
) -> bool:
    """
    Date & time not finalized — do not save. Uses __NEXT_DATA__ (event + eventInfo + edp context),
    schema dates, and TM boolean flags. If the URL slug contains a concrete -MM-DD-YYYY, we still
    allow (date known from URL) unless JSON explicitly sets dateTimeTBA-style flags.
    """
    event = event or {}
    schema = schema or {}
    merged = _merge_event_dates_from_next_data_eventinfo(event, event_json)
    if _event_dict_datetime_tba_flags(merged):
        return True
    if _dates_object_signals_tba(merged.get("dates")):
        return True
    ei = _pageprops_event_info(event_json)
    if _event_dict_datetime_tba_flags(ei):
        return True
    if _dates_object_signals_tba(ei.get("dates")):
        return True
    edp_ev = _edp_context_event_from_next_data(event_json)
    if _event_dict_datetime_tba_flags(edp_ev):
        return True
    if isinstance(edp_ev, dict) and _dates_object_signals_tba(edp_ev.get("dates")):
        return True

    start_s, end_s = extract_tm_start_end(merged, schema)
    no_start = start_s is None or _event_datetime_string_is_tbd(start_s)
    no_end = end_s is None or _event_datetime_string_is_tbd(end_s)
    if no_start and no_end:
        if page_url and _event_date_from_ticketmaster_url_slug(page_url) is not None:
            return False
        return True
    return False


def _saved_record_is_datetime_tba(record: dict) -> bool:
    """Skip CSV fetch when we already stored a TBD schedule and URL has no slug date."""
    if not isinstance(record, dict):
        return False
    url = _record_source_url(record)
    if url and _event_date_from_ticketmaster_url_slug(url) is not None:
        return False
    meta = record.get("metadata")
    ev = (meta.get("event") if isinstance(meta, dict) else None) or {}
    ev = ev if isinstance(ev, dict) else {}
    sched = ev.get("eventSchedule")
    if isinstance(sched, dict) and sched.get("kind") == "tbd":
        return True
    return False


def _saved_record_event_is_finished(record: dict) -> bool:
    """Skip re-fetch when URL slug date is past, saved schedule is past, or TBD → do not skip from schedule alone."""
    if not isinstance(record, dict):
        return False
    url = _record_source_url(record)
    if url and _url_slug_event_date_is_past(url):
        return True
    meta = record.get("metadata")
    ev = (meta.get("event") if isinstance(meta, dict) else None) or {}
    ev = ev if isinstance(ev, dict) else {}
    sched = ev.get("eventSchedule")
    if not isinstance(sched, dict):
        return False
    if sched.get("kind") == "tbd":
        return False
    now = datetime.now(timezone.utc)
    end_o = sched.get("end")
    start_o = sched.get("start")
    end_v = end_o.get("value") if isinstance(end_o, dict) else None
    start_v = start_o.get("value") if isinstance(start_o, dict) else None
    primary = end_v or start_v
    if primary is None or _event_datetime_string_is_tbd(primary):
        return False
    return _event_schedule_time_is_before_now(str(primary).strip(), now)


def _is_navigation_breadcrumb_organizer_name(name: str) -> bool:
    """True when organizer string looks like site nav (e.g. Home/Sports Tickets/...), not a real org."""
    n = (name or "").strip()
    if not n:
        return True
    if n.lower() == "home":
        return True
    if "/" in n or "\\" in n:
        return True
    low = n.lower()
    if "tickets" in low and ("sports" in low or "concert" in low or "theater" in low):
        return True
    return False


def build_event_roles_from_ticketmaster(event=None, schema=None, event_title: str | None = None):
    roles: list[dict] = []
    seen: set[tuple[str, str]] = set()

    norm_title = clean_str(event_title)

    def add_role(rtype, name):
        nonlocal roles, seen, norm_title
        if not name or not str(name).strip():
            return
        name = str(name).strip()
        if str(rtype).lower() == "organizer" and _is_navigation_breadcrumb_organizer_name(name):
            return
        if norm_title and name.lower() == norm_title.lower():
            return
        key = (str(rtype).lower(), name.lower())
        if key in seen:
            return
        seen.add(key)
        roles.append({"type": rtype, "name": name})

    event = event or {}
    schema = schema or {}

    for key in ("performers", "performer", "actor", "musicBy", "contributor"):
        _party_to_roles(schema.get(key), "performer", add_role)

    for a in event.get("artists") or []:
        _party_to_roles(a, "performer", add_role)

    _party_to_roles(event.get("primaryArtist"), "performer", add_role)

    for att in event.get("attractions") or []:
        _party_to_roles(att, "performer", add_role)

    for key in ("homeTeam", "awayTeam", "visitingTeam", "competitor"):
        _party_to_roles(schema.get(key), "performer", add_role)

    _party_to_roles(schema.get("organizer"), "organizer", add_role)
    _party_to_roles(event.get("organizer"), "organizer", add_role)
    _party_to_roles(schema.get("presentedBy"), "organizer", add_role)
    _party_to_roles(event.get("presentedBy"), "organizer", add_role)
    _party_to_roles(schema.get("host"), "organizer", add_role)

    _party_to_roles(event.get("promoter"), "promoter", add_role)
    for p in event.get("promoters") or []:
        _party_to_roles(p, "promoter", add_role)

    for s in event.get("sponsors") or []:
        _party_to_roles(s, "sponsor", add_role)

    return roles


def _schema_type_tags(d: dict):
    ot = d.get("@type") or d.get("type")
    if isinstance(ot, list):
        return {str(x) for x in ot}
    if ot is not None and str(ot).strip():
        return {str(ot)}
    return set()


def _is_schema_offerish(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    if any(d.get(k) is not None for k in ("lowPrice", "highPrice", "price")):
        return True
    tags = _schema_type_tags(d)
    return any("Offer" in t for t in tags)


def _consume_offer_dict(o: dict, lows, highs, currencies, urls, avail_bits):
    if not isinstance(o, dict):
        return
    for attr, lst in (("lowPrice", lows), ("highPrice", highs)):
        v = o.get(attr)
        if v is not None:
            try:
                lst.append(float(v))
            except (TypeError, ValueError):
                pass
    pr = o.get("price")
    if pr is not None:
        try:
            fp = float(pr)
            lows.append(fp)
            highs.append(fp)
        except (TypeError, ValueError):
            pass
    pc = o.get("priceCurrency")
    if pc:
        currencies.append(str(pc).strip())
    u = o.get("url")
    if u:
        urls.append(str(u).strip())
    av = o.get("availability")
    if av:
        avail_bits.append(str(av))


def normalize_schema_offers(offers):
    if not offers:
        return {}

    lows, highs, currencies, urls, avail_bits = [], [], [], [], []

    def walk(node):
        if node is None:
            return
        if isinstance(node, list):
            for it in node:
                walk(it)
            return
        if not isinstance(node, dict):
            return
        nested = node.get("offers")
        if nested is not None:
            walk(nested)
        if _is_schema_offerish(node):
            _consume_offer_dict(node, lows, highs, currencies, urls, avail_bits)

    walk(offers)

    return {
        "lowPrice": min(lows) if lows else None,
        "highPrice": max(highs) if highs else None,
        "priceCurrency": currencies[0] if currencies else None,
        "url": urls[0] if urls else None,
        "availability": next((a for a in avail_bits if a), ""),
    }


def build_event_pricing(
        min_price,
        max_price,
        currency: str | None,
        *,
        description: str | None = None,
) -> dict:
    desc_l = (description or "").lower()
    donation_hint = any(
        p in desc_l
        for p in (
            "donation", "suggested donation", "pay what you want",
            "pwyc", "donate what you can",
        )
    )

    def fnum(x):
        if x is None:
            return None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    lo = fnum(min_price)
    hi = fnum(max_price)

    if lo is None and hi is None:
        return {"kind": "tbd"}

    if lo is None:
        lo = hi
    if hi is None:
        hi = lo

    if lo == 0 and hi == 0:
        return {"kind": "free"}

    cur = (currency or "USD").strip() or "USD"

    if donation_hint:
        out: dict = {"kind": "donation", "currency": cur, "suggestedDonation": lo}
        if lo != hi:
            out["notes"] = f"suggested range {lo:g}-{hi:g} {cur}"
        return out

    if lo == hi:
        return {"kind": "point", "currency": cur, "amount": lo}
    return {"kind": "span", "currency": cur, "minAmount": lo, "maxAmount": hi}


def description_from_next_data(event, schema, event_json):
    for candidate in (
            clean_str((schema or {}).get("description")),
            clean_str((event or {}).get("description")),
            clean_str((event or {}).get("pleaseNote")),
            clean_str((event or {}).get("info")),
    ):
        if candidate:
            return candidate
    try:
        pp = (event_json or {}).get("props", {}).get("pageProps", {}) or {}
        ei = pp.get("eventInfo") or {}
        if isinstance(ei, dict):
            for k in ("description", "pleaseNote", "eventDescription", "subtitle"):
                s = clean_str(ei.get(k))
                if s:
                    return s
            seo = ei.get("seo")
            if isinstance(seo, dict):
                s = clean_str(seo.get("description"))
                if s:
                    return s
    except Exception:
        pass
    return None


def _title_from_edp_event_header(html_tree) -> str | None:
    """
    Visible event title from the Ticketmaster EDP header (same region as the live page H1).
    Multiple h1 nodes or repeated text are deduped; first unique non-empty string wins.
    """
    if html_tree is None:
        return None
    try:
        h1_nodes = html_tree.xpath("//div[@id='edp-event-header']//h1")
    except Exception:
        return None
    seen: set[str] = set()
    for h1 in h1_nodes:
        try:
            raw = "".join(h1.itertext())
        except Exception:
            raw = "".join(h1.xpath(".//text()") or ())
        t = clean_str(re.sub(r"\s+", " ", (raw or "").strip()))
        if not t:
            continue
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)
        return t
    return None


def _title_from_html_tree(html_tree) -> str | None:
    """
    Title as on the loaded Ticketmaster HTML document only (no meta description):
    #edp-event-header h1 (deduped), then og:title, JSON-LD Event name/title, then <title>.
    """
    if html_tree is None:
        return None
    try:
        edp = _title_from_edp_event_header(html_tree)
        if edp:
            return edp
        og = "".join(html_tree.xpath('//meta[@property="og:title"]/@content')).strip()
        t = clean_str(og)
        if t:
            return t
        try:
            ev = first_event_from_json_ld_tree(html_tree)
        except Exception:
            ev = None
        if isinstance(ev, dict):
            for key in ("name", "title"):
                ld_t = clean_str(ev.get(key))
                if ld_t:
                    return ld_t
        doc_title = "".join(html_tree.xpath("//title/text()")).strip()
        return clean_str(doc_title) or None
    except Exception:
        return None


def _title_from_next_eventschema(event_json) -> str | None:
    """eventschemaJSONLD embedded in __NEXT_DATA__ (same payload the page hydrates from)."""
    if not event_json:
        return None
    try:
        esj = _deep_find_eventschema_jsonld(event_json)
    except Exception:
        return None
    if not isinstance(esj, dict):
        return None
    for key in ("name", "title"):
        s = clean_str(esj.get(key))
        if s:
            return s
    return None


def title_from_frontend_metadata(event, schema, event_json, html_tree=None):
    """
    Single resolution path for the on-site event title (no description-derived title):
    HTML #edp-event-header h1 (deduped) → og:title → JSON-LD Event → <title> →
    __NEXT_DATA__ eventschemaJSONLD → pageProps eventInfo / seo / meta →
    schema.org / edp event name fields.
    """
    t = _title_from_html_tree(html_tree)
    if t:
        return t

    t = _title_from_next_eventschema(event_json)
    if t:
        return t

    try:
        pp = (event_json or {}).get("props", {}).get("pageProps", {}) or {}
        ei = pp.get("eventInfo") or {}
        if isinstance(ei, dict):
            for k in ("name", "title", "displayName", "eventTitle", "pageTitle"):
                s = clean_str(ei.get(k))
                if s:
                    return s
            seo = ei.get("seo")
            if isinstance(seo, dict):
                for k in ("title", "pageTitle", "metaTitle", "ogTitle", "twitterTitle"):
                    s = clean_str(seo.get(k))
                    if s:
                        return s

        for container in (
                pp.get("seo"),
                pp.get("meta"),
                pp.get("metadata"),
                (event_json or {}).get("seo"),
                (event_json or {}).get("meta"),
                (event_json or {}).get("metadata"),
        ):
            if not isinstance(container, dict):
                continue
            for k in ("title", "pageTitle", "metaTitle", "og:title", "ogTitle", "twitter:title", "twitterTitle"):
                s = clean_str(container.get(k))
                if s:
                    return s
    except Exception:
        pass

    for candidate in (
            clean_str((schema or {}).get("name")),
            clean_str((schema or {}).get("title")),
            clean_str((event or {}).get("name")),
            clean_str((event or {}).get("title")),
    ):
        if candidate:
            return candidate
    return ""


def collect_media_images(event, schema, image_url_fallback):
    seen = set()
    out = []

    def add_img(d: dict):
        if not isinstance(d, dict):
            return
        loc = clean_str(d.get("location")) or clean_str(d.get("url")) or clean_str(d.get("src"))
        if not loc or loc in seen:
            return
        seen.add(loc)
        out.append(d)

    schema = schema or {}
    event = event or {}

    simg = schema.get("image")
    if isinstance(simg, list):
        for u in simg:
            if clean_str(u):
                add_img({"location": str(u).strip(), "width": None, "height": None})
    elif clean_str(simg):
        add_img({"location": str(simg).strip(), "width": None, "height": None})

    for u in filter(clean_str, [image_url_fallback, event.get("eventImageUrl")]):
        add_img({"location": str(u).strip(), "width": None, "height": None})

    pa = event.get("primaryArtist") or {}
    if isinstance(pa, dict):
        for im in pa.get("images") or []:
            if isinstance(im, dict):
                add_img(im)

    for im in event.get("images") or []:
        if isinstance(im, dict):
            add_img(im)
        elif clean_str(im):
            add_img({"location": str(im).strip(), "width": None, "height": None})

    attractions = event.get("attractions") or []
    if isinstance(attractions, list):
        for att in attractions:
            if not isinstance(att, dict):
                continue
            for im in att.get("images") or []:
                if isinstance(im, dict):
                    add_img(im)

    return out


def _edp_event_venue_from_next(next_data):
    if not isinstance(next_data, dict):
        return None
    try:
        ctx = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("edpData", {})
            .get("context", {})
        )
        if not isinstance(ctx, dict):
            return None
        ev = ctx.get("event")
        if not isinstance(ev, dict):
            return None
        v = ev.get("venue")
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and it:
                    return it
            return None
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _lat_lon_from_venue_structure(venue):
    """Extract lat/lon from Ticketmaster venue blobs (top-level, geo, location, GeoJSON)."""
    if not isinstance(venue, dict):
        return None, None
    lat = safe_float(
        _pick_first_str(
            venue.get("latitude"),
            venue.get("Latitude"),
            venue.get("lat"),
            venue.get("Lat"),
        )
    )
    lon = safe_float(
        _pick_first_str(
            venue.get("longitude"),
            venue.get("Longitude"),
            venue.get("lng"),
            venue.get("lon"),
            venue.get("Lng"),
            venue.get("Lon"),
        )
    )
    geo = venue.get("geo") if isinstance(venue.get("geo"), dict) else {}
    if lat is None:
        lat = safe_float(_pick_first_str(geo.get("latitude"), geo.get("lat")))
    if lon is None:
        lon = safe_float(
            _pick_first_str(geo.get("longitude"), geo.get("lng"), geo.get("lon"))
        )
    loc = venue.get("location") if isinstance(venue.get("location"), dict) else {}
    if lat is None:
        lat = safe_float(_pick_first_str(loc.get("latitude"), loc.get("lat")))
    if lon is None:
        lon = safe_float(
            _pick_first_str(loc.get("longitude"), loc.get("lng"), loc.get("lon"))
        )
    coords = loc.get("coordinates")
    coords_d = coords if isinstance(coords, dict) else {}
    if lat is None:
        lat = safe_float(_pick_first_str(coords_d.get("latitude"), coords_d.get("lat")))
    if lon is None:
        lon = safe_float(
            _pick_first_str(coords_d.get("longitude"), coords_d.get("lng"), coords_d.get("lon"))
        )
    if (lat is None or lon is None) and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        c_lon = safe_float(coords[0])
        c_lat = safe_float(coords[1])
        if lat is None:
            lat = c_lat
        if lon is None:
            lon = c_lon
    for nk in ("geoCoordinates", "gps", "centroid", "mapCoordinates"):
        sub = venue.get(nk)
        if not isinstance(sub, dict):
            continue
        if lat is None:
            lat = safe_float(_pick_first_str(sub.get("latitude"), sub.get("lat")))
        if lon is None:
            lon = safe_float(
                _pick_first_str(sub.get("longitude"), sub.get("lng"), sub.get("lon"))
            )
    ll_raw = _pick_first_str(venue.get("latLng"), venue.get("latlng"), venue.get("googleLatLong"))
    if (lat is None or lon is None) and ll_raw and "," in ll_raw:
        left, _, right = ll_raw.partition(",")
        if lat is None:
            lat = safe_float(left.strip())
        if lon is None:
            lon = safe_float(right.strip())
    return lat, lon


def fill_missing_coordinates(location_address, latitude, longitude, schema=None, event=None, next_data=None):
    schema = schema or {}
    event = event or {}
    next_data = next_data or {}

    lat = latitude
    lon = longitude

    if lat is None or lon is None:
        slat, slon = _lat_lon_from_schema_org_event_dict(schema)
        if lat is None:
            lat = slat
        if lon is None:
            lon = slon

    if lat is None or lon is None:
        es = _eventschema_dict_from_next_data(next_data)
        if isinstance(es, dict):
            el, en = _lat_lon_from_schema_org_event_dict(es)
            if lat is None:
                lat = el
            if lon is None:
                lon = en

    if lat is None or lon is None:
        ej = _event_json_dict_from_next_data(next_data)
        if isinstance(ej, dict):
            vl, vn = _lat_lon_from_venue_structure(_coalesce_venue_dict(ej))
            if lat is None:
                lat = vl
            if lon is None:
                lon = vn
            if lat is None or lon is None:
                jl, jn = _deep_extract_lat_lon(ej)
                if lat is None:
                    lat = jl
                if lon is None:
                    lon = jn

    if lat is None or lon is None:
        # props.pageProps.edpData.context.event.venue.{latitude,longitude}
        edp_v = _edp_event_venue_from_next(next_data)
        if isinstance(edp_v, dict):
            vl, vn = _lat_lon_from_venue_structure(edp_v)
            if lat is None:
                lat = vl
            if lon is None:
                lon = vn

    if lat is None or lon is None:
        venue = _coalesce_venue_dict(event) if isinstance(event, dict) else {}
        if isinstance(venue, dict):
            vl, vn = _lat_lon_from_venue_structure(venue)
            if lat is None:
                lat = vl
            if lon is None:
                lon = vn

    if lat is None or lon is None:
        clat, clon = _deep_extract_lat_lon(schema)
        if lat is None:
            lat = clat
        if lon is None:
            lon = clon

    if lat is None or lon is None:
        clat, clon = _deep_extract_lat_lon(event)
        if lat is None:
            lat = clat
        if lon is None:
            lon = clon

    if lat is None or lon is None:
        n_lat, n_lon = _extract_lat_lon_from_next_data(next_data)
        if lat is None:
            lat = n_lat
        if lon is None:
            lon = n_lon

    if lat is None or lon is None:
        clat, clon = _deep_extract_lat_lon(next_data)
        if lat is None:
            lat = clat
        if lon is None:
            lon = clon

    return lat, lon


def extract_eventinfo_venue(next_data):
    try:
        v = (
            (next_data or {})
            .get("props", {})
            .get("pageProps", {})
            .get("eventInfo", {})
            .get("venue")
        )
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and it:
                    return it
            return {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _pick_first_str(*vals):
    for v in vals:
        s = clean_str(v)
        if s:
            return s
    return None


def _coords_plausible(lat, lon) -> bool:
    if lat is None or lon is None:
        return False
    try:
        if abs(float(lat)) > 90 or abs(float(lon)) > 180:
            return False
    except (TypeError, ValueError):
        return False
    if abs(float(lat)) < 1e-7 and abs(float(lon)) < 1e-7:
        return False
    return True


def _lat_lon_from_meta_icbm(raw) -> tuple[float | None, float | None]:
    if raw is None:
        return None, None
    t = str(raw).strip().replace(" ", "")
    for sep in (";", ","):
        if sep in t:
            a, _, b = t.partition(sep)
            la, lo = safe_float(a), safe_float(b)
            if _coords_plausible(la, lo):
                return la, lo
    return None, None


def _try_maps_url(url) -> tuple[float | None, float | None]:
    if not url:
        return None, None
    ul = str(url).lower()
    if not any(
        k in ul
        for k in (
            "google.",
            "goo.gl",
            "maps.google",
            "/maps",
            "mapbox",
            "openstreetmap",
            "apple.com/maps",
        )
    ):
        return None, None
    for rx in _HTML_MAP_COORD_RES:
        m = rx.search(str(url))
        if m:
            la, lo = safe_float(m.group(1)), safe_float(m.group(2))
            if _coords_plausible(la, lo):
                return la, lo
    return None, None


def extract_lat_lon_from_html(tree, body_text=None):
    """Extract latitude/longitude via XPath on the DOM (meta, itemprop, data-*, map links)."""
    if tree is not None:
        # Schema.org microdata on the page
        lat_nodes = tree.xpath('//*[@itemprop="latitude"]/@content')
        lon_nodes = tree.xpath('//*[@itemprop="longitude"]/@content')
        if lat_nodes and lon_nodes:
            la, lo = safe_float(lat_nodes[0]), safe_float(lon_nodes[0])
            if _coords_plausible(la, lo):
                return la, lo
        lat_t = tree.xpath('//*[@itemprop="latitude"]/text()')
        lon_t = tree.xpath('//*[@itemprop="longitude"]/text()')
        if lat_t and lon_t:
            la, lo = safe_float(lat_t[0]), safe_float(lon_t[0])
            if _coords_plausible(la, lo):
                return la, lo

        for xp in (
            '//meta[@name="ICBM"]/@content',
            '//meta[@name="icbm"]/@content',
            '//meta[@name="geo.position"]/@content',
            '//meta[@name="Geo.Position"]/@content',
        ):
            for v in tree.xpath(xp):
                la, lo = _lat_lon_from_meta_icbm(v)
                if _coords_plausible(la, lo):
                    return la, lo

        og_la = tree.xpath('//meta[@property="og:latitude"]/@content')
        og_lo = tree.xpath('//meta[@property="og:longitude"]/@content')
        if og_la and og_lo:
            la, lo = safe_float(og_la[0]), safe_float(og_lo[0])
            if _coords_plausible(la, lo):
                return la, lo

        pl_la = tree.xpath('//meta[@property="place:location:latitude"]/@content')
        pl_lo = tree.xpath('//meta[@property="place:location:longitude"]/@content')
        if pl_la and pl_lo:
            la, lo = safe_float(pl_la[0]), safe_float(pl_lo[0])
            if _coords_plausible(la, lo):
                return la, lo

        for el in tree.xpath("//*[@data-latitude][@data-longitude]")[:16]:
            la = safe_float(el.get("data-latitude"))
            lo = safe_float(el.get("data-longitude"))
            if _coords_plausible(la, lo):
                return la, lo
        for el in tree.xpath("//*[@data-lat][@data-lng]")[:16]:
            la = safe_float(el.get("data-lat"))
            lo = safe_float(el.get("data-lng"))
            if _coords_plausible(la, lo):
                return la, lo
        for el in tree.xpath("//*[@data-lat][@data-lon]")[:16]:
            la = safe_float(el.get("data-lat"))
            lo = safe_float(el.get("data-lon"))
            if _coords_plausible(la, lo):
                return la, lo

        for attr in ("href", "src", "data-url", "data-map-url", "data-src"):
            for u in tree.xpath(f"//*[@{attr}]/@{attr}"):
                s = clean_str(str(u))
                if not s or len(s) < 12:
                    continue
                la, lo = _try_maps_url(s)
                if _coords_plausible(la, lo):
                    return la, lo

    blob = clean_str(body_text)
    if blob:
        for rx in _HTML_MAP_COORD_RES:
            m = rx.search(blob)
            if m:
                la, lo = safe_float(m.group(1)), safe_float(m.group(2))
                if _coords_plausible(la, lo):
                    return la, lo
    return None, None


def _merge_coords_from_html(latitude, longitude, tree, body_text):
    xla, xlo = extract_lat_lon_from_html(tree, body_text)
    la, lo = latitude, longitude
    if _coords_plausible(xla, xlo):
        if la is None:
            la = xla
        if lo is None:
            lo = xlo
    return la, lo


def _deep_extract_lat_lon(obj, max_depth=6):
    """Recursively scan nested objects for a latitude/longitude pair."""
    if max_depth < 0:
        return None, None
    if isinstance(obj, dict):
        lat = safe_float(
            obj.get("latitude")
            if "latitude" in obj
            else obj.get("lat")
        )
        lon = safe_float(
            obj.get("longitude")
            if "longitude" in obj
            else obj.get("lng")
            if "lng" in obj
            else obj.get("lon")
            if "lon" in obj
            else obj.get("long")
        )
        if lat is not None and lon is not None:
            return lat, lon
        # GeoJSON-style: {"coordinates": [lon, lat]}
        coords = obj.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            c_lon = safe_float(coords[0])
            c_lat = safe_float(coords[1])
            if c_lat is not None and c_lon is not None:
                return c_lat, c_lon
        for v in obj.values():
            clat, clon = _deep_extract_lat_lon(v, max_depth=max_depth - 1)
            if clat is not None and clon is not None:
                return clat, clon
    elif isinstance(obj, list):
        # GeoJSON bare list fallback: [lon, lat]
        if len(obj) >= 2:
            c_lon = safe_float(obj[0])
            c_lat = safe_float(obj[1])
            if c_lat is not None and c_lon is not None:
                return c_lat, c_lon
        for it in obj:
            clat, clon = _deep_extract_lat_lon(it, max_depth=max_depth - 1)
            if clat is not None and clon is not None:
                return clat, clon
    return None, None


def _try_parse_json_object(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            o = json.loads(v)
            return o if isinstance(o, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _lat_lon_from_schema_org_event_dict(schema):
    """
    latitude/longitude from Schema.org Event JSON (e.g. eventschemaJSONLD): location + geo,
    or any nested lat/lon in the blob.
    """
    if not isinstance(schema, dict):
        return None, None
    loc = schema.get("location")
    if isinstance(loc, list) and loc:
        loc = loc[0]
    if isinstance(loc, str):
        dl, dn = _deep_extract_lat_lon(schema)
        return dl, dn
    if isinstance(loc, dict):
        lat = safe_float(
            _pick_first_str(
                loc.get("latitude"),
                loc.get("Latitude"),
                loc.get("lat"),
            )
        )
        lon = safe_float(
            _pick_first_str(
                loc.get("longitude"),
                loc.get("Longitude"),
                loc.get("lng"),
                loc.get("lon"),
            )
        )
        geo = loc.get("geo") if isinstance(loc.get("geo"), dict) else {}
        if lat is None:
            lat = safe_float(_pick_first_str(geo.get("latitude"), geo.get("lat")))
        if lon is None:
            lon = safe_float(
                _pick_first_str(geo.get("longitude"), geo.get("lng"), geo.get("lon"))
            )
        if lat is not None and lon is not None:
            return lat, lon
        vl, vn = _lat_lon_from_venue_structure(loc)
        if vl is not None and vn is not None:
            return vl, vn
        dl, dn = _deep_extract_lat_lon(loc)
        if dl is not None and dn is not None:
            return dl, dn
    return _deep_extract_lat_lon(schema)


def _eventschema_dict_from_next_data(next_data):
    """eventschemaJSONLD from edpData.context (string or dict) or deep search in __NEXT_DATA__."""
    if not isinstance(next_data, dict):
        return None
    try:
        ctx = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("edpData", {})
            .get("context")
        )
        if isinstance(ctx, dict):
            esj = ctx.get("eventschemaJSONLD")
            if isinstance(esj, dict):
                return esj
            got = _try_parse_json_object(esj)
            if isinstance(got, dict):
                return got
    except Exception:
        pass
    return _deep_find_eventschema_jsonld(next_data)


def _deep_find_event_json_blob(obj, max_depth=40):
    """Find eventJSON / eventJson (string or object) anywhere under __NEXT_DATA__."""
    found: list[dict] = []

    def walk(o, depth):
        if depth > max_depth:
            return
        if isinstance(o, dict):
            for k in ("eventJSON", "eventJson", "event_json"):
                if k not in o:
                    continue
                v = o.get(k)
                if isinstance(v, dict):
                    found.append(v)
                else:
                    p = _try_parse_json_object(v)
                    if isinstance(p, dict):
                        found.append(p)
            for vv in o.values():
                walk(vv, depth + 1)
        elif isinstance(o, list):
            for it in o:
                walk(it, depth + 1)

    walk(obj, 0)
    return found[0] if found else None


def _event_json_dict_from_next_data(next_data):
    """eventJSON on edpData.context / pageProps, else deep search."""
    if not isinstance(next_data, dict):
        return None
    for path in (
        ("props", "pageProps", "edpData", "context"),
        ("props", "pageProps",),
    ):
        cur: object = next_data
        for p in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(p)
        if not isinstance(cur, dict):
            continue
        for k in ("eventJSON", "eventJson", "event_json"):
            v = cur.get(k)
            if isinstance(v, dict):
                return v
            got = _try_parse_json_object(v)
            if isinstance(got, dict):
                return got
    return _deep_find_event_json_blob(next_data)


def _extract_lat_lon_from_next_data(next_data):
    """High-confidence extractor for known Ticketmaster nextData paths."""
    if not isinstance(next_data, dict):
        return None, None

    lat, lon = None, None
    edp_v = _edp_event_venue_from_next(next_data)
    if isinstance(edp_v, dict):
        lat, lon = _lat_lon_from_venue_structure(edp_v)

    es = _eventschema_dict_from_next_data(next_data)
    if isinstance(es, dict):
        el, en = _lat_lon_from_schema_org_event_dict(es)
        if lat is None:
            lat = el
        if lon is None:
            lon = en

    ej = _event_json_dict_from_next_data(next_data)
    if isinstance(ej, dict):
        vl, vn = _lat_lon_from_venue_structure(_coalesce_venue_dict(ej))
        if lat is None:
            lat = vl
        if lon is None:
            lon = vn
        if lat is None or lon is None:
            jl, jn = _deep_extract_lat_lon(ej)
            if lat is None:
                lat = jl
            if lon is None:
                lon = jn

    venue = extract_eventinfo_venue(next_data)
    if isinstance(venue, dict):
        el, en = _lat_lon_from_venue_structure(venue)
        if lat is None:
            lat = el
        if lon is None:
            lon = en

    return lat, lon


def _json_ld_type_is_event(ot):
    if ot == "Event":
        return True
    if isinstance(ot, list):
        return any(_json_ld_type_is_event(x) for x in ot)
    if isinstance(ot, str):
        if ot.endswith("/Event") or ot.endswith("#Event"):
            return True
        tail = ot.split("/")[-1].split("#")[-1]
        if tail.endswith("Event"):
            return True
    return False


def _iter_schema_org_events(obj):
    if obj is None:
        return
    if isinstance(obj, list):
        for it in obj:
            yield from _iter_schema_org_events(it)
        return
    if not isinstance(obj, dict):
        return
    if _json_ld_type_is_event(obj.get("@type")):
        yield obj
    graph = obj.get("@graph")
    if graph:
        yield from _iter_schema_org_events(graph)


def first_event_from_json_ld_tree(tree):
    texts = tree.xpath('//script[@type="application/ld+json"]/text()')
    if not texts:
        texts = tree.xpath('//script[contains(@type, "ld+json")]/text()')
    for text in texts:
        raw = (text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for ev in _iter_schema_org_events(data):
            return ev
    return None


def event_schema_from_html_tree(tree):
    """
    Prefer Ticketmaster's dedicated event schema script:
    <script type="application/ld+json" data-bdd="eventSchema">...</script>
    """
    if tree is None:
        return None
    texts = tree.xpath(
        '//script[@data-bdd="eventSchema" and contains(@type, "ld+json")]/text()'
    )
    if not texts:
        return first_event_from_json_ld_tree(tree)
    for text in texts:
        raw = (text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for ev in _iter_schema_org_events(data):
            return ev
    return first_event_from_json_ld_tree(tree)


def _merge_schema_geo_from_html(schema, html_tree):
    """
    If active schema misses coordinates, enrich it from page-level eventSchema JSON-LD.
    """
    if not isinstance(schema, dict):
        schema = {}
    lat, lon = _lat_lon_from_schema_org_event_dict(schema)
    if lat is not None and lon is not None:
        return schema
    html_schema = event_schema_from_html_tree(html_tree)
    if not isinstance(html_schema, dict):
        return schema

    merged = dict(schema)
    src_loc = html_schema.get("location")
    dst_loc = merged.get("location")
    if isinstance(src_loc, list) and src_loc:
        src_loc = src_loc[0]
    if isinstance(dst_loc, list) and dst_loc:
        dst_loc = dst_loc[0]

    if isinstance(src_loc, dict) and not isinstance(dst_loc, dict):
        merged["location"] = dict(src_loc)
        return merged
    if not isinstance(src_loc, dict) or not isinstance(dst_loc, dict):
        return merged

    out_loc = dict(dst_loc)
    for key in ("latitude", "longitude", "address", "name", "sameAs", "image"):
        if out_loc.get(key) in (None, "", []):
            val = src_loc.get(key)
            if val not in (None, "", []):
                out_loc[key] = val
    src_geo = src_loc.get("geo") if isinstance(src_loc.get("geo"), dict) else {}
    dst_geo = out_loc.get("geo") if isinstance(out_loc.get("geo"), dict) else {}
    if src_geo:
        out_geo = dict(dst_geo)
        for key in ("latitude", "longitude", "lat", "lng", "lon", "@type"):
            if out_geo.get(key) in (None, "", []):
                val = src_geo.get(key)
                if val not in (None, "", []):
                    out_geo[key] = val
        if out_geo:
            out_loc["geo"] = out_geo
    merged["location"] = out_loc
    return merged


def _status_from_html_json_ld_tree(html_tree) -> str | None:
    """First Event in visible page JSON-LD — secondary frontend source when edp schema lacks eventStatus."""
    if html_tree is None:
        return None
    try:
        ev = first_event_from_json_ld_tree(html_tree)
    except Exception:
        return None
    if not isinstance(ev, dict):
        return None
    return _map_raw_event_status_to_enum(ev.get("eventStatus"))


def _deep_find_eventschema_jsonld(obj, max_depth=40):
    found = []

    def walk(o, depth):
        if depth > max_depth: return
        if isinstance(o, dict):
            v = o.get("eventschemaJSONLD")
            if isinstance(v, str) and v.strip():
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, dict): found.append(parsed)
                except json.JSONDecodeError:
                    pass
            for vv in o.values(): walk(vv, depth + 1)
        elif isinstance(o, list):
            for it in o: walk(it, depth + 1)

    walk(obj, 0)
    for s in found:
        if s.get("startDate") or _json_ld_type_is_event(s.get("@type")): return s
    return found[0] if found else None


def _coalesce_venue_dict(event):
    """TM sports / TBA events may use place/location instead of venue, or omit nested venue."""
    if not isinstance(event, dict):
        return {}
    v = event.get("venue")
    if isinstance(v, list):
        for it in v:
            if isinstance(it, dict) and it:
                return it
        v = None
    if isinstance(v, dict) and v:
        return v
    pl = event.get("place")
    if isinstance(pl, dict) and pl:
        return pl
    loc = event.get("location")
    if isinstance(loc, dict) and (
        clean_str(loc.get("name"))
        or clean_str(loc.get("venueName"))
        or clean_str(loc.get("city"))
    ):
        return loc
    return {}


def _title_hint_from_ticketmaster_url(url: str | None) -> str | None:
    """Use URL path slug as a readable title when nextData names are missing."""
    if not url:
        return None
    try:
        path = urlparse(str(url).strip()).path
        segs = [s for s in path.split("/") if s]
        try:
            ei = next(i for i, s in enumerate(segs) if s.lower() == "event")
        except StopIteration:
            return None
        if ei < 1:
            return None
        slug = segs[ei - 1]
        if slug:
            return str(slug).replace("-", " ").strip().title()
    except Exception:
        pass
    return None


def _infer_city_state_from_ticketmaster_url(url: str | None) -> tuple[str | None, str | None]:
    """Parse .../{slug}/event/{id} slug tail `-city-state` for geocoding when JSON omits venue."""
    if not url:
        return None, None
    try:
        path = urlparse(str(url).strip()).path
        segs = [s for s in path.split("/") if s]
        try:
            ei = next(i for i, s in enumerate(segs) if s.lower() == "event")
        except StopIteration:
            return None, None
        if ei < 1:
            return None, None
        slug = segs[ei - 1].lower()
    except Exception:
        return None, None
    parts = [p for p in slug.split("-") if p]
    if len(parts) < 2:
        return None, None
    st_key = parts[-1]
    st_abbr = _US_STATE_NAME_TO_ABBR.get(st_key) or _TM_SLUG_STATE_TYPOS.get(st_key)
    if not st_abbr and len(st_key) == 2:
        st_abbr = st_key.upper()
    if not st_abbr:
        return None, None
    ci = -2
    if parts[ci] == "v" and len(parts) >= 3:
        ci = -3
    if abs(ci) > len(parts):
        return None, st_abbr
    city_raw = parts[ci]
    if city_raw.isdigit():
        return None, st_abbr
    city = city_raw.replace("-", " ").strip()
    if not city:
        return None, st_abbr
    return city.title(), st_abbr


def _is_tm_event_blob(d):
    if not isinstance(d, dict):
        return False
    cu = d.get("canonicalURL")
    if isinstance(cu, str) and "ticketmaster.com" in cu.lower():
        return True
    eid = d.get("id")
    if eid is not None and _TM_EVENT_ID_HEX_RE.match(str(eid).strip()):
        if clean_str(d.get("name")) or clean_str(d.get("title")):
            return True
    if d.get("id") and d.get("name") is not None and isinstance(d.get("venue"), dict):
        return True
    return False


def _deep_find_tm_event(obj, page_url, max_depth=40):
    candidates = []

    def walk(o, depth):
        if depth > max_depth: return
        if isinstance(o, dict):
            if _is_tm_event_blob(o): candidates.append(o)
            for vv in o.values(): walk(vv, depth + 1)
        elif isinstance(o, list):
            for it in o: walk(it, depth + 1)

    walk(obj, 0)
    if not candidates: return None
    norm_page = page_url.rstrip("/")
    tail = norm_page.split("/")[-1].upper()
    for c in candidates:
        cu = (c.get("canonicalURL") or "").rstrip("/")
        if cu and norm_page in cu.replace("http://", "https://"): return c
        if cu and cu.replace("http://", "https://") in norm_page:  return c
    for c in candidates:
        cid = str(c.get("id", "")).upper()
        if cid and cid == tail: return c
    return candidates[0]


def _venue_location_from_next(event_json):
    venue_fb = extract_eventinfo_venue(event_json)
    if not isinstance(venue_fb, dict):
        return None, None, None, None

    address_obj = venue_fb.get("address") if isinstance(venue_fb.get("address"), dict) else {}
    fb_name = _pick_first_str(venue_fb.get("name"), venue_fb.get("venueName"))

    street = _pick_first_str(
        venue_fb.get("streetAddress"), venue_fb.get("line1"),
        address_obj.get("streetAddress"), address_obj.get("line1"),
    )
    city = _pick_first_str(venue_fb.get("city"), address_obj.get("addressLocality"), address_obj.get("city"))
    state = _pick_first_str(venue_fb.get("state"), address_obj.get("addressRegion"), address_obj.get("state"))
    postal = _pick_first_str(venue_fb.get("postalCode"), address_obj.get("postalCode"), venue_fb.get("zip"))
    country = _pick_first_str(venue_fb.get("country"), address_obj.get("addressCountry"))
    venue_id = _pick_first_str(venue_fb.get("id"))

    parts = [street, city, state, postal, country, venue_id]
    fb_addr = ", ".join([p for p in parts if p]).strip(" ,")
    if not fb_addr:
        fb_addr = _pick_first_str(venue_fb.get("addressText"), venue_fb.get("fullAddress"))

    geo = venue_fb.get("geo")
    geo_d = geo if isinstance(geo, dict) else {}
    location_d = venue_fb.get("location") if isinstance(venue_fb.get("location"), dict) else {}
    coords_d = location_d.get("coordinates") if isinstance(location_d.get("coordinates"), dict) else {}
    lat = safe_float(
        _pick_first_str(
            venue_fb.get("latitude"),
            venue_fb.get("lat"),
            geo_d.get("latitude"),
            geo_d.get("lat"),
            location_d.get("latitude"),
            location_d.get("lat"),
            coords_d.get("latitude"),
            coords_d.get("lat"),
        )
    )
    lon = safe_float(
        _pick_first_str(
            venue_fb.get("longitude"),
            venue_fb.get("lng"),
            venue_fb.get("lon"),
            geo_d.get("longitude"),
            geo_d.get("lng"),
            geo_d.get("lon"),
            location_d.get("longitude"),
            location_d.get("lng"),
            location_d.get("lon"),
            coords_d.get("longitude"),
            coords_d.get("lng"),
            coords_d.get("lon"),
        )
    )
    return fb_name, fb_addr, lat, lon


# ======================================================================
#  SCHEDULE HELPERS
# ======================================================================

def _schedule_has_time(s: str | None) -> bool:
    if not s:
        return False
    s = str(s).strip()
    if len(s) > 10 and s[10] == " ":
        return True
    return "T" in s


def _schedule_date_only(s: str | None) -> bool:
    if not s:
        return False
    return bool(_DATE_ONLY_SCHEDULE_RE.match(str(s).strip()))


def _zoneinfo_or_utc(tz_name: str | None):
    if not tz_name or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(str(tz_name).strip())
    except Exception:
        return timezone.utc


def _fmt_utc_z(dt: datetime) -> str:
    u = dt.astimezone(timezone.utc)
    return u.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _iso_schedule_to_utc_z(
        raw: str,
        default_tz_name: str | None,
        *,
        end_of_day: bool,
) -> str | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if len(s) > 10 and s[10] == " ":
        s = s[:10] + "T" + s[11:]

    zi = _zoneinfo_or_utc(default_tz_name)

    if _schedule_date_only(s):
        d = datetime.strptime(s[:10], "%Y-%m-%d").date()
        t = dt_time(23, 59, 59) if end_of_day else dt_time.min
        dt = datetime.combine(d, t, tzinfo=zi)
        return _fmt_utc_z(dt)

    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zi)
    return _fmt_utc_z(dt)


def _iso_schedule_to_offset_iso(
        raw: str,
        default_tz_name: str | None,
        *,
        end_of_day: bool,
) -> str | None:
    """
    Local wall time as ISO-8601 with numeric offset (no Z), for timed events.
    Not used for calendar-only YYYY-MM-DD (those stay unconverted upstream).
    """
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if len(s) > 10 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    if _schedule_date_only(s):
        return None
    zi = _zoneinfo_or_utc(default_tz_name)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zi)
    if end_of_day:
        d = dt.date()
        dt = datetime.combine(d, dt_time(23, 59, 59), tzinfo=dt.tzinfo)
    return dt.isoformat(timespec="seconds")


def _event_schedule_apply_utc_values(schedule: dict, tz_name: str | None) -> dict:
    """
    Normalize schedule values: keep calendar-only dates as YYYY-MM-DD (no UTC shift);
    express timed instants with timezone offset instead of UTC Z.
    """
    if not schedule or schedule.get("kind") == "tbd":
        return schedule

    kind = schedule.get("kind")
    if kind == "point":
        st = schedule.get("start") or {}
        v = st.get("value")
        if not v:
            return schedule
        s = str(v).strip()
        if _schedule_date_only(s):
            return schedule
        if _schedule_has_time(s):
            oz = _iso_schedule_to_offset_iso(s, tz_name, end_of_day=False)
            if not oz:
                return schedule
            return {
                **schedule,
                "precision": "dateTime",
                "allDay": False,
                "start": {"type": "dateTime", "value": oz},
            }
        return schedule

    if kind != "span":
        return schedule

    st0 = schedule.get("start") or {}
    en0 = schedule.get("end") or {}
    sv, ev = st0.get("value"), en0.get("value")
    if not sv and not ev:
        return schedule

    s_do = _schedule_date_only(sv)
    e_do = _schedule_date_only(ev)
    s_tm = _schedule_has_time(sv) if sv else False
    e_tm = _schedule_has_time(ev) if ev else False

    if not s_tm and not e_tm and s_do and e_do:
        return schedule

    new_sv = None
    new_ev = None

    if s_tm:
        new_sv = _iso_schedule_to_offset_iso(sv, tz_name, end_of_day=False)

    if e_tm:
        new_ev = _iso_schedule_to_offset_iso(ev, tz_name, end_of_day=False)

    out = {**schedule}
    if new_sv:
        out["start"] = {"type": "dateTime", "value": new_sv}
    if new_ev:
        out["end"] = {"type": "dateTime", "value": new_ev}

    if new_sv or new_ev:
        had_time = s_tm or e_tm
        out["precision"] = "dateTime"
        if had_time:
            out["allDay"] = False
        elif s_do and e_do and tz_name:
            out["allDay"] = True

    return out


# ======================================================================
#  SPIDER
# ======================================================================

class IncrementalJsonPipeline:

    def open_spider(self, spider):
        self._json_lock = threading.Lock()
        self._new_this_run = 0
        out_path = Path(OUTPUT_PATH)
        overwrite_mode = bool(getattr(spider, "_overwrite_output_json", False))
        existing = [] if overwrite_mode else _load_existing_records_from_output(out_path)
        # In overwrite mode we always start fresh from 0.
        self._scraped_count = 0 if overwrite_mode else len(existing)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _pipeline_log.error("Cannot create output directory %s: %s", out_path.parent, exc)
            self._file = None
            return
        try:
            # overwrite=True -> fresh file each run; otherwise append/update existing shell.
            file_mode = "w+" if overwrite_mode else "a+"
            self._file = open(out_path, file_mode, encoding="utf-8", newline="\n")
        except OSError as exc:
            _pipeline_log.error("Cannot open JSON output %s: %s", out_path, exc)
            self._file = None
            return
        self._total_so_far = 0 if overwrite_mode else len(existing)
        # Ensure file has a valid JSON array shell.
        try:
            self._file.seek(0, os.SEEK_END)
            if self._file.tell() == 0:
                self._file.write("[]")
                self._file.flush()
        except OSError as exc:
            _pipeline_log.error("Cannot initialize JSON output %s: %s", out_path, exc)
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _serialize_block(self, record: dict) -> str | None:
        clean = _strip_nulls(_sanitize_for_json(record))
        try:
            serialized = json.dumps(
                clean,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            _pipeline_log.error("JSON encode failed after sanitize: %s", exc)
            return None
        indented = "\n".join("  " + line for line in serialized.splitlines())
        try:
            indented.encode("utf-8")
        except UnicodeEncodeError:
            serialized = json.dumps(
                _strip_nulls(_sanitize_for_json(record)),
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
            )
            indented = "\n".join("  " + line for line in serialized.splitlines())
        return indented

    def process_item(self, item, spider):
        record = dict(item)
        page_url = _record_source_url(record)
        if page_url and page_url in getattr(spider, "_done_urls", set()):
            return item
        block = self._serialize_block(record)
        if block is None:
            return item
        if self._file is None:
            return item
        lock = getattr(self, "_json_lock", None)
        if lock is None:
            return item
        try:
            with lock:
                self._file.seek(0, os.SEEK_END)
                end = self._file.tell()
                if end == 0:
                    self._file.write("[]")
                    self._file.flush()
                    end = 2
                pos = end - 1
                last = ""
                while pos >= 0:
                    self._file.seek(pos)
                    last = self._file.read(1)
                    if last and last.isspace():
                        pos -= 1
                        continue
                    break
                if last != "]":
                    _pipeline_log.error("JSON output malformed; cannot append safely: %s", OUTPUT_PATH)
                    return item
                self._file.seek(pos)
                if self._total_so_far > 0:
                    piece = ",\n" + block + "\n]"
                else:
                    piece = "\n" + block + "\n]"
                self._file.write(piece)
                self._file.truncate()
                self._total_so_far += 1
                self._new_this_run += 1
                self._file.flush()
        except OSError as exc:
            _pipeline_log.error("Write failed for %s: %s", OUTPUT_PATH, exc)
            return item
        if page_url:
            spider._done_urls.add(page_url)
        self._scraped_count += 1
        target = int(getattr(spider, "_new_url_target", 0) or 0)
        done_new = self._new_this_run
        suffix = ""
        if target > 0:
            remaining = max(0, target - done_new)
            t0 = getattr(spider, "_resume_monotonic_t0", None)
            eta_txt = "—"
            if t0 is not None and done_new >= 2 and remaining > 0:
                elapsed = time.monotonic() - t0
                sec_per = elapsed / done_new
                eta_txt = _format_eta_seconds(remaining * sec_per)
            elif t0 is not None and done_new >= 1 and remaining > 0:
                eta_txt = "calculating…"
            suffix = f" | new {done_new}/{target} Remaining {remaining} | ETA {eta_txt}"
        print(f"{self._scraped_count} event scraped ✅{suffix}")
        return item

    def close_spider(self, spider):
        fp = getattr(self, "_file", None)
        if fp is not None:
            try:
                fp.close()
            except OSError:
                pass
        st = getattr(spider, "_csv_resume_stats", None)
        if st:
            skipped = st.get("skipped_repeat_csv", 0)
            sched = int(st.get("scheduled_new", 0) or 0)
            saved = int(getattr(self, "_new_this_run", 0) or 0)
            pending = max(0, sched - saved)
            print(
                "\n=== Ticketmaster resume summary ===\n"
                f"Output JSON: {st.get('output_path', OUTPUT_PATH)}\n"
                f"Total CSV rows with Ticketmaster URL: {st.get('csv_rows_with_tm_url', 0)}\n"
                f"Unique recordSource URLs in JSON before run (info): {st.get('json_unique_urls', 0)}\n"
                f"Skipped duplicate CSV rows (same URL twice): {skipped}\n"
                f"Skipped (past date in URL slug, no fetch): {st.get('skipped_slug_past', 0)}\n"
                f"Skipped (date/time TBA in saved JSON, no fetch): {st.get('skipped_tba_json', 0)}\n"
                f"Skipped (past event date in saved JSON, no fetch): {st.get('skipped_finished_json', 0)}\n"
                f"Skipped (complete record in JSON, no fetch): {st.get('skipped_complete_json', 0)}\n"
                f"Skipped completed events (event date < now, __NEXT_DATA__): {getattr(spider, 'skipped_finished_event_count', 0)}\n"
                f"Skipped date/time TBA after fetch: {getattr(spider, 'skipped_tba_event_count', 0)}\n"
                f"Scheduled requests: {st.get('scheduled_new', 0)}\n"
                f"Newly scraped & saved this run: {saved}\n"
                f"New URLs not saved this run (fail/timeout/no-item): {pending}\n"
                f"Failed requests (errback): {getattr(spider, 'failed_count', 0)}\n"
                "=====================================\n"
            )


class TicketmasterDataSpider(scrapy.Spider):
    name = "ticketmaster_data"
    REQUEST_HEADERS = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://www.ticketmaster.com/search",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "x-tmclient-app": "marketplace_fe",
        "x-tmlangcode": "en-us",
        "x-tmplatform": "global",
        "x-tmregion": "200",
    }
    custom_settings = {
        "CONCURRENT_REQUESTS": 2,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "DOWNLOAD_DELAY": 3,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        # Ticketmaster + optional browser rendering need more than a few seconds.
        "DOWNLOAD_TIMEOUT": 60,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.5,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 3.0,
        "AUTOTHROTTLE_DEBUG": False,
        # Spider handles HTTP/network retries with backoff (see _schedule_tm_retry).
        "RETRY_ENABLED": False,
        "REDIRECT_ENABLED": True,
        "REDIRECT_MAX_TIMES": 10,
        # Scrapy 2.x: HttpError is raised by *spider* middleware for non-200 unless allowed here.
        "HTTPERROR_ALLOW_ALL": True,
        "DEFAULT_REQUEST_HEADERS": REQUEST_HEADERS,
        "COOKIES_ENABLED": False,
        "LOG_LEVEL": "INFO",
        "DOWNLOADER_MIDDLEWARES": {},
        "ITEM_PIPELINES": {
            f"{__name__}.IncrementalJsonPipeline": 300,
        },
        "EXTENSIONS": {
            "scrapy.extensions.logstats.LogStats": None,
            "scrapy.extensions.telnet.TelnetConsole": None,
        },
        "LOG_LEVEL": "ERROR",
    }
    if HAS_ZYTE_API:
        custom_settings["DOWNLOAD_HANDLERS"] = {
            "http": "scrapy_zyte_api.ScrapyZyteAPIDownloadHandler",
            "https": "scrapy_zyte_api.ScrapyZyteAPIDownloadHandler",
        }
        custom_settings["DOWNLOADER_MIDDLEWARES"].update({  # 👈 .update() not overwrite
            "scrapy_zyte_api.ScrapyZyteAPIDownloaderMiddleware": 1000,
        })
        custom_settings["REQUEST_FINGERPRINTER_CLASS"] = "scrapy_zyte_api.ScrapyZyteAPIRequestFingerprinter"
        custom_settings["ZYTE_API_KEY"] = ZYTE_API_KEY_ENV
        custom_settings["ZYTE_API_BROWSER_HTML"] = True
        custom_settings["DOWNLOAD_TIMEOUT"] = 180

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        for _name in ("scrapy_zyte_api", "scrapy_zyte_api._params"):
            logging.getLogger(_name).setLevel(logging.ERROR)
        return spider

    @classmethod
    def _get_dma_zip_lookup(cls):
        if hasattr(cls, "_zip_cache"):
            return cls._zip_cache

        path = _resolve_zip_market_csv_path()
        if path is None or not path.is_file():
            cls._zip_cache = {}
            return cls._zip_cache

        cls._zip_cache = _load_dma_csv(path)
        return cls._zip_cache

    @classmethod
    def _compute_near_by(cls, location_name: str, address: str):
        """
        Returns:
          True  – ZIP found, workbook row matches a target DMA + state
          False – ZIP found but no matching row or DMA/state mismatch
          None  – no address text provided
        """
        addr = clean_str(address)
        if not addr:
            return None

        lookup = cls._get_dma_zip_lookup()
        zip_code = _extract_us_zip_from_address(addr)
        if not zip_code:
            # No ZIP in address: use workbook market + state match.
            # Rule: market + state in address -> True, market only -> False.
            a_fold = " ".join(addr.lower().split())
            city_match = re.search(r",\s*([^,]+)\s*,\s*([A-Za-z]{2}|[A-Za-z][A-Za-z .'-]+)\s*$", addr)
            addr_city = ""
            addr_state_abbr = ""
            if city_match:
                addr_city = " ".join(city_match.group(1).lower().split())
                addr_state_abbr = _normalize_state_to_abbr(city_match.group(2))
            abbr_to_names: dict[str, list[str]] = {}
            for full_name, abbr in _US_STATE_NAME_TO_ABBR.items():
                abbr_to_names.setdefault(abbr, []).append(full_name)

            for market_name, st_val in lookup.values():
                market = " ".join(str(market_name or "").lower().split())
                if not market:
                    continue

                st_abbr = _normalize_state_to_abbr(st_val)
                if len(st_abbr) != 2:
                    continue
                state_in_address = re.search(rf"\b{re.escape(st_abbr.lower())}\b", a_fold) is not None
                if not state_in_address:
                    for full_name in abbr_to_names.get(st_abbr, []):
                        if re.search(rf"\b{re.escape(full_name)}\b", a_fold):
                            state_in_address = True
                            break
                if not state_in_address:
                    continue

                if re.search(rf"\b{re.escape(market)}\b", a_fold):
                    return True

                # Fallback for no-ZIP addresses like "ICON Park, Orlando, FL":
                # if city+state from address align with market+state from workbook, treat as nearBy.
                if addr_city and addr_state_abbr == st_abbr:
                    if addr_city == market or addr_city in market or market in addr_city:
                        return True

            return False

        zip_key = _normalize_zip_cell(str(zip_code).split(".")[0].strip()) or str(zip_code).strip()
        row = lookup.get(zip_key)
        if not row:
            return True if _address_matches_city_list(addr) else False

        dma_name, st_abv = row[0], row[1]
        if _dma_matches_target_market(dma_name, st_abv):
            return True
        return True if _address_matches_city_list(addr) else False

    def __init__(self, limit=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.limit = int(limit) if limit not in (None, "") else None
        self._overwrite_output_json = OVERWRITE_OUTPUT_JSON

        self.items_scraped = 0
        self.items_skipped = 0
        self.next_data_count = 0
        self.fallback_jsonld_count = 0
        self.fallback_meta_count = 0
        self.missing_count = 0
        self.failed_count = 0
        self.skipped_finished_event_count = 0
        self.skipped_tba_event_count = 0

        self._done_urls = set()
        self._venue_coord_cache = {}

        if not HAS_ZYTE_API:
            self.logger.warning(
                "scrapy_zyte_api not installed; running with default Scrapy downloader."
            )

    def _venue_coord_keys(self, location_name, location_address):
        keys = []
        nm = clean_str(location_name)
        addr = clean_str(location_address)
        if nm:
            keys.append(f"name::{nm.lower()}")
        if addr:
            keys.append(f"addr::{addr.lower()}")
        return keys

    def _fill_or_store_venue_coords(self, location_name, location_address, latitude, longitude):
        keys = self._venue_coord_keys(location_name, location_address)
        lat, lon = latitude, longitude

        if lat is None or lon is None:
            for key in keys:
                cached = self._venue_coord_cache.get(key)
                if not cached:
                    continue
                c_lat, c_lon = cached
                if lat is None:
                    lat = c_lat
                if lon is None:
                    lon = c_lon
                if lat is not None and lon is not None:
                    break

        nm = clean_str(location_name) or ""
        if (lat is None or lon is None) and "online" not in nm.lower():
            for q in _geocode_query_candidates(location_name, location_address):
                glat, glon = _geocode_address_nominatim(q)
                if lat is None:
                    lat = glat
                if lon is None:
                    lon = glon
                if lat is not None and lon is not None:
                    break

        if lat is not None and lon is not None:
            for key in keys:
                self._venue_coord_cache[key] = (lat, lon)
        return lat, lon

    # Same-URL retries with backoff until HTTP 200 + usable data.
    # None = no cap (keep retrying). Set to a positive int to stop after that many
    # failed attempts and log a single error (see _schedule_tm_retry).
    MAX_TM_HTTP_RETRIES: int | None = None
    _TM_RETRY_BASE_SEC = 2.25
    _TM_RETRY_MULT = 1.55
    _TM_RETRY_CAP_SEC = 120.0

    def _tm_retry_delay_sec(self, failures_so_far: int) -> float:
        base = min(
            self._TM_RETRY_BASE_SEC * (self._TM_RETRY_MULT ** failures_so_far),
            self._TM_RETRY_CAP_SEC,
        )
        return max(0.5, base * random.uniform(0.82, 1.18))

    @defer.inlineCallbacks
    def _schedule_tm_retry(
        self,
        page_url: str,
        url: str,
        failures_so_far: int,
        *,
        reason: str,
        status: int | None,
    ):
        """Wait (reactor), then re-queue the same URL. Retries use DEBUG only; ERROR once if capped out."""
        cap = self.MAX_TM_HTTP_RETRIES
        if cap is not None and failures_so_far >= cap:
            self.logger.error(
                "Ticketmaster give up after %d fetch attempts (%s): reason=%s last_http=%s",
                failures_so_far + 1,
                page_url,
                reason,
                status,
            )
            self.failed_count += 1
            self.missing_count += 1
            defer.returnValue(None)
        delay = self._tm_retry_delay_sec(failures_so_far)
        cap_label = str(cap) if cap is not None else "unlimited"
        self.logger.debug(
            "ticketmaster_data retry %s/%s in %.1fs (%s) http=%s url=%s",
            failures_so_far + 1,
            cap_label,
            delay,
            reason,
            status,
            page_url,
        )
        # Local import: importing reactor at module load pulls iocpreactor from the venv;
        # on Windows + OneDrive “online-only” packages that can raise OSError(22) and block Scrapy startup.
        from twisted.internet import reactor as twisted_reactor

        yield deferLater(twisted_reactor, delay, lambda: None)
        meta = self._request_meta(page_url)
        meta["tm_http_retries"] = failures_so_far + 1
        req = scrapy.Request(
            url=url,
            callback=self.parse,
            errback=self.errback,
            meta=meta,
            dont_filter=True,
            headers=self.REQUEST_HEADERS,
        )
        defer.returnValue(req)

    def start_requests(self):
        rows = []
        overwrite_mode = bool(getattr(self, "_overwrite_output_json", False))
        out_p = Path(OUTPUT_PATH)
        existing_records = [] if overwrite_mode else _load_existing_records_from_output(out_p)
        json_unique_urls = (
            0
            if overwrite_mode
            else len(
                {
                    u
                    for u in (_record_source_url(r) for r in existing_records)
                    if u
                }
            )
        )
        # Always schedule every Ticketmaster URL from the CSV. Do not seed _done_urls
        # from prior JSON or the pipeline would drop writes for URLs already on disk.
        self._done_urls.clear()
        try:
            csv_path = _resolve_all_urls_csv_path()
            if csv_path is None:
                self.logger.error(f"CSV not found: {CSV_PATH}")
                return
            with open(csv_path, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if not row: continue
                    first = str(row[0]).strip().lower().lstrip("\ufeff")
                    if first.startswith("scrapy "): continue
                    rows.append(row)
        except FileNotFoundError:
            self.logger.error(f"CSV not found: {CSV_PATH}")
            return

        if not rows:
            self.logger.warning(f"No usable rows in {CSV_PATH}")
            return

        first_row = rows[0]
        first_row_lc = [
            str(c).strip().lstrip("\ufeff").lower() for c in first_row
        ]
        has_header = (
            "url" in first_row_lc
            or "title" in first_row_lc
            or (
                "city" in first_row_lc
                and "state" in first_row_lc
                and "url" in first_row_lc
            )
        )

        url_col_idx = None
        if has_header:
            url_col_idx = next(
                (i for i, c in enumerate(first_row_lc) if c == "url"), None
            )

        def extract_url(r):
            if has_header:
                if url_col_idx is not None and url_col_idx < len(r):
                    v = str(r[url_col_idx]).strip()
                    if v.startswith("https://www.ticketmaster.com/"):
                        return v
                for cell in r:
                    v = str(cell).strip()
                    if v.startswith("https://www.ticketmaster.com/"):
                        return v
                return ""
            for cell in r:
                v = str(cell).strip()
                if v.startswith("https://www.ticketmaster.com/"):
                    return v
            return ""

        data_rows = rows[1:] if has_header else rows
        csv_seen_new = set()
        finished_urls = {
            _normalize_tm_event_url(_record_source_url(r))
            for r in existing_records
            if _saved_record_event_is_finished(r)
        }
        complete_urls = {
            _normalize_tm_event_url(_record_source_url(r))
            for r in existing_records
            if _ticketmaster_record_is_complete(r)
        }
        tba_urls = {
            _normalize_tm_event_url(_record_source_url(r))
            for r in existing_records
            if _saved_record_is_datetime_tba(r)
        }
        stats = {
            "output_path": OUTPUT_PATH,
            "json_unique_urls": json_unique_urls,
            "csv_rows_with_tm_url": 0,
            "skipped_repeat_csv": 0,
            "skipped_slug_past": 0,
            "skipped_tba_json": 0,
            "skipped_finished_json": 0,
            "skipped_complete_json": 0,
            "scheduled_new": 0,
        }
        new_urls: list[str] = []
        for r in data_rows:
            if self.limit is not None and len(new_urls) >= self.limit:
                break

            page_url = extract_url(r)
            if not page_url:
                continue
            stats["csv_rows_with_tm_url"] += 1
            if not is_allowed_url(page_url):
                self.items_skipped += 1
                continue

            if _url_slug_event_date_is_past(page_url):
                stats["skipped_slug_past"] += 1
                continue

            if page_url in csv_seen_new:
                stats["skipped_repeat_csv"] += 1
                continue

            nu = _normalize_tm_event_url(page_url)
            if nu in tba_urls:
                stats["skipped_tba_json"] += 1
                continue
            if nu in finished_urls:
                stats["skipped_finished_json"] += 1
                continue
            if nu in complete_urls:
                stats["skipped_complete_json"] += 1
                continue

            csv_seen_new.add(page_url)
            new_urls.append(page_url)

        # Fast-first ordering: easy canonical URLs first, slower ones later.
        new_urls.sort(key=_url_speed_priority)
        stats["scheduled_new"] = len(new_urls)
        self._csv_resume_stats = stats
        self._new_url_target = len(new_urls)
        self._resume_monotonic_t0 = time.monotonic()
        print(
            "\n=== Ticketmaster resume (start) ===\n"
            f"Output JSON: {OUTPUT_PATH}\n"
            f"Records loaded from JSON: {len(existing_records)}\n"
            f"Total URLs in CSV: {stats['csv_rows_with_tm_url']}\n"
            f"Unique recordSource URLs already in JSON (info only): {stats['json_unique_urls']}\n"
            f"Skipped (past date in URL slug -MM-DD-YYYY, no fetch): {stats['skipped_slug_past']}\n"
            f"Skipped (date/time TBA in saved JSON, no fetch): {stats['skipped_tba_json']}\n"
            f"Skipped (past event date in saved JSON, no fetch): {stats['skipped_finished_json']}\n"
            f"Skipped (already complete in JSON, no fetch): {stats['skipped_complete_json']}\n"
            f"Unique URLs scheduled this run: {stats['scheduled_new']}\n"
            f"Skipped (duplicate row in CSV, same URL): {stats['skipped_repeat_csv']}\n"
            f"Scheduled requests: {stats['scheduled_new']}\n"
            "ETA line prints after 2 successful new saves (avg speed).\n"
            "===================================\n"
        )

        for page_url in new_urls:
            yield scrapy.Request(
                url=page_url,
                callback=self.parse,
                errback=self.errback,
                meta=self._request_meta(page_url),
                headers=self.REQUEST_HEADERS,
            )

    def _request_meta(self, page_url):
        # Retries are handled in the spider (backoff + cap); disable downloader RetryMiddleware.
        meta = {
            "page_url": page_url,
            "dont_retry": True,
            "dont_redirect": False,
            "tm_http_retries": 0,
        }
        if HAS_ZYTE_API:
            meta["zyte_api_automap"] = {"browserHtml": True}
        return meta

    def _response_text(self, response) -> str:
        try:
            t = response.text
            if isinstance(t, str):
                return t
        except Exception:
            pass
        body = getattr(response, "body", b"") or b""
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return str(body)

    def _log_scraped(self, page_url, label=None):
        self.items_scraped += 1

    def parse(self, response):
        page_url = response.meta["page_url"]

        if response.status in (301, 302, 303, 307, 308):
            loc = response.headers.get("Location") or response.headers.get("location")
            if loc:
                redirect_url = response.urljoin(loc.decode("utf-8", errors="replace"))
                if not is_allowed_url(redirect_url):
                    self.items_skipped += 1
                    return
                meta = self._request_meta(redirect_url)
                meta["tm_http_retries"] = int(response.meta.get("tm_http_retries", 0) or 0)
                yield scrapy.Request(
                    url=redirect_url,
                    callback=self.parse,
                    errback=self.errback,
                    meta=meta,
                    dont_filter=True,
                    headers=self.REQUEST_HEADERS,
                )
                return

        if response.status != 200:
            fails = int(response.meta.get("tm_http_retries", 0) or 0)
            return self._schedule_tm_retry(
                page_url,
                response.url,
                fails,
                reason="http_non_200",
                status=response.status,
            )

        body_text = self._response_text(response)
        if not body_text.strip():
            fails = int(response.meta.get("tm_http_retries", 0) or 0)
            return self._schedule_tm_retry(
                page_url,
                response.url,
                fails,
                reason="empty_body",
                status=response.status,
            )

        try:
            tree = lxml_html.fromstring(body_text)
        except Exception:
            fails = int(response.meta.get("tm_http_retries", 0) or 0)
            return self._schedule_tm_retry(
                page_url,
                response.url,
                fails,
                reason="html_parse_error",
                status=response.status,
            )

        raw_next = "".join(tree.xpath('//script[@id="__NEXT_DATA__"]/text()')).strip()

        if raw_next:
            for _out in self._parse_next_data(
                raw_next, page_url, tree=tree, body_text=body_text, response=response
            ):
                yield _out
            return

        for _out in self._parse_html_fallback(
            response, tree, page_url, body_text=body_text
        ):
            yield _out

    def _parse_next_data(self, raw, page_url, tree=None, body_text=None, response=None):
        try:
            event_json = json.loads(raw)
        except json.JSONDecodeError:
            if response is not None:
                fails = int(response.meta.get("tm_http_retries", 0) or 0)
                yield self._schedule_tm_retry(
                    page_url,
                    response.url,
                    fails,
                    reason="next_data_json_error",
                    status=getattr(response, "status", None),
                )
            else:
                self.missing_count += 1
            return

        try:
            ctx = event_json["props"]["pageProps"]["edpData"]["context"]
            event = ctx["event"]
            esj = ctx.get("eventschemaJSONLD")
            if isinstance(esj, str) and esj.strip():
                schema = json.loads(esj)
            elif isinstance(esj, dict):
                schema = esj
            else:
                schema = {}
        except (KeyError, TypeError, json.JSONDecodeError):
            schema_embed = _deep_find_eventschema_jsonld(event_json)
            event_embed = _deep_find_tm_event(event_json, page_url)
            if schema_embed and event_embed:
                if tm_site_event_is_datetime_tba(
                    event_embed, schema_embed, event_json, page_url=page_url
                ):
                    self.skipped_tba_event_count += 1
                    self.logger.info(
                        "Skipped date/time TBA (__NEXT_DATA__): %s",
                        page_url,
                    )
                    return
                if tm_site_event_is_finished(
                    event_embed, schema_embed, event_json, page_url=page_url
                ):
                    self.skipped_finished_event_count += 1
                    self.logger.info(
                        "Skipped completed event (event date < now, __NEXT_DATA__): %s",
                        page_url,
                    )
                    return
                self.next_data_count += 1
                yield from self._build_from_edp_event_schema(
                    event_embed,
                    schema_embed,
                    event_json,
                    page_url,
                    html_tree=tree,
                    html_body=body_text,
                )
                return
            if schema_embed:
                if tm_site_event_is_datetime_tba(
                    {}, schema_embed, event_json, page_url=page_url
                ):
                    self.skipped_tba_event_count += 1
                    self.logger.info(
                        "Skipped date/time TBA (__NEXT_DATA__): %s",
                        page_url,
                    )
                    return
                if tm_site_event_is_finished({}, schema_embed, event_json, page_url=page_url):
                    self.skipped_finished_event_count += 1
                    self.logger.info(
                        "Skipped completed event (event date < now, __NEXT_DATA__): %s",
                        page_url,
                    )
                    return
                self.next_data_count += 1
                yield from self._from_schema_org_event(
                    schema_embed,
                    page_url,
                    source_label="next_embed_schema",
                    event_json_fallback=event_json,
                    html_tree=tree,
                    html_body=body_text,
                )
                return
            if event_embed:
                if tm_site_event_is_datetime_tba(
                    event_embed, {}, event_json, page_url=page_url
                ):
                    self.skipped_tba_event_count += 1
                    self.logger.info(
                        "Skipped date/time TBA (__NEXT_DATA__): %s",
                        page_url,
                    )
                    return
                if tm_site_event_is_finished(event_embed, {}, event_json, page_url=page_url):
                    self.skipped_finished_event_count += 1
                    self.logger.info(
                        "Skipped completed event (event date < now, __NEXT_DATA__): %s",
                        page_url,
                    )
                    return
                self.next_data_count += 1
                yield from self._build_from_edp_event_schema(
                    event_embed,
                    {},
                    event_json,
                    page_url,
                    html_tree=tree,
                    html_body=body_text,
                )
                return
            if response is not None:
                fails = int(response.meta.get("tm_http_retries", 0) or 0)
                yield self._schedule_tm_retry(
                    page_url,
                    response.url,
                    fails,
                    reason="next_data_no_event",
                    status=getattr(response, "status", None),
                )
            else:
                self.missing_count += 1
            return

        if tm_site_event_is_datetime_tba(event, schema, event_json, page_url=page_url):
            self.skipped_tba_event_count += 1
            self.logger.info(
                "Skipped date/time TBA (__NEXT_DATA__): %s",
                page_url,
            )
            return
        if tm_site_event_is_finished(event, schema, event_json, page_url=page_url):
            self.skipped_finished_event_count += 1
            self.logger.info(
                "Skipped completed event (event date < now, __NEXT_DATA__): %s",
                page_url,
            )
            return
        self.next_data_count += 1
        yield from self._build_from_edp_event_schema(
            event,
            schema,
            event_json,
            page_url,
            html_tree=tree,
            html_body=body_text,
        )

    def _build_from_edp_event_schema(
        self, event, schema, event_json, page_url, html_tree=None, html_body=None
    ):
        event = event or {}
        schema = schema or {}
        schema = _merge_schema_geo_from_html(schema, html_tree)

        record_id = event.get("id")
        if record_id is None and page_url:
            record_id = page_url.rstrip("/").split("/")[-1] or None
        record_url = event.get("canonicalURL") or page_url
        group_id = triple_id("group", "ticketmaster", str(record_id or ""), str(record_url or ""))
        e_id = triple_id("id", "ticketmaster", str(record_id or ""), str(record_url or ""))
        title = title_from_frontend_metadata(event, schema, event_json, html_tree=html_tree)
        if not clean_str(title):
            title = _title_hint_from_ticketmaster_url(page_url) or "Ticketmaster event"
        description = description_from_next_data(event, schema, event_json)

        venue = _coalesce_venue_dict(event)
        venue_name = (
            _pick_first_str(
                venue.get("name"),
                venue.get("venueName"),
                event.get("venueName"),
                event.get("venueTitle"),
            )
            or ""
        )
        city = _pick_first_str(
            venue.get("city"),
            event.get("venueCity"),
            event.get("cityName"),
            event.get("city"),
        )
        state = _pick_first_str(
            venue.get("state"),
            event.get("venueState"),
            event.get("stateCode"),
            event.get("state"),
        )
        vloc = venue.get("location") if isinstance(venue.get("location"), dict) else {}
        vaddr = vloc.get("address") if isinstance(vloc.get("address"), dict) else {}
        postal = _pick_first_str(
            vaddr.get("postalCode"),
            venue.get("postalCode"),
            venue.get("zip"),
            event.get("postalCode"),
            event.get("venuePostalCode"),
        )
        latitude, longitude = _lat_lon_from_venue_structure(venue)
        edp_venue = _edp_event_venue_from_next(event_json)
        if isinstance(edp_venue, dict):
            el, en = _lat_lon_from_venue_structure(edp_venue)
            if latitude is None:
                latitude = el
            if longitude is None:
                longitude = en

        location_name = (venue_name or "").strip()
        location_address = f"{venue_name}, {city or ''}, {state or ''} {postal or ''}".strip()

        if not clean_str(location_name) or not clean_str(location_address):
            try:
                fb_name, fb_addr, fb_lat, fb_lon = _venue_location_from_next(event_json)
                if fb_name and not clean_str(location_name):
                    location_name = fb_name
                if fb_addr and not clean_str(location_address):
                    location_address = fb_addr
                if latitude is None and fb_lat is not None:
                    latitude = fb_lat
                if longitude is None and fb_lon is not None:
                    longitude = fb_lon
            except Exception:
                pass

        if not clean_str(location_name) or not clean_str(location_address):
            try:
                d_venue, d_city, d_state, _ = parse_meta_description(description)
                if d_venue and not clean_str(location_name):
                    location_name = d_venue
                if not clean_str(location_address):
                    parts = [clean_str(d_venue), clean_str(d_city), clean_str(d_state)]
                    d_addr = ", ".join([p for p in parts if p]).strip(" ,")
                    if d_addr:
                        location_address = d_addr
            except Exception:
                pass

        if not clean_str(location_address) or not clean_str(location_name):
            u_city, u_st = _infer_city_state_from_ticketmaster_url(page_url)
            if u_city and u_st:
                hint = f"{u_city}, {u_st}"
                if not clean_str(location_name):
                    location_name = hint
                if not clean_str(location_address):
                    location_address = hint

        latitude, longitude = _merge_coords_from_html(
            latitude, longitude, html_tree, html_body
        )
        latitude, longitude = fill_missing_coordinates(
            location_address, latitude, longitude, schema=schema, event=event, next_data=event_json
        )
        latitude, longitude = self._fill_or_store_venue_coords(
            location_name, location_address, latitude, longitude
        )

        site_id = resolve_site_id(location_address)

        start_date, end_date = extract_tm_start_end(event, schema)

        ev_tz = extract_event_timezone_with_fallback(
            event, venue_state=clean_str(state) or None
        )

        event_schedule = infer_event_schedule(start_date, end_date, ev_tz)

        status = status_from_event_and_schema(event, schema, html_tree=html_tree)

        availability = "closed" if (
                event.get('eventOffsale') or event.get('isPastEvent') or event.get('isCanceled')
        ) else "onSale"

        offers = normalize_schema_offers(schema.get("offers"))
        min_price = offers.get("lowPrice")
        max_price = offers.get("highPrice")

        outlets = event.get('outlets', []) or []
        purchase_url = (
                           clean_str((outlets[0] or {}).get("url")) if outlets else None
                       ) or clean_str(offers.get("url")) or None

        event_roles = build_event_roles_from_ticketmaster(event, schema, title)

        currency = (
            event_json.get('props', {})
            .get('pageProps', {})
            .get('clientConfig', {})
            .get('currency', 'USD')
        )

        primary_artist = event.get('primaryArtist', {}) or {}
        image_title = primary_artist.get('name', '') or title
        images = collect_media_images(
            event, schema, clean_str(event.get("eventImageUrl")),
        )
        thumbnail_img = {}
        thumbnail_url = ""
        if images:
            def _thumb_key(x):
                w = x.get("width")
                return 9999 if not isinstance(w, (int, float)) else w

            thumbnail_img = min(images, key=_thumb_key)
            thumbnail_url = (
                    clean_str(thumbnail_img.get("location"))
                    or clean_str(thumbnail_img.get("url"))
                    or ""
            )
        if not thumbnail_url and images:
            thumbnail_url = (
                    clean_str(images[0].get("location"))
                    or clean_str(images[0].get("url"))
                    or ""
            )
        image_url = thumbnail_url or clean_str(event.get("eventImageUrl")) or ""

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._log_scraped(page_url)

        is_online = event_is_online(event, schema, event_json)

        yield self._build_record(
            group_id, e_id, ts, title, description, record_id, record_url,
            location_name, location_address, latitude, longitude,
            event_schedule, status, currency, min_price, max_price,
            availability, purchase_url, event_roles,
            image_title, thumbnail_url, thumbnail_img,
            image_url, images, site_id,
            is_online,
        )

    def _parse_html_fallback(self, response, tree, page_url, body_text=None):
        ev = first_event_from_json_ld_tree(tree)
        if ev:
            self.fallback_jsonld_count += 1
            yield from self._from_schema_org_event(
                ev,
                page_url,
                source_label="jsonld",
                event_json_fallback={},
                html_tree=tree,
                html_body=body_text or self._response_text(response),
            )
            return

        title = (response.xpath('//meta[@property="og:title"]/@content').get() or "").strip()
        if not title:
            title = "".join(tree.xpath("//title/text()")).strip()
        if title or response.xpath('//meta[@property="og:image"]/@content').get():
            self.fallback_meta_count += 1
            yield from self._from_meta_only(
                response,
                tree,
                page_url,
                html_body=body_text or self._response_text(response),
            )
            return

        fails = int(response.meta.get("tm_http_retries", 0) or 0)
        yield self._schedule_tm_retry(
            page_url,
            response.url,
            fails,
            reason="html_fallback_no_signals",
            status=response.status,
        )

    def _from_meta_only(self, response, tree, page_url, html_body=None):
        title = (response.xpath('//meta[@property="og:title"]/@content').get() or "").strip()
        if not title:
            title = "".join(tree.xpath("//title/text()")).strip() or "Unknown event"
        description = (response.xpath('//meta[@property="og:description"]/@content').get() or "").strip() or None
        image_url = (response.xpath('//meta[@property="og:image"]/@content').get() or "").strip()
        record_url = (response.xpath('//link[@rel="canonical"]/@href').get() or "").strip() or page_url
        record_id = record_url.rstrip("/").split("/")[-1] or page_url.split("/")[-1]

        venue, city, state, start_date = parse_meta_description(description)
        meta_schema: dict = {}
        if start_date and not _event_datetime_string_is_tbd(start_date):
            meta_schema["startDate"] = start_date
        if tm_site_event_is_datetime_tba({}, meta_schema, None, page_url=page_url):
            self.skipped_tba_event_count += 1
            self.logger.info(
                "Skipped date/time TBA (meta fallback): %s",
                page_url,
            )
            return
        if (
            start_date
            and not _event_datetime_string_is_tbd(start_date)
            and _event_schedule_time_is_before_now(start_date)
        ):
            self.skipped_finished_event_count += 1
            self.logger.info(
                "Skipped completed event (meta description date < now): %s",
                page_url,
            )
            return

        location_name = (venue or "").strip()
        location_address = ""
        if venue or city or state:
            location_address = f"{venue or ''}, {city or ''}, {state or ''}".replace(" ,", ",").strip().strip(",")

        site_id = resolve_site_id(location_address)

        meta_tz = _US_STATE_APPX_IANA.get((state or "").strip().upper())
        event_schedule = infer_event_schedule(start_date, None, meta_tz)

        group_id = triple_id("group", "ticketmaster", str(record_id), str(record_url))
        e_id = triple_id("id", "ticketmaster", str(record_id), str(record_url))
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        self._log_scraped(page_url, "meta")

        desc_l = (description or "").lower()
        loc_l = (location_name or "").lower()
        meta_online = "online" in loc_l or "online event" in desc_l

        latitude = None
        longitude = None
        latitude, longitude = _merge_coords_from_html(
            latitude,
            longitude,
            tree,
            html_body or self._response_text(response),
        )
        if not meta_online and clean_str(location_address):
            latitude, longitude = self._fill_or_store_venue_coords(
                location_name, location_address, latitude, longitude
            )

        yield self._build_record(
            group_id, e_id, ts, title, description, record_id, record_url,
            location_name, location_address, latitude, longitude,
            event_schedule, "scheduled", "USD", None, None,
            "onSale", None, [],
            title, image_url or "", {},
                   image_url or "", [], site_id,
            meta_online,
        )

    def _from_schema_org_event(
            self,
            schema,
            page_url,
            source_label="schema",
            event_fallback=None,
            event_json_fallback=None,
            html_tree=None,
            html_body=None,
    ):
        event_fallback = event_fallback or {}
        event_json_fallback = event_json_fallback or {}
        schema = _merge_schema_geo_from_html(schema, html_tree)

        if tm_site_event_is_datetime_tba(
            event_fallback, schema, event_json_fallback, page_url=page_url
        ):
            self.skipped_tba_event_count += 1
            self.logger.info(
                "Skipped date/time TBA (__NEXT_DATA__/schema): %s",
                page_url,
            )
            return
        if tm_site_event_is_finished(
            event_fallback, schema, event_json_fallback, page_url=page_url
        ):
            self.skipped_finished_event_count += 1
            self.logger.info(
                "Skipped completed event (event date < now, __NEXT_DATA__/schema): %s",
                page_url,
            )
            return

        record_id = clean_str(schema.get("identifier")) or clean_str(event_fallback.get("id")) or page_url.split("/")[
            -1]
        record_url = clean_str(schema.get("url")) or clean_str(event_fallback.get("canonicalURL")) or page_url
        group_id = triple_id("group", "ticketmaster", str(record_id), str(record_url))
        e_id = triple_id("id", "ticketmaster", str(record_id), str(record_url))
        title = title_from_frontend_metadata(
            event_fallback, schema, event_json_fallback, html_tree=html_tree
        )
        description = description_from_next_data(event_fallback, schema, event_json_fallback)

        location_obj = schema.get("location", {}) or {}
        address_obj = location_obj.get("address", {}) or {}
        if not isinstance(address_obj, dict):
            address_obj = {}
        venue_name = clean_str(location_obj.get("name")) or clean_str(
            (event_fallback.get("venue") or {}).get("name")) or ""
        city = address_obj.get("addressLocality", "")
        state = address_obj.get("addressRegion", "")
        postal = address_obj.get("postalCode", "")
        geo = location_obj.get("geo", {}) if isinstance(location_obj.get("geo"), dict) else {}
        latitude = safe_float(location_obj.get("latitude"))
        if latitude is None:
            latitude = safe_float(geo.get("latitude"))
        longitude = safe_float(location_obj.get("longitude"))
        if longitude is None:
            longitude = safe_float(geo.get("longitude"))
        slat, slon = _lat_lon_from_venue_structure(
            location_obj if isinstance(location_obj, dict) else {}
        )
        if latitude is None:
            latitude = slat
        if longitude is None:
            longitude = slon

        location_name = venue_name.strip()
        if "online event" in location_name.lower():
            latitude = longitude = None

        location_address = f"{venue_name}, {city}, {state} {postal}".strip()

        if not clean_str(location_name) or not clean_str(location_address):
            try:
                fb_name, fb_address, fb_lat, fb_lon = _venue_location_from_next(event_json_fallback)
                if fb_name and not clean_str(location_name):
                    location_name = fb_name
                if fb_address and not clean_str(location_address):
                    location_address = fb_address
                if latitude is None and fb_lat is not None:
                    latitude = fb_lat
                if longitude is None and fb_lon is not None:
                    longitude = fb_lon
            except Exception:
                pass

        if not clean_str(location_name) or not clean_str(location_address):
            try:
                desc_venue, desc_city, desc_state, _ = parse_meta_description(description)
                if desc_venue and not clean_str(location_name):
                    location_name = desc_venue.strip()
                if not clean_str(location_address):
                    parts = [clean_str(desc_venue), clean_str(desc_city), clean_str(desc_state)]
                    inferred_addr = ", ".join([p for p in parts if p]).strip(" ,")
                    if inferred_addr:
                        location_address = inferred_addr
            except Exception:
                pass

        latitude, longitude = _merge_coords_from_html(
            latitude, longitude, html_tree, html_body
        )
        latitude, longitude = fill_missing_coordinates(
            location_address, latitude, longitude, schema=schema, event=event_fallback, next_data=event_json_fallback,
        )
        latitude, longitude = self._fill_or_store_venue_coords(
            location_name, location_address, latitude, longitude
        )

        site_id = resolve_site_id(location_address)
        start_date = clean_str(schema.get("startDate"))
        end_date = clean_str(schema.get("endDate"))
        if not start_date and not end_date:
            start_date, end_date = extract_tm_start_end(event_fallback, schema)

        ev_tz = extract_event_timezone_with_fallback(event_fallback, venue_state=state)

        event_schedule = infer_event_schedule(start_date, end_date, ev_tz)
        status = status_from_event_and_schema(event_fallback, schema, html_tree=html_tree)

        offers_obj = normalize_schema_offers(schema.get("offers"))
        avail_raw = str(offers_obj.get("availability") or "")
        availability = "closed" if ("SoldOut" in avail_raw or "Discontinued" in avail_raw) else "onSale"
        min_price = offers_obj.get("lowPrice")
        max_price = offers_obj.get("highPrice")
        currency = offers_obj.get("priceCurrency")
        if not currency:
            currency = (
                event_json_fallback.get('props', {})
                .get('pageProps', {})
                .get('clientConfig', {})
                .get('currency')
            )
        purchase_url = clean_str(offers_obj.get("url")) or clean_str(
            ((event_fallback.get("outlets") or [{}])[0] or {}).get("url")) or None

        event_roles = build_event_roles_from_ticketmaster(event_fallback, schema, title)

        images = collect_media_images(
            event_fallback, schema, clean_str(event_fallback.get("eventImageUrl")),
        )
        thumbnail_img = {}
        image_url = ""
        if images:
            def _thumb_key2(x):
                w = x.get("width")
                return 9999 if not isinstance(w, (int, float)) else w

            thumbnail_img = min(images, key=_thumb_key2)
            image_url = (
                    clean_str(thumbnail_img.get("location"))
                    or clean_str(thumbnail_img.get("url"))
                    or clean_str(images[0].get("location"))
                    or clean_str(images[0].get("url"))
                    or ""
            )
        if not image_url:
            image_url = clean_str(schema.get("image") if isinstance(schema.get("image"), str) else None) or ""

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._log_scraped(page_url, source_label)

        is_online = event_is_online(event_fallback, schema, event_json_fallback)

        yield self._build_record(
            group_id, e_id, ts, title, description, record_id, record_url,
            location_name, location_address, latitude, longitude,
            event_schedule, status, currency, min_price, max_price,
            availability, purchase_url, event_roles,
            title, image_url, thumbnail_img,
            image_url, images, site_id,
            is_online,
        )

    # ------------------------------------------------------------------ #
    #  SHARED RECORD BUILDER                                               #
    # ------------------------------------------------------------------ #
    def _build_record(
            self, group_id, e_id, ts, title, description, record_id, record_url,
            location_name, location_address, latitude, longitude,
            event_schedule, status, currency, min_price, max_price,
            availability, purchase_url, event_roles,
            image_title, thumbnail_url, thumbnail_img,
            image_url, images, site_id,
            is_online: bool = False,
    ):
        # Final location safety net
        if not clean_str(location_name) or not clean_str(location_address):
            try:
                d_venue, d_city, d_state, _ = parse_meta_description(description)
                if d_venue and not clean_str(location_name):
                    location_name = d_venue
                if not clean_str(location_address):
                    parts = [clean_str(d_venue), clean_str(d_city), clean_str(d_state)]
                    inferred = ", ".join([p for p in parts if p]).strip(" ,")
                    if inferred:
                        location_address = inferred
            except Exception:
                pass

        name_for_online = clean_str(location_name) or ""
        name_has_online = "online" in name_for_online.lower()
        desc_l = (description or "").lower()
        is_online_venue = (
            bool(is_online) or name_has_online or ("online event" in desc_l)
        )
        if not is_online_venue and (latitude is None or longitude is None):
            latitude, longitude = self._fill_or_store_venue_coords(
                clean_str(location_name),
                clean_str(location_address),
                latitude,
                longitude,
            )

        # ----------------------------------------------------------------
        # FIX 2: pass BOTH location_name AND location_address to
        # _compute_near_by. Previously only location_name was passed,
        # so the ZIP could never be extracted → always returned None.
        # ----------------------------------------------------------------
        near_by: bool | None
        if is_online_venue:
            location_name = "Online Event"
            location_address = None
            latitude = None
            longitude = None
            near_by = None
        else:
            near_by = self._compute_near_by(
                clean_str(location_name),
                clean_str(location_address),   # FIX: was missing entirely
            )

        if site_id is None and clean_str(location_address):
            site_id = resolve_site_id(location_address)

        event_pricing = build_event_pricing(
            min_price, max_price, currency, description=description
        )

        if images:
            media_candidates = images
        elif image_url:
            media_candidates = [{
                "location": image_url,
                "width": thumbnail_img.get("width") if isinstance(thumbnail_img, dict) else None,
                "height": thumbnail_img.get("height") if isinstance(thumbnail_img, dict) else None,
                "id": None,
            }]
        else:
            media_candidates = []

        media_items = []
        for idx, img in enumerate(media_candidates, start=1):
            if not isinstance(img, dict): continue
            img_loc = (
                    clean_str(img.get("location"))
                    or clean_str(img.get("url"))
                    or clean_str(img.get("src"))
                    or clean_str(image_url)
            )
            if not img_loc: continue
            img_width = img.get("width")
            img_height = img.get("height")
            source_image_id = ticketmaster_image_id_from_img(img, img_loc)
            media_id = f"media-{idx:03d}"
            variants = []
            raw_variants = img.get("variants") if isinstance(img.get("variants"), list) else []
            for v in raw_variants:
                if not isinstance(v, dict):
                    continue
                v_url = clean_str(v.get("url"))
                if not v_url:
                    continue
                mime = clean_str(v.get("mimeType"))
                if (mime or "").lower() == "image/jpeg":
                    continue
                variants.append({
                    "url": v_url,
                    "mimeType": mime,
                    "width": v.get("width"),
                    "height": v.get("height"),
                })
            media_items.append({
                "id": media_id,
                "type": "image",
                "title": image_title,
                "source": {"name": "Ticketmaster", "id": source_image_id, "url": img_loc},
                "thumbnail": {"url": img_loc, "width": img_width, "height": img_height},
                "variants": variants,
            })

        nm_online = clean_str(location_name) or ""
        desc_l = (description or "").lower()
        online_flag = (
                bool(is_online)
                or ("online" in nm_online.lower())
                or ("online event" in desc_l)
        )

        if isinstance(event_schedule, dict):
            event_schedule_out = {**event_schedule}
        else:
            event_schedule_out = {"kind": "tbd"}

        draft = {
            "provider": "forage",
            "module": "events",
            "groupId": group_id,
            "id": e_id,
            "createdAt": ts,
            "updatedAt": ts,
            "title": title,
            "source": {"name": "Ticketmaster", "id": "ticketmaster",
                       "url": "https://www.ticketmaster.com"},
            "recordSource": {"id": record_id, "url": record_url},
            "location": {
                "name": location_name,
                "address": location_address,
                "latitude": latitude,
                "longitude": longitude,
                "nearBy": near_by,
            },
            "siteId": site_id,
            "metadata": {
                "event": {
                    "online": online_flag,
                    "description": description,
                    "eventRoles": event_roles or [],
                    "eventSchedule": event_schedule_out,
                    "status": status,
                    "eventPricing": event_pricing,
                    "availability": availability,
                    "purchaseUrl": purchase_url,
                    "media": media_items,
                }
            },
        }
        return finalize_ticketmaster_event_record(draft)

    def errback(self, failure):
        request = failure.request
        page_url = request.meta.get("page_url") or request.url
        err_msg = failure.getErrorMessage()
        err_name = failure.type.__name__ if getattr(failure, "type", None) else type(failure.value).__name__

        if failure.check(HttpError):
            resp = getattr(failure.value, "response", None)
            st = getattr(resp, "status", None) if resp is not None else None
            fails = int(request.meta.get("tm_http_retries", 0) or 0)
            return self._schedule_tm_retry(
                page_url,
                request.url,
                fails,
                reason="HttpError",
                status=st,
            )

        transient = bool(
            failure.check(
                twisted_error.TimeoutError,
                twisted_error.TCPTimedOutError,
                twisted_error.DNSLookupError,
                twisted_error.ConnectionRefusedError,
                twisted_error.ConnectionLost,
                twisted_error.ConnectError,
            )
        )
        if not transient and err_msg:
            low = err_msg.lower()
            transient = "timeout" in low or "timed out" in low

        if transient:
            fails = int(request.meta.get("tm_http_retries", 0) or 0)
            return self._schedule_tm_retry(
                page_url,
                request.url,
                fails,
                reason=err_name,
                status=None,
            )

        fails = int(request.meta.get("tm_http_retries", 0) or 0)
        resp = getattr(failure.value, "response", None)
        st = getattr(resp, "status", None) if resp is not None else None
        return self._schedule_tm_retry(
            page_url,
            request.url,
            fails,
            reason=err_name,
            status=st,
        )

    def closed(self, reason):
        pass    