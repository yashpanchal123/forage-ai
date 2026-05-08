"""
D.R. Horton details spider.

Input (from `drhorton_listing`):
  state, community_url, house_url

Output:
  One row per house (QMI). Each input row requests the unique `house_url` first
  (so Scrapy does not drop duplicate `community_url` requests), then merges
  community page fields in a second request.
"""

import csv
import json
import re
from pathlib import Path
from urllib.parse import urldefrag, urlparse

import scrapy

BASE = "https://www.drhorton.com"
BASE_WEBSITE = "https://www.drhorton.com/"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

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


class DrHortonDetailsSpider(scrapy.Spider):
    name = "drhorton_details"
    allowed_domains = ["drhorton.com", "www.drhorton.com"]

    custom_settings = {
        "ROBOTSTXT_OBEY": False,
        "DOWNLOAD_DELAY": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DEFAULT_REQUEST_HEADERS": {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "FEEDS": {
            "drhorton_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            }
        },
    }

    def __init__(self, input_csv="drhorton_listings.csv", urls=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv
        self.urls_arg = urls

    async def start(self):
        if self.urls_arg:
            for raw_url in str(self.urls_arg).split(","):
                house_url = self._normalize_url(raw_url)
                if not house_url:
                    continue
                community_url = self._community_url_from_house_url(house_url)
                yield scrapy.Request(
                    url=house_url,
                    callback=self.parse_house,
                    meta={
                        "state": "",
                        "community_url": community_url,
                        "house_url": house_url,
                    },
                )
            return

        csv_path = Path(self.input_csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path

        if not csv_path.exists():
            raise FileNotFoundError(f"Input CSV not found: {csv_path}")

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                state = (row.get("state") or "").strip().lower()
                community_url = self._normalize_url(row.get("community_url") or row.get("community") or row.get("url"))
                house_url = self._normalize_url(row.get("house_url") or row.get("house") or "")
                if not house_url:
                    continue
                if not community_url:
                    community_url = self._community_url_from_house_url(house_url)
                if not community_url:
                    continue
                yield scrapy.Request(
                    url=house_url,
                    callback=self.parse_house,
                    meta={
                        "state": state,
                        "community_url": community_url,
                        "house_url": house_url,
                    },
                )

    def parse_house(self, response):
        """Each row uses a unique house URL so Scrapy does not drop duplicates."""
        state = (response.meta.get("state") or "").strip().lower()
        community_url = response.meta.get("community_url") or ""
        house_url = response.meta.get("house_url") or self._normalize_url(response.url)

        item = {field: "" for field in DETAIL_FIELDS}
        item["url"] = house_url
        item["Website"] = BASE_WEBSITE
        item["Builder"] = "D.R. Horton"

        page_text = response.text or ""
        place_ld, business_ld = self._extract_ld_json(response)
        residence_addr = self._extract_residence_address_from_ld(response)
        segmentation = self._extract_segmentation_context(page_text)
        qmis_model = self._extract_embedded_model(page_text, "SortQmis")
        qmis = qmis_model.get("Items") or []
        qmi = self._match_qmi(qmis, house_url)
        visible_text = self._clean_join(response.xpath("//body//text()").getall())

        if qmi:
            item["Street Address"] = self._stringify(qmi.get("Address"))
            city, st, zip_code = self._parse_city_state_zip(qmi.get("CityStateZip"))
            item["City"] = city
            item["State"] = st
            item["Zip Code"] = zip_code
            item["Model Name"] = self._stringify(qmi.get("PlanName"))
            item["# BR"] = self._stringify(qmi.get("NumberOfBedrooms"))
            ba_val, half_val = self._split_single_bath_value(qmi.get("NumberOfBathrooms"))
            item["# BA"] = ba_val
            item["# 1/2 BA"] = half_val
            item["# of Floors"] = self._stringify(qmi.get("NumberOfStories"))
            item["# Garages"] = self._stringify(qmi.get("NumberOfGarages"))
            item["# of Garages"] = self._stringify(qmi.get("NumberOfGarages"))
            item["Garages (Y/N)"] = "Y" if self._to_number(qmi.get("NumberOfGarages")) > 0 else "N"
            item["Parking Type"] = "Garage" if item["Garages (Y/N)"] == "Y" else ""
            item["Minimum SQFT"] = self._stringify(qmi.get("SquareFootage"))
            item["status"] = self._normalize_status(qmi.get("Status"))

            lot_lat, lot_lng = self._extract_lot_lat_lng(qmi)
            if lot_lat:
                item["latitude"] = lot_lat
            if lot_lng:
                item["longitude"] = lot_lng

            qmi_thumb = ""
            if qmi.get("Thumbnail"):
                qmi_thumb = response.urljoin(str(qmi.get("Thumbnail")).split("?")[0])
            house_imgs = self._extract_images(response)
            item["images"] = self._dedupe(([qmi_thumb] if qmi_thumb else []) + house_imgs)
        else:
            merged = self._first_nonempty_postal(
                residence_addr,
                business_ld.get("address"),
                place_ld.get("address"),
            )
            item["Street Address"] = merged.get("streetAddress") or ""
            item["City"] = merged.get("addressLocality") or ""
            item["State"] = merged.get("addressRegion") or ""
            item["Zip Code"] = merged.get("postalCode") or ""
            item["images"] = self._extract_images(response)

        ld_fill = self._first_nonempty_postal(
            residence_addr,
            business_ld.get("address"),
            place_ld.get("address"),
        )
        if not item.get("Street Address"):
            item["Street Address"] = ld_fill.get("streetAddress") or ""
        if not item.get("City"):
            item["City"] = ld_fill.get("addressLocality") or ""
        if not item.get("State"):
            item["State"] = ld_fill.get("addressRegion") or ""
        if not item.get("Zip Code"):
            item["Zip Code"] = ld_fill.get("postalCode") or ""

        item["Street Address"] = self._full_address_line(
            item.get("Street Address"),
            item.get("City"),
            item.get("State"),
            item.get("Zip Code"),
        )

        item["County"] = self._extract_county_from_text(page_text)
        item["Product Type (SFD/SFA/CO)"] = self._map_product_type(segmentation.get("homeType"))
        if not item.get("latitude"):
            item["latitude"] = self._stringify(place_ld.get("latitude"))
        if not item.get("longitude"):
            item["longitude"] = self._stringify(place_ld.get("longitude"))
        if not item.get("status"):
            item["status"] = self._normalize_status(self._extract_status(response, visible_text))

        if not item.get("Model Name") or not item.get("# BR") or not item.get("# BA"):
            quick = self._extract_quick_stats_from_text(visible_text)
            if not item.get("Model Name"):
                item["Model Name"] = quick.get("model_name", "")
            if not item.get("# BR"):
                item["# BR"] = quick.get("beds", "")
            if not item.get("# BA"):
                item["# BA"] = quick.get("full_ba", "")
            if not item.get("# 1/2 BA"):
                item["# 1/2 BA"] = quick.get("half_ba", "")
            if not item.get("# Garages"):
                item["# Garages"] = quick.get("garages", "")
            if not item.get("# of Garages"):
                item["# of Garages"] = quick.get("garages", "")
            if not item.get("# of Floors"):
                item["# of Floors"] = quick.get("floors", "")
            if not item.get("Minimum SQFT"):
                item["Minimum SQFT"] = quick.get("sqft", "")
            if not item.get("Parking Type") and quick.get("garages"):
                item["Parking Type"] = "Garage"
            if not item.get("Garages (Y/N)") and quick.get("garages"):
                item["Garages (Y/N)"] = "Y"
        self._blank_qmi_fields(item)

        if not community_url:
            item["Community Details"] = ""
            yield item
            return

        yield scrapy.Request(
            url=community_url,
            callback=self.parse_community_merge,
            meta={
                "base_item": item,
                "state": state,
                "community_url": community_url,
                "house_url": house_url,
            },
            dont_filter=True,
        )

    def parse_community_merge(self, response):
        """Merge community page data; keep house-level address and QMI from base_item."""
        item = dict(response.meta["base_item"])
        community_url = response.meta.get("community_url") or response.url
        house_url = response.meta.get("house_url") or ""
        item["Website"] = BASE_WEBSITE

        page_text = response.text or ""
        description = self._clean_join(
            response.xpath('//*[contains(@class, "community-main-details")]//text()').getall()
        )
        amenities_text = self._clean_join(
            response.xpath(
                '//*[contains(translate(@class, "AMENITY", "amenity"), "amenit")]//text()'
            ).getall()
        )

        place_ld, business_ld = self._extract_ld_json(response)
        segmentation = self._extract_segmentation_context(page_text)
        plans_model = self._extract_embedded_model(page_text, "SortPlans")
        plans = plans_model.get("Items") or []

        place_name = (business_ld.get("name") or place_ld.get("name") or "").strip()
        item["Community Name"] = self._strip_title_prefix(place_name)

        if not item.get("latitude"):
            item["latitude"] = self._stringify(place_ld.get("latitude"))
        if not item.get("longitude"):
            item["longitude"] = self._stringify(place_ld.get("longitude"))

        if not item.get("County"):
            item["County"] = self._extract_county_from_text(page_text)
        if not item.get("Product Type (SFD/SFA/CO)"):
            item["Product Type (SFD/SFA/CO)"] = self._map_product_type(segmentation.get("homeType"))

        item["Model hours"] = self._format_opening_hours(business_ld.get("openingHoursSpecification"))
        phone = self._normalize_phone(response.css('a[href^="tel:"]::text').get())
        if not phone:
            phone = self._normalize_phone(response.xpath('//a[starts-with(@href, "tel:")]/@href').get())
        item["Phone number"] = phone

        if not item.get("status"):
            item["status"] = self._normalize_status(self._extract_status(response, self._clean_join(response.xpath("//body//text()").getall())))
        item["Sold Out Date"] = ""
        item["Model/Product Types Available"] = "Y" if item.get("Product Type (SFD/SFA/CO)") else "N"

        garage_values = self._numeric_values([p.get("NumberOfGarages") for p in plans])
        if not item.get("Garages (Y/N)"):
            item["Garages (Y/N)"] = "Y" if any(value > 0 for value in garage_values) else "N"
        if not item.get("# of Garages"):
            item["# of Garages"] = self._range_string(garage_values)
        if not item.get("# Garages"):
            item["# Garages"] = self._range_string(self._numeric_values([p.get("NumberOfGarages") for p in plans]))

        item["Adult Community (Y/N)"] = ""

        amenities = self._extract_amenities(amenities_text)
        item["Amenities Available"] = "Y" if amenities else "N"
        item["Amenity Type"] = ""
        item["Attributes/Features"] = ""
        item["Overall Description of the Community"] = description

        item["Incentives"] = ""
        item["QMI Incentives"] = ""

        item["Model Details"] = ""
        if not item.get("Model Name"):
            item["Model Name"] = self._join_unique([p.get("PlanName") for p in plans])
        if not item.get("# BR"):
            item["# BR"] = self._range_string(self._numeric_values([p.get("NumberOfBedrooms") for p in plans]))
        if not item.get("# BA"):
            full_ba, half_ba = self._split_bathroom_ranges([p.get("NumberOfBathrooms") for p in plans])
            item["# BA"] = full_ba
            if not item.get("# 1/2 BA"):
                item["# 1/2 BA"] = half_ba
        elif not item.get("# 1/2 BA"):
            _, half_ba = self._split_bathroom_ranges([p.get("NumberOfBathrooms") for p in plans])
            item["# 1/2 BA"] = half_ba
        if not item.get("# of Floors"):
            item["# of Floors"] = self._range_string(self._numeric_values([p.get("NumberOfStories") for p in plans]))
        sqft_values = self._numeric_values([p.get("SquareFootage") for p in plans])
        if not item.get("Minimum SQFT"):
            item["Minimum SQFT"] = self._stringify(min(sqft_values)) if sqft_values else ""
        item["Maximum SQFT"] = ""
        price_values = self._numeric_values([p.get("Price") for p in plans])
        item["Minimum Base Price (Current)"] = self._money(min(price_values)) if price_values else ""
        item["Maximum Base Price/All In Price"] = ""

        item["Plan Features"] = ""
        item["Interior Specifications Descriptions"] = ""
        item["First Floor Master (Y/N)"] = ""

        community_imgs = self._extract_images(response)
        if item.get("images"):
            existing = item["images"] if isinstance(item["images"], list) else []
            item["images"] = self._dedupe(existing + community_imgs)
        else:
            item["images"] = community_imgs

        item["Community Details"] = ""
        yield item

    def _community_url_from_house_url(self, house_url):
        u = self._normalize_url(house_url)
        if not u or "/qmis/" not in u.lower():
            return ""
        return u.split("/qmis/", 1)[0].rstrip("/")

    @staticmethod
    def _blank_qmi_fields(item):
        for key in [
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
        ]:
            item[key] = ""

    def _split_bathroom_ranges(self, values):
        nums = self._numeric_values(values)
        if not nums:
            return "", ""
        full_vals = []
        half_vals = []
        for v in nums:
            full = int(v)
            frac = abs(v - full)
            half = 1 if frac >= 0.49 else 0
            full_vals.append(full)
            half_vals.append(half)
        full_range = self._range_string(full_vals)
        half_range = self._range_string(half_vals)
        return full_range, half_range

    def _split_single_bath_value(self, value):
        n = self._to_number(value)
        if n is None:
            return "", ""
        full = int(n)
        frac = abs(n - full)
        half = 1 if frac >= 0.49 else 0
        return self._stringify(full), self._stringify(half)

    def _to_number(self, value):
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value or "").strip()
        if not s:
            return None
        m = re.search(r"\d+(?:\.\d+)?", s)
        if not m:
            return None
        try:
            return float(m.group(0))
        except ValueError:
            return None

    def _normalize_status(self, raw):
        text = self._clean_join([raw]).lower()
        if not text:
            return ""
        if "sold out" in text:
            return "Sold Out"
        if "under contract" in text or "pending" in text:
            return "Under Contract"
        if "for sale" in text or "available" in text or "move-in ready" in text or "move in ready" in text:
            return "For Sale"
        if "coming soon" in text:
            return "Coming Soon"
        return self._clean_join([raw])

    def _extract_lot_lat_lng(self, qmi):
        if not isinstance(qmi, dict):
            return "", ""
        lat = None
        lng = None
        for key in ("Latitude", "latitude", "Lat", "lat"):
            lat = qmi.get(key)
            if lat not in (None, ""):
                break
        for key in ("Longitude", "longitude", "Lng", "lng", "Lon", "lon"):
            lng = qmi.get(key)
            if lng not in (None, ""):
                break
        return self._stringify(lat), self._stringify(lng)

    def _extract_quick_stats_from_text(self, text):
        out = {
            "model_name": "",
            "beds": "",
            "full_ba": "",
            "half_ba": "",
            "garages": "",
            "floors": "",
            "sqft": "",
        }
        body = self._clean_join([text])
        m = re.search(
            r"(\d+)\s*Bed\s*\|\s*([\d.]+)\s*Bath\s*\|\s*(\d+)\s*Garage(?:\s*\|\s*(\d+)\s*Story)?",
            body,
            re.I,
        )
        if m:
            out["beds"] = m.group(1)
            full_ba, half_ba = self._split_single_bath_value(m.group(2))
            out["full_ba"] = full_ba
            out["half_ba"] = half_ba
            out["garages"] = m.group(3)
            out["floors"] = m.group(4) or ""
        m_sqft = re.search(r"([\d,]+)\s*Sq\.\s*Ft\.", body, re.I)
        if m_sqft:
            out["sqft"] = m_sqft.group(1).replace(",", "")
        m_plan = re.search(r"\|\s*([A-Za-z0-9 .'-]+?)\s+floor plan", body, re.I)
        if m_plan:
            out["model_name"] = self._clean_join([m_plan.group(1)])
        return out

    def _extract_county_from_text(self, text):
        scoped = str(text or "")
        for marker in ["Starting your search?", "#### Areas in", "Legal Information"]:
            if marker in scoped:
                scoped = scoped.split(marker, 1)[0]
        m = re.search(r"\b([A-Za-z][A-Za-z .'-]{1,60}\sCounty)\b", scoped, re.I)
        if not m:
            return ""
        return self._clean_join([m.group(1)])

    def _full_address_line(self, street, city, state, zip_code):
        street = self._clean_join([street])
        city = self._clean_join([city])
        state = self._clean_join([state])
        zip_code = self._clean_join([zip_code])
        if not street:
            return ""
        city_state_zip = ""
        if city and state and zip_code:
            city_state_zip = f"{city}, {state} {zip_code}"
        elif city and state:
            city_state_zip = f"{city}, {state}"
        elif city:
            city_state_zip = city
        elif state and zip_code:
            city_state_zip = f"{state} {zip_code}"
        elif state:
            city_state_zip = state
        elif zip_code:
            city_state_zip = zip_code
        if city_state_zip:
            return f"{street}, {city_state_zip}"
        return street

    def _parse_city_state_zip(self, raw):
        if raw is None:
            return "", "", ""
        s = str(raw).strip()
        if not s:
            return "", "", ""
        m = re.match(r"^(.+?)\s+([A-Za-z]{2})\s*,\s*(\d{5})(?:-\d{4})?\s*$", s)
        if m:
            return m.group(1).strip(), m.group(2).upper(), m.group(3)
        m2 = re.match(r"^(.+),\s*([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\s*$", s)
        if m2:
            return m2.group(1).strip(), m2.group(2).upper(), m2.group(3)
        return "", "", ""

    @staticmethod
    def _slugify_addressish(text):
        t = str(text or "").lower().strip()
        if not t:
            return ""
        t = re.sub(r"[^a-z0-9]+", "-", t)
        return t.strip("-")

    def _listing_slug_from_house_url(self, house_url):
        parts = [p for p in self._url_path(house_url).split("/") if p]
        if not parts:
            return ""
        return self._slugify_addressish(parts[-1].replace("-", " "))

    @staticmethod
    def _normalize_postal_dict(addr):
        if not isinstance(addr, dict):
            return {}
        low = {str(k).lower(): v for k, v in addr.items() if str(k).lower() != "@type"}

        def pick(*keys):
            for key in keys:
                v = low.get(key.lower())
                if v is not None and str(v).strip():
                    return str(v).strip()
            return ""

        return {
            "streetAddress": pick("streetAddress", "streetaddress"),
            "addressLocality": pick("addressLocality", "addresslocality"),
            "addressRegion": pick("addressRegion", "addressregion"),
            "postalCode": pick("postalCode", "postalcode"),
        }

    def _first_nonempty_postal(self, *sources):
        for src in sources:
            norm = self._normalize_postal_dict(src)
            if norm.get("streetAddress"):
                return norm
        return {}

    @staticmethod
    def _ld_type_tokens(obj):
        t = obj.get("@type")
        if isinstance(t, list):
            return [str(x).lower() for x in t]
        return [str(t or "").lower()]

    def _ld_collect_address_dicts(self, obj, seen):
        oid = id(obj)
        if oid in seen:
            return []
        seen.add(oid)
        out = []
        addr = obj.get("address")
        if isinstance(addr, dict):
            out.append(addr)
        elif isinstance(addr, str) and addr.strip():
            out.append({"streetAddress": addr.strip()})
        for g in obj.get("@graph", []) or []:
            if isinstance(g, dict):
                out.extend(self._ld_collect_address_dicts(g, seen))
        return out

    def _extract_residence_address_from_ld(self, response):
        fallback = {}
        seen = set()
        for raw in response.xpath('//script[@type="application/ld+json"]/text()').getall():
            text = (raw or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            objs = payload if isinstance(payload, list) else [payload]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                for cand in self._ld_collect_address_dicts(obj, seen):
                    norm = self._normalize_postal_dict(cand)
                    if not norm.get("streetAddress"):
                        continue
                    for ot in self._ld_type_tokens(obj):
                        if "house" in ot or "residence" in ot or "product" in ot:
                            return norm
                    if not fallback:
                        fallback = norm
        return fallback

    def _house_ld_list_price_hint(self, response):
        """When SortQmis omits the current QMI, House JSON-LD description often includes list price."""
        for raw in response.xpath('//script[@type="application/ld+json"]/text()').getall():
            text = (raw or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            objs = payload if isinstance(payload, list) else [payload]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                if not any("house" in t for t in self._ld_type_tokens(obj)):
                    continue
                desc = str(obj.get("description") or "")
                if re.search(r"contact\s+for\s+price", desc, re.I):
                    return "0"
                m = re.search(r"\$\s*([\d,]+)", desc)
                if m:
                    digits = m.group(1).replace(",", "")
                    if digits.isdigit():
                        return self._money(int(digits))
        return None

    def _qmi_price_value(self, qmi):
        if not isinstance(qmi, dict):
            return ""
        cf = qmi.get("CallForPrice")
        if str(cf).strip().lower() in ("true", "1", "yes"):
            return "0"
        price = qmi.get("Price")
        if isinstance(price, (int, float)):
            return self._money(price)
        return "0"

    def _match_qmi(self, qmis, house_url):
        if not house_url or not isinstance(qmis, list):
            return None
        want_path = self._url_path(house_url)
        if not want_path:
            return None
        for qmi in qmis:
            if not isinstance(qmi, dict):
                continue
            url_val = qmi.get("Url") or qmi.get("url")
            if not url_val:
                continue
            got_path = self._url_path(url_val)
            if got_path == want_path:
                return qmi
        want_slug = self._listing_slug_from_house_url(house_url)
        if want_slug:
            for qmi in qmis:
                if not isinstance(qmi, dict):
                    continue
                addr = qmi.get("Address")
                if addr and self._slugify_addressish(addr) == want_slug:
                    return qmi
        return None

    def _url_path(self, url):
        raw = str(url or "").strip()
        if not raw:
            return ""
        if raw.startswith("http://") or raw.startswith("https://"):
            return urlparse(raw).path.rstrip("/")
        return ("/" + raw.lstrip("/")).rstrip("/")

    def _extract_ld_json(self, response):
        place = {}
        business = {}
        for raw in response.xpath('//script[@type="application/ld+json"]/text()').getall():
            text = (raw or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            payloads = payload if isinstance(payload, list) else [payload]
            for obj in payloads:
                if not isinstance(obj, dict):
                    continue
                obj_type = str(obj.get("@type") or "").lower()
                if obj_type == "place" and not place:
                    place = obj
                elif obj_type == "localbusiness" and not business:
                    business = obj
        return place, business

    def _extract_segmentation_context(self, text):
        match = re.search(r"segmentationContext\s*:\s*\{(.*?)\}", text or "", re.S)
        if not match:
            return {}
        block = match.group(1)
        data = {}
        for key, value in re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*:\s*['\"]([^'\"]*)['\"]", block):
            data[key] = value.strip()
        return data

    def _extract_embedded_model(self, text, function_name):
        marker = f"function {function_name}(clickedElement)"
        start_idx = text.find(marker)
        if start_idx == -1:
            return {}
        var_idx = text.find("var model =", start_idx)
        if var_idx == -1:
            return {}
        brace_idx = text.find("{", var_idx)
        if brace_idx == -1:
            return {}

        depth = 0
        in_string = False
        escape = False
        for pos in range(brace_idx, len(text)):
            char = text[pos]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[brace_idx : pos + 1])
                        except Exception:
                            return {}
        return {}

    def _extract_status(self, response, visible_text):
        status_texts = response.xpath('//*[contains(@class, "status")]//text()').getall()
        status = self._join_unique(status_texts)
        if status:
            return status
        for label in [
            "Now Selling",
            "Sold Out",
            "Move-In Ready",
            "Move in Ready",
            "Coming Soon",
            "Final Opportunities",
            "Closeout",
        ]:
            if re.search(re.escape(label), visible_text, re.I):
                return label
        return ""

    def _extract_product_label(self, visible_text):
        for label in ["Single family", "Townhome", "Townhomes", "Condo", "Condos"]:
            if re.search(re.escape(label), visible_text, re.I):
                return label
        return ""

    def _map_product_type(self, label):
        low = (label or "").lower()
        if "single" in low:
            return "SFD"
        if "town" in low:
            return "SFA"
        if "condo" in low:
            return "CO"
        return ""

    def _extract_amenities(self, amenities_text):
        if not amenities_text:
            return []
        text = re.sub(r"(?i)^community amenities", "", amenities_text).strip()
        parts = re.split(r"\s{2,}|\s\|\s|,\s*", text)
        out = []
        for part in parts:
            clean = self._clean_join([part])
            if not clean:
                continue
            if clean.lower() == "hoa":
                continue
            out.append(clean)
        return self._dedupe(out)

    def _extract_incentives(self, response):
        texts = response.xpath('//*[contains(@class, "tag")]//text()').getall()
        cleaned = []
        for text in texts:
            value = self._clean_join([text])
            if not value:
                continue
            if value.startswith(".") or "fill:" in value.lower():
                continue
            cleaned.append(value)
        cleaned = self._dedupe(cleaned)
        return " | ".join(cleaned)

    def _extract_qmi_incentives(self, qmis):
        messages = []
        for qmi in qmis:
            price = qmi.get("Price")
            original = qmi.get("OriginalPrice")
            name = qmi.get("PlanName") or qmi.get("Address")
            if isinstance(price, (int, float)) and isinstance(original, (int, float)) and original > price:
                messages.append(f"{name}: reduced from {self._money(original)} to {self._money(price)}")
        return " | ".join(self._dedupe(messages))

    def _build_model_details(self, plans):
        details = []
        for plan in plans:
            name = self._stringify(plan.get("PlanName"))
            beds = self._stringify(plan.get("NumberOfBedrooms"))
            baths = self._stringify(plan.get("NumberOfBathrooms"))
            garages = self._stringify(plan.get("NumberOfGarages"))
            sqft = self._stringify(plan.get("SquareFootage"))
            price = self._money(plan.get("Price"))
            parts = [name]
            if beds:
                parts.append(f"{beds} BR")
            if baths:
                parts.append(f"{baths} BA")
            if garages:
                parts.append(f"{garages} Garage")
            if sqft:
                parts.append(f"{sqft} sqft")
            if price:
                parts.append(price)
            details.append(", ".join(parts))
        return " | ".join(details)

    def _extract_images(self, response):
        urls = []
        for src in response.xpath("//img/@src").getall():
            if "/productcatalog/" not in src.lower():
                continue
            urls.append(response.urljoin(src).split("?")[0])
        return self._dedupe(urls)

    def _format_opening_hours(self, specs):
        if not isinstance(specs, list):
            return ""
        rows = []
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            days = spec.get("dayOfWeek") or []
            if isinstance(days, str):
                days = [days]
            days = [str(day).split("/")[-1] for day in days if day]
            opens = self._format_time(spec.get("opens"))
            closes = self._format_time(spec.get("closes"))
            if not days:
                continue
            day_text = days[0] if len(days) == 1 else f"{days[0]} - {days[-1]}"
            if opens and closes:
                rows.append(f"{day_text} {opens} - {closes}")
            else:
                rows.append(day_text)
        return " | ".join(rows)

    def _format_time(self, value):
        match = re.match(r"^(\d{1,2}):(\d{2})", str(value or ""))
        if not match:
            return ""
        hour = int(match.group(1))
        minute = match.group(2)
        suffix = "AM" if hour < 12 else "PM"
        hour = hour % 12 or 12
        return f"{hour}:{minute} {suffix}"

    def _normalize_phone(self, value):
        raw = str(value or "").replace("tel:", "").strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return raw

    def _range_string(self, values):
        clean = self._numeric_values(values)
        if not clean:
            return ""
        if min(clean) == max(clean):
            return self._stringify(min(clean))
        return f"{self._stringify(min(clean))}-{self._stringify(max(clean))}"

    def _money_range_string(self, values):
        clean = self._numeric_values(values)
        if not clean:
            return ""
        if min(clean) == max(clean):
            return self._money(min(clean))
        return f"{self._money(min(clean))}-{self._money(max(clean))}"

    def _money(self, value):
        if not isinstance(value, (int, float)):
            return ""
        return f"${int(round(value))}"

    def _numeric_values(self, values):
        out = []
        for value in values:
            if isinstance(value, (int, float)):
                out.append(value)
        return out

    def _join_unique(self, values):
        return " | ".join(self._dedupe([self._stringify(value) for value in values if self._stringify(value)]))

    def _dedupe(self, values):
        seen = set()
        out = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    def _strip_title_prefix(self, value):
        text = self._stringify(value)
        if " | " in text:
            parts = [part.strip() for part in text.split("|") if part.strip()]
            if parts:
                return parts[0].replace("Houses For Sale in", "").strip()
        return text

    def _clean_join(self, values):
        if isinstance(values, str):
            values = [values]
        text = " ".join(str(value or "").strip() for value in values if str(value or "").strip())
        return re.sub(r"\s+", " ", text).strip()

    def _stringify(self, value):
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    @staticmethod
    def _normalize_url(url):
        raw = str(url or "").strip()
        if not raw:
            return ""
        if raw.startswith("http://") or raw.startswith("https://"):
            clean, _ = urldefrag(raw)
            return clean.rstrip("/")
        clean, _ = urldefrag(f"{BASE}/{raw.lstrip('/')}")
        return clean.rstrip("/")
