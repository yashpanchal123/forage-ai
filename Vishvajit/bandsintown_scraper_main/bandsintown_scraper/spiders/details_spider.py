"""
details_spider.py
=================
Reads event URLs from a CSV file (default: `bandsintown_event_urls.csv`) and
scrapes the full event details from each Bandsintown event page.

Extracted fields
----------------
  event_id, url, event_name, artist_name,
  datetime, venue_name, venue_location, venue_latitude, venue_longitude,
  description, cost, availability,
  promoter

Extraction strategy
-------------------
Only `window.__data` script payload (XPath to locate script; JSON parse).
Nested fields such as `jsonLdContainer.eventJsonLd` are read from that object.

Usage
-----
    scrapy crawl listing
    scrapy crawl details
    scrapy crawl details -a urls_csv=path/to/bandsintown_event_urls.csv
"""
import csv
import json
import re
from pathlib import Path
from datetime import datetime, timezone
import uuid
from zoneinfo import ZoneInfo

import scrapy
from bandsintown_scraper.items import BandsintownEventDetailsItem, EventItem

# ── Schema helpers ─────────────────────────────────────────────────────────────

# Availability enum (per Crawlmagic schema)
# notApplicable | unknown | comingSoon | presale | onSale | soldOut | waitList | closed
_SCHEMA_AVAILABILITY = {
    "instock":             "onSale",
    "limitedavailability": "onSale",
    "preorder":            "presale",
    "preorderd":           "presale",
    "soldout":             "soldOut",
    "discontinued":        "closed",
    "outofstock":          "soldOut",
}

PROVIDER = "forage"
SOURCE = {"name": "Bandsintown", "id": "bandsintown", "url": "https://www.bandsintown.com"}
TARGET_METROS = (
    ("atlanta", "GA"),
    ("austin", "TX"),
    ("orlando", "FL"),
    ("nashville", "TN"),
)
TARGET_MARKET_CITY_NAMES = frozenset(city for city, _st in TARGET_METROS)


def _venue_address_in_target_markets(address: str | None) -> bool:
    """True if venue address text mentions one of the four pilot cities (same idea as listing locationText)."""
    if not address or not str(address).strip():
        return False
    low = str(address).strip().lower()
    return any(city in low for city in TARGET_MARKET_CITY_NAMES)


ZIP_PATTERN = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
ZIP_AFTER_ZIP_WORD = re.compile(r"\bzip\s*(\d{5})(?:-\d{4})?\b", re.I)
DEFAULT_DMA_CSV = "Tegna - Pilot Scope Locations.csv"

# Naive event datetimes (no Z / offset) are local wall time in this zone, then converted to UTC.
NAIVE_EVENT_TZ = ZoneInfo("America/Chicago")

# Crawlmagic availability enum values (pass through if already set from ticket UI)
_SCHEMA_AVAIL_VALUES = frozenset({
    "notApplicable", "unknown", "comingSoon", "presale", "onSale",
    "soldOut", "waitList", "closed",
})


def triple_id(*parts: str) -> str:
    u = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts))
    h = f"{u.int:032d}"
    return f"{int(h[0:4])}-{int(h[4:10])}-{int(h[10:18])}"


def _is_date_only(value: str | None) -> bool:
    if not value:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()))

def _to_utc_iso8601_z(value: str | None) -> str | None:
    """
    Parse an ISO-like dateTime string and return UTC as YYYY-MM-DDTHH:MM:SSZ.

    - Naive datetimes are interpreted as US Central (America/Chicago), then converted to UTC.
    - Strings with Z or an explicit offset are converted to UTC from that instant.
    - Returns None if empty, date-only, or invalid.
    """
    if not value or not isinstance(value, str):
        return None

    s = value.strip()
    if not s or _is_date_only(s):
        return None

    # Normalize Z → +00:00
    normalized = s.replace("Z", "+00:00", 1) if s.endswith("Z") else s

    # Normalize space → T
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)

    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NAIVE_EVENT_TZ)

    dt = dt.astimezone(timezone.utc)

    # Format output
    if dt.microsecond:
        frac = f"{dt.microsecond:06d}".rstrip("0")
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + (f".{frac}" if frac else "") + "Z"

    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

_COST_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _parse_availability(raw: str) -> str:
    """Normalise a schema.org availability URL or plain string."""
    if not raw:
        return "unknown"
    key = raw.rstrip("/").split("/")[-1].lower().replace(" ", "")
    return _SCHEMA_AVAILABILITY.get(key, "unknown")


def _map_ticket_availability_token(s: str) -> str | None:
    """Map Bandsintown / UI strings to Crawlmagic availability enum."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip().lower().replace(" ", "").replace("-", "")
    # Do not map bare "tickets" — common button copy (e.g. status/label) is not inventory state.
    if t in ("onsale", "available", "buy", "instock"):
        return "onSale"
    if t in ("soldout", "sold_out"):
        return "soldOut"
    # Do not map bare "unavailable" — often means no online tickets / door-only, not sold out.
    if t in ("presale", "preorder"):
        return "presale"
    if t in ("waitlist", "wait_list"):
        return "waitList"
    if t in ("comingsoon", "soon"):
        return "comingSoon"
    if t in ("closed", "ended"):
        return "closed"
    if t in ("notapplicable", "na", "n/a"):
        return "notApplicable"
    if t == "unknown":
        return "unknown"
    return None


def _deep_find_first_url(obj, depth: int = 0) -> str | None:
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, str) and obj.startswith("http"):
        return obj
    if isinstance(obj, dict):
        for key in ("purchaseUrl", "url", "ticketUrl", "href", "link", "checkoutUrl"):
            v = obj.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            u = _deep_find_first_url(v, depth + 1)
            if u:
                return u
    elif isinstance(obj, list):
        for v in obj:
            u = _deep_find_first_url(v, depth + 1)
            if u:
                return u
    return None


def _deep_find_availability_string(obj, depth: int = 0) -> str | None:
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, dict):
        for key in (
            "availability", "ticketAvailability", "ticketStatus", "status",
            "buttonState", "saleStatus", "inventoryStatus",
        ):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                m = _map_ticket_availability_token(v)
                if m:
                    return m
            if isinstance(v, dict):
                inner = _deep_find_availability_string(v, depth + 1)
                if inner:
                    return inner
        for v in obj.values():
            inner = _deep_find_availability_string(v, depth + 1)
            if inner:
                return inner
    elif isinstance(obj, list):
        for v in obj:
            inner = _deep_find_availability_string(v, depth + 1)
            if inner:
                return inner
    return None


def _map_status_from_ticket_logic(obj, depth: int = 0) -> str | None:
    """Infer metadata.event.status from ticketButtonLogic subtree."""
    if depth > 8 or not isinstance(obj, dict):
        return None
    for key in ("eventStatus", "event_status", "status", "buttonState", "state"):
        v = obj.get(key)
        if isinstance(v, str):
            sk = v.strip().lower().rstrip("/").split("/")[-1].replace("http:", "").replace("schema.org", "")
            if "cancel" in sk:
                return "cancelled"
            if "postpone" in sk or "reschedule" in sk:
                return "postponed"
            # "onsale" is ticket availability, not event lifecycle — do not map to scheduled here.
            if "schedule" in sk or sk in ("upcoming", "active", "live"):
                return "scheduled"
        if isinstance(v, dict):
            inner = _map_status_from_ticket_logic(v, depth + 1)
            if inner:
                return inner
    for v in obj.values():
        if isinstance(v, dict):
            inner = _map_status_from_ticket_logic(v, depth + 1)
            if inner:
                return inner
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    inner = _map_status_from_ticket_logic(x, depth + 1)
                    if inner:
                        return inner
    return None


def _explicit_sold_out_in_ticket_logic(logic) -> bool:
    """True only when ticket UI explicitly indicates sold out (not merely hasTickets=false)."""
    if not isinstance(logic, dict):
        return False
    stack = [logic]
    while stack:
        d = stack.pop()
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            lk = str(k).lower().replace("_", "")
            if isinstance(v, bool) and v and ("soldout" in lk or lk in ("issoldout", "ticketsoldout")):
                return True
            if isinstance(v, str) and v.strip():
                m = _map_ticket_availability_token(v)
                if m == "soldOut":
                    return True
                low = v.lower()
                if "sold out" in low or low.strip() in ("soldout", "sold_out"):
                    return True
            if isinstance(v, dict):
                stack.append(v)
            elif isinstance(v, list):
                stack.extend(x for x in v if isinstance(x, dict))
    return False


def _availability_from_rsvp_container(rsvp: dict | None) -> tuple[str, str | None]:
    """
    Bandsintown ticket UX (ticketsButtons + ticketButtonLogic):
    - hasTickets=true + getTicketsButton.url → onSale; purchase URL is the ticket button only
      (do not use bitPlus.playerUrl / watch_live from logic as purchaseUrl).
    - hasTickets=false + notify-me flow → comingSoon (not JSON-LD offers "InStock").
    - hasTickets=false + isFreeEvent → notApplicable.
    - Explicit sold-out flags in logic → soldOut.
    """
    if not isinstance(rsvp, dict):
        return "unknown", None

    logic = rsvp.get("ticketButtonLogic")
    tb = rsvp.get("ticketsButtons")
    if not isinstance(tb, dict):
        tb = {}
    has_tickets = tb.get("hasTickets")

    gtb = tb.get("getTicketsButton")
    tb_url = None
    if isinstance(gtb, dict):
        u = gtb.get("url")
        if isinstance(u, str) and u.strip():
            tb_url = u.strip()

    # Ticket checkout URL only from getTicketsButton — not player/watch_live buried under logic.
    purchase_url = tb_url

    av_from_logic = _deep_find_availability_string(logic) if isinstance(logic, dict) else None
    if av_from_logic:
        return av_from_logic, purchase_url

    if has_tickets is True:
        return "onSale", purchase_url

    if has_tickets is False:
        if isinstance(logic, dict) and _explicit_sold_out_in_ticket_logic(logic):
            return "soldOut", purchase_url
        if isinstance(logic, dict) and logic.get("isFreeEvent") is True:
            return "notApplicable", purchase_url
        # API typo: shoudlRenderInPersonNotifyMe — "Notify Me" / not on sale yet
        if tb.get("shoudlRenderInPersonNotifyMe") is True:
            return "comingSoon", None
        if tb_url is None and tb.get("displayDetailedTickets") is False:
            return "comingSoon", None
        return "unknown", purchase_url

    return "unknown", purchase_url


def _live_photo_large_urls(data: dict) -> list[str]:
    """Bandsintown live gallery: initialState.livePhotoState.photos[].largePhotoUrl"""
    roots: list[dict] = []
    if isinstance(data.get("initialState"), dict):
        roots.append(data["initialState"])
    ev = data.get("eventView")
    if isinstance(ev, dict) and isinstance(ev.get("initialState"), dict):
        roots.append(ev["initialState"])
    out: list[str] = []
    for st in roots:
        lps = st.get("livePhotoState")
        if not isinstance(lps, dict):
            continue
        photos = lps.get("photos")
        if not isinstance(photos, list):
            continue
        for p in photos:
            if not isinstance(p, dict):
                continue
            u = (
                p.get("largePhotoUrl")
                or p.get("large_photo_url")
                or p.get("photoUrl")
                or p.get("url")
            )
            if isinstance(u, str) and u.startswith("http"):
                out.append(u.strip())
    return list(dict.fromkeys(out))


def _format_cost(offers: list) -> str:
    """Build a human-readable cost string from a list of offer dicts."""
    prices = []
    currency = ""
    for o in offers:
        price = o.get("price") or o.get("minPrice")
        if price:
            try:
                prices.append(float(str(price).replace(",", "")))
            except ValueError:
                pass
        if not currency:
            currency = o.get("priceCurrency", "")

    if not prices:
        return "Free" if any(
            str(o.get("price", "")).strip() in ("0", "0.0", "free", "Free")
            for o in offers
        ) else "Unknown"

    lo, hi = min(prices), max(prices)
    prefix = f"{currency} " if currency else ""
    if lo == 0 and hi == 0:
        return "Free"
    if lo == hi:
        return f"{prefix}{lo:.2f}"
    return f"{prefix}{lo:.2f} – {hi:.2f}"


def _format_cost_from_ticket_list(ticket_list) -> str | None:
    """Parse detailedTicketList.ticketList when JSON-LD offers omit numeric prices."""
    if not isinstance(ticket_list, list):
        return None
    prices: list[float] = []
    currency = ""
    for t in ticket_list:
        if not isinstance(t, dict):
            continue
        p = t.get("price")
        if p is not None and p != "":
            try:
                prices.append(float(str(p).replace(",", "")))
            except (TypeError, ValueError):
                pass
        pc = t.get("currency") or t.get("priceCurrency")
        if not currency and isinstance(pc, str) and pc.strip():
            currency = pc.strip().upper()
        fp = t.get("formattedPrice")
        if isinstance(fp, str) and fp.strip():
            for m in _COST_NUMBER_RE.finditer(fp.replace(",", "")):
                try:
                    prices.append(float(m.group(1)))
                except ValueError:
                    pass
    if not prices:
        return None
    lo, hi = min(prices), max(prices)
    prefix = f"{currency} " if currency else ""
    if lo == 0 and hi == 0:
        return "Free"
    if lo == hi:
        return f"{prefix}{lo:.2f}".strip()
    return f"{prefix}{lo:.2f} – {hi:.2f}".strip()


def _truthy_has_description_tab(flag) -> bool:
    if flag is True:
        return True
    if isinstance(flag, str) and flag.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def _event_description_from_body(body: dict, event_jsonld: dict, ev: dict) -> str:
    """
    Prefer the on-page About copy when the UI exposes a description tab.
    When hasDescription is absent/false (including Bandsintown's empty string), ignore JSON-LD
    placeholders like "{artist} at {venue} {iso-datetime}".
    """
    info = body.get("eventInfoContainer") if isinstance(body.get("eventInfoContainer"), dict) else {}
    event_info = info.get("eventInfo") if isinstance(info.get("eventInfo"), dict) else {}
    desc_block = event_info.get("description") if isinstance(event_info.get("description"), dict) else {}

    has_tab = _truthy_has_description_tab(desc_block.get("hasDescription"))
    long_raw = desc_block.get("longDescription")
    short_raw = desc_block.get("shortDescription")
    long_s = long_raw.strip() if isinstance(long_raw, str) else ""
    short_s = short_raw.strip() if isinstance(short_raw, str) else ""

    jsonld_s = str(event_jsonld.get("description") or "").strip()
    ev_s = str(ev.get("description") or "").strip()

    if has_tab and (long_s or short_s):
        return long_s or short_s
    if has_tab:
        return jsonld_s or ev_s
    return ""


def _collect_image_urls(obj, out: list[str] | None = None, depth: int = 0) -> list[str]:
    """Collect likely image URLs from nested dict/list payloads."""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, str) and v.startswith("http") and any(x in lk for x in ("image", "photo", "thumbnail", "poster", "cover")):
                out.append(v)
            else:
                _collect_image_urls(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _collect_image_urls(v, out, depth + 1)
    return out


def _image_id_from_url(url: str) -> str:
    """
    Extract an image id from a Bandsintown image URL using regex.
    Falls back to empty string if no id-like segment is found.
    """
    if not isinstance(url, str):
        return ""
    u = url.strip()
    if not u:
        return ""
    # Try to grab the last numeric segment before optional extension or query.
    m = re.search(r"(\d+)(?:\.[a-zA-Z0-9]+)?(?:\?|$)", u)
    if m:
        return m.group(1)
    # Fallback: last path segment without query
    try:
        from urllib.parse import urlparse

        path = urlparse(u).path or ""
        segment = path.rstrip("/").split("/")[-1]
        if segment:
            return segment.split(".")[0]
    except Exception:
        pass
    return ""


def _normalize_st(st: str) -> str:
    s = (st or "").strip().upper()
    return s[:2] if len(s) > 2 else s


def _metro_match(dma_name: str, st_abv: str) -> bool:
    dma = (dma_name or "").strip().lower()
    st = _normalize_st(st_abv)
    for city, req_st in TARGET_METROS:
        if st != req_st:
            continue
        if dma == city or city in dma:
            return True
    return False


def _extract_zip(address: str) -> str | None:
    s = str(address or "").strip()
    if not s:
        return None
    s = re.sub(r'^["\s]+|["\s]+$', "", s)
    s = s.replace('""', '"').strip('"').strip("'")
    s = " ".join(s.split())
    if not s:
        return None
    m_zip_word = ZIP_AFTER_ZIP_WORD.search(s)
    if m_zip_word:
        return m_zip_word.group(1)
    matches = ZIP_PATTERN.findall(s)
    if not matches:
        return None
    zip5 = matches[-1]
    if len(matches) == 1 and re.match(r"^\s*\d{5}\b", s):
        return None
    return zip5


def compute_near_by(venue_address: str, zip_dma_lookup: dict[str, set[tuple[str, str]]]) -> bool | None:
    """
    Match do_details nearBy behavior:
    - None: empty address / no ZIP parsed
    - True: ZIP mapped and DMA/state in target metros
    - False: ZIP parsed but unmapped/non-target
    """
    if not venue_address or not str(venue_address).strip():
        return None
    zip5 = _extract_zip(venue_address)
    if not zip5:
        return None
    if not zip_dma_lookup:
        return False
    rows = zip_dma_lookup.get(zip5, set())
    if not rows:
        return False
    for dma_name, st_abv in rows:
        if _metro_match(dma_name, st_abv):
            return True
    return False


def _event_view_body(data: dict) -> dict:
    ev = data.get("eventView")
    if not isinstance(ev, dict):
        return {}
    body = ev.get("body")
    return body if isinstance(body, dict) else {}


def _roles_from_jsonld_organizer(organizer) -> list[dict]:
    """eventJsonLd.organizer — Organization name(s) → eventRoles with key organizer."""
    out: list[dict] = []
    if organizer is None:
        return out
    if isinstance(organizer, dict):
        name = organizer.get("name")
        if name and str(name).strip():
            out.append({"organizer": str(name).strip()})
        return out
    if isinstance(organizer, list):
        for o in organizer:
            if isinstance(o, dict):
                name = o.get("name")
                if name and str(name).strip():
                    out.append({"organizer": str(name).strip()})
        return out
    return out


def _lineup_performer_roles(lineup_items) -> list[dict]:
    """eventView.body.eventInfoContainer.lineupContainer.lineupItems → {"performer": name}."""
    roles: list[dict] = []
    if not isinstance(lineup_items, list):
        return roles
    for item in lineup_items:
        if not isinstance(item, dict):
            continue
        art = item.get("artist")
        name = None
        if isinstance(art, dict):
            name = art.get("name") or art.get("title")
        if not name:
            name = item.get("artistName") or item.get("name") or item.get("title")
        if name and str(name).strip():
            roles.append({"performer": str(name).strip()})
    return roles


def load_zip_dma_map(path: Path, logger) -> dict[str, set[tuple[str, str]]]:
    """
    Build ZIP -> {(market, state), ...} lookup from CSV columns:
    Zip, Market, State
    """
    if not path.exists():
        logger.warning(
            "Pilot scope CSV not found at %s. nearBy matching will be limited.",
            path,
        )
        return {}
    out: dict[str, set[tuple[str, str]]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"Zip", "Market", "State"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            logger.error("CSV must contain columns %s. Found: %s", sorted(required), reader.fieldnames)
            return {}
        for row in reader:
            z_raw = row.get("Zip")
            if z_raw is None:
                continue
            z_match = re.search(r"(\d{5})", str(z_raw).strip())
            if not z_match:
                continue
            z5 = z_match.group(1)
            market = (row.get("Market") or "").strip().lower()
            state = (row.get("State") or "").strip().upper()
            if not market or not state:
                continue
            out.setdefault(z5, set()).add((market, state))
    return out


# ─────────────────────────────────────────────────────────────────────────────

class DetailsSpider(scrapy.Spider):
    name = "bandsintown_details"
    custom_settings = {
        # Geonode: fewer parallel tunnels reduces ProxyError / RemoteDisconnected.
        "CONCURRENT_REQUESTS": 10,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 8,
        "REACTOR_THREADPOOL_MAXSIZE": 12,
        "DOWNLOAD_DELAY": 0.75,
        # Retry transient anti-bot/network responses (5 retries × long backoff = slow crawl).
        "RETRY_HTTP_CODES": [403, 408, 429, 500, 502, 503, 504, 522, 524],
        "RETRY_TIMES": 3,
        "FEEDS": {
            "bandsintown_events_dataset_29_04_2026.json": {
                "format": "json",
                "overwrite": True,
                "indent": 4,
                "encoding": "utf-8",
            }
        }
    }

    def __init__(self, urls_csv: str | None = None, dma_csv: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        project_root = Path(__file__).resolve().parents[2]
        csv_path = Path(urls_csv) if urls_csv else (project_root / "bandsintown_event_urls.csv")
        if not csv_path.is_absolute():
            csv_path = project_root / csv_path
        self.urls_csv = str(csv_path)
        dma_path = Path(dma_csv) if dma_csv else (project_root / DEFAULT_DMA_CSV)
        if not dma_path.is_absolute():
            dma_path = (project_root / dma_path).resolve()
        self._zip_dma_map = load_zip_dma_map(dma_path, self.logger)

        # Dedupe within a run (in case urls_csv has duplicates).
        self._seen_event_urls: set[str] = set()
        self.max_url_retries = int(kwargs.get("max_url_retries", 3))
        self.stats_total_urls = 0
        self.stats_success_items = 0
        self.stats_retry_requests = 0
        self.stats_failed_urls = 0
        self.stats_skipped_out_of_scope = 0

    async def start(self):
        for req in self.start_requests():
            yield req

    def start_requests(self):
        try:
            self.logger.info(
                "Starting details crawl from %s (max_url_retries=%s)",
                self.urls_csv,
                self.max_url_retries,
            )
            with open(self.urls_csv, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    event_url = (row.get("event_url") or "").strip()
                    if not event_url:
                        continue
                    if event_url in self._seen_event_urls:
                        continue
                    self._seen_event_urls.add(event_url)
                    self.stats_total_urls += 1

                    yield scrapy.Request(
                        url=event_url,
                        callback=self.parse,
                        errback=self.errback,
                        dont_filter=True,
                        meta={"event_url": event_url, "retry_attempt": 0},
                    )
            self.logger.info("Queued %s unique event URLs", self.stats_total_urls)
        except FileNotFoundError:
            self.logger.error(
                "URLs CSV not found at %s. Run: scrapy crawl listing",
                self.urls_csv,
            )

    # ── main parse ────────────────────────────────────────────────────────────

    def parse(self, response):
        url = response.meta["event_url"]
        retry_attempt = int(response.meta.get("retry_attempt", 0))
        try:
            payload = self._extract_window_data_payload(response.text)
            if payload:
                item = self._to_schema_from_window_data(payload, url)
                if item is not None:
                    self.stats_success_items += 1
                    yield item
                else:
                    self.stats_skipped_out_of_scope += 1
                    self.logger.debug("Skipped out-of-scope venue (address has no target market): %s", url)
            else:
                self.logger.warning("No window.__data payload for: %s (attempt %s)", url, retry_attempt)
                retry_req = self._build_retry_request(response.request, reason="missing_window_data")
                if retry_req is not None:
                    yield retry_req
        except Exception as exc:
            self.logger.error("Extraction error for %s (attempt %s): %s", url, retry_attempt, exc)
            retry_req = self._build_retry_request(response.request, reason=f"parse_exception:{type(exc).__name__}")
            if retry_req is not None:
                yield retry_req

    def _build_retry_request(self, request: scrapy.Request, reason: str) -> scrapy.Request | None:
        """Create a bounded retry request for this event URL."""
        attempt = int(request.meta.get("retry_attempt", 0))
        if attempt >= self.max_url_retries:
            self.logger.warning(
                "Giving up after %s attempts for %s (%s)",
                attempt,
                request.meta.get("event_url", request.url),
                reason,
            )
            return None
        next_attempt = attempt + 1
        self.stats_retry_requests += 1
        self.logger.info(
            "Retrying %s (attempt %s/%s) due to %s",
            request.meta.get("event_url", request.url),
            next_attempt,
            self.max_url_retries,
            reason,
        )
        return request.replace(
            dont_filter=True,
            priority=request.priority + 1,
            meta={**request.meta, "retry_attempt": next_attempt},
        )

    def _extract_window_data_payload(self, html: str) -> dict | None:
        sel = scrapy.Selector(text=html)
        scripts = sel.xpath("//script[contains(text(),'window.__data=')]/text()").getall()
        if not scripts:
            scripts = sel.xpath("//script[contains(text(),'window.__data =')]/text()").getall()
        for script_text in scripts:
            text = (script_text or "").strip()
            if not text:
                continue
            m = re.search(r"window\.__data\s*=\s*(\{.*\})\s*;?\s*$", text, re.DOTALL)
            if not m:
                m = re.search(r"window\.__data\s*=\s*(\{.*\})", text, re.DOTALL)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            return data if isinstance(data, dict) else None
        return None

    def _find_event_in_window_data(self, d, depth: int = 0) -> dict | None:
        """Locate event-like dict inside window.__data JSON (not HTML/Next fallbacks)."""
        if depth > 10 or not isinstance(d, (dict, list)):
            return None
        if isinstance(d, dict):
            if ("lineup" in d or "venue" in d) and "datetime" in d:
                return d
            for v in d.values():
                found = self._find_event_in_window_data(v, depth + 1)
                if found:
                    return found
        else:
            for item in d:
                found = self._find_event_in_window_data(item, depth + 1)
                if found:
                    return found
        return None

    def _to_schema_from_window_data(self, data: dict, url: str) -> BandsintownEventDetailsItem | None:
        ev = self._find_event_in_window_data(data) or data.get("event") or data.get("eventData") or {}
        if not isinstance(ev, dict):
            ev = {}
        body = _event_view_body(data)
        jsonld_container = data.get("jsonLdContainer") if isinstance(data, dict) else {}
        if not isinstance(jsonld_container, dict):
            jsonld_container = {}
        event_jsonld = jsonld_container.get("eventJsonLd")
        if not isinstance(event_jsonld, dict):
            event_jsonld = {}

        event_view_root = data.get("eventView") if isinstance(data.get("eventView"), dict) else {}
        checkin = event_view_root.get("checkinInfo") if isinstance(event_view_root, dict) else None
        if not isinstance(checkin, dict):
            checkin = {}
        event_id = str(checkin.get("eventId") or "").strip()
        title = str(event_jsonld.get("name") or ev.get("title") or ev.get("name") or "")

        artist = ev.get("artist") or {}
        if not isinstance(artist, dict):
            artist = {}
        performer_block = event_jsonld.get("performer")
        performers = performer_block if isinstance(performer_block, list) else [performer_block] if isinstance(performer_block, dict) else []
        performer_names = [str(p.get("name")).strip() for p in performers if isinstance(p, dict) and p.get("name")]
        artist_name = str(performer_names[0] if performer_names else (artist.get("name") or title))

        venue = ev.get("venue") or {}
        if not isinstance(venue, dict):
            venue = {}
        location = event_jsonld.get("location") if isinstance(event_jsonld, dict) else {}
        if not isinstance(location, dict):
            location = {}
        venue_name = str(location.get("name") or venue.get("name") or "")
        address_obj = location.get("address")
        if isinstance(address_obj, dict):
            venue_address = ", ".join(
                x for x in [
                    str(address_obj.get("streetAddress") or "").strip(),
                    str(address_obj.get("addressLocality") or "").strip(),
                    str(address_obj.get("addressRegion") or "").strip(),
                    str(address_obj.get("postalCode") or "").strip(),
                ] if x
            )
        elif isinstance(address_obj, str):
            venue_address = address_obj.strip()
        else:
            venue_address = ", ".join(
                x for x in [
                    str(venue.get("address") or "").strip(),
                    str(venue.get("city") or "").strip(),
                    str(venue.get("region") or "").strip(),
                ] if x
            )
        geo = location.get("geo") if isinstance(location, dict) else {}
        if not isinstance(geo, dict):
            geo = {}
        venue_lat = self._to_float(geo.get("latitude") or venue.get("latitude") or venue.get("lat"))
        venue_lng = self._to_float(geo.get("longitude") or venue.get("longitude") or venue.get("lng"))

        dt_value = str(event_jsonld.get("startDate") or ev.get("datetime") or ev.get("start_datetime") or ev.get("startDate") or "")
        end_dt_value = str(event_jsonld.get("endDate") or ev.get("end_datetime") or "")

        raw_offers = event_jsonld.get("offers") or ev.get("offers") or []
        if isinstance(raw_offers, dict):
            raw_offers = [raw_offers]
        if not isinstance(raw_offers, list):
            raw_offers = []
        cost = _format_cost(raw_offers)

        rsvp = body.get("rsvpAndTicketsContainer")
        rsvp_d = rsvp if isinstance(rsvp, dict) else None
        availability, purchase_url = _availability_from_rsvp_container(rsvp_d)
        if availability == "unknown":
            availability = _parse_availability(
                next((o.get("availability", "") for o in raw_offers if isinstance(o, dict)), "")
            )
        # Do not promote JSON-LD offer URLs when UI says tickets are not for sale yet / N/A.
        if not purchase_url and availability not in ("comingSoon", "notApplicable"):
            purchase_url = next(
                (str(o.get("url")) for o in raw_offers if isinstance(o, dict) and o.get("url")),
                None,
            )
        # detailedTicketList only when the UI lists tickets for sale (avoids player-only / notify flows)
        tb_ht = None
        if rsvp_d and isinstance(rsvp_d.get("ticketsButtons"), dict):
            tb_ht = rsvp_d["ticketsButtons"].get("hasTickets")
        if tb_ht is True:
            dtl = body.get("detailedTicketList")
            if isinstance(dtl, dict):
                ticket_list = dtl.get("ticketList")
                if isinstance(ticket_list, list):
                    urls: list[str] = []
                    for t in ticket_list:
                        if not isinstance(t, dict):
                            continue
                        u = t.get("url")
                        if isinstance(u, str) and u.strip():
                            urls.append(u.strip())
                    if urls:
                        purchase_url = ", ".join(urls)
                    ticket_cost = _format_cost_from_ticket_list(ticket_list)
                    if ticket_cost:
                        cost = ticket_cost

        ticket_logic = rsvp_d.get("ticketButtonLogic") if isinstance(rsvp_d, dict) else None
        if isinstance(ticket_logic, dict) and ticket_logic.get("isFreeEvent") is True:
            cost = "Free"
        elif event_jsonld.get("isAccessibleForFree") is True:
            cost = "Free"

        raw_status = str(event_jsonld.get("eventStatus") or ev.get("status") or ev.get("timeStatus") or "").strip().lower()
        status_key = raw_status.rstrip("/").split("/")[-1] if raw_status else ""
        if status_key in ("eventcancelled", "cancelled", "canceled"):
            status = "cancelled"
        elif status_key in ("eventpostponed", "postponed", "rescheduled"):
            status = "postponed"
        elif status_key in ("eventscheduled", "scheduled", "upcoming", "active"):
            status = "scheduled"
        else:
            status = None
        if isinstance(rsvp, dict):
            logic = rsvp.get("ticketButtonLogic")
            if isinstance(logic, dict):
                ticket_st = _map_status_from_ticket_logic(logic)
                if ticket_st:
                    status = ticket_st

        description = _event_description_from_body(body, event_jsonld, ev)

        tags = []
        for t in (ev.get("tags") or ev.get("genres") or []):
            if isinstance(t, str) and t.strip():
                tags.append(t.strip())
            elif isinstance(t, dict):
                n = t.get("name") or t.get("title")
                if isinstance(n, str) and n.strip():
                    tags.append(n.strip())
        tags = sorted(set(tags))
        image_field = event_jsonld.get("image")
        media_urls: list[str] = []
        if isinstance(image_field, str):
            media_urls = [image_field]
        elif isinstance(image_field, list):
            media_urls = [x for x in image_field if isinstance(x, str)]
        elif isinstance(image_field, dict):
            iu = image_field.get("url")
            if isinstance(iu, str):
                media_urls = [iu]
        if not media_urls:
            media_urls = list(dict.fromkeys(_collect_image_urls(ev)))
        # Live photo gallery (often many images per event)
        live_urls = _live_photo_large_urls(data)
        media_urls = list(dict.fromkeys([*media_urls, *live_urls]))

        info = body.get("eventInfoContainer") if isinstance(body.get("eventInfoContainer"), dict) else {}
        lc = info.get("lineupContainer") if isinstance(info.get("lineupContainer"), dict) else {}
        lineup_items = lc.get("lineupItems")
        # 1) Organizer first (JSON-LD Organization), 2) performers from lineup
        event_roles = _roles_from_jsonld_organizer(event_jsonld.get("organizer"))
        event_roles.extend(_lineup_performer_roles(lineup_items))
        promoter_name = str(ev.get("promoter") or ev.get("sponsor") or "")
        if not any(r.get("organizer") for r in event_roles) and promoter_name.strip():
            event_roles.insert(0, {"organizer": promoter_name.strip()})
        if not any(r.get("performer") for r in event_roles):
            for n in performer_names:
                event_roles.append({"performer": n})

        if not promoter_name.strip():
            for r in event_roles:
                o = r.get("organizer")
                if o:
                    promoter_name = str(o).strip()
                    break

        if not _venue_address_in_target_markets(venue_address):
            return None

        # Build a minimal EventItem-like object and reuse schema mapper.
        flat = EventItem(
            event_id=event_id,
            url=url,
            event_name=title,
            artist_name=artist_name,
            datetime=dt_value,
            end_datetime=end_dt_value,
            description=description,
            venue_name=venue_name,
            venue_location=venue_address,
            venue_latitude=venue_lat,
            venue_longitude=venue_lng,
            cost=cost,
            availability=availability,
            promoter=promoter_name,
            status=status or "",
            event_roles=event_roles,
            purchase_url=purchase_url or "",
            tags=tags,
            media_urls=media_urls,
            category="",
            target_demographic="",
        )
        return self._to_schema_item(flat)

    def _to_schema_item(self, ev: EventItem) -> BandsintownEventDetailsItem:
        event_id = (ev.get("event_id") or "").strip()
        url = (ev.get("url") or "").strip()
        title = (ev.get("event_name") or "").strip()
        artist_name = (ev.get("artist_name") or "").strip()
        promoter = (ev.get("promoter") or "").strip()
        dt_value = (ev.get("datetime") or "").strip()
        end_dt_value = (ev.get("end_datetime") or "").strip()
        venue_name = (ev.get("venue_name") or "").strip()
        venue_address = (ev.get("venue_location") or "").strip()
        venue_lat = ev.get("venue_latitude")
        venue_lng = ev.get("venue_longitude")
        description = (ev.get("description") or "").strip()
        raw_status = (ev.get("status") or "").strip().lower()
        purchase_url = (ev.get("purchase_url") or "").strip() or None
        tags = ev.get("tags") or []
        media_urls = ev.get("media_urls") or []

        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

        
        address_key = venue_address.lower()
        if "atlanta" in address_key:
            site_id = 85
        elif "austin" in address_key:
            site_id = 269
        elif "orlando" in address_key:
            site_id = None
        elif "nashville" in address_key:
            site_id = None
        else:
            site_id = None

        near_by = compute_near_by(venue_address, self._zip_dma_map)

        # availability enum (schema) — keep value if already mapped (e.g. from ticket UI)
        av_in = ev.get("availability") or ""
        if isinstance(av_in, str) and av_in.strip() in _SCHEMA_AVAIL_VALUES:
            availability = av_in.strip()
        else:
            availability = _parse_availability(str(av_in))
        if raw_status in ("cancelled", "canceled"):
            status = "cancelled"
        elif raw_status in ("postponed",):
            status = "postponed"
        elif raw_status in ("scheduled", "upcoming", "active"):
            status = "scheduled"
        else:
            status = None

        # pricing from cost string
        cost_s = (ev.get("cost") or "").strip()
        if not cost_s or cost_s.lower() == "unknown":
            pricing = {"kind": "tbd", "currency": None, "minAmount": None, "maxAmount": None}
        elif cost_s.lower() == "free":
            pricing = {"kind": "free", "currency": None, "minAmount": None, "maxAmount": None}
        else:
            currency = cost_s[:3] if re.match(r"^[A-Z]{3}\b", cost_s) else None
            nums = [float(m.group(1)) for m in _COST_NUMBER_RE.finditer(cost_s)]
            if not nums:
                pricing = {"kind": "tbd", "currency": None, "minAmount": None, "maxAmount": None}
            else:
                lo, hi = min(nums), max(nums)
                pricing = {
                    "kind": "point" if lo == hi else "span",
                    "currency": currency,
                    "minAmount": lo,
                    "maxAmount": hi,
                }

        # eventRoles — from window data (organizer + lineup performers); else promoter as organizer, artist as performer
        roles = ev.get("event_roles") or []
        if not isinstance(roles, list):
            roles = []
        if not roles:
            p = (promoter or "").strip()
            if p:
                roles.append({"organizer": p})
            a = (artist_name or "").strip()
            if a:
                roles.append({"performer": a})
        event_roles = roles

        # eventSchedule — dateTime start/end normalized to UTC (…Z); date-only unchanged.
        # Omit eventSchedule.end entirely when there is no valid end value.
        if dt_value:
            start_date_only = _is_date_only(dt_value)
            end_raw = (end_dt_value or "").strip()
            if start_date_only:
                # Date-only span only when both ends are calendar dates and end differs.
                end_date_ok = bool(end_raw and _is_date_only(end_raw))
                has_end = bool(end_date_ok and end_raw != dt_value.strip())
                schedule = {
                    "kind": "span" if has_end else "point",
                    "precision": "date",
                    "allDay": True,
                    "start": {"type": "date", "value": dt_value},
                }
                if has_end:
                    schedule["end"] = {"type": "date", "value": end_raw}
            else:
                start_out = _to_utc_iso8601_z(dt_value) or dt_value
                # JSON-LD often sets endDate to a calendar date only — not a real end time for a timed event.
                use_end_raw = bool(
                    end_raw
                    and end_raw != dt_value.strip()
                    and not _is_date_only(end_raw)
                )
                end_out: str | None = None
                if use_end_raw:
                    end_out = _to_utc_iso8601_z(end_raw) or end_raw
                has_end = bool(end_out and end_out != start_out)
                if not has_end:
                    end_out = None
                schedule = {
                    "kind": "span" if has_end else "point",
                    "precision": "dateTime",
                    "allDay": False,
                    "start": {"type": "dateTime", "value": start_out},
                }
                if has_end:
                    schedule["end"] = {"type": "dateTime", "value": end_out}
        else:
            schedule = {"kind": "tbd"}

        record_id = triple_id("id", PROVIDER, event_id or url, url or event_id)
        group_id = triple_id("group", PROVIDER, artist_name or title) if (artist_name or title) else None
        media = []
        for i, murl in enumerate([m for m in media_urls if isinstance(m, str) and m.strip()], start=1):
            seq = f"{i:03d}"
            img_id = _image_id_from_url(murl) or seq
            media.append(
                {
                    "id": f"media-{seq}",
                    "type": "image",
                    "title": title or artist_name or None,
                    "shortDescription": None,
                    "source": {
                        "name": "Bandsintown",
                        "id": img_id,
                        "url": murl,
                    },
                    "thumbnail": {
                        "url": murl,
                        "width": None,
                        "height": None,
                    },
                    "variants": [],
                }
            )

        details = {
            "provider": PROVIDER,
            "module": "events",
            "groupId": group_id,
            "id": record_id,
            "createdAt": now,
            "updatedAt": now,
            "title": title or artist_name,
            "source": SOURCE,
            "recordSource": {
                "id": event_id or None,
                "url": url or None,
            },
            "location": {
                "name": venue_name or None,
                "address": venue_address or None,
                "latitude": venue_lat,
                "longitude": venue_lng,
                "nearBy": near_by,
            },
            "siteId": site_id,
            "metadata": {
                "event": {
                    "description": description or None,
                    "eventRoles": event_roles,
                    "eventSchedule": schedule,
                    "status": status,
                    "eventPricing": pricing,
                    "availability": availability,
                    "purchaseUrl": purchase_url,
                    "audiences": [],
                    "categories": [],
                    "tags": tags if isinstance(tags, list) else [],
                    "media": media,
                }
            },
        }

        return BandsintownEventDetailsItem(**details)


    @staticmethod
    def _to_float(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ── error handler ─────────────────────────────────────────────────────────

    def errback(self, failure):
        url = failure.request.meta.get("event_url", failure.request.url)
        retry_attempt = int(failure.request.meta.get("retry_attempt", 0))
        self.logger.error("Failed to fetch %s (attempt %s): %s", url, retry_attempt, failure.value)
        retry_req = self._build_retry_request(failure.request, reason=f"request_failure:{failure.type.__name__}")
        if retry_req is not None:
            return retry_req
        self.stats_failed_urls += 1
        return None

    def closed(self, reason):
        self.logger.info(
            "Details crawl finished (reason=%s, queued=%s, success_items=%s, skipped_out_of_scope=%s, retries=%s, failed_urls=%s)",
            reason,
            self.stats_total_urls,
            self.stats_success_items,
            self.stats_skipped_out_of_scope,
            self.stats_retry_requests,
            self.stats_failed_urls,
        )
