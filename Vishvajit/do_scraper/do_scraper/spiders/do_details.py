import csv
import re
import uuid
from html import unescape
from datetime import datetime, timezone
from pathlib import Path
import scrapy
from ..items import DoEventDetailsItem

PROVIDER = "forage"

ZIP_PATTERN = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
ZIP_AFTER_ZIP_WORD = re.compile(r"\bzip\s*(\d{5})(?:-\d{4})?\b", re.I)

# Default ZIP/DMA CSV (place next to scrapy.cfg in do_scraper/).
DEFAULT_DMA_CSV = "Tegna - Pilot Scope Locations.csv"


def triple_id(*parts: str) -> str:
    value = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(str(part) for part in parts))
    digits = f"{value.int:032d}"
    return f"{int(digits[0:4])}-{int(digits[4:10])}-{int(digits[10:18])}"


def _zip_cell_to_str(cell):
    if cell is None:
        return None
    if isinstance(cell, float) and cell == int(cell):
        return str(int(cell))
    s = str(cell).strip()
    if not s:
        return None
    zm = re.search(r"(\d{5})", s)
    return zm.group(1) if zm else None


def load_zip_dma_map(path: Path, logger):
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

    lookup = {}
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        required = {"Zip", "Market", "State"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            logger.error("CSV must contain columns %s. Found: %s", sorted(required), reader.fieldnames)
            return {}

        for row in reader:
            zip5 = _zip_cell_to_str(row.get("Zip"))
            if not zip5:
                continue
            market = (row.get("Market") or "").strip().lower()
            state = (row.get("State") or "").strip().upper()
            if not market or not state:
                continue
            lookup.setdefault(zip5, set()).add((market, state))

    return lookup


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


TARGET_CITY_STATE = {
    "atlanta": ("atlanta", "GA"),
    "austin": ("austin", "TX"),
    "orlando": ("orlando", "FL"),
    "nashville": ("nashville", "TN"),
}


def detect_target_location(city, address):
    text = f"{city or ''} {address or ''}".lower()
    for city_name, pair in TARGET_CITY_STATE.items():
        if city_name in text:
            return pair
    return None


def compute_near_by(venue_address, city, zip_dma_lookup):
    """
    null: empty address / no ZIP parsed.
    true: ZIP matched + Market and State validated, or fallback city/state phrase.
    false: ZIP parsed but not matched and fallback phrase absent.
    """
    if not venue_address or not str(venue_address).strip():
        return None

    target = detect_target_location(city, venue_address)
    zip5 = extract_zip_from_address(venue_address)

    # ZIP-based validation: ZIP -> (Market, State)
    if zip5 and target and zip_dma_lookup:
        target_market, target_state = target
        for market, state in zip_dma_lookup.get(zip5, set()):
            if market == target_market and state == target_state:
                return True

    # Fallback phrase check when ZIP missing or no ZIP match.
    fallback_phrases = [
        "atlanta, ga", "atlanta ga",
        "austin, tx", "austin tx",
        "orlando, fl", "orlando fl",
        "nashville, tn", "nashville tn",
    ]
    lowered = venue_address.lower()
    if any(phrase in lowered for phrase in fallback_phrases):
        return True

    if not zip5:
        return None
    return False


def _is_date_only(value):
    if value is None:
        return False
    s = str(value).strip()
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


def _schedule_meta(start_value):
    if _is_date_only(start_value):
        return "date", "date", True
    return "dateTime", "dateTime", False


def _normalize_schedule_value(value, value_type):
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if value_type == "date":
        m = re.search(r"\d{4}-\d{2}-\d{2}", s)
        return m.group(0) if m else s
    return DoDetailsSpider._to_utc_iso(value)


def _build_event_schedule_for_start_value(start_value, start_raw, end_raw):
    """
    Like _build_event_schedule but start.value is an already-normalized instant (UTC Z string).
    start_raw is still used for date vs dateTime precision.
    """
    schedule_precision, schedule_type, schedule_all_day = _schedule_meta(start_raw)
    end_norm = None
    if end_raw and str(end_raw).strip():
        end_norm = _normalize_schedule_value(end_raw, schedule_type)

    has_end = end_norm is not None
    schedule_kind = "span" if has_end else "point"

    start_block = {}
    if start_value is not None:
        start_block = {"type": schedule_type, "value": start_value}

    end_block = {}
    if has_end:
        end_block = {"type": schedule_type, "value": end_norm}

    schedule = {
        "kind": schedule_kind,
        "precision": schedule_precision,
        "allDay": schedule_all_day,
        "start": start_block,
    }
    if end_block:
        schedule["end"] = end_block
    return schedule


def _build_event_schedule(start_raw, end_raw):
    """
    kind=span only when a real end boundary exists in markup and normalizes.
    If end is missing, end block is {} (no type/value) per QA.
    """
    schedule_precision, schedule_type, schedule_all_day = _schedule_meta(start_raw)
    start_value = _normalize_schedule_value(start_raw, schedule_type) if start_raw else None
    end_norm = None
    if end_raw and str(end_raw).strip():
        end_norm = _normalize_schedule_value(end_raw, schedule_type)

    has_end = end_norm is not None
    schedule_kind = "span" if has_end else "point"

    start_block = {}
    if start_value is not None:
        start_block = {"type": schedule_type, "value": start_value}

    end_block = {}
    if has_end:
        end_block = {"type": schedule_type, "value": end_norm}

    schedule = {
        "kind": schedule_kind,
        "precision": schedule_precision,
        "allDay": schedule_all_day,
        "start": start_block,
    }
    if end_block:
        schedule["end"] = end_block
    return schedule


class DoDetailsSpider(scrapy.Spider):
    name = "do_details"

    custom_settings = {
        "FEEDS": {
            "do_events_full_dataset_28_04_2026.json": {
                "format": "json",
                "overwrite": True,
                "indent": 2,
                "encoding": "utf-8",
            }
        }
    }

    def __init__(self, urls_csv=None, dma_csv=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        project_root = Path(__file__).resolve().parents[2]
        self.urls_csv = Path(urls_csv) if urls_csv else project_root / "do_urls.csv"
        if not self.urls_csv.is_absolute():
            self.urls_csv = (project_root / self.urls_csv).resolve()

        if dma_csv:
            dma_path = Path(dma_csv)
        else:
            dma_path = project_root / DEFAULT_DMA_CSV
        if not dma_path.is_absolute():
            dma_path = (project_root / dma_path).resolve()
        self._zip_dma_lookup = load_zip_dma_map(dma_path, self.logger)

        self._seen_urls = set()
        self._rows_seen = 0
        self._rows_emitted = 0
        self._rows_skipped_past = 0
        self._rows_dual = 0
        self._rows_missing_description = 0

    def start_requests(self):
        if not self.urls_csv.exists():
            self.logger.error("URLs CSV not found: %s", self.urls_csv)
            return

        self.logger.info(
            "Starting do_details spider: urls_csv=%s zip_dma_rows=%d",
            self.urls_csv,
            len(self._zip_dma_lookup),
        )

        with self.urls_csv.open("r", encoding="utf-8", newline="") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                self._rows_seen += 1
                event_url = (row.get("event_url") or "").strip()
                city = (row.get("city") or "").strip()
                if not event_url or event_url in self._seen_urls:
                    continue
                self._seen_urls.add(event_url)
                yield scrapy.Request(
                    url=event_url,
                    callback=self.parse_event,
                    cb_kwargs={"city": city},
                    dont_filter=True,
                )

    def parse_event(self, response, city):
        title = self._clean_text(response.xpath('//h1/span[@itemprop="name"]/text()').get()) or self._clean_text(
            response.xpath('//meta[@property="og:title"]/@content').get()
        )

        description = self._extract_description_frontend(response)

        start_raw = response.xpath('//meta[@itemprop="startDate"]/@content').get()
        end_raw = response.xpath('//meta[@itemprop="endDate"]/@content').get()

        location_name = self._clean_text(
            response.xpath('normalize-space(//*[@itemprop="location"]//*[@itemprop="name"]/text())').get())
        street = self._clean_text(response.xpath('//meta[@itemprop="streetAddress"]/@content').get())
        locality = self._clean_text(response.xpath('//meta[@itemprop="addressLocality"]/@content').get())
        region = self._clean_text(response.xpath('//meta[@itemprop="addressRegion"]/@content').get())
        postal = self._clean_text(response.xpath('//meta[@itemprop="postalCode"]/@content').get())
        address = " ".join([part for part in [street, locality, region, postal] if part])
        address = re.sub(r"\s+", " ", address).strip()
        latitude = self._safe_float(response.xpath('//meta[@itemprop="latitude"]/@content').get())
        longitude = self._safe_float(response.xpath('//meta[@itemprop="longitude"]/@content').get())

        near_by = compute_near_by(address, city, self._zip_dma_lookup)

        addr_lower = (address or "").lower()

        if "atlanta" in addr_lower:
            site_id = 85
        elif "austin" in addr_lower:
            site_id = 269
        elif "orlando" in addr_lower:
            site_id = None
        elif "nashville" in addr_lower:
            site_id = None
        else:
            site_id = None

        if location_name and "online" in location_name.lower():
            latitude = None
            longitude = None

        organizer = self._clean_text(
            response.xpath('normalize-space(//*[@itemprop="organizer"]//*[@itemprop="name"]/text())').get())
        if not organizer:
            organizer = self._clean_text(response.xpath('normalize-space(//*[@itemprop="organizer"]/text())').get())

        ticket_href = response.xpath(
            '//*[@itemprop="offers"]//a[contains(@class,"ticket")]/@href | //a[contains(@class,"ds-buy-tix")]/@href'
        ).get()
        purchase_url = response.urljoin(ticket_href) if ticket_href else None

        source = self.get_source(response)
        event_id = response.xpath('//body/@data-event-id').get()
        image_url = self._clean_text(response.xpath('//meta[@property="og:image"]/@content').get())

        start_utc = _normalize_schedule_value(start_raw, "dateTime")
        if self._is_past_year(start_utc):
            self._rows_skipped_past += 1
            return
        event_status = "scheduled" if start_utc else None

        min_price, max_price, is_free = self._extract_price_bounds(response)
        event_pricing = self._build_event_pricing(min_price, max_price, is_free)
        now_iso = self._utc_now_iso()

        tags = self._extract_event_tags(response)
        ticket_blob = self._ticket_price_title_and_text(response)

        dual_starts = self._dual_show_start_utc_values(start_raw, end_raw, ticket_blob)

        base_event = {
            "description": description,
            "eventRoles": (
                [{"name": organizer, "role": "organizer"}] if organizer else []
            ),
            "status": event_status,
            "eventPricing": {
                **event_pricing
            },
            "availability": None,
            "purchaseUrl": purchase_url,
            "audiences": [],
            "categories": [],
            "tags": tags,
            "media": self._build_media(image_url, title, event_id, source),
        }

        if dual_starts:
            eid = str(event_id or "")
            for show_index, start_value in enumerate(dual_starts, start=1):
                # Each performance gets its own start instant, groupId, and id (all differ across rows).
                id_seed = (PROVIDER, eid, response.url, str(start_value), str(show_index))
                event_schedule = _build_event_schedule_for_start_value(
                    start_value, start_raw, end_raw
                )
                payload = {
                    "provider": PROVIDER,
                    "module": "events",
                    "groupId": triple_id("group", *id_seed),
                    "id": triple_id("id", *id_seed),
                    "createdAt": now_iso,
                    "updatedAt": now_iso,
                    "title": title,
                    "source": source,
                    "recordSource": {
                        "id": event_id,
                        "url": response.url,
                    },
                    "location": {
                        "name": location_name or city,
                        "address": address,
                        "latitude": latitude,
                        "longitude": longitude,
                        "nearBy": near_by,
                    },
                    "siteId": site_id,
                    "metadata": {
                        "event": {
                            **base_event,
                            "eventSchedule": event_schedule,
                        }
                    },
                }
                self._rows_emitted += 1
                yield DoEventDetailsItem(**payload)
            self._rows_dual += 1
            return

        event_schedule = _build_event_schedule(start_raw, end_raw)
        payload = {
            "provider": PROVIDER,
            "module": "events",
            "groupId": triple_id("group", PROVIDER, event_id, response.url),
            "id": triple_id("id", PROVIDER, event_id, response.url),
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "title": title,
            "source": source,
            "recordSource": {
                "id": event_id,
                "url": response.url,
            },
            "location": {
                "name": location_name or city,
                "address": address,
                "latitude": latitude,
                "longitude": longitude,
                "nearBy": near_by,
            },
            "siteId": site_id,
            
            "metadata": {
                "event": {
                    **base_event,
                    "eventSchedule": event_schedule,
                }
            },
        }
        if description is None:
            self._rows_missing_description += 1
            self.logger.debug("Description missing for url=%s", response.url)
        self._rows_emitted += 1
        yield DoEventDetailsItem(**payload)

    def closed(self, reason):
        self.logger.info(
            (
                "do_details finished: reason=%s csv_rows_seen=%d urls_requested=%d emitted_rows=%d "
                "dual_show_events=%d skipped_past_year=%d missing_description=%d"
            ),
            reason,
            self._rows_seen,
            len(self._seen_urls),
            self._rows_emitted,
            self._rows_dual,
            self._rows_skipped_past,
            self._rows_missing_description,
        )

    @staticmethod
    def _extract_description_frontend(response):
        """
        Primary: //div[contains(@class,"ds-event-description")]/div/text()
        No fallback to og:description.
        """
        parts = response.xpath(
            '//div[contains(@class,"ds-event-description")]/div/text()'
        ).getall()
        text = " ".join(p.strip() for p in parts if p and str(p).strip())
        cleaned = DoDetailsSpider._clean_text(text)
        # Some pages yield non-description tokens (e.g., "150", "- $150") via /div/text().
        looks_like_price_token = bool(
            cleaned and re.fullmatch(r"[-\s]*\$?\d+(?:\.\d+)?", cleaned)
        )
        if cleaned and not looks_like_price_token:
            return unescape(cleaned)

        # Still frontend-only: broaden to nested text before falling back to og:description.
        parts_nested = response.xpath(
            '//div[contains(@class,"ds-event-description")]/div//text()'
        ).getall()
        nested = " ".join(p.strip() for p in parts_nested if p and str(p).strip())
        cleaned_nested = DoDetailsSpider._clean_text(nested)
        if cleaned_nested and not re.fullmatch(r"\d+(?:\.\d+)?", cleaned_nested):
            return unescape(cleaned_nested)
        return None

    @staticmethod
    def _ticket_price_title_and_text(response):
        chunks = []
        for node in response.xpath('//*[@itemprop="price"][contains(@class,"ds-ticket-info")]'):
            title = unescape((node.xpath("./@title").get() or "").strip())
            body = unescape(" ".join(node.xpath(".//text()").getall() or []).strip())
            chunks.append(" ".join([title, body]).strip())
        return " ".join(c for c in chunks if c).strip()

    @staticmethod
    def _parse_time_segment_to_hm(segment):
        """Parse fragments like '7 PM', '7:30pm', '10 PM' -> (hour24, minute)."""
        segment = segment.strip()
        if not segment:
            return None
        m = re.search(
            r"(?i)(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
            segment,
        )
        if not m:
            return None
        h = int(m.group(1))
        minute = int(m.group(2) or 0)
        ap = (m.group(3) or "").lower()
        if ap == "am":
            if h == 12:
                h = 0
        else:
            if h != 12:
                h += 12
        if h > 23 or minute > 59:
            return None
        return h, minute

    @staticmethod
    def _dual_show_start_utc_values(start_raw, end_raw, ticket_blob):
        """
        When ticket copy lists two show times (e.g. 'Shows at 7 PM & 10 PM') and schema
        has no endDate, emit two start instants on the same calendar date as start_raw.
        Returns None if single-record logic should apply.
        """
        if not start_raw or not str(start_raw).strip():
            return None
        _, schedule_type, _ = _schedule_meta(start_raw)
        if end_raw and str(end_raw).strip():
            end_norm = _normalize_schedule_value(end_raw, schedule_type)
            if end_norm is not None:
                return None
        if not ticket_blob:
            return None
        m = re.search(r"shows?\s+at\s+(.+)$", ticket_blob, re.I)
        if not m:
            return None
        tail = m.group(1).strip()
        segments = re.split(r"\s*&\s*|\s+and\s+", tail, flags=re.I)
        segments = [s.strip() for s in segments if s.strip()]
        if len(segments) < 2:
            return None

        hms = []
        for seg in segments[:2]:
            hm = DoDetailsSpider._parse_time_segment_to_hm(seg)
            if hm is None:
                return None
            hms.append(hm)

        text = str(start_raw).strip().replace("Z", "+00:00")
        if re.match(r".*[+-]\d{4}$", text):
            text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
        try:
            base = datetime.fromisoformat(text)
        except ValueError:
            return None
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)

        out = []
        for h, minute in hms:
            local_start = base.replace(hour=h, minute=minute, second=0, microsecond=0)
            out.append(
                DoDetailsSpider._to_utc_iso(
                    local_start.isoformat(timespec="seconds"),
                )
            )
        if len(out) == 2 and out[0] == out[1]:
            return None
        return out if len(out) == 2 else None

    @staticmethod
    def _extract_event_tags(response):
        """
        Age lines and showtime hints often live in the ticket row (itemprop=price),
        e.g. title='$20.54,  18+' or 'Shows at 7 PM & 10 PM'.
        """
        tags = []
        seen = set()

        def add(tag):
            tag = DoDetailsSpider._clean_text(tag)
            if not tag or tag.lower() in seen:
                return
            seen.add(tag.lower())
            tags.append(tag)

        nodes = response.xpath(
            '//*[@itemprop="price"][contains(@class,"ds-ticket-info")]'
        )
        for node in nodes:
            title = unescape((node.xpath("./@title").get() or "").strip())
            body = unescape(" ".join(node.xpath(".//text()").getall() or []).strip())
            blob = " ".join([title, body]).strip()

            # Word boundaries fail after '+' (non-word); match ages without trailing \\b.
            for m in re.finditer(r"(?<![0-9])(1[89]\+|21\+)(?![0-9])", blob, re.I):
                add(m.group(1))

            for m in re.finditer(
                r"\b(all\s*ages|family[-\s]?friendly|kid[s]?\s*friendly|no\s*cover)\b",
                blob,
                re.I,
            ):
                add(m.group(1).replace("  ", " ").strip())

        # Drop pure time-window tags like "7 - 9PM" / "7PM-9PM".
        filtered = []
        for tag in tags:
            if re.fullmatch(
                r"(?i)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*[-–]\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*",
                tag,
            ):
                continue
            filtered.append(tag)
        return filtered

    @staticmethod
    def get_source(response):
        url = response.url

        if "do615" in url:
            return {
                "name": "Do615",
                "id": "do615",
                "url": "https://do615.com"
            }
        elif "do512" in url:
            return {
                "name": "Do512",
                "id": "do512",
                "url": "https://do512.com"
            }
        else:
            return {"name": "DoEvents", "id": "doevents", "url": url}

    @staticmethod
    def _extract_price_bounds(response):
        price_values = response.xpath(
            '//*[@itemprop="price"]/@title | //*[@itemprop="price"]/text()'
        ).getall()

        parsed = []
        is_free = False

        for value in price_values:
            if not value:
                continue

            value = value.lower().strip()

            # Handle "free" or "no cover"
            if "free" in value or "no cover" in value:
                is_free = True

            # Extract $ amounts
            matches = re.findall(r"\$(\d+(?:\.\d+)?)", value)

            for m in matches:
                try:
                    num = float(m)
                    parsed.append(int(num) if num.is_integer() else num)
                except ValueError:
                    continue

        if not parsed:
            return None, None, is_free

        return min(parsed), max(parsed), is_free

    def _build_media(self, image_url, title, event_id, source):
        if not image_url:
            return []

        media_id = None

        try:
            if "aws_asset/aws_asset/" in image_url:
                media_id = image_url.split("aws_asset/aws_asset/")[1].split("/")[0]
        except Exception:
            media_id = None

        # media_id = f"media-{media_id}" if media_id else f"media-{event_id}"

        return [
            {
                "id": "media-001",
                "type": "image",
                "title": title,
                "shortDescription": None,
                "source": {
                    "name": source.get("name"),
                    "id": str(event_id),
                    "url": image_url,
                },
                "thumbnail": {
                    "url": image_url,
                    "width": None,
                    "height": None,
                },
                "variants": [],
            }
        ]

    @staticmethod
    def _safe_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return None
        value = re.sub(r"\s+", " ", str(value)).strip()
        return value or None

    @staticmethod
    def _to_utc_iso(value):
        if not value:
            return None

        text = str(value).strip().replace("Z", "+00:00")
        if re.match(r".*[+-]\d{4}$", text):
            text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"

        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_utc = dt.astimezone(timezone.utc)
            return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        except ValueError:
            return value

    @staticmethod
    def _build_event_pricing(min_price, max_price, is_free):
        if min_price is None and max_price is None:
            return {
                "kind": "tbd",
                "currency": None,
                "minAmount": None,
                "maxAmount": None,
            }

        if is_free or ((min_price == 0) and (max_price == 0)):
            return {"kind": "free", "currency": "USD", "amount": 0}

        if min_price == max_price:
            # Keep only kind for zero-value events.
            if min_price == 0:
                return {"kind": "free", "currency": "USD", "amount": 0}
            return {"kind": "point", "currency": "USD", "amount": min_price}

        return {
            "kind": "span",
            "currency": "USD",
            "minAmount": min_price,
            "maxAmount": max_price,
        }

    @staticmethod
    def _is_past_year(start_value):
        if not start_value:
            return False
        year_match = re.match(r"^(\d{4})", str(start_value))
        if not year_match:
            return False
        return int(year_match.group(1)) < datetime.now(timezone.utc).year

    @staticmethod
    def _utc_now_iso():
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
