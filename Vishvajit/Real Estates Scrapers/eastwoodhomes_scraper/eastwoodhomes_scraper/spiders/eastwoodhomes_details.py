from __future__ import annotations
import csv
import html
import json
import re
from pathlib import Path
from urllib.parse import urldefrag
import scrapy

BASE = "https://www.eastwoodhomes.com"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/",
    "Accept-Encoding": "gzip, deflate",
}

DETAIL_FIELDS = [
    "url",
    "Community Details",
    "Community Name",
    "latitude",
    "longitude",
    "Street Address",
    "City",
    "State",
    "Zip Code",
    "County",
    "Model hours",
    "Phone number",
    "Website",
    "status",
    "Builder",
    "Product Type (SFD/SFA/CO)",
    "Model/Product Types Available",
    "Avg Lot Size",
    "Avg Lot - Width/Depth",
    "Garages (Y/N)",
    "# of Garages",
    "Adult Community (Y/N)",
    "Amenities Available",
    "Amenity Type",
    "Attributes/Features",
    "Overall Description of the Community",
    "Foundation Type",
    "Exterior Specifications Available",
    "Interior Specifications Available",
    "HOA Fee",
    "HOA Services",
    "Other Fees (i.e. CDD)",
    "City/Town/Property Tax %",
    "Sales Start Date",
    "Sold Out Date",
    "Total Lots Sold",
    "Incentives",
    "QMI Incentives",
    "QMI Model Name",
    "QMI # of Garages",
    "QMI # of BR",
    "QMI # of BA",
    "QMI # of 1/2 BA",
    "QMI Model SQFT",
    "QMI Model Price",
    "QMI Availability Date",
    "QMI Lot SQFT",
    "QMI Interior/Exterior Attributes",
    "Model Details",
    "Model Name",
    "Plan Description",
    "# BR",
    "# BA",
    "# 1/2 BA",
    "# of Floors",
    "Parking Type",
    "Plan Garage Entry (Front load)",
    "# Garages",
    "Minimum SQFT",
    "Maximum SQFT",
    "Minimum Base Price (Current)",
    "Maximum Base Price/All In Price",
    "Previous Price",
    "Last Updated",
    "Plan Features",
    "Interior Specifications Descriptions",
    "First Floor Master (Y/N)",
    "images",
]


class EastwoodHomesDetailsSpider(scrapy.Spider):
    name = "eastwoodhomes_details"
    allowed_domains = ["eastwoodhomes.com", "www.eastwoodhomes.com"]

    custom_settings = {
        "FEEDS": {
            "eastwoodhomes_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            }
        }
    }

    def __init__(self, input_csv="eastwoodhomes_listings.csv", urls=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv
        self.urls_arg = urls

    def start_requests(self):
        rows = []
        if self.urls_arg:
            for raw in str(self.urls_arg).split(","):
                house_url = self._normalize_url(raw)
                if house_url:
                    rows.append({"house_url": house_url})
        else:
            csv_path = Path(self.input_csv)
            if not csv_path.is_absolute():
                csv_path = Path.cwd() / csv_path
            if not csv_path.exists():
                raise FileNotFoundError(f"Input CSV not found: {csv_path}")

            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    house_url = self._normalize_url(row.get("house_url") or row.get("url"))
                    if not house_url:
                        continue
                    rows.append({"house_url": house_url})

        for row in rows:
            house_url = row["house_url"]
            yield scrapy.Request(
                house_url,
                callback=self.parse_house,
                headers=REQUEST_HEADERS,
                meta={"house_url": house_url},
                dont_filter=True,
            )

    def parse_house(self, response):
        house_url = response.meta.get("house_url") or self._normalize_url(response.url)

        item = {field: "" for field in DETAIL_FIELDS}
        item["url"] = house_url
        item["Website"] = BASE + "/"
        item["Builder"] = "Eastwood Homes"
        atlas = self._extract_atlas_params(response.text or "")
        map_data = self._extract_map_data(response.text or "")
        map_state_abbr = self._state_abbr(map_data.get("state"))
        map_full_address = self._full_address_line(
            self._clean_join([map_data.get("address")]),
            self._clean_join([map_data.get("city")]),
            map_state_abbr,
            self._clean_join([map_data.get("zip")]),
        )

        ld_objs = self._json_ld_objects(response)
        place = self._pick_ld(ld_objs, ("localbusiness", "place", "residence"))
        ld_residence = self._pick_ld(ld_objs, ("singlefamilyresidence", "residence", "house", "product"))

        item["QMI Model Name"] = self._first_non_empty([atlas.get("planName"), map_data.get("name")])
        item["Model Name"] = item["QMI Model Name"]

        text_blob = self._clean_join(response.xpath("//body//text()[normalize-space()]").getall())
        item["Community Name"] = self._first_non_empty([atlas.get("communityName"), map_data.get("neighborhood_name")])
        item["County"] = self._normalize_county(self._first_non_empty([map_data.get("county"), self._county_from_text(text_blob)]))
        item["Street Address"] = map_full_address
        if item["Street Address"]:
            city, state, zip_code = self._city_state_zip_from_address(item["Street Address"])
            item["City"] = city
            item["State"] = state
            item["Zip Code"] = zip_code
        item["City"] = item["City"] or self._clean_join([map_data.get("city")])
        item["State"] = item["State"] or map_state_abbr
        item["Zip Code"] = item["Zip Code"] or self._clean_join([map_data.get("zip")])
        item["latitude"] = self._clean_join([map_data.get("latitude")])
        item["longitude"] = self._clean_join([map_data.get("longitude")])

        item["status"] = self._status_name(map_data.get("status"))
        ld_sqft = self._ld_floor_size_value(ld_residence)
        item["QMI Model SQFT"] = self._digits_only(ld_sqft)
        item["Minimum SQFT"] = item["QMI Model SQFT"]
        item["QMI # of BR"] = self._digits_only(map_data.get("bedrooms"))
        item["QMI # of BA"] = self._digits_only(map_data.get("bathrooms"))
        item["QMI # of 1/2 BA"] = self._digits_only(map_data.get("half_baths"))
        item["QMI # of Garages"] = self._digits_only(map_data.get("garage"))
        item["QMI Model Price"] = self._money_digits(map_data.get("price"))

        item["# BR"] = item["QMI # of BR"]
        item["# BA"] = item["QMI # of BA"]
        item["# 1/2 BA"] = item["QMI # of 1/2 BA"]
        item["# Garages"] = item["QMI # of Garages"]
        item["# of Garages"] = item["QMI # of Garages"]
        item["Garages (Y/N)"] = ""
        item["Parking Type"] = ""
        item["Minimum Base Price (Current)"] = item["QMI Model Price"]
        item["# of Floors"] = self._digits_only(map_data.get("stories"))

        community_obj = map_data.get("community") if isinstance(map_data.get("community"), dict) else {}
        item["Phone number"] = self._normalize_phone(community_obj.get("model_home_phone") or "")
        item["Model hours"] = self._model_hours(response)
        item["Plan Description"] = self._strip_html(map_data.get("description"))
        item["Overall Description of the Community"] = ""
        amenity_lines = self._amenities_list(response)
        plan_features = self._unique_features_list(response)
        item["Plan Features"] = " | ".join(amenity for amenity in plan_features if amenity)
        item["Attributes/Features"] = item["Plan Features"]
        item["Amenity Type"] = " | ".join(amenity for amenity in amenity_lines if amenity)
        item["Amenities Available"] = "Y" if item["Amenity Type"] else ""
        item["Incentives"] = ""
        item["QMI Incentives"] = item["Incentives"]
        item["Product Type (SFD/SFA/CO)"] = self._ld_product_type(ld_residence)
        item["Model/Product Types Available"] = "Y" if item["Product Type (SFD/SFA/CO)"] else "N"
        item["Previous Price"] = self._money_digits(map_data.get("before_price"))

        if isinstance(place, dict):
            geo = place.get("geo")
            if isinstance(geo, dict):
                item["latitude"] = item["latitude"] or self._clean_join([geo.get("latitude")])
                item["longitude"] = item["longitude"] or self._clean_join([geo.get("longitude")])

        item["images"] = self._extract_home_images(map_data)
        self._apply_html_fallbacks(response, item)

        # Floor-plan URL from the home page only (no HTTP request to floor-plan pages).
        fp_href = response.xpath('//a[contains(@href, "-floor-plan")]/@href').get()
        if fp_href:
            item["Model Details"] = self._normalize_url(response.urljoin(fp_href))

        # Final cleanup from script-derived fields.
        if atlas.get("planName"):
            item["QMI Model Name"] = str(atlas.get("planName")).strip()
            item["Model Name"] = item["QMI Model Name"]
        if atlas.get("communityName"):
            item["Community Name"] = str(atlas.get("communityName")).strip()
        item["Community Details"] = ""
        item["County"] = self._normalize_county(item.get("County") or "")

        self._blank_zero_numeric_fields(item)
        yield item

    @staticmethod
    def _json_ld_objects(response):
        out = []
        for raw in response.xpath('//script[@type="application/ld+json"]/text()').getall():
            text = (raw or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, list):
                out.extend([x for x in payload if isinstance(x, dict)])
            elif isinstance(payload, dict):
                out.append(payload)
        return out

    @classmethod
    def _pick_ld(cls, objs, type_substrings: tuple[str, ...]):
        for obj in objs:
            types = cls._ld_types(obj)
            low_types = [t.lower() for t in types]
            for sub in type_substrings:
                if any(sub in t for t in low_types):
                    return obj
        return {}

    @staticmethod
    def _ld_types(obj):
        t = obj.get("@type")
        if isinstance(t, list):
            return [str(x) for x in t]
        return [str(t or "")]

    @staticmethod
    def _normalize_url(url: str) -> str:
        raw = str(url or "").strip()
        if not raw:
            return ""
        clean, _ = urldefrag(raw)
        clean = clean.split("?")[0].strip()
        if clean.startswith("http://") or clean.startswith("https://"):
            return clean.rstrip("/")
        return f"{BASE}{clean if clean.startswith('/') else '/' + clean}".rstrip("/")

    @staticmethod
    def _digits_only(value) -> str:
        if value is None:
            return ""
        m = re.search(r"\d+", str(value))
        return m.group(0) if m else ""

    @staticmethod
    def _money_digits(value) -> str:
        if value is None:
            return ""
        m = re.search(r"\$?\s*([\d,]+)", str(value))
        if not m:
            return ""
        return m.group(1).replace(",", "")

    @staticmethod
    def _to_int(value):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _extract_atlas_params(html_text: str) -> dict:
        m = re.search(r"rtxContext:\s*(\{.*?\})\s*,\s*display\s*:", html_text or "", flags=re.S)
        if not m:
            return {}
        raw = m.group(1).strip()
        try:
            payload = json.loads(raw)
        except Exception:
            return {}
        params = payload.get("params") if isinstance(payload, dict) else {}
        return params if isinstance(params, dict) else {}

    @staticmethod
    def _extract_map_data(html_text: str) -> dict:
        m = re.search(r"var\s+map,\s*mainMarker,\s*data\s*=\s*(\{.*?\});", html_text or "", flags=re.S)
        if not m:
            return {}
        raw = m.group(1).strip()
        try:
            payload = json.loads(raw)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _extract_home_images(map_data: dict):
        if not isinstance(map_data, dict):
            return []
        home_images = map_data.get("home_images")
        if not isinstance(home_images, list) or not home_images:
            return []
        out = []
        seen = set()
        for row in home_images:
            if not isinstance(row, dict):
                continue
            image = row.get("image")
            if not isinstance(image, dict):
                continue
            image2 = image.get("image")
            if not isinstance(image2, dict):
                continue
            url = str(image2.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    @classmethod
    def _apply_html_fallbacks(cls, response, item: dict):
        specs = cls._extract_specs_from_html(response)
        page_source_specs = cls._extract_specs_from_page_source(response.text or "")

        bedrooms_value = cls._first_non_empty([specs.get("bedrooms"), page_source_specs.get("bedrooms")])
        item["# BR"] = item["# BR"] or cls._digits_only(bedrooms_value)
        item["QMI # of BR"] = item["QMI # of BR"] or item["# BR"]

        full_baths_value = cls._first_non_empty([specs.get("full_baths"), page_source_specs.get("full_baths")])
        item["# BA"] = item["# BA"] or cls._digits_only(full_baths_value)
        item["QMI # of BA"] = item["QMI # of BA"] or item["# BA"]

        half_baths_value = cls._first_non_empty([specs.get("half_baths"), page_source_specs.get("half_baths")])
        item["# 1/2 BA"] = item["# 1/2 BA"] or cls._digits_only(half_baths_value)
        item["QMI # of 1/2 BA"] = item["QMI # of 1/2 BA"] or item["# 1/2 BA"]

        stories_value = cls._first_non_empty([specs.get("stories"), page_source_specs.get("stories")])
        item["# of Floors"] = item["# of Floors"] or cls._first_numeric(stories_value)

        html_price = cls._extract_price_from_html(response)
        if not html_price:
            html_price = cls._extract_price_from_page_source(response.text or "")
        if html_price:
            item["Minimum Base Price (Current)"] = item["Minimum Base Price (Current)"] or html_price
            item["QMI Model Price"] = item["QMI Model Price"] or html_price

        html_phone = cls._extract_phone_from_html(response)
        if not html_phone:
            html_phone = cls._extract_phone_from_page_source(response.text or "")
        item["Phone number"] = item["Phone number"] or html_phone

        if not item.get("images"):
            item["images"] = cls._extract_home_images_from_html(response)
        if not item.get("images"):
            item["images"] = cls._extract_home_images_from_page_source(response.text or "")

    @classmethod
    def _extract_specs_from_html(cls, response) -> dict:
        out = {}
        rows = response.xpath(
            '//div[contains(@class,"e-desc-list-specs")]//li[contains(@class,"e-desc-list-specs__list__item")]'
        )
        for row in rows:
            label = cls._clean_join(row.xpath('.//p[contains(@class,"e-spec__eyebrow")]//text()').getall()).lower()
            value = cls._clean_join(row.xpath('.//p[contains(@class,"e-spec__content")]//text()').getall())
            if not label or not value:
                continue
            if "bedroom" in label:
                out["bedrooms"] = out.get("bedrooms") or value
            elif "full-bath" in label or "full bath" in label or label == "bathrooms":
                out["full_baths"] = out.get("full_baths") or value
            elif "half-bath" in label or "half bath" in label:
                out["half_baths"] = out.get("half_baths") or value
            elif "stories" in label or "story" in label:
                out["stories"] = out.get("stories") or value
        return out

    @classmethod
    def _extract_price_from_html(cls, response) -> str:
        candidates = []
        candidates.extend(
            response.xpath(
                '//dt[.//span[contains(translate(normalize-space(),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"base price")]]'
                '/following-sibling::dd[1]//text()'
            ).getall()
        )
        for row in candidates:
            val = cls._money_digits(row)
            if val:
                return val
        return ""

    @classmethod
    def _extract_previous_price_from_html(cls, response) -> str:
        candidates = response.xpath(
            '//span[contains(translate(normalize-space(),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"old price")]'
            '/following::span[1]//text()'
        ).getall()
        for row in candidates:
            val = cls._money_digits(row)
            if val:
                return val
        return ""

    @classmethod
    def _extract_phone_from_html(cls, response) -> str:
        tel = cls._clean_join(response.xpath('//a[starts-with(@href,"tel:")]/@href').getall())
        if tel:
            return cls._normalize_phone(tel)
        text_candidate = cls._clean_join(response.xpath('//*[contains(@class,"phone")]//text()').getall())
        m = re.search(r"(?:\+?1[\s\-\.]?)?\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", text_candidate)
        return cls._normalize_phone(m.group(0)) if m else ""

    @classmethod
    def _extract_home_images_from_html(cls, response):
        urls = []
        seen = set()
        raw_urls = []
        raw_urls.extend(response.xpath('//meta[@property="og:image"]/@content').getall())
        raw_urls.extend(
            response.xpath(
                '//img/@src | //img/@data-src | //img/@data-lazy-src | //img/@data-original | //source/@srcset'
            ).getall()
        )
        for raw in raw_urls:
            first = str(raw or "").split(",")[0].strip().split(" ")[0].strip()
            if not first:
                continue
            first_lower = first.lower()
            if first_lower.endswith(".svg"):
                continue
            if "/mir_statuses/" in first_lower or "/icons/" in first_lower or "logo" in first_lower:
                continue
            full = response.urljoin(first)
            if full in seen:
                continue
            seen.add(full)
            urls.append(full)
        return urls

    @classmethod
    def _extract_specs_from_page_source(cls, html_text: str) -> dict:
        out = {}
        if not html_text:
            return out

        block_pattern = re.compile(
            r'<p[^>]*class="[^"]*e-spec__eyebrow[^"]*"[^>]*>(.*?)</p>\s*'
            r'<p[^>]*class="[^"]*e-spec__content[^"]*"[^>]*>(.*?)</p>',
            flags=re.I | re.S,
        )
        for label_html, value_html in block_pattern.findall(html_text):
            label = cls._strip_html(label_html).lower()
            value = cls._strip_html(value_html)
            if not label or not value:
                continue
            if "bedroom" in label:
                out["bedrooms"] = out.get("bedrooms") or value
            elif "full-bath" in label or "full bath" in label or label == "bathrooms":
                out["full_baths"] = out.get("full_baths") or value
            elif "half-bath" in label or "half bath" in label:
                out["half_baths"] = out.get("half_baths") or value
            elif "stories" in label or "story" in label:
                out["stories"] = out.get("stories") or value
        return out

    @classmethod
    def _extract_price_from_page_source(cls, html_text: str) -> str:
        if not html_text:
            return ""
        match = re.search(r'base\s+price[^$]{0,140}\$\s*([\d,]+)', html_text, flags=re.I)
        return match.group(1).replace(",", "") if match else ""

    @classmethod
    def _extract_phone_from_page_source(cls, html_text: str) -> str:
        if not html_text:
            return ""

        tel_match = re.search(r'href=["\']tel:([^"\']+)["\']', html_text, flags=re.I)
        if tel_match:
            return cls._normalize_phone(tel_match.group(1))

        phone_match = re.search(r'phone[^0-9]{0,30}((?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4})', html_text, flags=re.I)
        if phone_match:
            return cls._normalize_phone(phone_match.group(1))
        phone_match = re.search(r'model_home_phone[^0-9]{0,30}((?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4})', html_text, flags=re.I)
        return cls._normalize_phone(phone_match.group(1)) if phone_match else ""

    @classmethod
    def _extract_home_images_from_page_source(cls, html_text: str):
        if not html_text:
            return []

        urls = []
        seen = set()
        raw_matches = re.findall(
            r'https?://[^"\'\s<>]+\.(?:jpg|jpeg|png|webp)(?:\?[^"\'\s<>]*)?',
            html_text,
            flags=re.I,
        )
        for raw in raw_matches:
            clean = str(raw or "").strip()
            if not clean:
                continue
            if "/mir_statuses/" in clean:
                continue
            if clean in seen:
                continue
            seen.add(clean)
            urls.append(clean)
        return urls

    @staticmethod
    def _first_numeric(value) -> str:
        m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
        return m.group(0) if m else ""

    @staticmethod
    def _sqft_range_from_text(text: str):
        nums = re.findall(r"\d[\d,]*", str(text or ""))
        cleaned = [n.replace(",", "") for n in nums if n]
        if not cleaned:
            return "", ""
        if len(cleaned) == 1:
            return cleaned[0], ""
        ordered = sorted((int(n), n) for n in cleaned)
        return ordered[0][1], ordered[-1][1]

    @staticmethod
    def _normalize_county(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"^[A-Z]{2}\s+", "", text).strip()
        m = re.search(r"([A-Za-z][A-Za-z .'-]*County)", text, flags=re.I)
        if m:
            clean = re.sub(r"\s+", " ", m.group(1)).strip()
            return clean[0].upper() + clean[1:]
        return text

    @staticmethod
    def _state_abbr(value) -> str:
        if isinstance(value, dict):
            return str(value.get("state_abbr") or value.get("abbr") or "").strip().upper()
        raw = str(value or "").strip().upper()
        if len(raw) == 2:
            return raw
        return ""

    @staticmethod
    def _status_name(value) -> str:
        if isinstance(value, dict):
            return str(value.get("name") or value.get("internal_name") or "").strip()
        return str(value or "").strip()

    @staticmethod
    def _full_address_line(street, city, state, zip_code):
        street = str(street or "").strip()
        city = str(city or "").strip()
        state = str(state or "").strip()
        zip_code = str(zip_code or "").strip()
        if not street:
            return ""
        if city and state and zip_code:
            return f"{street}, {city}, {state} {zip_code}"
        if city and state:
            return f"{street}, {city}, {state}"
        return street

    @staticmethod
    def _clean_join(values):
        if isinstance(values, str):
            values = [values]
        text = " ".join(str(v or "").strip() for v in values if str(v or "").strip())
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _first_non_empty(values):
        for value in values or []:
            if str(value or "").strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _strip_html(value):
        text = str(value or "")
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _normalize_phone(raw: str) -> str:
        s = str(raw or "").replace("tel:", "").strip()
        digits = re.sub(r"\D", "", s)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return s

    @classmethod
    def _ld_floor_size_value(cls, ld_obj) -> str:
        if not isinstance(ld_obj, dict):
            return ""
        floor_size = ld_obj.get("floorSize")
        if isinstance(floor_size, dict):
            return cls._clean_join([floor_size.get("value")])
        return cls._clean_join([floor_size])

    @classmethod
    def _ld_product_type(cls, ld_obj) -> str:
        if not isinstance(ld_obj, dict):
            return ""
        types = cls._ld_types(ld_obj)
        blob = " ".join(str(t or "").lower() for t in types)
        if "singlefamilyresidence" in blob or "single family residence" in blob:
            return "SFD"
        if "townhouse" in blob or "townhome" in blob:
            return "SFA"
        if "condo" in blob or "apartment" in blob:
            return "CO"
        return ""

    @classmethod
    def _amenities_list(cls, response):
        rows = response.xpath(
            '//div[contains(@class,"s-caption__inner")][.//div[contains(@class,"s-caption__title") and contains(normalize-space(),"Amenities")]]'
            '//div[contains(@class,"s-caption__desc")]//li//text()'
        ).getall()
        out = []
        for row in rows:
            clean = cls._clean_join([row])
            if clean:
                out.append(clean)
        return out

    @classmethod
    def _unique_features_list(cls, response):
        rows = response.xpath(
            '//*[self::h1 or self::h2 or self::h3][contains(translate(normalize-space(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "unique features")]'
            '/following::ul[1]/li//text()'
        ).getall()
        out = []
        for row in rows:
            clean = cls._clean_join([row])
            if clean:
                out.append(clean)
        return out

    @staticmethod
    def _city_state_zip_from_address(address: str):
        if not address:
            return "", "", ""
        m = re.search(r",\s*([^,]+),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)", address)
        if m:
            return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        return "", "", ""

    @staticmethod
    def _community_from_text(text: str) -> str:
        m = re.search(r"\bCommunity\s+([A-Za-z0-9'&.\-\s]{2,80}?)\s+[A-Za-z .'-]+,\s*[A-Z]{2}\b", text)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        return ""

    @staticmethod
    def _county_from_text(text: str) -> str:
        m = re.search(r"\b([A-Za-z][A-Za-z\s]{1,40}\sCounty)\b", text)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _address_from_text(text: str) -> str:
        m = re.search(r"\b\d{1,6}\s+[A-Za-z0-9.'\- ]+,\s*[A-Za-z .'-]+,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?\b", text)
        return m.group(0).strip() if m else ""

    @classmethod
    def _model_hours(cls, response):
        lines = response.xpath('//*[contains(text(),"Model Home Hours")]/following::*[position()<=20]//text()').getall()
        cleaned = [cls._clean_join([x]) for x in lines if cls._clean_join([x])]
        out = []
        for idx, val in enumerate(cleaned):
            if ":" in val and idx + 1 < len(cleaned) and ("am" in cleaned[idx + 1].lower() or "pm" in cleaned[idx + 1].lower() or "appointment" in cleaned[idx + 1].lower()):
                out.append(f"{val} {cleaned[idx + 1]}")
            elif ":" in val and ("am" in val.lower() or "pm" in val.lower() or "appointment" in val.lower()):
                out.append(val)
        unique = []
        for row in out:
            if row not in unique and "model home hours" not in row.lower():
                unique.append(row)
        return " | ".join(unique[:7])

    @staticmethod
    def _blank_zero_numeric_fields(item: dict):
        numeric_fields = [
            "# BR",
            "# BA",
            "# 1/2 BA",
            "# Garages",
            "# of Garages",
            "# of Floors",
            "Minimum SQFT",
            "Maximum SQFT",
            "Minimum Base Price (Current)",
            "Maximum Base Price/All In Price",
            "Previous Price",
            "QMI # of Garages",
            "QMI # of BR",
            "QMI # of BA",
            "QMI # of 1/2 BA",
            "QMI Model SQFT",
            "QMI Model Price",
            "QMI Lot SQFT",
        ]
        for key in numeric_fields:
            value = str(item.get(key, "")).strip()
            if value in {"0", "0.0", "0.00"}:
                item[key] = ""
