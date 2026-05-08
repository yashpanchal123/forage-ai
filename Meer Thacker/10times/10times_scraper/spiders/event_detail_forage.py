import csv
import html as html_module
import json
import re
import uuid
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import scrapy
from zoneinfo import ZoneInfo


PROVIDER = "10times"
STATIC_SOURCE: Dict[str, str] = {
    "name": "10times",
    "id": "10times",
    "url": "https://10times.com",
}
_SKIP_PATTERNS = ("/usa/", "/atlanta-us/", "/austin-us/")
_NEARBY_CSV_PATH = Path(
    r"C:\Users\Dell\OneDrive\Desktop\forageAI\10times\Tegna - Pilot Scope Locations.csv"
)
if not _NEARBY_CSV_PATH.is_file():
    _NEARBY_CSV_PATH = Path(__file__).resolve().parents[2] / "Tegna - Pilot Scope Locations.csv"
_EVENT_LOCAL_TZ = ZoneInfo("America/New_York")
_STATUS_MAP = {
    "eventscheduled": "scheduled",
    "scheduled": "scheduled",
    "eventpostponed": "postponed",
    "postponed": "postponed",
    "eventcancelled": "cancelled",
    "cancelled": "cancelled",
    "eventrescheduled": "postponed",
    "past": "cancelled",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def triple_id(*parts: str) -> str:
    u = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts))
    h = f"{u.int:032d}"
    return f"{int(h[0:4])}-{int(h[4:10])}-{int(h[10:18])}"


def _normalize_schema_value(val: Any, join_lists: bool = False) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        return s or None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return str(val)
    if isinstance(val, dict):
        for key in ("@value", "@id", "startDate", "endDate", "description", "name", "text", "url"):
            if key in val:
                return _normalize_schema_value(val[key], join_lists=join_lists)
        return None
    if isinstance(val, list):
        acc: List[str] = []
        for x in val:
            n = _normalize_schema_value(x, join_lists=join_lists)
            if n:
                if not join_lists:
                    return n
                acc.append(n)
        return " ".join(acc) if (join_lists and acc) else None
    return None


def replace_empty_strings_with_none(obj: Any) -> Any:
    if isinstance(obj, str):
        return None if obj.strip() == "" else obj
    if isinstance(obj, list):
        return [replace_empty_strings_with_none(v) for v in obj]
    if isinstance(obj, dict):
        return {k: replace_empty_strings_with_none(v) for k, v in obj.items()}
    return obj


def _walk_ld_nodes(data: Any):
    if isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            for item in data["@graph"]:
                yield from _walk_ld_nodes(item)
        else:
            yield data
    elif isinstance(data, list):
        for item in data:
            yield from _walk_ld_nodes(item)


def _is_event_type(atype: Any) -> bool:
    event_type_tails = {
        "Event",
        "BusinessEvent",
        "SocialEvent",
        "MusicEvent",
        "EducationEvent",
        "Festival",
        "TheaterEvent",
        "SportsEvent",
        "SaleEvent",
        "TradeEvent",
    }
    if isinstance(atype, str):
        tail = atype.strip().split("/")[-1]
        return tail in event_type_tails
    if isinstance(atype, list):
        return any(_is_event_type(x) for x in atype)
    return False


def _event_score(node: dict) -> int:
    score = 0
    if _normalize_schema_value(node.get("startDate")):
        score += 5
    if _normalize_schema_value(node.get("endDate")):
        score += 3
    if node.get("location"):
        score += 2
    if node.get("image"):
        score += 2
    if node.get("description"):
        score += 1
    if node.get("name"):
        score += 2
    if node.get("url"):
        score += 1
    return score


def _find_event_status_in_obj(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_norm = str(k).replace(" ", "").replace("_", "").lower()
            if key_norm == "eventstatus":
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if v is not None and not isinstance(v, (dict, list)):
                    return str(v).strip()
            found = _find_event_status_in_obj(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_event_status_in_obj(item)
            if found:
                return found
    return None


def extract_event_status_from_script(response: scrapy.http.Response) -> Optional[str]:
    """
    JSON text from script nodes that mention 'Event Status'.
    XPath: //script[contains(text(),'Event Status')]/text()
    """
    for raw in response.xpath("//script[contains(text(),'Event Status')]/text()").getall():
        text = (raw or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
            status = _find_event_status_in_obj(data)
            if status:
                return status
        except json.JSONDecodeError:
            pass
        m = re.search(r'"Event Status"\s*:\s*"([^"]*)"', text, re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
        m = re.search(r"'Event Status'\s*:\s*'([^']*)'", text, re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
        m = re.search(r'"eventStatus"\s*:\s*"([^"]*)"', text, re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def extract_event_json(response: scrapy.http.Response) -> dict:
    def _cleanup_json_text(txt: str) -> str:
        t = (txt or "").strip()
        if t.startswith("<!--"):
            t = t.removeprefix("<!--").removesuffix("-->").strip()
        if t.startswith("<![CDATA["):
            t = t.removeprefix("<![CDATA[").removesuffix("]]>").strip()
        if t.endswith(";"):
            t = t[:-1].strip()
        return t

    event_nodes: List[dict] = []
    fallback_nodes: List[dict] = []

    raw_scripts = response.xpath('//script[@type="application/ld+json"]/text()').getall()
    for raw in raw_scripts:
        cleaned = _cleanup_json_text(raw)
        if not cleaned:
            continue
        # Some pages HTML-escape JSON-LD content inside the script tag.
        cleaned = html_module.unescape(cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Try common fixes for slightly-invalid JSON-LD blocks
            cleaned2 = cleaned
            # Remove ASCII control chars except common whitespace
            cleaned2 = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned2)
            # Remove trailing commas before } or ]
            cleaned2 = re.sub(r",(\s*[}\]])", r"\1", cleaned2)
            try:
                data = json.loads(cleaned2)
            except json.JSONDecodeError:
                continue

        # Sometimes the parsed value is itself a JSON string containing JSON.
        if isinstance(data, str):
            inner = data.strip()
            if inner and inner[0] in "{[":
                try:
                    data = json.loads(inner)
                except json.JSONDecodeError:
                    pass
        for node in _walk_ld_nodes(data):
            if not isinstance(node, dict):
                continue
            # Strong signal: has startDate and a title/url, even if @type is odd/missing.
            if node.get("startDate") and (node.get("name") or node.get("url")):
                event_nodes.append(node)
                continue
            if _is_event_type(node.get("@type")):
                fallback_nodes.append(node)

    if event_nodes:
        return max(event_nodes, key=_event_score)
    if fallback_nodes:
        return max(fallback_nodes, key=_event_score)

    # Last-resort: some ld+json blocks are not valid JSON but still contain event keys.
    for raw in raw_scripts:
        text = html_module.unescape(_cleanup_json_text(raw))
        if not text:
            continue
        if "startDate" not in text or "name" not in text:
            continue
        m_name = re.search(r'"name"\s*:\s*"([^"]+)"', text)
        m_url = re.search(r'"url"\s*:\s*"([^"]+)"', text)
        m_start = re.search(r'"startDate"\s*:\s*"([^"]+)"', text)
        m_end = re.search(r'"endDate"\s*:\s*"([^"]+)"', text)
        if m_start and (m_name or m_url):
            return {
                "@type": "Event",
                "name": m_name.group(1) if m_name else None,
                "url": m_url.group(1) if m_url else None,
                "startDate": m_start.group(1),
                "endDate": m_end.group(1) if m_end else None,
            }

    # Debug hint: JSON-LD exists but no Event detected
    if raw_scripts:
        response.request.meta.setdefault("_had_ldjson", True)
    return {}


def _organizer_name_from_malformed_ld_json(raw_text: str) -> Optional[str]:
    """
    When JSON-LD fails json.loads() but still contains an organizer Organization block,
    extract the name (e.g. invalid UTF-8 / broken JSON in the same script).
    """
    text = html_module.unescape((raw_text or "").strip())
    if not text or '"organizer"' not in text:
        return None
    # Typical 10times shape: "organizer": { "@type": "Organization", "name": "..." , ... }
    for pat in (
        r'"organizer"\s*:\s*\{[^}]*?"name"\s*:\s*"((?:\\.|[^"\\])*)"',
        r'"organizer"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]*)"',
    ):
        m = re.search(pat, text, re.S)
        if m:
            try:
                name = json.loads('"' + m.group(1) + '"')
            except json.JSONDecodeError:
                name = m.group(1).replace(r"\"", '"')
            name = html_module.unescape((name or "").strip())
            if name:
                return re.sub(r"\s+", " ", name).strip() or None
    return None


def extract_organizer_and_event_roles(response: scrapy.http.Response, ld: dict) -> tuple[Optional[str], List[dict]]:
    organizer_name: Optional[str] = None
    role_names: List[str] = []

    def _collect_name(candidate: Any) -> Optional[str]:
        if isinstance(candidate, dict):
            name = _normalize_schema_value(candidate.get("name"), join_lists=True)
            if name:
                return html_module.unescape(name.strip())
            return None
        if isinstance(candidate, str):
            name = candidate.strip()
            return html_module.unescape(name) if name else None
        return None

    def _append_role(name: Optional[str]) -> None:
        nonlocal organizer_name
        if not name:
            return
        if not organizer_name:
            organizer_name = name
        if name not in role_names:
            role_names.append(name)

    # Standard schema.org on the chosen Event node (most reliable when JSON-LD parsed).
    _append_role(_collect_name(ld.get("organizer")))

    # Site-specific extension on the Event node (older pages).
    organizer_val = ld.get("https://10times.com/e1rf-2s8p-3h2d")
    if isinstance(organizer_val, list):
        for item in organizer_val:
            _append_role(_collect_name(item))
    else:
        _append_role(_collect_name(organizer_val))

    # Preferred source requested by user.
    org_scripts = response.xpath(
        "//script[@type='application/ld+json'][contains(text(),'Organization')]/text()"
    ).getall()
    for raw in org_scripts:
        text = html_module.unescape((raw or "").strip())
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        for node in _walk_ld_nodes(data):
            if not isinstance(node, dict):
                continue
            if not _is_event_type(node.get("@type")) and "organizer" not in node:
                node_type = node.get("@type")
                if isinstance(node_type, str):
                    if "Organization" not in node_type:
                        continue
                elif isinstance(node_type, list):
                    if not any(isinstance(x, str) and "Organization" in x for x in node_type):
                        continue
                else:
                    continue
            organizer = node.get("organizer", node)
            if isinstance(organizer, list):
                for item in organizer:
                    _append_role(_collect_name(item))
            else:
                _append_role(_collect_name(organizer))

    # Fallback: scan all JSON-LD blocks for organizer fields.
    if not role_names:
        for raw in response.xpath("//script[@type='application/ld+json']/text()").getall():
            text = html_module.unescape((raw or "").strip())
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for node in _walk_ld_nodes(data):
                if not isinstance(node, dict):
                    continue
                organizer = node.get("organizer")
                if isinstance(organizer, list):
                    for item in organizer:
                        _append_role(_collect_name(item))
                else:
                    _append_role(_collect_name(organizer))

    # Regex on raw JSON-LD text when the block is not valid JSON but still lists organizer.
    if not role_names:
        for raw in response.xpath("//script[@type='application/ld+json']/text()").getall():
            parsed = _organizer_name_from_malformed_ld_json(raw)
            if parsed:
                _append_role(parsed)
                break

    # DOM fallback: 10times uses multiple structures for organizer block.
    # Always attempt DOM extraction (not only when role_names is empty) so we can recover
    # from malformed/partial JSON-LD and still keep existing parsed names deduped.
    strong_first = (
        response.xpath('normalize-space(//*[@id="org-name"]//strong[1])').get()
        or response.xpath('normalize-space(//*[@id="org-name"])').get()
    )
    if strong_first:
        cleaned = re.sub(r"\s+", " ", html_module.unescape(strong_first.strip()))
        # Trim trailing "N Total Events" if bundled in same node.
        cleaned = re.sub(r"\s*\d+\s+Total Events\b.*$", "", cleaned, flags=re.I).strip()
        _append_role(cleaned)

    dom_organizers = response.xpath('//*[@id="organzr"]//*[@id="org-name"]//text()').getall()
    merged = re.sub(r"\s+", " ", "".join(html_module.unescape(t or "") for t in dom_organizers)).strip()
    if merged:
        merged = re.sub(r"\s*\d+\s+Total Events\b.*$", "", merged, flags=re.I).strip()
        _append_role(merged)

    # Extra fallback: company organizer link in organizer row.
    org_company = response.xpath(
        'normalize-space(//*[@id="organzr"]//a[contains(@href,"/company/")][1])'
    ).get()
    if org_company:
        _append_role(re.sub(r"\s+", " ", html_module.unescape(org_company.strip())))

    event_roles = [{"name": name, "type": "organizer"} for name in role_names]
    return organizer_name, event_roles


def parse_event_time(response: scrapy.http.Response) -> str:
    timing_candidates = [
        "".join(response.xpath("//h2[contains(text(),'Timings')]/parent::td/text()").getall()).strip(),
        "".join(response.xpath("//h2[contains(text(),'Timings')]/parent::td/div[1]/text()").getall()).strip(),
        " ".join(
            response.xpath(
                "//h2[contains(translate(normalize-space(.), 'TIMING', 'timing'), 'timing')]/parent::td//text()"
            ).getall()
        ).strip(),
        " ".join(
            response.xpath(
                "//th[contains(translate(normalize-space(.), 'TIMING', 'timing'), 'timing')]/following-sibling::td[1]//text()"
            ).getall()
        ).strip(),
        " ".join(
            response.xpath(
                "//div[contains(@id,'eventtime') or contains(@class,'event-time') or contains(@class,'event_timing')]//text()"
            ).getall()
        ).strip(),
    ]
    for candidate in timing_candidates:
        cleaned = re.sub(r"\s+", " ", candidate or "").strip()
        if cleaned:
            # Ignore label-only artifacts like "Timings" / "Timing".
            if re.fullmatch(r"timings?", cleaned, flags=re.I):
                continue
            # Keep only real time-like values.
            if not re.search(r"\d{1,2}:\d{2}\s*(?:AM|PM)?", cleaned, re.I):
                continue
            return cleaned

    # Some pages expose timing only inside JS blobs.
    body = response.text or ""
    script_patterns = (
        r'"(?:event\s*)?timings?"\s*:\s*"([^"]+)"',
        r'"eventTime"\s*:\s*"([^"]+)"',
        r"'(?:event\s*)?timings?'\s*:\s*'([^']+)'",
        r"'eventTime'\s*:\s*'([^']+)'",
    )
    for pat in script_patterns:
        m = re.search(pat, body, re.I)
        if m and m.group(1).strip():
            cleaned = re.sub(r"\s+", " ", html_module.unescape(m.group(1)).strip())
            if re.fullmatch(r"timings?", cleaned, flags=re.I):
                continue
            if not re.search(r"\d{1,2}:\d{2}\s*(?:AM|PM)?", cleaned, re.I):
                continue
            return cleaned
    return ""


def extract_location_fields(response: scrapy.http.Response) -> tuple[Optional[str], Optional[str]]:
    """
    Primary source: hidden inputs.
    Fallback source: Venue section block when hidden inputs are empty.
    """
    location_name = "".join(response.xpath('//input[@id="venueName"]/@value').getall()).strip() or None
    location_address = "".join(response.xpath('//input[@id="venue_address"]/@value').getall()).strip() or None

    if not location_name:
        location_name = (
            response.xpath(
                'normalize-space(//*[@id="venue_direction_block"]//a[contains(@href,"/venues/")][1])'
            ).get()
            or response.xpath(
                'normalize-space(//*[@id="venue_direction_block"]//div[contains(@class,"mb-1")]//a[contains(@href,"/venues/")][1])'
            ).get()
            or None
        )
        if isinstance(location_name, str):
            location_name = re.sub(r"\s+", " ", location_name).strip() or None

    if not location_address:
        venue_addr = (
            response.xpath(
                'normalize-space(//*[@id="venue_direction_block"]//a[contains(@href,"/venues/")][1]/ancestor::div[1]/following-sibling::p[1])'
            ).get()
            or response.xpath('normalize-space(//*[@id="venue_direction_block"]//p[contains(@class,"text-muted")][1])').get()
            or response.xpath('normalize-space(//*[@id="venue_direction_block"]//small[contains(.,",")][1])').get()
            or None
        )
        if isinstance(venue_addr, str):
            venue_addr = re.sub(r"\s+", " ", venue_addr).strip()
            location_address = venue_addr or None

    # Some events hide exact street details but still expose city/country in Venue block.
    if not location_address:
        parts = [
            re.sub(r"\s+", " ", (p or "")).strip(" ,")
            for p in response.xpath(
                '//*[@id="venue_direction_block"]//p[contains(@class,"text-muted")][1]//small/text()'
            ).getall()
        ]
        parts = [p for p in parts if p]
        if parts:
            location_address = ", ".join(parts)

    return location_name, location_address


def combine_date_time(date_value: Optional[str], event_time: Optional[str]) -> Optional[str]:
    if not date_value:
        return None
    if "T" in date_value or re.search(r"\d{1,2}:\d{2}", date_value):
        return date_value.strip()
    if event_time and event_time.strip():
        return f"{date_value.strip()} {event_time.strip()}"
    return date_value.strip()


def _extract_date_only(date_value: Optional[str]) -> Optional[str]:
    if not date_value:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", date_value)
    if m:
        return m.group(0)
    return date_value.strip()


def split_event_time_range(event_time: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Extract first time range from eventTime text.
    Example:
      "10:00 AM - 5:00 PM (Jul 21) ... 10:00 AM - 2:00 PM ..."
      -> ("10:00 AM", "5:00 PM")
    """
    if not event_time:
        return None, None
    m = re.search(
        r"(\d{1,2}:\d{2}\s*(?:AM|PM))\s*-\s*(\d{1,2}:\d{2}\s*(?:AM|PM))",
        event_time,
        re.I,
    )
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()


def build_start_end_from_start_date(start_raw: Optional[str], event_time: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    User rule:
    - Use start date part for both start/end values.
    - Use time before '-' for start, after '-' for end.
    """
    base_date = _extract_date_only(start_raw)
    if not base_date:
        return None, None
    t_start, t_end = split_event_time_range(event_time)
    if t_start and t_end:
        return f"{base_date} {t_start}", f"{base_date} {t_end}"
    return combine_date_time(base_date, event_time), combine_date_time(base_date, event_time)


def _to_utc_datetime_str(date_value: Optional[str], time_value: Optional[str]) -> Optional[str]:
    """
    Convert local event date/time (America/New_York) to UTC ISO string.
    Returns format: YYYY-MM-DDTHH:MM:SSZ
    """
    if not date_value or not time_value:
        return None
    try:
        dt_local = datetime.strptime(
            f"{date_value.strip()} {time_value.strip().upper()}",
            "%Y-%m-%d %I:%M %p",
        ).replace(tzinfo=_EVENT_LOCAL_TZ)
        return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def build_start_end_utc_from_start_date(start_raw: Optional[str], event_time: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    base_date = _extract_date_only(start_raw)
    if not base_date:
        return None, None
    t_start, t_end = split_event_time_range(event_time)
    start_utc = _to_utc_datetime_str(base_date, t_start)
    end_utc = _to_utc_datetime_str(base_date, t_end)
    if start_utc and end_utc:
        return start_utc, end_utc
    return build_start_end_from_start_date(start_raw, event_time)


def normalize_event_status(raw_value: Optional[str]) -> str:
    if not raw_value:
        return "scheduled"
    key = str(raw_value).replace("https://schema.org/", "").replace("http://schema.org/", "")
    key = re.sub(r"[^a-z]", "", key.lower())
    return _STATUS_MAP.get(key, "scheduled")


def to_iso_temporal(raw_value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Convert raw schema date/dateTime to:
    - temporal type: date | dateTime
    - value: RFC3339 for dateTime, YYYY-MM-DD for date
    """
    value = _normalize_schema_value(raw_value)
    if not value:
        return None, None
    txt = value.strip()

    # Date-only schema value.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
        return "date", txt

    dt = txt
    # Normalize trailing Z to explicit offset for parsing.
    if dt.endswith("Z"):
        dt = dt[:-1] + "+00:00"
    # Normalize timezone offsets like -0600 to -06:00.
    if re.search(r"[+-]\d{4}$", dt):
        dt = dt[:-5] + dt[-5:-2] + ":" + dt[-2:]

    try:
        parsed = datetime.fromisoformat(dt)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_EVENT_LOCAL_TZ)
        return "dateTime", parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass

    # Fallback for non-ISO date-like text.
    date_only = _extract_date_only(txt)
    if date_only and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_only):
        return "date", date_only
    return None, None


def build_event_schedule(start_raw: Optional[str], end_raw: Optional[str], event_time: Optional[str]) -> Dict[str, Any]:
    start_type, start_value = to_iso_temporal(start_raw)
    end_type, end_value = to_iso_temporal(end_raw)

    # Use on-page timing only when schema gives date-only values.
    if start_type == "date" and event_time:
        start_utc, end_utc = build_start_end_utc_from_start_date(start_raw, event_time)
        if start_utc:
            start_type, start_value = "dateTime", start_utc
        if end_utc and not end_raw:
            end_type, end_value = "dateTime", end_utc

    if end_type == "date" and event_time:
        _, end_utc = build_start_end_utc_from_start_date(end_raw, event_time)
        if end_utc:
            end_type, end_value = "dateTime", end_utc

    if not start_value and not end_value:
        return {"kind": "tbd"}

    kind = "span" if end_value else "point"
    precision = "dateTime" if (start_type == "dateTime" or end_type == "dateTime") else "date"
    schedule: Dict[str, Any] = {
        "kind": kind,
        "precision": precision,
        "allDay": precision == "date",
    }
    if start_value:
        schedule["start"] = {"type": start_type, "value": start_value}
    if end_value and kind == "span":
        schedule["end"] = {"type": end_type, "value": end_value}
    return schedule


def extract_coordinates(response: scrapy.http.Response, ld: dict) -> tuple[Optional[float], Optional[float]]:
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # Prefer backend lat/lng first to match source-of-truth values.
    geo_input = "".join(response.xpath('//input[@id="geoLatLng"]/@value').getall()).strip()
    if geo_input and "," in geo_input:
        parts = geo_input.split(",")
        try:
            latitude = float(parts[0].strip())
            longitude = float(parts[1].strip())
        except (TypeError, ValueError, IndexError):
            latitude, longitude = None, None

    if latitude is None or longitude is None:
        location = ld.get("location")
        if isinstance(location, dict):
            geo = location.get("geo")
            if isinstance(geo, dict):
                try:
                    latitude = float(geo.get("latitude")) if geo.get("latitude") is not None else None
                    longitude = float(geo.get("longitude")) if geo.get("longitude") is not None else None
                except (TypeError, ValueError):
                    latitude, longitude = None, None

    if latitude is None:
        lat_txt = (
            "".join(response.xpath('//span[@id="event_latitude"]/text()').getall()).strip()
            or "".join(response.xpath('//meta[@itemprop="latitude"]/@content').getall()).strip()
        )
        try:
            latitude = float(lat_txt) if lat_txt else None
        except (TypeError, ValueError):
            pass

    if longitude is None:
        lon_txt = (
            "".join(response.xpath('//span[@id="event_longitude"]/text()').getall()).strip()
            or "".join(response.xpath('//span[@id="event_longude"]/text()').getall()).strip()
            or "".join(response.xpath('//meta[@itemprop="longitude"]/@content').getall()).strip()
        )
        try:
            longitude = float(lon_txt) if lon_txt else None
        except (TypeError, ValueError):
            pass

    return latitude, longitude


def image_url_id_candidate(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"___([0-9]+)(?:\.[A-Za-z0-9]+)?$", url)
    if m:
        return m.group(1)
    filename = url.rstrip("/").split("/")[-1]
    base = re.sub(r"\.[A-Za-z0-9]+$", "", filename)
    parts = base.split("_")
    if not parts:
        return None
    cand = parts[-1].strip()
    return cand or None


def normalize_image_url(url: str) -> str:
    # Strip img resizing querystring to keep original high-resolution URL.
    return (url or "").strip().split("?", 1)[0]


def extract_dimensions_from_url(url: str) -> tuple[Optional[int], Optional[int]]:
    raw = (url or "").strip()
    m = re.search(r"[?&]imgeng=/w_(\d+)/h_(\d+)", raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _collect_image_urls(response: scrapy.http.Response) -> List[str]:
    urls: List[str] = []
    for extra in response.xpath(
        "//div[contains(@class,'photos_tumbnail')]//div//img/@data-url"
    ).getall():
        e = (extra or "").strip()
        if e.startswith("http"):
            urls.append(e)

    if not urls:
        for extra in response.xpath(
            '//meta[@property="og:image"]/@content'
        ).getall():
            e = (extra or "").strip()
            if e.startswith("http"):
                urls.append(e)

    deduped: List[str] = []
    seen = set()
    for u in urls:
        key = normalize_image_url(u)
        if u and key not in seen:
            seen.add(key)
            deduped.append(u)
    return deduped


def build_media(images: List[str], title: str) -> List[dict]:
    if not images:
        return []

    media: List[dict] = []
    for i, img_url in enumerate(images, start=1):
        if not isinstance(img_url, str) or not img_url.strip():
            continue
        url = normalize_image_url(img_url)
        width, height = extract_dimensions_from_url(img_url)
        media.append({
            "id": f"media-{i:03d}",
            "type": "image",
            "title": title,
            "shortDescription": None,
            "source": {
                "name": "10times",
                "id": image_url_id_candidate(url),
                "url": url,
            },
            "thumbnail": {
                "url": url,
                "width": width,
                "height": height,
            },
            "variants": [],
        })
    return media


def get_site_id(address: Optional[str]) -> Optional[int]:
    addr = (address or "").lower()
    if "atlanta" in addr and "ga" in addr:
        return 85
    if "austin" in addr and ("us" in addr or "tx" in addr):
        return 269
    return None


@lru_cache(maxsize=1)
def _load_nearby_zip_index() -> Dict[str, List[dict]]:
    index: Dict[str, List[dict]] = {}
    try:
        with open(_NEARBY_CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                z = (row.get("Zip") or row.get("ZIP CODE") or "").strip()
                if not z:
                    continue
                index.setdefault(z, []).append(
                    {
                        "market": (row.get("Market") or row.get("DMA NAME") or "").strip().upper(),
                        "state": (row.get("State") or row.get("ST ABV") or "").strip().upper(),
                    }
                )
    except FileNotFoundError:
        return {}
    return index


@lru_cache(maxsize=1)
def _load_pilot_zip_market_state() -> tuple[set[str], tuple[str, ...], set[str]]:
    """
    From Tegna Pilot Scope CSV: ZIP codes, Market (DMA) names, and State codes.
    Used to filter events: keep if ZIP or Market or State matches any row (when address exists).
    Markets are returned longest-first for safer substring / word-boundary matching.
    """
    zips: set[str] = set()
    markets: set[str] = set()
    states: set[str] = set()
    try:
        with open(_NEARBY_CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                z = (row.get("Zip") or row.get("ZIP CODE") or "").strip()
                if z.isdigit() and len(z) == 5:
                    zips.add(z)
                mkt = (row.get("Market") or row.get("DMA NAME") or "").strip().upper()
                if mkt:
                    markets.add(re.sub(r"\s+", " ", mkt))
                st = (row.get("State") or row.get("ST ABV") or "").strip().upper()
                if st and re.fullmatch(r"[A-Z]{2}", st):
                    states.add(st)
    except FileNotFoundError:
        return set(), tuple(), set()
    markets_sorted = tuple(sorted(markets, key=len, reverse=True))
    return zips, markets_sorted, states


def _extract_zip_from_address(address: Optional[str]) -> Optional[str]:
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", address or "")
    return m.group(1) if m else None


def _has_indian_six_digit_pin(address: Optional[str]) -> bool:
    """
    Detect Indian-style PIN code pattern in address text (6 digits).
    Used to skip non-US records based on user's rule.
    """
    return bool(re.search(r"\b\d{6}\b", address or ""))


def _extract_state_from_address(address: Optional[str]) -> Optional[str]:
    addr = (address or "").upper()
    m = re.search(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b", addr)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b", addr)
    if m:
        return m.group(1)
    m = re.search(r",\s*([A-Z]{2})\s*(?:,|$)", addr)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z]{2})\b(?:\s+\d{5}(?:-\d{4})?)?\b", addr)
    return m.group(1) if m else None


def _normalize_scope_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").upper()).strip()


def _extract_city_from_address(address: Optional[str]) -> Optional[str]:
    """
    Best-effort US-style city extraction:
    '<street>, <city>, <ST> <ZIP>, ...' -> city
    """
    addr = _normalize_scope_text(address)
    if not addr:
        return None
    m = re.search(r",\s*([^,]+?)\s*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", addr)
    if m:
        city = _normalize_scope_text(m.group(1))
        return city or None
    # Also support no-comma format: "... <CITY> <ST> <ZIP>"
    m = re.search(r"\b([A-Z][A-Z .'-]+?)\s+([A-Z]{2})\s+\d{5}(?:-\d{4})?\b", addr)
    if m:
        city = _normalize_scope_text(m.group(1))
        if city:
            # Trim potential street suffix accidentally captured before city.
            tokens = city.split()
            if len(tokens) >= 1:
                street_words = {
                    "ST", "STREET", "RD", "ROAD", "DR", "DRIVE", "AVE", "AVENUE",
                    "BLVD", "BOULEVARD", "LN", "LANE", "PKWY", "PARKWAY",
                    "HWY", "HIGHWAY", "CT", "COURT", "WAY", "PL", "PLACE",
                    "CIR", "CIRCLE", "TRAIL", "TRL",
                }
                cut = 0
                for i, tok in enumerate(tokens):
                    if tok in street_words:
                        cut = i + 1
                if cut < len(tokens):
                    city = " ".join(tokens[cut:])
            return city or None
    return None


_NEARBY_ALLOWED_LOCATIONS: tuple[tuple[str, str], ...] = (
    ("ATLANTA", "GA"),
    ("AUSTIN", "TX"),
    ("ORLANDO", "FL"),
    ("NASHVILLE", "TN"),
)


@lru_cache(maxsize=1)
def _load_nearby_scope_rows() -> tuple[tuple[str, str, str], ...]:
    """
    Load normalized (zip, market, state) rows from Tegna pilot scope CSV.
    Column mapping:
      - Zip
      - Market
      - State
    """
    rows: list[tuple[str, str, str]] = []
    try:
        with open(_NEARBY_CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                zip_code = (row.get("Zip") or "").strip()
                market = _normalize_scope_text(row.get("Market"))
                state_code = _normalize_scope_text(row.get("State"))
                rows.append((zip_code, market, state_code))
    except FileNotFoundError:
        return tuple()
    return tuple(rows)


def compute_nearby(address: Optional[str], city: Optional[str] = None, state: Optional[str] = None) -> bool:
    """
    nearby rules:
    1) Extract ZIP from address and match it against CSV Zip.
    2) When ZIP matches, validate Market (DMA) and State.
    3) If ZIP is unavailable or doesn't match, fallback to fixed locations:
       Atlanta, GA | Austin, TX | Orlando, FL | Nashville, TN.
    4) Otherwise False.
    """
    addr = _normalize_scope_text(address)
    market_input = _normalize_scope_text(city) or _extract_city_from_address(addr) or ""
    state_input = _normalize_scope_text(state) or _extract_state_from_address(addr) or ""
    zip_input = _extract_zip_from_address(addr) or ""

    scope_rows = _load_nearby_scope_rows()
    if zip_input:
        zip_rows = [(z, m, s) for z, m, s in scope_rows if z == zip_input]
        if zip_rows:
            # User expectation: if ZIP matches pilot file, nearBy should be true.
            return True
            for _, market, st in zip_rows:
                if (market, st) not in _NEARBY_ALLOWED_LOCATIONS:
                    continue
                if market_input == market and state_input == st:
                    return True

    # Fallback when ZIP is missing or ZIP doesn't validate to known market/state.
    for market, st in _NEARBY_ALLOWED_LOCATIONS:
        if f"{market}, {st}" in addr:
            return True
    if "ATLANTA, USA" in addr or "AUSTIN, USA" in addr:
        return True

    return False
 
 

def is_event_in_scope(
    address: Optional[str],
    city: Optional[str] = None,
    state: Optional[str] = None,
    location_name: Optional[str] = None,
) -> bool:
    """
    Pilot scope (Tegna CSV: Zip, Market, State):
    - If venue address is missing/empty (null): always include the event.
    - If address is present: include only when ZIP OR Market OR State matches
      any row in the pilot file.
    """
    _ = city  # Reserved; pilot filter uses venue address / name vs Tegna Zip, Market, State only.

    addr_raw = (address or "").strip()
    if not addr_raw:
        return True

    addr_u = re.sub(r"\s+", " ", addr_raw.upper()).strip()
    loc_u = re.sub(r"\s+", " ", (location_name or "").upper()).strip()
    combined = re.sub(r"\s+", " ", f"{loc_u} {addr_u}").strip()

    pilot_zips, pilot_markets, pilot_states = _load_pilot_zip_market_state()

    zipcode = _extract_zip_from_address(addr_u) or _extract_zip_from_address(loc_u)
    if zipcode and zipcode in pilot_zips:
        return True

    if combined and pilot_markets:
        for mkt in pilot_markets:
            if mkt and re.search(rf"\b{re.escape(mkt)}\b", combined):
                return True

    state_from_address = (
        _extract_state_from_address(addr_u) or _extract_state_from_address(loc_u)
    )
    state_norm = ((state or "").strip().upper())
    cand_state = None
    if state_from_address and len(state_from_address) == 2:
        cand_state = state_from_address
    elif state_norm and len(state_norm) == 2:
        cand_state = state_norm
    if cand_state and cand_state in pilot_states:
        return True

    return False


def parse_pricing(response: scrapy.http.Response) -> Dict[str, Any]:
    fee_text = " ".join(response.xpath("//h2[contains(text(),'Entry Fees')]/parent::td//text()").getall()).strip()
    if not fee_text or "free" in fee_text.lower():
        return {"kind": "free", "currency": None, "minAmount": None, "maxAmount": None}
    amounts = [float(a.replace(",", "")) for a in re.findall(r"\$?([\d,]+(?:\.\d{1,2})?)", fee_text)]
    if not amounts:
        return {"kind": "tbd", "currency": None, "minAmount": None, "maxAmount": None}
    lo, hi = min(amounts), max(amounts)
    if lo == hi:
        return {"kind": "point", "currency": "USD", "minAmount": lo, "maxAmount": None}
    return {"kind": "span", "currency": "USD", "minAmount": lo, "maxAmount": hi}


def parse_tags(response: scrapy.http.Response) -> List[str]:
    tags: List[str] = []
    for raw in response.xpath('//div[@id="nav_btn"]//a/text() | //div[@id="nav_btn"]//span[contains(@class,"text-muted-new")]/text()').getall():
        t = (raw or "").strip().lstrip("#").strip()
        t = re.sub(r"\s+", " ", t).strip()
        if t and len(t) > 1:
            tags.append(t)
    return list(dict.fromkeys(tags))


class EventDetailForageSpider(scrapy.Spider):
    name = "event_detail_forage"
    allowed_domains = ["10times.com"]

    custom_settings = {
        "FEEDS": {
            "Updated_data_05_05_2026.json": {
                "format": "json",
                "overwrite": True,
                "indent": 2,
                "encoding": "utf-8",
            }
        },
        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": False,
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 2,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "DOWNLOADER_MIDDLEWARES": {
            "10times_scraper.middlewares.ZyteProxyMiddleware": 543,
        },
        "LOG_LEVEL": "INFO",
    }

    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://10times.com/",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._run_counts: Dict[str, int] = {
            "csv_rows_total": 0,
            "csv_rows_empty_url": 0,
            "csv_rows_skip_pattern": 0,
            "csv_rows_duplicate_url": 0,
            "requests_scheduled": 0,
            "responses_received": 0,
            "skipped_non_200": 0,
            "skipped_past_event": 0,
            "skipped_no_event_json": 0,
            "request_errors": 0,
            "records_yielded": 0,
            "skipped_csv_rows": 0,
        }
        self._skipped_events_csv_file: Optional[Any] = None
        self._skipped_events_csv_writer: Optional[csv.DictWriter] = None
        self._init_skipped_events_csv()

    def _init_skipped_events_csv(self) -> None:
        csv_columns = [
            "reason",
            "row",
            "city",
            "state",
            "url",
            "httpStatus",
            "eventStatus",
            "details",
        ]
        out_path = Path.cwd() / "skipped_events.csv"
        f = open(out_path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(f, fieldnames=csv_columns)
        writer.writeheader()
        self._skipped_events_csv_file = f
        self._skipped_events_csv_writer = writer

    def _write_skipped_event(
        self,
        reason: str,
        row_num: Any,
        city: str,
        state: str,
        url: str,
        http_status: Optional[Any] = None,
        event_status: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        if not self._skipped_events_csv_writer:
            return
        self._skipped_events_csv_writer.writerow(
            {
                "reason": reason,
                "row": row_num,
                "city": city,
                "state": state,
                "url": url,
                "httpStatus": http_status,
                "eventStatus": event_status,
                "details": details,
            }
        )
        self._run_counts["skipped_csv_rows"] += 1

    def start_requests(self):
        seen_urls = set()
        csv_candidates = [
            Path.cwd() / "event_urls.csv",
            Path(__file__).resolve().parent / "event_urls.csv",
        ]
        csv_path = next((p for p in csv_candidates if p.exists()), None)
        if not csv_path:
            self.logger.error(
                "event_urls.csv not found. Expected at %s or %s. Run 'city_events' spider first.",
                csv_candidates[0],
                csv_candidates[1],
            )
            return
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row_num, row in enumerate(reader, start=2):
                    self._run_counts["csv_rows_total"] += 1
                    url = (row.get("event_url") or "").strip()
                    city = (row.get("city") or "").strip() or "UNKNOWN_CITY"
                    state = (row.get("state") or "").strip() or "UNKNOWN_STATE"

                    if not url:
                        self._run_counts["csv_rows_empty_url"] += 1
                        self._write_skipped_event(
                            reason="empty_event_url",
                            row_num=row_num,
                            city=city,
                            state=state,
                            url="",
                            details="CSV row has empty event_url",
                        )
                        self.logger.warning(
                            "CSV row %s (%s, %s): empty event_url, skipping row",
                            row_num,
                            city,
                            state,
                        )
                        continue

                    if any(pat in url for pat in _SKIP_PATTERNS):
                        self._run_counts["csv_rows_skip_pattern"] += 1
                        self._write_skipped_event(
                            reason="skip_pattern",
                            row_num=row_num,
                            city=city,
                            state=state,
                            url=url,
                            details="URL matched configured skip pattern",
                        )
                        self.logger.info(
                            "CSV row %s (%s, %s): skip-pattern URL skipped: %s",
                            row_num,
                            city,
                            state,
                            url,
                        )
                        continue

                    if url in seen_urls:
                        self._run_counts["csv_rows_duplicate_url"] += 1
                        self._write_skipped_event(
                            reason="duplicate_url",
                            row_num=row_num,
                            city=city,
                            state=state,
                            url=url,
                            details="Duplicate event URL in input CSV",
                        )
                        self.logger.info(
                            "CSV row %s (%s, %s): duplicate URL in CSV skipped: %s",
                            row_num,
                            city,
                            state,
                            url,
                        )
                        continue
                    seen_urls.add(url)
                    self._run_counts["requests_scheduled"] += 1
                    self.logger.info(
                        "Scheduling request %s | row=%s | city=%s, %s | url=%s",
                        self._run_counts["requests_scheduled"],
                        row_num,
                        city,
                        state,
                        url,
                    )
                    yield scrapy.Request(
                        url=url,
                        headers=self._HEADERS,
                        callback=self.parse_event,
                        errback=self.handle_error,
                        meta={"city": city, "state": state, "csv_row": row_num},
                    )
            self.logger.info(
                "START SUMMARY | csv_rows_total=%s | empty_url=%s | skip_pattern=%s | "
                "duplicate_url=%s | requests_scheduled=%s",
                self._run_counts["csv_rows_total"],
                self._run_counts["csv_rows_empty_url"],
                self._run_counts["csv_rows_skip_pattern"],
                self._run_counts["csv_rows_duplicate_url"],
                self._run_counts["requests_scheduled"],
            )
        except FileNotFoundError:
            self.logger.error("event_urls.csv not found. Run 'city_events' spider first.")
  
    def parse_event(self, response: scrapy.http.Response):
        self._run_counts["responses_received"] += 1
        city = response.meta.get("city", "UNKNOWN_CITY")
        state = response.meta.get("state", "UNKNOWN_STATE")
        row_num = response.meta.get("csv_row", "?")
        self.logger.info(
            "Fetched response %s | row=%s | city=%s, %s | url=%s | HTTP=%s",
            self._run_counts["responses_received"],
            row_num,
            city,
            state,
            response.url,
            response.status,
        )
        if response.status != 200:
            self._run_counts["skipped_non_200"] += 1
            self._write_skipped_event(
                reason="non_200",
                row_num=row_num,
                city=city,
                state=state,
                url=response.url,
                http_status=response.status,
                details="Response status is not 200",
            )
            self.logger.warning(
                "SKIP non-200 | row=%s | city=%s, %s | url=%s | HTTP=%s",
                row_num,
                city,
                state,
                response.url,
                response.status,
            )
            return

        event_status_raw = extract_event_status_from_script(response)
        if event_status_raw and event_status_raw.strip().lower() == "past":
            self._run_counts["skipped_past_event"] += 1
            self._write_skipped_event(
                reason="past_event",
                row_num=row_num,
                city=city,
                state=state,
                url=response.url,
                http_status=response.status,
                event_status=event_status_raw,
                details="Event status marked as past",
            )
            self.logger.info(
                "SKIP past event | row=%s | city=%s, %s | url=%s | event_status=%s",
                row_num,
                city,
                state,
                response.url,
                event_status_raw,
            )
            return

        ld = extract_event_json(response)
        if not ld:
            self._run_counts["skipped_no_event_json"] += 1
            # Common causes: blocked/gated HTML, missing JSON-LD, or bot-check response.
            title = (response.xpath("//title/text()").get() or "").strip()
            had_ldjson = bool(response.request.meta.get("_had_ldjson"))
            self._write_skipped_event(
                reason="missing_event_json",
                row_num=row_num,
                city=city,
                state=state,
                url=response.url,
                http_status=response.status,
                details=f"title={title!r}; ldjson_scripts={'present' if had_ldjson else 'none'}",
            )
            self.logger.warning(
                "SKIP no Event JSON-LD | row=%s | city=%s, %s | url=%s | title=%r | ldjson_scripts=%s",
                row_num,
                city,
                state,
                response.url,
                title,
                "present" if had_ldjson else "none",
            )
            return

        event_url = ld.get("url") or response.url
        event_id = event_url.rstrip("/").split("/")[-1]
        title = _normalize_schema_value(ld.get("name")) or ""
        page_title = (
            response.xpath('normalize-space(string(//div[@id="online-header-left"]//h1))').get() or ""
        ).strip()
        page_title = " ".join(page_title.split())
        # Some pages have truncated JSON-LD name; prefer fuller visible title when available.
        if page_title and (
            not title
            or (title.lower() in page_title.lower() and len(page_title) > len(title))
            or ("(" in page_title and ")" in page_title and "(" not in title)
        ):
            title = page_title
        description = " ".join(
            t.strip() for t in response.xpath('//div[@class="mb-2"]/span/text()').getall() if t and t.strip()
        ).strip() or None

        # LOCATION: hidden inputs first, then fallback to Venue section details.
        location_name, location_address = extract_location_fields(response)
        # If venueName is only city/country like "Atlanta, US", keep name blank and use it as address.
        if location_name and re.fullmatch(r"[A-Za-z .'-]+,\s*[A-Za-z]{2,3}", location_name):
            if not location_address:
                location_address = location_name
            location_name = None
        if _has_indian_six_digit_pin(location_address):
            self._write_skipped_event(
                reason="indian_zip_code",
                row_num=row_num,
                city=city,
                state=state,
                url=response.url,
                http_status=response.status,
                details=f"location_name={location_name!r}; location_address={location_address!r}",
            )
            self.logger.info(
                "SKIP indian 6-digit ZIP | row=%s | city=%s, %s | url=%s | address=%r",
                row_num,
                city,
                state,
                response.url,
                location_address,
            )
            return
        if not is_event_in_scope(
            address=location_address,
            city=city,
            state=state,
            location_name=location_name,
        ):
            self._write_skipped_event(
                reason="not_in_scope",
                row_num=row_num,
                city=city,
                state=state,
                url=response.url,
                http_status=response.status,
                details=f"location_name={location_name!r}; location_address={location_address!r}",
            )
            self.logger.info(
                "SKIP not in scope | row=%s | city=%s, %s | url=%s | address=%r",
                row_num,
                city,
                state,
                response.url,
                location_address,
            )
            return

        start_raw = _normalize_schema_value(ld.get("startDate"))
        end_raw = _normalize_schema_value(ld.get("endDate"))
        event_time = parse_event_time(response) or None
        schedule = build_event_schedule(start_raw, end_raw, event_time)

        organizer, event_roles = extract_organizer_and_event_roles(response, ld)

        tags = parse_tags(response)
        if location_address:
            latitude, longitude = extract_coordinates(response, ld)
        else:
            latitude, longitude = None, None
        image_urls = _collect_image_urls(response)
        media = build_media(image_urls, title)
        run_time = _now_iso()

        status_source = event_status_raw or _normalize_schema_value(ld.get("eventStatus"), join_lists=False)
        status_value = normalize_event_status(status_source)

        record = {
            "provider": "forage",
            "module": "events",
            "groupId": triple_id("group", PROVIDER, event_id, event_url),
            "id": triple_id("id", PROVIDER, event_id, event_url),
            "createdAt": run_time,
            "updatedAt": run_time,
            "title": title,
            "source": STATIC_SOURCE,
            "recordSource": {"id": event_id, "url": event_url},
            "location": {
                "name": location_name,
                "address": location_address,
                "latitude": latitude,
                "longitude": longitude,
                "nearBy": compute_nearby(location_address, city=city, state=state),
            },
            "siteId": get_site_id(location_address),
            "metadata": {
                "event": {
                    "description": description,
                    "organizer": organizer,
                    "eventRoles": event_roles,
                    "eventSchedule": schedule,
                    "status": status_value,
                    "availability": None,
                    "purchaseUrl": None,
                    "audiences": [],
                    "categories": [],
                    "tags": tags,
                    "media": media,
                }
            },
        }
        cleaned_record = replace_empty_strings_with_none(record)
        self._run_counts["records_yielded"] += 1
        self.logger.info(
            "YIELD record %s | row=%s | city=%s, %s | url=%s",
            self._run_counts["records_yielded"],
            row_num,
            city,
            state,
            response.url,
        )
        yield cleaned_record

    def handle_error(self, failure):
        self._run_counts["request_errors"] += 1
        city = failure.request.meta.get("city", "UNKNOWN_CITY")
        state = failure.request.meta.get("state", "UNKNOWN_STATE")
        row_num = failure.request.meta.get("csv_row", "?")
        self._write_skipped_event(
            reason="request_error",
            row_num=row_num,
            city=city,
            state=state,
            url=failure.request.url,
            details=str(failure.value),
        )
        self.logger.error(
            "REQUEST ERROR %s | row=%s | city=%s, %s | url=%s | error=%s",
            self._run_counts["request_errors"],
            row_num,
            city,
            state,
            failure.request.url,
            failure.value,
        )

    def closed(self, reason):
        if self._skipped_events_csv_file is not None:
            try:
                self._skipped_events_csv_file.close()
            except Exception:
                pass
        self.logger.info(
            "RUN SUMMARY | reason=%s | csv_rows_total=%s | empty_url=%s | skip_pattern=%s | "
            "duplicate_url=%s | requests_scheduled=%s | responses_received=%s | "
            "skipped_non_200=%s | skipped_past_event=%s | skipped_no_event_json=%s | "
            "request_errors=%s | records_yielded=%s | skipped_csv_rows=%s",
            reason,
            self._run_counts["csv_rows_total"],
            self._run_counts["csv_rows_empty_url"],
            self._run_counts["csv_rows_skip_pattern"],
            self._run_counts["csv_rows_duplicate_url"],
            self._run_counts["requests_scheduled"],
            self._run_counts["responses_received"],
            self._run_counts["skipped_non_200"],
            self._run_counts["skipped_past_event"],
            self._run_counts["skipped_no_event_json"],
            self._run_counts["request_errors"],
            self._run_counts["records_yielded"],
            self._run_counts["skipped_csv_rows"],
        )

