import csv
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import scrapy
from ..items import EventbriteEventItem
from parsel import Selector
from scrapy.http import Request
from scrapy.spidermiddlewares.httperror import HttpError

# Project root (directory that contains scrapy.cfg and listing CSV by default).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_JSON_FEED = "eventbrite_events_data_08_04_2026.json"
_DMA_CSV_FILENAME = "Tegna - Pilot Scope Locations.csv"

# DMA Name must contain this keyword (substring, case-insensitive); St Abv must match.
_TARGET_DMA_KEYWORDS = (
    ("ATLANTA", "GA"),
    ("AUSTIN", "TX"),
    ("ORLANDO", "FL"),
    ("NASHVILLE", "TN"),
)

_logger = logging.getLogger(__name__)


class EventbriteDetailSpider(scrapy.Spider):
    name = "eventbrite_detail"
    allowed_domains = ["eventbrite.com"]

    # zip (5 digits str) -> (dma_name, st_abv); None = not loaded yet
    _dma_zip_lookup = None

    default_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua-mobile": "?0",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }

    custom_settings = {
        "DEFAULT_REQUEST_HEADERS": default_headers,
    }

    @classmethod
    def update_settings(cls, settings):
        """
        Write FEEDS to an absolute path under the project root. Relative FEEDS paths
        are resolved from the process cwd, so running `scrapy crawl` from another
        folder would create the JSON elsewhere or make it look like no file was written.
        """
        super().update_settings(settings)
        output_path = (_PROJECT_ROOT / _DEFAULT_JSON_FEED).resolve()
        settings.set(
            "FEEDS",
            {
                str(output_path): {
                    "format": "json",
                    "overwrite": True,
                    "indent": 2,
                    "encoding": "utf-8",
                }
            },
        )

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        uris = list((crawler.settings.get("FEEDS") or {}).keys())
        spider.logger.info("eventbrite_detail JSON feed path(s): %s", uris)
        return spider

    def __init__(self, urls_file="eventbrite_listing_urls.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.urls_file = urls_file

    def closed(self, reason):
        count = 0
        if self.crawler.stats:
            count = self.crawler.stats.get_value("item_scraped_count", 0) or 0
        self.logger.info(
            "eventbrite_detail finished (%s); items scraped: %s",
            reason,
            count,
        )
        if count == 0:
            self.logger.warning(
                "No items were written. Ensure eventbrite_listing_urls.csv exists under "
                "%s and contains URL rows; use `scrapy crawl eventbrite_detail` from the "
                "project folder (where scrapy.cfg is).",
                _PROJECT_ROOT,
            )

    def start_requests(self):
        urls_path = Path(self.urls_file)
        if not urls_path.is_absolute():
            urls_path = _PROJECT_ROOT / urls_path

        if not urls_path.exists():
            self.logger.error("URLs file not found: %s", urls_path)
            return

        with urls_path.open("r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            for idx, row in enumerate(reader, start=1):
                url = (row.get("url") or "").strip()
                if not url:
                    self.logger.warning("Skipping empty URL at row %d", idx)
                    continue

                yield Request(
                    url=url,
                    callback=self.parse_event,
                    errback=self.handle_request_error,
                    cb_kwargs={"csv_row": idx},
                    dont_filter=True,
                )

    def handle_request_error(self, failure):
        request = failure.request
        if failure.check(HttpError):
            response = failure.value.response
            self.logger.error(
                "HTTP error on %s (status=%s): %s",
                request.url,
                response.status,
                failure.getErrorMessage(),
            )
            return

        self.logger.error(
            "Request failed on %s: %s",
            request.url,
            failure.getErrorMessage(),
        )

    def parse_event(self, response, csv_row):
        try:
            payload = self._extract_next_data(response)
            context = payload["props"]["pageProps"]["context"]
            basic_info = context.get("basicInfo", {})
        except Exception as exc:
            self.logger.exception(
                "Failed parsing JSON payload for row=%s url=%s (%s)",
                csv_row,
                response.url,
                exc,
            )
            return

        try:
            item = self._build_item(payload, context, basic_info, response.url)
            yield item
        except Exception as exc:
            self.logger.exception(
                "Failed mapping event fields row=%s url=%s (%s)",
                csv_row,
                response.url,
                exc,
            )

    def _extract_next_data(self, response):
        json_text = response.xpath('//script[@id="__NEXT_DATA__"]/text()').get()
        if not json_text:
            raise ValueError("Missing __NEXT_DATA__ script")
        return json.loads(json_text)

    def _build_item(self, payload, context, basic_info, response_url):
        event_id = str(basic_info.get("id") or "")
        scrape_time = self._utc_now_iso()
        location = self._build_location(
            basic_info.get("venue") or {},
            basic_info.get("isOnline"),
        )
        site_id = self._resolve_site_id(
            location.get("address") or "",
            location.get("name") or "",
        )
        title = basic_info.get("name")
        is_online_flag = self._is_online_event_title_or_location(title, location.get("name"))
        provider = "forage"
        record_url = basic_info.get("url") or response_url

        summary_text = (basic_info.get("summary") or "").strip()
        details_text = self._extract_structured_description(context)
        description = self._merge_description(summary_text, details_text)

        item = EventbriteEventItem()
        item.update({
            "provider": provider,
            "module": "events",
            "groupId": self.triple_id("group", provider, event_id, record_url),
            "id": self.triple_id("id", provider, event_id, record_url),
            "createdAt": scrape_time,
            "updatedAt": scrape_time,
            "title": title,
            "source": {
                "name": "Eventbrite",
                "id": "eventbrite",
                "url": "https://www.eventbrite.com",
            },
            "recordSource": {
                "id": event_id,
                "url": record_url,
            },
            "location": location,
            "siteId": site_id,
            "isonline": is_online_flag,
            "metadata": {
                "event": {
                    "description": description,
                    "eventRoles": self._build_event_roles(basic_info),
                    "eventSchedule": self._build_schedule(basic_info),
                    "status": self._map_status(basic_info.get("status")),
                    "eventPricing": self._build_pricing(context),
                    "availability": self._map_availability(
                        (context.get("salesStatus") or {}).get("salesStatus")
                    ),
                    "purchaseUrl": record_url,
                    "audiences": [],
                    "categories": [],
                    "tags": self._build_tags(context),
                    "media": self._build_media(context, basic_info),
                }
            },
        })
        return item

    @staticmethod
    def _utc_now_iso():
        """
        Return current UTC time in ISO 8601 format like '2026-03-31T03:38:11Z'.
        """
        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _is_online_event_title_or_location(title, location_name) -> bool:
        t = (title or "").lower()
        n = (location_name or "").lower()
        return "online event" in t or "online event" in n

    @classmethod
    def _site_id_from_dma_zip(cls, address: str):
        """
        Map siteId using ZIP + market lookup when the metro name (e.g. Austin)
        is not in the address text.
        """
        zip_code = EventbriteDetailSpider._extract_us_zip_from_address(address or "")
        if not zip_code:
            return None
        row = cls._get_dma_zip_lookup().get(zip_code)
        if not row:
            return None
        dma_name, st_abv = row[0], row[1]
        st = re.sub(r"[^A-Za-z]", "", str(st_abv or ""))[:2].upper()
        if len(st) != 2:
            st = str(st_abv).strip().upper()[:2]
        dma_upper = re.sub(r"\s+", " ", str(dma_name or "").strip().upper())
        if "ATLANTA" in dma_upper and st == "GA":
            return 85
        if "AUSTIN" in dma_upper and st == "TX":
            return 269
        if "ORLANDO" in dma_upper and st == "FL":
            return None
        if "NASHVILLE" in dma_upper and st == "TN":
            return None
        return None

    @classmethod
    def _resolve_site_id(cls, address: str, name: str):
        """
        siteId from explicit city text first, then ZIP → DMA (e.g. Buda, TX in Austin DMA → 269).
        """
        text = f"{address} {name}".lower()
        if "orlando, fl" in text or "orlando" in text:
            return None
        if "nashville, tn" in text or "nashville" in text:
            return None
        if "atlanta, ga" in text or "atlanta" in text:
            return 85
        if "austin, tx" in text or "austin" in text:
            return 269
        return cls._site_id_from_dma_zip(address or "")

    @staticmethod
    def triple_id(*parts: str) -> str:
        joined = "|".join(str(part) for part in parts)
        value = uuid.uuid5(uuid.NAMESPACE_URL, joined)
        digits = f"{value.int:032d}"
        return f"{int(digits[0:4])}-{int(digits[4:10])}-{int(digits[10:18])}"

    @staticmethod
    def _extract_text_from_html(html_content):
        if not html_content:
            return ""
        selector = Selector(text=html_content)
        raw_text = selector.xpath("string(.)").get() or ""
        cleaned = re.sub(r"\s+", " ", unescape(raw_text)).strip()
        return cleaned

    def _extract_structured_description(self, context):
        modules = ((context.get("structuredContent") or {}).get("modules") or [])
        parts = []
        for module in modules:
            text_html = module.get("text")
            text_content = self._extract_text_from_html(text_html)
            if text_content:
                parts.append(text_content)
        return "\n\n".join(parts)

    @staticmethod
    def _merge_description(summary, details):
        if summary and details:
            return f"{summary}\n\n{details}"
        return summary or details or None

    @classmethod
    def _get_dma_zip_lookup(cls):
        if cls._dma_zip_lookup is not None:
            return cls._dma_zip_lookup
        path = _PROJECT_ROOT / _DMA_CSV_FILENAME
        cls._dma_zip_lookup = EventbriteDetailSpider._load_dma_csv(path)
        return cls._dma_zip_lookup

    @staticmethod
    def _load_dma_csv(path: Path) -> dict:
        """Build zip (5 chars) -> (market, state) from Tegna pilot scope CSV."""
        result = {}
        if not path.exists():
            _logger.warning(
                "Market CSV not found at %s; nearBy will be false for physical events.",
                path,
            )
            return result

        with path.open("r", newline="", encoding="utf-8-sig") as file_obj:
            reader = csv.DictReader(file_obj)
            field_names = [f.strip() for f in (reader.fieldnames or [])]
            needed = {"Zip", "Market", "State"}
            if not needed.issubset(set(field_names)):
                _logger.warning(
                    "Could not detect Zip/Market/State columns in %s; headers=%s",
                    path,
                    field_names,
                )
                return result

            for row in reader:
                z = EventbriteDetailSpider._normalize_zip_cell(row.get("Zip"))
                if not z:
                    continue
                market = (row.get("Market") or "").strip()
                state = (row.get("State") or "").strip().upper()[:2]
                if not market and not state:
                    continue
                result[z] = (market, state)

        _logger.info("Loaded %d ZIP rows from market CSV %s", len(result), path.name)
        return result

    @staticmethod
    def _normalize_zip_cell(value):
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
        return None

    @staticmethod
    def _extract_us_zip_from_address(address: str):
        if not address:
            return None
        text = address.strip()
        # Prefer "ST 12345" or "ST 12345-6789" (avoids taking a leading street number like 12231).
        m = re.search(r"\b([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\b", text)
        if m:
            return m.group(2)
        # ZIP+4 or 5-digit ZIP at end of line/string.
        m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", text)
        if m:
            return m.group(1)
        # Last 5-digit token (street numbers often appear first).
        matches = list(re.finditer(r"\b(\d{5})(?:-\d{4})?\b", text))
        if matches:
            return matches[-1].group(1)
        m = re.search(r"(?<![0-9])(\d{5})(?![0-9])", text)
        return m.group(1) if m else None

    @staticmethod
    def _dma_matches_target_market(dma_name: str, st_abv: str) -> bool:
        if not dma_name or not st_abv:
            return False
        st = re.sub(r"[^A-Za-z]", "", str(st_abv))[:2].upper()
        if len(st) != 2:
            st = str(st_abv).strip().upper()[:2]
        dma_upper = re.sub(r"\s+", " ", str(dma_name).strip().upper())
        for keyword, state_key in _TARGET_DMA_KEYWORDS:
            if st != state_key:
                continue
            # Substring match so "ORLANDO-DAYTONA BCH-MELBRN" matches ORLANDO + FL
            if keyword in dma_upper:
                return True
        return False

    @staticmethod
    def _address_has_target_location_text(address: str) -> bool:
        text = (address or "").lower()
        targets = (
            "atlanta, ga",
            "austin, tx",
            "orlando, fl",
            "nashville, tn",
        )
        return any(target in text for target in targets)

    @classmethod
    def _compute_near_by(cls, location_name: str, address: str):
        """
        Returns:
          - True: ZIP found in address, workbook row matches target DMA + state
          - False: ZIP found but no row or DMA/state does not match targets
          - None: no ZIP in address (e.g. "Atlantic Station Atlanta, GA") or no address text
        """
        if not address or not str(address).strip():
            return None
        zip_code = EventbriteDetailSpider._extract_us_zip_from_address(address)
        if not zip_code:
            return True if EventbriteDetailSpider._address_has_target_location_text(address) else None
        lookup = cls._get_dma_zip_lookup()
        row = lookup.get(zip_code)
        if not row:
            return True if EventbriteDetailSpider._address_has_target_location_text(address) else False
        dma_name, st_abv = row[0], row[1]
        if EventbriteDetailSpider._dma_matches_target_market(dma_name, st_abv):
            return True
        return True if EventbriteDetailSpider._address_has_target_location_text(address) else False

    @classmethod
    def _build_location(cls, venue, is_online):
        address_obj = venue.get("address") or {}
        lines = address_obj.get("localizedMultiLineAddressDisplay") or []
        address = " ".join([line.strip() for line in lines if line and line.strip()]) or None
        address = EventbriteDetailSpider._clean_address_phone(address)
        city = address_obj.get("city")
        region = address_obj.get("region")
        raw_name = (venue.get("name") or "").strip()
        lowered_name = " ".join(raw_name.split()).strip().lower()
        keyword_triggers = (
            "recommended",
            "your choice",
            "use pdf",
            "pdf tickets",
            "tickets emailed",
            "qr",
            "no mobile",
        )
        if address and any(kw in lowered_name for kw in keyword_triggers):
            # If the "name" is really instructions, use the address as the display name.
            location_name = address
        else:
            place_parts = [raw_name or None, city, region]
            location_name = ", ".join([part for part in place_parts if part]) or None

        if not location_name and not address and is_online is True:
            location_name = "Online Event"

        latitude = EventbriteDetailSpider._safe_float(address_obj.get("latitude"))
        longitude = EventbriteDetailSpider._safe_float(address_obj.get("longitude"))

        # Invalid venue text (contact/instruction string) => null location details.
        if EventbriteDetailSpider._is_invalid_location_name(location_name):
            return {
                "name": None,
                "address": None,
                "latitude": None,
                "longitude": None,
                "nearBy": None,
            }

        # Online: "online" in name → normalize and null out geo fields + nearBy
        if location_name and "online" in location_name.lower():
            return {
                "name": "Online Event",
                "address": None,
                "latitude": None,
                "longitude": None,
                "nearBy": None,
            }

        near_by = cls._compute_near_by(location_name or "", address or "")

        return {
            "name": location_name,
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
            "nearBy": near_by,
        }

    @staticmethod
    def _clean_address_phone(address):
        if not address:
            return None
        cleaned = address
        # Remove labels like "PH-" / "Phone:" followed by phone numbers.
        cleaned = re.sub(
            r"(?i)\b(?:ph|phone|tel|telephone)\s*[:\-]?\s*(?:\+?\d[\d\-\s().]{6,}\d)\b",
            " ",
            cleaned,
        )
        # Remove standalone international/us phone-number patterns.
        cleaned = re.sub(r"\b\+?\d[\d\-\s().]{6,}\d\b", " ", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;-")
        return cleaned or None

    @staticmethod
    def _is_invalid_location_name(location_name):
        if not location_name:
            return False
        text = str(location_name).strip().lower()
        if "venue details" in text and ("reach us at" in text or "contact" in text):
            return True
        # Any embedded email in location name is treated as invalid venue text.
        if re.search(r"\b[\w.\-+%]+@[\w.\-]+\.[a-z]{2,}\b", text):
            return True
        return False

    @staticmethod
    def _safe_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_event_roles(basic_info):
        organizer = (basic_info.get("organizer") or {}).get("name")
        if organizer:
            return [{"organizer": organizer}]
        return []

    @staticmethod
    def _build_schedule(basic_info):
        start_local = ((basic_info.get("startDate") or {}).get("utc"))
        end_local = ((basic_info.get("endDate") or {}).get("utc"))
        same_start_end = bool(start_local and end_local and start_local == end_local)
        start_is_date_only = bool(
            isinstance(start_local, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_local)
        )
        end_is_date_only = bool(
            isinstance(end_local, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_local)
        )
        both_date_only = start_is_date_only and end_is_date_only

        precision = "date" if both_date_only else "dateTime"
        value_type = "date" if both_date_only else "dateTime"
        all_day = both_date_only

        return {
            "kind": "point" if same_start_end else "span",
            "precision": precision,
            "allDay": all_day,
            "start": {"type": value_type, "value": start_local},
            "end": {"type": value_type, "value": None if same_start_end else end_local},
        }

    @staticmethod
    def _build_pricing(context):
        offers = ((context.get("seo") or {}).get("offersSchema") or [])
        low_price = None
        high_price = None
        currency = None
        for offer in offers:
            currency = currency or offer.get("priceCurrency")
            low_price = EventbriteDetailSpider._coerce_number(
                offer.get("lowPrice"), fallback=low_price
            )
            high_price = EventbriteDetailSpider._coerce_number(
                offer.get("highPrice"), fallback=high_price
            )

        has_donation = bool(
            context.get("hasDonationTicketsAvailable")
            or (context.get("basicInfo") or {}).get("hasDonationTicketsAvailable")
        )

        if low_price == 0 and high_price == 0:
            if has_donation:
                return {
                    "kind": "donation",
                    "currency": currency,
                }
            return {"kind": "free"}

        if low_price is not None and high_price is not None and low_price == high_price:
            return {
                "kind": "point",
                "currency": currency,
                "amount": low_price,
            }

        return {
            "kind": "span",
            "currency": currency,
            "minAmount": low_price,
            "maxAmount": high_price,
        }

    @staticmethod
    def _coerce_number(value, fallback=None):
        if value is None:
            return fallback
        try:
            number = float(value)
            return int(number) if number.is_integer() else number
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _map_status(status):
        if not status:
            return "scheduled"
        mapping = {
            "live": "scheduled",
            "ended": "completed",
            "canceled": "cancelled",
            "cancel":"cancelled",
            "started": "scheduled",
            "postponed":"postponed",
            "postpone":"postponed",
        }
        return mapping.get(str(status).lower(), "scheduled")

    @staticmethod
    def _map_availability(sales_status):
        if not sales_status:
            return None
        mapping = {
            "on_sale": "onSale",
            "sold_out": "soldOut",
            "off_sale": "offSale",
            "notApplicable": "notApplicable",
            "not_applicable": "notApplicable",
            "comingSoon": "comingSoon",
            "coming_soon": "comingSoon",
            "presale": "presale",
            "pre_sale": "presale",
            "waitList": "waitList",
            "wait_list": "waitList",
            "closed": "closed",
            "not_yet_on_sale": "comingSoon"
        }
        return mapping.get(str(sales_status).lower(), str(sales_status))

    @staticmethod
    def _build_categories(context):
        taxonomies = context.get("taxonomies") or {}
        values = [taxonomies.get("category"), taxonomies.get("subcategory")]
        return [v for v in values if v]

    @staticmethod
    def _build_tags(context):
        tags = []
        breadcrumb_tags = context.get("tags") or []
        for tag in breadcrumb_tags:
            text_obj = tag.get("text") or {}
            placeholders = text_obj.get("placeholders") or {}
            category_string = placeholders.get("category_string")
            format_string = placeholders.get("format_string")
            if category_string:
                tags.append(category_string)
            if format_string:
                tags.append(format_string)

        highlights = ((context.get("goodToKnow") or {}).get("highlights") or {})
        parking = highlights.get("parking")
        if parking:
            tags.append(parking)

        return list(dict.fromkeys(tags))

    @staticmethod
    def _build_audiences(context):
        highlights = ((context.get("goodToKnow") or {}).get("highlights") or {})
        age = highlights.get("ageRestriction")
        if age is None:
            return []
        return [age]

    def _build_media(self, context, basic_info):
        images = ((context.get("gallery") or {}).get("images") or [])
        media = []
        event_name = basic_info.get("name")
        seen_urls = set()
        index = 0

        for image in images:
            main_url = self._pick_gallery_main_url(image)
            if not main_url or main_url in seen_urls:
                continue
            seen_urls.add(main_url)
            index += 1

            media_id = f"media-{index:03d}"
            thumb_w = self._extract_width_from_url(main_url)
            thumb_h = self._extract_height_from_url(main_url)
            media.append(
                {
                    "id": media_id,
                    "type": "image",
                    "title": event_name or "Event image",
                    "shortDescription": None,
                    "source": {
                        "name": "Eventbrite",
                        "id": None,
                        "url": main_url,
                    },
                    "thumbnail": {
                        "url": main_url,
                        "width": thumb_w,
                        "height": thumb_h,
                    },
                    "variantUrl": self._build_media_variant_urls(image, main_url),
                }
            )

        return media

    @staticmethod
    def _pick_gallery_main_url(image):
        """
        Some events have empty `gallery.images[*].url` but the cropped URLs exist.
        Use the first available URL as the main image.
        """
        # for key in ("url", "croppedLogoUrl940", "croppedLogoUrl600", "croppedLogoUrl480"):
        #     value = (image.get(key) or "").strip()
        #     if value:
        #         return value
        # return None

        for key,value in image.items():
            if value and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_height_from_url(url):
        try:
            query = parse_qs(urlparse(url).query)
            values = query.get("h")
            if values:
                return int(values[0])
        except Exception:
            return None
        return None

    @staticmethod
    def _extract_width_from_url(url):
        try:
            query = parse_qs(urlparse(url).query)
            values = query.get("w")
            if values:
                return int(values[0])
        except Exception:
            return None
        return None

    @staticmethod
    def _build_media_variant_urls(image, main_url: str):
        """Alternate sizes only; each entry uses key variantUrl plus width/height from query if present."""
        urls = []

        for key,value in image.items():
            if value and value.strip():
                urls.append(value.strip())

        out = []
        seen = set()
        main_norm = (main_url or "").strip()
        for url in urls:
            u = (url or "").strip()
            if not u or u in seen:
                continue
            seen.add(u)
            if u == main_norm:
                continue
            out.append(
                {
                    "variantUrl": u,
                    "width": EventbriteDetailSpider._extract_width_from_url(u),
                    "height": EventbriteDetailSpider._extract_height_from_url(u),
                }
            )
        return out
