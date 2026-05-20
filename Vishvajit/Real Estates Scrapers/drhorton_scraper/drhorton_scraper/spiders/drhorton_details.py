"""
D.R. Horton details spider.

Input CSV (same shape as cheshomes / nealcommunities listing exports):
  house_url, community_url

`house_url` may be a QMI (inventory) URL or a floor-plan URL; both are crawled the same way.

Output schema matches cheshomes / nealcommunities details field names (CSV + JSON).
Prices are stored as plain numbers (no dollar sign or comma thousands separators).
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
    "Community Name",
    "Latitude",
    "Longitude",
    "Street Address",
    "City",
    "State",
    "Zip Code",
    "County",
    "School District",
    "Status",
    "Builder",
    "Product Type",
    "Avg Lot Size",
    "# of Garages",
    "Adult Community (Y/N)",
    "Incentive %",
    "Incentive $",
    "Current Incentive Type",
    "Model hours",
    "Phone number",
    "Website",
    "Overall Description of the Community",
    "Foundation Type",
    "Model Name",
    "Plan Description",
    "# BR",
    "# BA",
    "# 1/2 BA",
    "# of Floors",
    "Minimum SQFT",
    "Minimum Base Price",
    "Previous Price",
    "Last Updated",
    "Maximum Price",
    "Maximum SQFT",
    "Plan Features",
    "Interior Specifications Descriptions",
    "First Floor Master (Y/N)",
    "Amenities Available",
    "Attributes/Features",
    "Exterior Specifications Available",
    "Interior Specifications Available",
    "QMI Incentive %",
    "QMI Incentive $",
    "QMI Current Incentive Type",
    "QMI Model Name",
    "QMI # of Garages",
    "QMI # of BR",
    "QMI # of BA",
    "QMI # of 1/2 BA",
    "QMI Model SQFT",
    "QMI Model # of Floor",
    "QMI Model Price",
    "QMI Availability Date",
    "QMI Lot SQFT",
    "QMI Interior/Exterior Attributes",
    "HOA Fee",
    "HOA Services",
    "Other Fees (i.e. CDD)",
    "Community - Open Date",
    "Community - Closed Date",
    "Total Lots",
    "# of Sales per month",
    "Total Lots Sold",
    "Total Lots - Total Sold",
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
            },
            "drhorton_details.json": {
                "format": "json",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
                "indent": 4,
            },
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
                state = self._state_from_drhorton_url(house_url)
                yield scrapy.Request(
                    url=house_url,
                    callback=self.parse_house,
                    meta={
                        "state": state,
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
                community_url = self._normalize_url(row.get("community_url") or row.get("community") or "")
                house_url = self._normalize_url(row.get("house_url") or row.get("house") or "")
                if not house_url:
                    continue
                if not community_url:
                    community_url = self._community_url_from_house_url(house_url)
                if not community_url:
                    continue
                state = (row.get("state") or "").strip().lower() or self._state_from_drhorton_url(house_url)
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
        """Listing stats from house page only; community merge adds name, hours, description."""
        house_url = response.meta.get("house_url") or self._normalize_url(response.url)
        state = (response.meta.get("state") or "").strip().lower() or self._state_from_drhorton_url(house_url)
        community_url = response.meta.get("community_url") or ""

        item = {field: "" for field in DETAIL_FIELDS}
        item["url"] = house_url
        item["Status"] = ""
        item["Phone number"] = ""
        item["Builder"] = "D R Horton"
        item["Website"] = BASE_WEBSITE
        item["School District"] = ""

        page_text = response.text or ""
        listing_ld = self._extract_listing_ld(response, house_url)
        place_ld, _business_ld = self._extract_ld_json(response)
        residence_addr = self._extract_residence_address_from_ld(response)
        segmentation = self._extract_segmentation_context(page_text)
        qmis_model = self._extract_embedded_model(page_text, "SortQmis")
        qmi = self._match_qmi(qmis_model.get("Items") or [], house_url)

        plans_model = self._extract_embedded_model(page_text, "SortPlans")
        plan = None
        if not qmi and self._is_floor_plan_url(house_url):
            plan = self._match_plan(plans_model.get("Items") or [], house_url)

        if qmi:
            item["Street Address"] = self._stringify(qmi.get("Address"))
            city, st, zip_code = self._parse_city_state_zip(qmi.get("CityStateZip"))
            item["City"] = city
            item["State"] = st
            item["Zip Code"] = zip_code
            item["Model Name"] = self._stringify(qmi.get("PlanName"))
            item["# BR"] = self._stringify_nonzero(qmi.get("NumberOfBedrooms"))
            ba_val, half_val = self._split_single_bath_value(qmi.get("NumberOfBathrooms"))
            item["# BA"] = ba_val
            item["# 1/2 BA"] = half_val
            item["# of Floors"] = self._stringify_nonzero(qmi.get("NumberOfStories"))
            item["# of Garages"] = self._stringify_nonzero(qmi.get("NumberOfGarages"))
            item["Minimum SQFT"] = self._stringify_nonzero(qmi.get("SquareFootage"))
            item["Minimum Base Price"] = self._embedded_item_price(qmi)
            item["QMI Availability Date"] = self._stringify(qmi.get("MoveInDate") or qmi.get("AvailableDate"))
            item["QMI Lot SQFT"] = self._stringify_nonzero(
                qmi.get("LotSquareFootage") or qmi.get("LotSqft") or qmi.get("LotSize")
            )
            lot_lat, lot_lng = self._extract_lot_lat_lng(qmi)
            if lot_lat:
                item["Latitude"] = lot_lat
            if lot_lng:
                item["Longitude"] = lot_lng
        elif plan:
            item["Model Name"] = self._stringify(plan.get("PlanName"))
            item["# BR"] = self._stringify_nonzero(plan.get("NumberOfBedrooms"))
            ba_val, half_val = self._split_single_bath_value(plan.get("NumberOfBathrooms"))
            item["# BA"] = ba_val
            item["# 1/2 BA"] = half_val
            item["# of Floors"] = self._stringify_nonzero(plan.get("NumberOfStories"))
            item["# of Garages"] = self._stringify_nonzero(plan.get("NumberOfGarages"))
            item["Minimum SQFT"] = self._stringify_nonzero(plan.get("SquareFootage"))
            item["Minimum Base Price"] = self._embedded_item_price(plan)
        elif listing_ld:
            self._apply_listing_ld_to_item(item, listing_ld, only_empty=False)
        else:
            merged = self._first_nonempty_postal(residence_addr, place_ld.get("address"))
            item["Street Address"] = merged.get("streetAddress") or ""
            item["City"] = merged.get("addressLocality") or ""
            item["State"] = merged.get("addressRegion") or ""
            item["Zip Code"] = merged.get("postalCode") or ""

        if listing_ld and (qmi or plan):
            self._apply_listing_ld_to_item(item, listing_ld, only_empty=True)

        if not item.get("Latitude") or not item.get("Longitude"):
            lat, lng = self._coords_from_ld_object(listing_ld) if listing_ld else ("", "")
            if lat and not item.get("Latitude"):
                item["Latitude"] = lat
            if lng and not item.get("Longitude"):
                item["Longitude"] = lng
        if not item.get("Latitude"):
            item["Latitude"] = self._stringify(place_ld.get("latitude"))
        if not item.get("Longitude"):
            item["Longitude"] = self._stringify(place_ld.get("longitude"))

        item["Plan Description"] = self._extract_plan_description(response)

        if not item.get("Minimum Base Price"):
            item["Minimum Base Price"] = self._extract_home_price(response, house_url)
        if not item.get("Minimum Base Price") and listing_ld:
            item["Minimum Base Price"] = self._listing_ld_price(listing_ld)

        item["Street Address"] = self._full_address_line(
            item.get("Street Address"),
            item.get("City"),
            item.get("State"),
            item.get("Zip Code"),
        )

        home_type = segmentation.get("homeType")
        if home_type and str(home_type).strip():
            item["Product Type"] = self._map_product_type(home_type)

        item["images"] = self._extract_property_gallery_images(response)

        self._apply_standard_qmi_columns(item)

        if not community_url:
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
        """Community page: name, description, and model hours only."""
        item = dict(response.meta["base_item"])

        place_ld, business_ld = self._extract_ld_json(response)
        place_name = (business_ld.get("name") or place_ld.get("name") or "").strip()
        item["Community Name"] = self._strip_title_prefix(place_name)

        description = self._clean_join(
            response.xpath('//*[contains(@class, "community-main-details")]//text()').getall()
        )
        item["Overall Description of the Community"] = description

        item["Model hours"] = self._format_opening_hours(
            business_ld.get("openingHoursSpecification")
        )

        self._apply_standard_qmi_columns(item)
        yield item

    def _community_url_from_house_url(self, house_url):
        u = self._normalize_url(house_url)
        if not u:
            return ""
        low = u.lower()
        if "/qmis/" in low:
            return u.split("/qmis/", 1)[0].rstrip("/")
        if "/floor-plans/" in low:
            return u.split("/floor-plans/", 1)[0].rstrip("/")
        return ""

    @staticmethod
    def _state_from_drhorton_url(url):
        parts = [p for p in urlparse(str(url or "")).path.strip("/").split("/") if p]
        return parts[0].lower() if parts else ""

    @staticmethod
    def _is_floor_plan_url(url):
        return "/floor-plans/" in (url or "").lower()

    @staticmethod
    def _apply_standard_qmi_columns(item):
        """Align QMI* columns with cheshomes / nealcommunities schema."""
        item["QMI Incentive %"] = ""
        item["QMI Incentive $"] = ""
        item["QMI Current Incentive Type"] = ""
        item["QMI Model Name"] = item.get("Model Name") or ""
        item["QMI # of Garages"] = item.get("# of Garages") or ""
        item["QMI # of BR"] = item.get("# BR") or ""
        item["QMI # of BA"] = item.get("# BA") or ""
        item["QMI # of 1/2 BA"] = item.get("# 1/2 BA") or ""
        item["QMI Model SQFT"] = item.get("Minimum SQFT") or ""
        item["QMI Model # of Floor"] = item.get("# of Floors") or ""
        item["QMI Model Price"] = item.get("Minimum Base Price") or ""
        item["QMI Availability Date"] = item.get("QMI Availability Date") or ""
        item["QMI Lot SQFT"] = item.get("QMI Lot SQFT") or ""
        item["QMI Interior/Exterior Attributes"] = item.get("QMI Interior/Exterior Attributes") or ""

    @staticmethod
    def _stringify_nonzero(value):
        if value is None or value == "":
            return ""
        if isinstance(value, (int, float)) and float(value) == 0:
            return ""
        text = str(value).strip()
        if text in ("0", "0.0"):
            return ""
        return text

    def _split_single_bath_value(self, value):
        """e.g. 2.5 baths -> # BA=2, # 1/2 BA=1 (never output 0, use empty string)."""
        n = self._to_number(value)
        if n is None:
            return "", ""
        full = int(n)
        frac = abs(n - full)
        half = 1 if frac >= 0.49 else 0
        return self._stringify_nonzero(full), self._stringify_nonzero(half)

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

    @staticmethod
    def _coords_from_ld_object(obj):
        if not isinstance(obj, dict):
            return "", ""
        geo = obj.get("geo")
        if isinstance(geo, dict):
            lat = geo.get("latitude")
            lng = geo.get("longitude")
            if lat not in (None, "") and lng not in (None, ""):
                return str(lat).strip(), str(lng).strip()
        lat = obj.get("latitude")
        lng = obj.get("longitude")
        if lat not in (None, "") and lng not in (None, ""):
            return str(lat).strip(), str(lng).strip()
        return "", ""

    def _ld_iterate_objects(self, payload, seen=None):
        if seen is None:
            seen = set()
        if isinstance(payload, list):
            for entry in payload:
                yield from self._ld_iterate_objects(entry, seen)
            return
        if not isinstance(payload, dict):
            return
        oid = id(payload)
        if oid in seen:
            return
        seen.add(oid)
        yield payload
        for graph in payload.get("@graph") or []:
            yield from self._ld_iterate_objects(graph, seen)

    def _extract_listing_ld(self, response, house_url):
        want_path = self._url_path(house_url)
        want_url = self._normalize_url(house_url)
        type_hints = ("house", "floorplan", "product", "residence")
        for raw in response.xpath('//script[@type="application/ld+json"]/text()').getall():
            text = (raw or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            for obj in self._ld_iterate_objects(payload):
                tokens = self._ld_type_tokens(obj)
                if not any(any(hint in token for hint in type_hints) for token in tokens):
                    continue
                obj_url = self._normalize_url(obj.get("url") or "")
                if obj_url and obj_url != want_url and self._url_path(obj_url) != want_path:
                    continue
                return obj
        return {}

    def _apply_listing_ld_to_item(self, item, ld_obj, only_empty=True):
        def set_field(key, value):
            val = self._stringify(value)
            if not val:
                return
            if only_empty and item.get(key):
                return
            item[key] = val

        addr = self._normalize_postal_dict(ld_obj.get("address") or {})
        set_field("Street Address", addr.get("streetAddress"))
        set_field("City", addr.get("addressLocality"))
        set_field("State", addr.get("addressRegion"))
        set_field("Zip Code", addr.get("postalCode"))
        set_field("# BR", self._stringify_nonzero(ld_obj.get("numberOfBedrooms")))
        ba_val, half_val = self._split_single_bath_value(ld_obj.get("numberOfBathroomsTotal"))
        set_field("# BA", ba_val)
        set_field("# 1/2 BA", half_val)
        set_field("Minimum SQFT", self._stringify_nonzero(ld_obj.get("floorSize")))
        lat, lng = self._coords_from_ld_object(ld_obj)
        set_field("Latitude", lat)
        set_field("Longitude", lng)

    def _extract_plan_description(self, response):
        parts = response.xpath(
            '//div[contains(@class,"about-this-home")]/p/text()'
            ' | //div[contains(@class,"about-this-home")]/div/text()'
            ' | //div[contains(@class,"about-this-plan")]/p/text()'
            ' | //div[contains(@class,"about-this-plan")]/div/text()'
            ' | //div[contains(@class,"about-this-plan")]/text()'
        ).getall()
        return self._clean_join(parts)

    def _extract_property_gallery_images(self, response):
        urls = []
        for src in response.xpath('//div[@class="PropertyGallery"]//img/@src').getall():
            if not src or not str(src).strip():
                continue
            urls.append(response.urljoin(str(src).strip()).split("?")[0])
        return self._dedupe(urls)

    def _extract_home_price(self, response, house_url=""):
        """Visible listing price only; never community header 'From $X' amounts."""
        css_selectors = []
        if self._is_floor_plan_url(house_url):
            css_selectors.extend(
                [
                    ".plan-price-brand .home-price ::text",
                    ".plan-price-brand ::text",
                ]
            )
        css_selectors.append("div.home-price ::text")
        for selector in css_selectors:
            price = self._parse_first_price(" ".join(response.css(selector).getall()))
            if price:
                return price
        parts = response.xpath(
            '//h2[contains(@class,"home-price")]'
            '[not(ancestor::*[contains(@class,"community-price-brand")])]//text()'
        ).getall()
        return self._parse_first_price(" ".join(parts))

    @staticmethod
    def _listing_ld_price(ld_obj):
        if not isinstance(ld_obj, dict):
            return ""
        offers = ld_obj.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not isinstance(offers, dict):
            return ""
        for key in ("price", "lowPrice", "highPrice"):
            val = offers.get(key)
            if val in (None, ""):
                continue
            if isinstance(val, (int, float)):
                n = float(val)
                if n > 0:
                    if abs(n - round(n)) < 1e-9:
                        return str(int(round(n)))
                    return f"{n:.2f}".rstrip("0").rstrip(".")
            text = re.sub(r"\D", "", str(val))
            if text:
                return text
        return ""

    def _extract_county_from_text(self, text):
        scoped = str(text or "")
        for marker in ["Starting your search?", "#### Areas in", "Legal Information"]:
            if marker in scoped:
                scoped = scoped.split(marker, 1)[0]
        m = re.search(r"\b([A-Za-z][A-Za-z .'-]{1,60}\sCounty)\b", scoped, re.I)
        if not m:
            return ""
        return self._clean_join([m.group(1)])

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
        return self._range_string(full_vals), self._range_string(half_vals)

    def _range_string(self, values):
        clean = self._numeric_values(values)
        if not clean:
            return ""
        if min(clean) == max(clean):
            return self._stringify(min(clean))
        return f"{self._stringify(min(clean))}-{self._stringify(max(clean))}"

    def _numeric_values(self, values):
        out = []
        for value in values:
            if isinstance(value, (int, float)):
                out.append(value)
        return out

    def _join_unique(self, values):
        return " | ".join(self._dedupe([self._stringify(value) for value in values if self._stringify(value)]))

    @staticmethod
    def _price_digits(text):
        digits = re.sub(r"\D", "", str(text or ""))
        return digits if digits else ""

    def _parse_first_price(self, text):
        body = self._clean_join([text])
        if not body or re.search(r"contact\s+for\s+price", body, re.I):
            return ""
        match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", body)
        if match:
            return match.group(1).replace(",", "").split(".")[0]
        return ""

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

    def _embedded_item_price(self, item):
        if not isinstance(item, dict):
            return ""
        cf = item.get("CallForPrice")
        if str(cf).strip().lower() in ("true", "1", "yes"):
            return ""
        price = item.get("Price")
        if isinstance(price, (int, float)):
            return self._money(price)
        if isinstance(price, str) and price.strip():
            return self._parse_first_price(price) or self._price_digits(price)
        return ""

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

    def _match_plan(self, plans, house_url):
        if not house_url or not isinstance(plans, list):
            return None
        want_path = self._url_path(house_url)
        if not want_path:
            return None
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            url_val = plan.get("Url") or plan.get("url")
            if not url_val:
                continue
            got_path = self._url_path(url_val)
            if got_path == want_path:
                return plan
        want_slug = self._listing_slug_from_house_url(house_url)
        if want_slug:
            for plan in plans:
                if not isinstance(plan, dict):
                    continue
                code = str(plan.get("PlanCode") or plan.get("Code") or "").strip().lower()
                if code and code == want_slug.lower():
                    return plan
                name = plan.get("PlanName")
                if name and self._slugify_addressish(str(name).replace("-", " ")) == want_slug:
                    return plan
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

    def _extract_images(self, response, house_url=None, listing_ld=None):
        urls = []
        slug = self._listing_slug_from_house_url(house_url) if house_url else ""

        def add_raw(raw):
            if not raw:
                return
            full = response.urljoin(str(raw).strip()).split("?")[0]
            if "/productcatalog/" not in full.lower():
                return
            urls.append(full)

        if isinstance(listing_ld, dict):
            image_val = listing_ld.get("image")
            if isinstance(image_val, list):
                for entry in image_val:
                    add_raw(entry)
            else:
                add_raw(image_val)

        for src in response.xpath(
            "//img[contains(@src,'productcatalog') or contains(@data-src,'productcatalog')]"
            "/@src | //img[contains(@src,'productcatalog') or contains(@data-src,'productcatalog')]"
            "/@data-src"
        ).getall():
            add_raw(src)

        for src in response.xpath('//div[contains(@class,"more-pictures-content")]//img/@src').getall():
            add_raw(src)

        deduped = self._dedupe(urls)
        if slug:
            slug_matches = [u for u in deduped if slug.lower() in u.lower()]
            if slug_matches:
                return slug_matches
        return deduped

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

    def _money(self, value):
        """Plain numeric dollars (no $ or thousands separators)."""
        if not isinstance(value, (int, float)):
            return ""
        n = float(value)
        if abs(n - round(n)) < 1e-9:
            return str(int(round(n)))
        s = f"{n:.2f}".rstrip("0").rstrip(".")
        return s if s else "0"

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

