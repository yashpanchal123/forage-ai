import scrapy
import json
import uuid
import csv
import html
import re
import os
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, Any


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_get(d: dict, *keys, default=None):
    """Safely navigate nested dict keys without raising KeyError."""
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key, default)
        if d is None:
            return default
    return d


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Computed once when the spider process starts — used for createdAt / updatedAt
RUN_TIME: str = now_iso()


def triple_id(*parts: str) -> str:
    u = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts))
    h = f"{u.int:032d}"
    return f"{int(h[0:4])}-{int(h[4:10])}-{int(h[10:18])}"


AVAILABILITY_MAP = {
    "https://schema.org/InStock":      "onSale",
    "https://schema.org/SoldOut":      "soldOut",
    "https://schema.org/PreOrder":     "onSale",
    "https://schema.org/Discontinued": "cancelled",
}


def image_url_dimensions(url: str) -> Optional[Tuple[int, int]]:
    """If *url* contains a WxH token (e.g. 800x600), return (width, height); else None."""
    if not url:
        return None
    # Match `800x600` even when embedded in filenames like `events1_800x600.jpg`.
    # `\b` doesn't work well with underscores because `_` is considered a word character.
    m = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", url)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def image_url_id_candidate(url: str) -> Optional[str]:
    """
    Extract a stable image id from Patch image URLs.

    Prefer the numeric id segment after `___` when present (e.g. `...___23143934901.jpg`).
    Otherwise fall back to the last `_` token before the extension.
    """
    if not url:
        return None

    # Common Patch pattern: `...___<digits>.<ext>`
    m = re.search(r"___([0-9]+)(?:\.[A-Za-z0-9]+)?$", url)
    if m:
        return m.group(1)

    # Fallback: last `_` segment before extension
    filename = url.split("/")[-1]
    base = re.sub(r"\.[A-Za-z0-9]+$", "", filename)
    parts = base.split("_")
    if not parts:
        return None
    cand = parts[-1].strip()
    return cand or None


# ── Site ID resolver ──────────────────────────────────────────────────────────

def get_site_id(address: str):
    """
    Return the siteId based on the event address:
        Atlanta, GA  → 85
        Austin, TX   → 269
        Orlando, FL  → None
        Nashville, TN→ None
        (default)    → None
    """
    addr_lower = address.lower()
    if "atlanta" in addr_lower or ", ga" in addr_lower:
        return 85
    if "austin" in addr_lower or ", tx" in addr_lower:
        return 269
    return None


# ── ZIP → DMA lookup table ────────────────────────────────────────────────────

_logger = logging.getLogger(__name__)

# Match rules: DMA name contains keyword (case-insensitive) and ST ABV matches.
# Substring match is intentional so e.g. "ORLANDO-DAYTONA BCH-MELBRN" matches ORLANDO.
_TARGET_DMA_KEYWORDS = (
    ("ATLANTA", "GA"),
    ("AUSTIN", "TX"),
    ("ORLANDO", "FL"),
    ("NASHVILLE", "TN"),
)

_FALLBACK_LOCATION_TOKENS = (
    "ATLANTA, GA",
    "AUSTIN, TX",
    "ORLANDO, FL",
    "NASHVILLE, TN",
)


def _norm_dma_name(val) -> str:
    if val is None:
        return ""
    s = str(val).strip().upper()
    # Normalize common unicode dashes to ASCII hyphen and collapse whitespace.
    s = s.replace("\u2010", "-").replace("\u2011", "-").replace("\u2012", "-").replace("\u2013", "-").replace("\u2014", "-")
    return " ".join(s.split())


def _zip_from_cell(val) -> str:
    """Normalize ZIP CODE cell (str, int, or float) to a 5-digit string."""
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return str(int(val)).zfill(5)
    s = str(val).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s.zfill(5)
    return ""


def _normalize_zip_cell(value) -> Optional[str]:
    """
    Normalize a ZIP cell to a 5-digit ZIP string.

    Handles ints/floats, "12345.0", "12345-6789", or text containing digits.
    """
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

    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", s)
    if m:
        return m.group(1)

    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 5:
        return digits[:5]
    return None


def build_zip_lookup(csv_path: str) -> dict:
    """
    Build zip (5 chars) -> list[(market, state)] from Tegna pilot CSV.

    Expected headers:
      - Zip
      - Market
      - State
    """
    lookup: Dict[str, list[Tuple[str, str]]] = {}
    try:
        with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = {str(h).strip().lower(): h for h in (reader.fieldnames or [])}
            zip_key = headers.get("zip")
            market_key = headers.get("market")
            state_key = headers.get("state")
            if not zip_key or not market_key or not state_key:
                _logger.warning(
                    "Could not detect Zip/Market/State columns in %s; nearBy will be false.",
                    csv_path,
                )
                return {}

            for row in reader:
                z = _normalize_zip_cell(row.get(zip_key))
                if not z:
                    continue
                market = _norm_dma_name(row.get(market_key))
                state = str(row.get(state_key) or "").strip().upper()
                if len(state) > 2:
                    state = state[:2]
                if not market or not state:
                    continue
                lookup.setdefault(z, []).append((market, state))
    except Exception as exc:
        _logger.warning("Failed opening pilot scope CSV at %s (%s); nearBy will be false.", csv_path, exc)
        return {}

    return lookup


def resolve_zip_dma_csv_path() -> Optional[str]:
    """
    Find the Tegna pilot scope CSV in common locations.

    Order:
    - `PATCH_ZIP_DMA_CSV` env var (explicit override)
    - alongside this spider (`.../spiders/`)
    - package/project parent directories (common repo layout)
    - ~/Downloads/
    """
    env_path = os.environ.get("PATCH_ZIP_DMA_CSV", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    spiders_dir = os.path.dirname(os.path.abspath(__file__))
    name = "Tegna - Pilot Scope Locations.csv"

    # Same folder as this file
    candidate = os.path.join(spiders_dir, name)
    if os.path.isfile(candidate):
        return candidate

    # One and two levels up are common in this repo (e.g. `patch_calendar/`)
    parent1 = os.path.dirname(spiders_dir)
    candidate = os.path.join(parent1, name)
    if os.path.isfile(candidate):
        return candidate

    parent2 = os.path.dirname(parent1)
    candidate = os.path.join(parent2, name)
    if os.path.isfile(candidate):
        return candidate

    downloads = os.path.join(os.path.expanduser("~"), "Downloads", name)
    if os.path.isfile(downloads):
        return downloads
    return None


_ZIP_DMA_CSV = None
ZIP_DMA_LOOKUP = None


def get_zip_dma_lookup() -> dict:
    """
    Lazy-load the ZIP→(DMA,ST) lookup once.

    This avoids silently freezing the lookup as empty when the XLSX path
    isn't resolvable at module import time (common in Scrapy runs).
    """
    global _ZIP_DMA_CSV, ZIP_DMA_LOOKUP
    if isinstance(ZIP_DMA_LOOKUP, dict):
        return ZIP_DMA_LOOKUP

    _ZIP_DMA_CSV = resolve_zip_dma_csv_path()
    if _ZIP_DMA_CSV:
        _logger.info("Loading ZIP/Market/State lookup from: %s", _ZIP_DMA_CSV)
    else:
        _logger.warning(
            "ZIP/Market/State CSV not found. nearBy may be false/None for physical events."
        )
    ZIP_DMA_LOOKUP = build_zip_lookup(_ZIP_DMA_CSV) if _ZIP_DMA_CSV else {}
    _logger.info("ZIP/Market/State lookup rows loaded: %d", len(ZIP_DMA_LOOKUP))
    return ZIP_DMA_LOOKUP
# ─────────────────────────────────────────────────────────────────────────────


def _extract_us_zip_from_address(address: str) -> Optional[str]:
    """
    Extract a US ZIP from an address string.

    Prefer patterns near the end / after state to avoid picking up street numbers.
    """
    if not address:
        return None
    text = str(address).strip()
    if not text:
        return None

    m = re.search(r"\b([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\b", text)
    if m:
        return m.group(2)
    m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", text)
    if m:
        return m.group(1)
    matches = list(re.finditer(r"\b(\d{5})(?:-\d{4})?\b", text))
    if matches:
        return matches[-1].group(1)
    m = re.search(r"(?<![0-9])(\d{5})(?![0-9])", text)
    return m.group(1) if m else None


def _dma_matches_target_market(dma_name: str, st_abv: str) -> bool:
    if not dma_name or not st_abv:
        return False
    st = re.sub(r"[^A-Za-z]", "", str(st_abv))[:2].upper()
    if len(st) != 2:
        st = str(st_abv).strip().upper()[:2]
    dma_upper = _norm_dma_name(dma_name)
    for keyword, state_key in _TARGET_DMA_KEYWORDS:
        if st != state_key:
            continue
        if keyword in dma_upper:
            return True
    return False


def _fallback_location_match(address_text: str) -> bool:
    text = (address_text or "").upper()
    return any(token in text for token in _FALLBACK_LOCATION_TOKENS)


def get_nearby(address_for_zip: str):
    """
    Determine the nearBy value for a physical event using ZIP from address text.

    Returns:
        True   — ZIP maps to one of the 4 target DMA + state pairs in the workbook.
        False  — ZIP found in text but not in target DMA set (or lookup table empty).
        None   — No ZIP could be extracted.
    """
    zip_code = _extract_us_zip_from_address(address_for_zip or "")
    if not zip_code:
        return True if _fallback_location_match(address_for_zip or "") else None

    lookup = get_zip_dma_lookup()
    if not lookup:
        return True if _fallback_location_match(address_for_zip or "") else False

    hits = lookup.get(zip_code)
    if not hits:
        return True if _fallback_location_match(address_for_zip or "") else False

    for dma_name, st_abv in hits:
        if _dma_matches_target_market(dma_name, st_abv):
            return True

    return True if _fallback_location_match(address_for_zip or "") else False


def replace_empty_strings_with_none(obj):
    """
    Recursively replace empty strings ("") with None for JSON output.

    This keeps lists/dicts intact while removing empty-string leaf values.
    """
    if isinstance(obj, str):
        return None if obj.strip() == "" else obj
    if isinstance(obj, list):
        return [replace_empty_strings_with_none(v) for v in obj]
    if isinstance(obj, dict):
        return {k: replace_empty_strings_with_none(v) for k, v in obj.items()}
    return obj


# ── Spider ────────────────────────────────────────────────────────────────────

class EventDetailsSpider(scrapy.Spider):
    """
    Spider 2 – Event Details
    ========================
    Reads event URLs from event_urls.csv and scrapes each event page,
    outputting structured JSON matching the forage format.

    Run:
        scrapy crawl event_details
        scrapy crawl event_details -a csv_file=my_urls.csv
    """

    name = "event_details"
    allowed_domains = ["patch.com"]

    # ── Init ──────────────────────────────────────────────────────────────────

    def __init__(self, csv_file: str = "event_urls.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.csv_file = csv_file
        self.processed_count = 0

    def closed(self, reason):
        self.logger.info(
            "Spider finished (reason=%s). Total events processed: %d",
            reason,
            self.processed_count,
        )

    # ── Entry point ───────────────────────────────────────────────────────────

    def start_requests(self):
        self.logger.info("Starting event scrape using CSV: %s", self.csv_file)
        try:
            with open(self.csv_file, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except FileNotFoundError:
            self.logger.error(f"CSV not found: {self.csv_file}")
            return

        self.logger.info(f"Loaded {len(rows)} URLs from {self.csv_file}")

        for row in rows:
            url  = row.get("event_url", "").strip()
            city = row.get("city", "").strip()
            if not url:
                continue
            yield scrapy.Request(
                url=url,
                callback=self.parse_event,
                errback=self.handle_error,
                cb_kwargs={"city": city},
            )

    # ── Parse one event page ──────────────────────────────────────────────────

    def parse_event(self, response, city: str = ""):
        url = response.url
        self.logger.info("Processing event URL: %s", url)

        # ── ld+json ──
        ld_text = "".join(response.xpath('//script[@type="application/ld+json"]/text()').getall())
        try:
            ld = json.loads(ld_text)
        except Exception:
            ld = {}

        # ── __NEXT_DATA__ ──
        next_text = "".join(response.xpath('//script[@id="__NEXT_DATA__"]/text()').getall())
        try:
            next_data = json.loads(next_text)
        except Exception:
            next_data = {}

        main = safe_get(next_data, "props", "pageProps", "mainContent", "item", default={})

        # ── Basic fields ──
        event_name  = html.unescape(ld.get("name") or safe_get(main, "title", default=""))
        description = " ".join(response.xpath("//div[contains(@class,'HTMLContent')]//text()").getall()).strip()

        # ── Organizer ──
        organizer_name = safe_get(ld, "organizer", "name", default="")
        organizer_url  = safe_get(ld, "organizer", "url",  default="")

        event_site_url = safe_get(main, "eventSiteUrl", default="")

        # Priority: organizer_url (if has 'tickets') → event_site_url → organizer_url → None
        if organizer_url and 'tickets' in organizer_url:
            purchase_url = organizer_url
        elif event_site_url:
            purchase_url = event_site_url
        elif organizer_url:
            purchase_url = organizer_url
        else:
            purchase_url = None

        # ── Schedule ──
        start_date = ld.get("startDate") or safe_get(main, "startDate", default="")
        end_date   = ld.get("endDate")   or safe_get(main, "endDate",   default="")
        if not start_date:
            kind = "tbd"
        elif end_date:
            kind = "span"
        else:
            kind = "point"

        # ── Coordinates ──
        addr_block  = safe_get(main, "address", default={})
        patch_block = safe_get(main, "patch",   default={})

        latitude  = (addr_block.get("latitude")  or patch_block.get("latitude")
                     or safe_get(ld, "location", "geo", "latitude"))
        longitude = (addr_block.get("longitude") or patch_block.get("longitude")
                     or safe_get(ld, "location", "geo", "longitude"))

        # ── Address string ──
        address_text = " ".join(
            response.xpath("//address[contains(@class,'Address')]/text()").getall()
        ).strip()
        if not address_text:
            loc = safe_get(ld, "location", "address", default={})
            address_text = " ".join(filter(None, [
                loc.get("streetAddress", ""),
                loc.get("addressLocality", ""),
                loc.get("addressRegion", ""),
                loc.get("postalCode", ""),
            ])).strip()

        ld_location_name = (safe_get(ld, "location", "name", default="") or "").strip()
        # Online only per schema location.name (not inferred from street address).
        is_online = "online" in ld_location_name.lower()

        # ── Online vs Physical location ───────────────────────────────────────
        if is_online:
            location_name = "Online Event"
            address_text = None
            latitude = None
            longitude = None
            near_by = None
        else:
            location_name = ld_location_name or address_text
            ld_loc_addr = safe_get(ld, "location", "address", default={}) or {}
            ld_postal = str(ld_loc_addr.get("postalCode") or "").strip()
            address_for_zip = (address_text or "").strip()
            if ld_postal and ld_postal not in re.sub(r"\s+", "", address_for_zip):
                address_for_zip = f"{address_for_zip} {ld_postal}".strip()
            near_by = get_nearby(address_for_zip)

        # If the venue name is effectively the same as the address,
        # prefer an empty `location.name` (the downstream formatter can use `address`).
        if isinstance(location_name, str) and isinstance(address_text, str):
            norm_name = " ".join(location_name.split()).strip().lower()
            norm_addr = " ".join(address_text.split()).strip().lower()
            if norm_name and norm_name == norm_addr:
                location_name = ""

        # If both location.name and location.address are empty/null, coordinates must be null.
        if not location_name and not address_text:
            latitude = None
            longitude = None
        # ─────────────────────────────────────────────────────────────────────

        # ── Pricing ──
        offers = ld.get("offers") or {}
        if isinstance(offers, list):
            prices    = [o.get("price") for o in offers if o.get("price") is not None]
            min_price = min(prices) if prices else None
            max_price = max(prices) if prices else None
            currency  = offers[0].get("priceCurrency", "USD") if offers else None
            avail_raw = offers[0].get("availability", "")     if offers else ""
        else:
            min_price = max_price = offers.get("price")
            currency  = offers.get("priceCurrency", "USD")
            avail_raw = offers.get("availability", "")

        availability = AVAILABILITY_MAP.get(avail_raw, "onSale")

        # ── Pricing kind ──────────────────────────────────────────────────────
        min_amount = float(min_price) if min_price is not None else None
        max_amount = float(max_price) if max_price is not None else None
        if min_amount is None and max_amount is None:
            pricing_kind = "tbd"
            currency     = None
        elif min_amount == max_amount:
            pricing_kind = "point"
        else:
            pricing_kind = "span"

        # ── Image ──
        image_url = ld.get("image") or ""
        if isinstance(image_url, list):
            image_url = image_url[0] if image_url else ""
        dims = image_url_dimensions(image_url)
        if dims:
            thumb_w, thumb_h = dims

            # If the only "id" embedded in the filename is the WxH token itself
            # (e.g. `events1_800x600.jpg`), then source.id must be null.
            # Otherwise, keep the extracted numeric id when available.
            cand = image_url_id_candidate(image_url)
            dim_token = f"{thumb_w}x{thumb_h}"
            image_id = None if cand == dim_token else cand
        else:
            image_id = image_url_id_candidate(image_url)
            thumb_w, thumb_h = None, None

        media = []
        if image_url:
            media.append({
                "id":               "media-001",
                "type":             "image",
                "title":            event_name,
                "shortDescription": "",
                "source":           {"name": "Patch", "id": image_id, "url": image_url},
                "thumbnail":        {"url": image_url, "width": thumb_w, "height": thumb_h},
                "variants":[],
            })

        # ── Tags ──
        keywords = ld.get("keywords") or ""
        tags = [k.strip() for k in keywords.split(",")] if keywords else []

        # ── Record source ID (UUID segment from URL) ──
        record_id = url.rstrip("/").split("/")[-2]

        PROVIDER = "patch"

        # ── Resolve siteId based on address ──────────────────────────────────
        site_id = get_site_id(address_text or city)

        # ── Build eventSchedule ───────────────────────────────────────────────
        # "end" is always present with the nested object structure.
        # The "value" inside is null when there is no end date (point / tbd),
        # and the actual dateTime string when there is one (span).
        event_schedule = {
            "kind":      kind,
            "precision": "dateTime",
            "allDay":    False,
            "start":     {"type": "dateTime", "value": start_date},
            "end":       {"type": "dateTime", "value": end_date if end_date else None},
        }
        # ─────────────────────────────────────────────────────────────────────

        # ── Build final output ────────────────────────────────────────────────
        now = RUN_TIME
        out = {
            "provider":  "forage",
            "module":    "events",
            "groupId":   triple_id("group", PROVIDER, record_id, url),
            "id":        triple_id("id", PROVIDER, record_id, url),
            "createdAt": now,
            "updatedAt": now,
            "title":     event_name,
            "source": {
                "name": "Patch",
                "id":   "patch",
                "url":  "https://patch.com",
            },
            "recordSource": {
                "id":  record_id,
                "url": url,
            },
            "location": {
                "name":      location_name,
                "address":   address_text,
                "latitude":  float(latitude)  if latitude  else None,
                "longitude": float(longitude) if longitude else None,
                "nearBy":    near_by,
            },
            "siteId": site_id,
            "metadata": {
                "event": {
                    "description":   description,
                    "eventRoles":    [{"type": "organizer", "name": organizer_name}] if organizer_name else [],
                    "eventSchedule": event_schedule,
                    "status": "scheduled",
                    "eventPricing": {
                        "kind":      pricing_kind,
                        "currency":  currency,
                        "minAmount": min_amount,
                        "maxAmount": max_amount,
                    },
                    "availability": availability,
                    "purchaseUrl":  purchase_url,
                    "audiences":    [],
                    "categories":   [],
                    "tags":         tags,
                    "media":        media,
                },
            },
        }

        yield replace_empty_strings_with_none(out)

        self.processed_count += 1
        self.logger.info(f"✓ {event_name}")

    # ── Error handler ─────────────────────────────────────────────────────────

    def handle_error(self, failure):
        self.logger.error(f"✗ Request failed: {failure.request.url} — {failure.value}")
