import base64
import csv
import json
import re
from html import unescape
from pathlib import Path

import scrapy

BASE = "https://www.truehomes.com"

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


class TrueHomesDetailsSpider(scrapy.Spider):
    name = "truehomes_details"
    allowed_domains = ["truehomes.com", "www.truehomes.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [400, 404, 500],
        "FEEDS": {
            "truehomes_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            }
        },
    }

    def __init__(self, input_csv="truehomes_listings.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv

    def start_requests(self):
        csv_path = Path(self.input_csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        if not csv_path.exists():
            raise FileNotFoundError(f"Input CSV not found: {csv_path}")

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                url = (row.get("house_url") or "").strip()
                if not url:
                    continue
                url = url.split("#")[0].split("?")[0].rstrip("/")
                yield scrapy.Request(url, callback=self.parse_details, meta={"source_row": row}, dont_filter=True)

    def parse_details(self, response):
        if response.status != 200:
            return

        item = {k: "" for k in DETAIL_FIELDS}
        item["url"] = response.url.split("#")[0].split("?")[0].rstrip("/")
        item["Website"] = BASE
        item["Builder"] = "True Homes"

        row = response.meta.get("source_row") or {}
        if row.get("state"):
            item["State"] = self._normalize_state(row.get("state"))

        rowdata = self._extract_duda_rowdata(response.text or "")
        if rowdata:
            self._apply_qmi_rowdata(item, rowdata)

        # Prefer listing-specific phone from rowdata keys if available.
        row_phone = self._extract_phone_from_rowdata(rowdata)
        if row_phone:
            item["Phone number"] = row_phone
        else:
            # Fallback to tel: links, but ignore the global corporate number that appears everywhere.
            phones = self._extract_tel_numbers(response.text or "")
            phones = [p for p in phones if p != "803-824-2900"]
            if phones:
                item["Phone number"] = phones[0]

        yield item

    def _apply_qmi_rowdata(self, item: dict, data: dict):
        item["Community Name"] = self._stringify(data.get("Community Name (API)"))
        # On QMI pages this value is the "About This Home" text (plan/home description), not community description.
        item["Overall Description of the Community"] = ""
        item["status"] = self._stringify(data.get("Active Status (API)"))

        street = self._stringify(data.get("Street Address (API)"))
        city = self._stringify(data.get("City (API)"))
        state = self._normalize_state(data.get("State (API)"))
        zip_code = self._stringify(data.get("Zip (API)"))
        item["Street Address"] = self._full_address_line(street, city, state, zip_code)
        item["City"] = city
        if state:
            item["State"] = state
        item["Zip Code"] = zip_code

        item["latitude"] = self._lat_long_round(data.get("Latitude (API)"))
        item["longitude"] = self._lat_long_round(data.get("Longitude (API)"))

        # Listing title on the page matches "Title (API)" (e.g. "The Wakefield - Lot #35").
        # "Floorplan Name (API)" is often an internal line name (e.g. "Elements"), not the display name.
        item["QMI Model Name"] = self._stringify(
            data.get("Title (API)") or data.get("Floorplan Name (API)")
        )
        item["Plan Description"] = self._strip_html(data.get("Description"))

        br = self._to_int(data.get("Bedrooms (API)"))
        ba = self._to_float(data.get("Bathrooms (API)"))
        floors = self._to_int(data.get("Number of Stories (API)"))
        sqft = self._to_int(data.get("Square Footage (API)"))
        price = self._to_int(data.get("Price (API)"))

        if br > 0:
            item["QMI # of BR"] = str(br)
            item["# BR"] = str(br)

        if ba is not None:
            full = int(ba)
            if ba == full:
                item["QMI # of BA"] = str(full)
                item["# BA"] = str(full)
            elif ba > full:
                item["QMI # of BA"] = str(full)
                item["# BA"] = str(full)
                item["QMI # of 1/2 BA"] = "1"
                item["# 1/2 BA"] = "1"

        if floors > 0:
            item["# of Floors"] = str(floors)

        if sqft > 0:
            item["QMI Model SQFT"] = str(sqft)
            item["Minimum SQFT"] = str(sqft)

        if price > 0:
            item["QMI Model Price"] = str(price)
            item["Minimum Base Price (Current)"] = str(price)

        images = self._collect_images(data)
        if images:
            item["images"] = images

    def _extract_phone_from_rowdata(self, data: dict) -> str:
        if not isinstance(data, dict):
            return ""
        for key in (
            "Phone (API)",
            "Phone",
            "Sales Phone (API)",
            "Sales Phone",
            "Community Phone (API)",
            "Community Phone",
            "Contact Phone (API)",
            "Contact Phone",
        ):
            phone = self._format_phone(data.get(key))
            if phone:
                return phone
        return ""

    @staticmethod
    def _extract_duda_rowdata(html: str) -> dict:
        if not html:
            return {}
        m = re.search(r"base64JsonRowData:\s*'([^']+)'", html)
        if not m:
            return {}
        try:
            decoded = base64.b64decode(m.group(1)).decode("utf-8")
            data = json.loads(decoded)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _collect_images(self, data: dict):
        """
        Hero/main photo is in "Hero Image" (same as UI carousel first slide).
        "Photo Gallery" order does not start at (1); first gallery row may be another shot.
        """
        if not isinstance(data, dict):
            return []
        urls = []
        hero = data.get("Hero Image")
        if hero:
            urls.append(str(hero).split("?")[0].strip())
        gallery = data.get("Photo Gallery")
        if isinstance(gallery, list):
            for row in gallery:
                if not isinstance(row, dict):
                    continue
                img = row.get("image")
                if img:
                    urls.append(str(img).split("?")[0].strip())
        seen = set()
        out = []
        for u in urls:
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out

    def _extract_tel_numbers(self, html: str):
        values = re.findall(r"tel:([0-9\-\+\(\)\s]+)", html or "", flags=re.I)
        out = []
        seen = set()
        for raw in values:
            phone = self._format_phone(raw)
            if not phone or phone in seen:
                continue
            seen.add(phone)
            out.append(phone)
        return out

    @staticmethod
    def _strip_html(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _stringify(value):
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    @staticmethod
    def _format_phone(raw):
        digits = re.sub(r"\D", "", str(raw or ""))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            return ""
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"

    @staticmethod
    def _lat_long_round(value):
        if value in (None, ""):
            return ""
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return ""

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
        if city:
            return f"{street}, {city}"
        if state and zip_code:
            return f"{street}, {state} {zip_code}"
        if state:
            return f"{street}, {state}"
        if zip_code:
            return f"{street}, {zip_code}"
        return street

    @staticmethod
    def _to_int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_state(value):
        v = str(value or "").strip().lower()
        if v in ("north-carolina", "north carolina", "nc"):
            return "NC"
        if v in ("south-carolina", "south carolina", "sc"):
            return "SC"
        return str(value or "").strip().upper()

