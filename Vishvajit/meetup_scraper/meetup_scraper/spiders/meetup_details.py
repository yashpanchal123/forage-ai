import csv
import json
import re
from pathlib import Path
from datetime import datetime, timezone
import uuid
import scrapy
from ..items import MeetupEventDetailsItem



PROVIDER = "forage"

# Target metros: compare Market + State to these (case-insensitive).
TARGET_METROS = (
    ("atlanta", "GA"),
    ("austin", "TX"),
    ("orlando", "FL"),
    ("nashville", "TN"),
)

ZIP_PATTERN = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
ZIP_AFTER_ZIP_WORD = re.compile(r"\bzip\s*(\d{5})(?:-\d{4})?\b", re.I)

# Related topics on event HTML (primary); __NEXT_DATA__ topics used only if empty.
RELATED_TOPICS_TAGS_XPATH = (
    '//h2[contains(text(),"Related topics")]/parent::div/parent::div/parent::div/'
    "following-sibling::div//*/text()"
)


def _tags_from_related_topics_xpath(response):
    raw = response.xpath(RELATED_TOPICS_TAGS_XPATH).getall()
    tags = []
    seen = set()
    for t in raw:
        s = " ".join(str(t).split()).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(s)
    return tags


def _tags_from_event_topics_json(event):
    tags = []
    topics_conn = event.get("topics") or {}
    edges = topics_conn.get("edges") if isinstance(topics_conn, dict) else []
    if isinstance(edges, list):
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict) and node.get("name"):
                tags.append(node["name"])
    return tags


def triple_id(*parts: str) -> str:
    u = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts))
    h = f"{u.int:032d}"
    return f"{int(h[0:4])}-{int(h[4:10])}-{int(h[10:18])}"


def _col_index(headers, patterns):
    for i, h in enumerate(headers):
        hl = (h or "").strip().lower()
        for pat in patterns:
            if re.search(pat, hl, re.I):
                return i
    return None


def load_zip_dma_map(path: Path, logger):
    """
    Load mapping ZIP (5 digits) -> (Market, State) from CSV.
    Expected columns: Zip, Market, State.
    """
    logger.info("Loading ZIP->DMA map from: %s", path)

    if not path.exists():
        logger.warning(
            "ZIP lookup CSV not found at %s. nearBy will be None when ZIP-based check is needed.",
            path,
        )
        return {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        zi = _col_index(headers, [r"^zip$", r"zip\s*code"])
        di = _col_index(headers, [r"^market$", r"dma\s*name", r"^dma$"])
        si = _col_index(headers, [r"^state$", r"st\s*abv", r"state\s*abbr"])

        if zi is None or di is None or si is None:
            logger.error(
                "ZIP lookup CSV needs Zip, Market, and State columns. Headers: %s",
                headers,
            )
            return {}

        zip_key = headers[zi]
        market_key = headers[di]
        state_key = headers[si]

        m = {}
        for row in reader:
            if not row:
                continue
            z_str = str(row.get(zip_key) or "").strip()
            zm = re.search(r"(\d{5})", z_str)
            if not zm:
                continue
            z5 = zm.group(1)
            market = str(row.get(market_key) or "").strip()
            st = str(row.get(state_key) or "").strip()
            if z5 not in m:
                m[z5] = (market, st)

        logger.info("ZIP->DMA map loaded: %d entries from %s", len(m), path)
        return m


def normalize_address_for_zip(address):
    if not address:
        return ""
    s = str(address).strip()
    s = re.sub(r'^["\s]+|["\s]+$', "", s)
    s = s.replace('""', '"').strip('"').strip("'")
    return " ".join(s.split())


def extract_zip_from_address(address):
    s = normalize_address_for_zip(address)
    if not s:
        return None
    # Explicit "Zip 32801" style (word boundary issues on some strings)
    m_zip_word = ZIP_AFTER_ZIP_WORD.search(s)
    if m_zip_word:
        return m_zip_word.group(1)

    matches = ZIP_PATTERN.findall(s)
    if not matches:
        return None

    # ZIP is usually last; this avoids confusing a 5-digit street number with ZIP.
    zip5 = matches[-1]

    # If the address starts with a 5-digit street number and that's the only
    # 5-digit sequence present, treat it as NOT a ZIP.
    if len(matches) == 1 and re.match(r"^\s*\d{5}\b", s):
        return None

    return zip5


def metro_matches_dma_state(dma_name, st_abv):
    dma = (dma_name or "").strip().lower()
    st = (st_abv or "").strip().upper()
    if len(st) > 2:
        st = st[:2]
    if len(st) != 2:
        return False
    for city_key, st_req in TARGET_METROS:
        if st != st_req:
            continue
        if dma == city_key:
            return True
        if city_key in dma or dma.startswith(city_key + " ") or dma.startswith(city_key + "-"):
            return True
    return False


def address_contains_target_location(venue_address):
    s = (venue_address or "").lower()
    if not s:
        return False
    return any(
        f"{city}, {state.lower()}" in s
        for city, state in TARGET_METROS
    )


def compute_near_by(venue_address, zip_dma_map):
    """
    None: no address.
    False: address present but no ZIP/city-state target match, or ZIP matches non-target Market/State.
    True: ZIP in sheet and DMA + state match Atlanta/Austin/Orlando/Nashville.
    """
    if not venue_address or not str(venue_address).strip():
        return None
    z = extract_zip_from_address(venue_address)
    if not z:
        if address_contains_target_location(venue_address):
            return True
        return False
    if not zip_dma_map:
        if address_contains_target_location(venue_address):
            return True
        return False
    row = zip_dma_map.get(z)
    if not row:
        if address_contains_target_location(venue_address):
            return True
        return False
    dma, st = row
    return metro_matches_dma_state(dma, st)


def _trim_trailing_duplicate_state(segments):
    if len(segments) < 2:
        return segments
    last = segments[-1].strip()
    if not re.fullmatch(r"[A-Z]{2}", last):
        return segments

    token = last.upper()
    earlier_joined = ", ".join(segments[:-1])
    if re.search(r"\b" + re.escape(token) + r"\b", earlier_joined, re.I):
        return segments[:-1]
    return segments


def _venue_name_same_as_address_line(name, address_line):
    """Meetup often sets venue name to the street when there is no named place."""
    n = (name or "").strip()
    a = (address_line or "").strip()
    if not n or not a:
        return False
    return " ".join(n.split()).lower() == " ".join(a.split()).lower()


def _normalize_meetup_address_string(address, venue_name=None):
    if not address:
        return ""
    s = str(address).strip()
    s = re.sub(r"[\u2018\u2019]", "'", s)
    s = re.sub(r"\s*'\s*", ", ", s)
    s = re.sub(r",+", ",", s)
    s = " ".join(s.split())

    segments = [seg.strip() for seg in s.split(",") if seg.strip()]
    drop_country = frozenset(
        x.lower() for x in ("United States", "USA", "U.S.A.", "US", "U.S.")
    )
    segments = [seg for seg in segments if seg.lower() not in drop_country]

    cleaned = []
    seen = set()
    for seg in segments:
        key = seg.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(seg)

    cleaned = _trim_trailing_duplicate_state(cleaned)

    out = ", ".join(cleaned)
    if venue_name:
        vn = re.sub(r"[\u2018\u2019']+", "", venue_name).strip()
        vn = " ".join(vn.split())
        if vn and out.lower().startswith(vn.lower()):
            n = len(vn)
            if len(out) >= n and out[:n].lower() == vn.lower():
                rest = out[n:].lstrip(" ,").strip()
                out = rest
    return out


def _clean_lat_lon(lat, lng):
    def one(v):
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f == 0.0:
            return None
        return f

    return one(lat), one(lng)


def to_utc(iso_time_str):
    """
    Convert ISO 8601 time with timezone to UTC ISO format
    Example:
        2026-04-03T19:00:00-04:00 -> 2026-04-03T23:00:00Z
    """
    # Parse ISO string (Python 3.7+)
    dt = datetime.fromisoformat(iso_time_str)

    # Convert to UTC
    dt_utc = dt.astimezone(timezone.utc)

    # Return in standard UTC format
    return dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ')


def _is_date_only(value):
    if value is None:
        return False
    s = str(value).strip()
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


def _date_or_datetime_meta(start_value):
    if _is_date_only(start_value):
        return "date", "date", True
    return "dateTime", "dateTime", False


class MeetupDetailsSpider(scrapy.Spider):
    name = "meetup_details"
    allowed_domains = ["www.meetup.com", "meetup.com"]

    custom_settings = {
        "FEEDS": {
            "meetup_events_dataset_13_04.json": {
                "format": "json",
                "overwrite": True,
                "indent": 4,
                "encoding": "utf-8",
            }
        }
    }

    def __init__(self, urls_csv=None, dma_excel=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # self.media_counter = 0
        project_root = Path(__file__).resolve().parents[2]
        self.urls_csv = urls_csv or str(project_root / "meetup_urls.csv")

        dma_path = Path(dma_excel) if dma_excel else (project_root / "Tegna - Pilot Scope Locations.csv")
        if not dma_path.is_absolute():
            dma_path = project_root / dma_path

        self.logger.info(
            "Spider initialised | urls_csv=%s | dma_path=%s",
            self.urls_csv, dma_path,
        )

        self._zip_dma_map = load_zip_dma_map(dma_path, self.logger)

        # Dedupe within a run (in case urls.csv has duplicates).
        self._seen_event_urls = set()

    def start_requests(self):
        self.logger.info("Reading event URLs from: %s", self.urls_csv)
        dispatched = 0
        skipped_duplicates = 0

        with open(self.urls_csv, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                event_url = row.get("event_url")
                if not event_url:
                    self.logger.debug("Skipping row with missing event_url: %s", row)
                    continue
                if event_url in self._seen_event_urls:
                    self.logger.debug("Skipping duplicate URL: %s", event_url)
                    skipped_duplicates += 1
                    continue
                self._seen_event_urls.add(event_url)

                self.logger.debug("Scheduling request: %s", event_url)
                dispatched += 1
                yield scrapy.Request(
                    url=event_url,
                    callback=self.parse_event,
                )

        self.logger.info(
            "start_requests complete | dispatched=%d | duplicates_skipped=%d",
            dispatched, skipped_duplicates,
        )

    def clean_address(self, venue):
        addr_line = (venue.get("address") or "").strip()
        venue_name = (venue.get("name") or "").strip()
        # If name duplicates the street line, do not strip it from the formatted address
        # (otherwise we only keep "City, ST" and lose the street).
        strip_prefix = (
            None
            if _venue_name_same_as_address_line(venue_name, addr_line)
            else (venue_name or None)
        )
        parts = [
            addr_line,
            (venue.get("city") or "").strip(),
            (venue.get("state") or "").strip(),
        ]
        address = ", ".join([p for p in parts if p])
        address = address.replace(",,", ",")
        address = " ".join(address.split())
        return _normalize_meetup_address_string(address, strip_prefix)

    def parse_event(self, response):
        self.logger.debug("Parsing event page: %s", response.url)

        raw = response.xpath("//script[@id='__NEXT_DATA__']/text()").get()
        if not raw:
            self.logger.error("No __NEXT_DATA__ for %s", response.url)
            return

        data = json.loads(raw)
        page_props = data.get("props", {}).get("pageProps", {})
        event = page_props.get("event", {}) or {}
        event_id = str(event.get("id") or page_props.get("eventId") or "")
        source_url = event.get("eventUrl") or response.url

        self.logger.debug(
            "Extracted event | id=%s | url=%s", event_id, source_url
        )

        group = event.get("group") or {}

        venue = event.get("venue") or {}
        hosts = event.get("eventHosts") or []

        title = event.get("title")
        self.logger.debug("Event title: %s", title)

        date_time = event.get("dateTime")
        end_time = event.get("endTime")
        schedule_precision, schedule_type, _ = _date_or_datetime_meta(date_time)

        if date_time:
            if not end_time or _is_date_only(date_time):
                schedule_all_day = True
            else:
                schedule_all_day = False
        else:
            schedule_all_day = False
        kind = None
        if date_time and end_time:
            if date_time == end_time:
                kind = "point"
                end_time = None
            else:
                kind = "span"
        elif date_time:
            kind = "point"
        else:
            kind = "tbd"

        self.logger.debug(
            "Schedule | kind=%s | allDay=%s | start=%s | end=%s",
            kind, schedule_all_day, date_time, end_time,
        )

        time_status = event.get("timeStatus")
        if time_status in ("UPCOMING", "ACTIVE"):
            status = "scheduled"
        elif time_status == "CANCELLED":
            status = "cancelled"
        else:
            status = None
        availability = "onSale" if time_status in ("UPCOMING", "ACTIVE") else None

        self.logger.debug(
            "Event status | timeStatus=%s | status=%s | availability=%s",
            time_status, status, availability,
        )

        # Tags: Related topics from HTML; fallback to __NEXT_DATA__ topics graph.
        tags = _tags_from_related_topics_xpath(response)
        if not tags:
            tags = _tags_from_event_topics_json(event)
        tags = sorted(set(tags))

        self.logger.debug("Tags extracted (%d): %s", len(tags), tags)

        # Hosts -> eventRoles
        event_roles = []

        if isinstance(hosts, list):
            for host in hosts:
                if not isinstance(host, dict):
                    continue

                name = host.get("name")

                if name:
                    event_roles.append({
                        "name": name,
                        "role": "organizer"
                    })

        self.logger.debug("Event roles extracted (%d): %s", len(event_roles), event_roles)

        # Photo -> media
        featured_photo = event.get("featuredEventPhoto") or event.get("displayPhoto") or {}
        media = []
        photo_url = (featured_photo.get("source") or featured_photo.get("highResUrl"))

        if isinstance(featured_photo, dict) and photo_url:
            self.logger.debug("Featured photo found: %s", photo_url)
            # self.media_counter += 1
            # media_id = f"media-{self.media_counter}"
            media = [
                {
                    "id": "media-001",
                    "type": "image",
                    "title": title,
                    "shortDescription": None,
                    "source": {
                        "name": "Meetup",
                        "id": str(featured_photo.get("id")),
                        "url": photo_url,
                    },
                    "thumbnail": {
                        "url": photo_url,
                        "width": None,
                        "height": None,
                    },
                    "variants": [],
                }
            ]
        else:
            self.logger.debug("No featured photo found for event id=%s", event_id)

        # Keep only event-page location data in details output.
        venue_name_raw = (venue.get("name") or "").strip()
        addr_line_raw = (venue.get("address") or "").strip()
        # No separate venue label when Meetup reused the street as the name.
        if _venue_name_same_as_address_line(venue_name_raw, addr_line_raw):
            venue_name = ""
        else:
            venue_name = venue_name_raw
        if "online event" in venue_name.lower():
            venue_lat = None
            venue_lng = None
        else:
            venue_lat, venue_lng = _clean_lat_lon(venue.get("lat"), venue.get("lng"))

        venue_address = self.clean_address(venue)
        self.logger.debug(
            "Venue | name=%s | address=%s | lat=%s | lng=%s",
            venue_name, venue_address, venue_lat, venue_lng,
        )

        if "online event" in (venue_name or "").lower():
            near_by = None
        else:
            near_by = compute_near_by(venue_address, self._zip_dma_map)

        self.logger.debug("nearBy computed: %s", near_by)

        city_text = (venue.get("city") or "").lower()

        if "atlanta" in city_text:
            site_id = 85
        elif "austin" in city_text:
            site_id = 269
        elif "orlando" in city_text:
            site_id = None
        elif "nashville" in city_text:
            site_id = None
        else:
            site_id = None

        self.logger.debug("siteId resolved: %s (city_text=%s)", site_id, city_text)

        group_id = triple_id("group", PROVIDER, event_id, source_url)
        record_id = triple_id("id", PROVIDER, event_id, source_url)

        details = {
            "provider": PROVIDER,
            "module": "events",
            "groupId": group_id,
            "id": record_id,
            "createdAt": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "updatedAt": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "title": title,
            "source": {
                "name": "Meetup",
                "id": "meetup",
                "url": "https://www.meetup.com",
            },
            "recordSource": {
                "id": event_id,
                "url": source_url,
            },
            "location": {
                "name": venue_name or None,
                "address": venue_address,
                "latitude": venue_lat,
                "longitude": venue_lng,
                "nearBy": near_by,
            },
            "siteId": site_id,
            "metadata": {
                "event": {
                    "description": event.get("description"),
                    "eventRoles": event_roles,
                    "eventSchedule": {
                        "kind": kind,
                        "precision": schedule_precision,
                        "allDay": schedule_all_day,
                        "start": {
                            "type": schedule_type,
                            "value": to_utc(date_time) if date_time else None,
                        },
                        "end": {
                            "type": schedule_type,
                            "value": to_utc(end_time) if end_time else None,
                        },
                    },
                    "status": status,
                    "eventPricing": {
                        "kind": "tbd",
                        # "currency": event.get("currency"),
                        "currency": None,
                        "minAmount": None,
                        "maxAmount": None,
                    },
                    "availability": availability,
                    "purchaseUrl": None,
                    "audiences": [],
                    "categories": [],
                    "tags": tags,
                    "media": media,
                }
            },
        }

        self.logger.info(
            "Yielding item | event_id=%s | title=%s | nearBy=%s | siteId=%s",
            event_id, title, near_by, site_id,
        )

        yield MeetupEventDetailsItem(**details)