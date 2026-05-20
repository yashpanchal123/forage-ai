import csv
import re
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urlparse

import scrapy

API_ORIGIN = "https://www.drbhomes.com"

BY_NAME_URL = "https://api.drbhomes.com/api/v1/public/inventory/state/region/by-name"

DEFAULT_API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": API_ORIGIN,
    "referer": f"{API_ORIGIN}/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
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


class DrbHomesDetailsSpider(scrapy.Spider):
    name = "drbhomes_details"
    allowed_domains = ["drbhomes.com", "www.drbhomes.com", "api.drbhomes.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [400, 404],
        "FEEDS": {
            "drbhomes_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            }
        }
    }

    def __init__(self, input_csv="drbhomes_listings.csv", *args, **kwargs):
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
                house_url = (row.get("house_url") or "").strip().rstrip("/")
                if not house_url:
                    continue
                inv_raw = (
                    row.get("inventory_id")
                    or row.get("id")
                    or row.get("inventoryId")
                    or ""
                )
                inv_raw = str(inv_raw).strip()
                if inv_raw.isdigit():
                    detail_url = f"https://api.drbhomes.com/api/v1/public/inventory/{inv_raw}"
                    yield scrapy.Request(
                        detail_url,
                        method="GET",
                        headers=DEFAULT_API_HEADERS,
                        callback=self.parse_inventory,
                        cb_kwargs={"house_url": house_url},
                        meta={"dont_cache": True},
                        dont_filter=True,
                    )
                    continue

                parsed = self._parse_quick_move_in_url(house_url)
                if not parsed:
                    self.logger.warning("Could not parse API params from URL: %s", house_url)
                    continue
                by_name_full = f"{BY_NAME_URL}?{urlencode(parsed)}"
                yield scrapy.Request(
                    by_name_full,
                    method="GET",
                    headers=DEFAULT_API_HEADERS,
                    callback=self.parse_by_name,
                    cb_kwargs={"house_url": house_url, "params": parsed},
                    meta={"dont_cache": True},
                )

    def parse_by_name(self, response, house_url, params):
        if response.status != 200:
            self.logger.warning(
                "by-name HTTP %s for %s params=%s body=%s",
                response.status,
                house_url,
                params,
                (response.text or "")[:400],
            )
            return
        try:
            payload = response.json()
        except Exception:
            self.logger.error("by-name not JSON %s status=%s", response.url, response.status)
            return

        inv_id = payload.get("id")
        if inv_id is None:
            self.logger.warning(
                "by-name missing id state=%s region=%s community=%s address=%s body=%s",
                params.get("state"),
                params.get("region"),
                params.get("community"),
                params.get("address"),
                (response.text or "")[:300],
            )
            return

        # Same listing often appears twice in the sitemap under different metro path
        # segments (e.g. georgia/atlanta/... vs georgia/south-atlanta/...) but resolves
        # to the same inventory id. Scrapy dedupes identical GET URLs by default, which
        # would drop later rows — keep one request per CSV row.
        detail_url = f"https://api.drbhomes.com/api/v1/public/inventory/{inv_id}"
        yield scrapy.Request(
            detail_url,
            method="GET",
            headers=DEFAULT_API_HEADERS,
            callback=self.parse_inventory,
            cb_kwargs={"house_url": house_url},
            meta={"dont_cache": True},
            dont_filter=True,
        )

    def parse_inventory(self, response, house_url):
        if response.status != 200:
            self.logger.warning(
                "inventory HTTP %s for house_url=%s", response.status, house_url
            )
            return
        try:
            data = response.json()
        except Exception:
            self.logger.error("inventory not JSON %s status=%s", response.url, response.status)
            return

        if not isinstance(data, dict):
            return

        item = {k: "" for k in DETAIL_FIELDS}
        item["url"] = house_url
        item["Website"] = API_ORIGIN
        item["Builder"] = "DRB Homes"
        item["Community Details"] = ""
        addr = data.get("address") or {}
        comm = data.get("community") or {}

        item["Community Name"] = self._stringify(data.get("communityName") or comm.get("name"))
        street = self._stringify(addr.get("address1"))
        city = self._stringify(addr.get("city"))
        state_code = self._stringify(addr.get("stateName") or addr.get("stateLabel"))
        zip_code = self._stringify(addr.get("zip"))
        item["Street Address"] = self._full_address_line(street, city, state_code, zip_code)
        item["City"] = city
        item["State"] = state_code
        item["Zip Code"] = zip_code
        county = self._stringify(addr.get("countyName"))
        item["County"] = f"{county} County" if county and "county" not in county.lower() else county

        item["latitude"] = self.lat_long_roundof(addr.get("latitude"))
        item["longitude"] = self.lat_long_roundof(addr.get("longitude"))

        item["Phone number"] = self._format_phone(
            data.get("callTrackingNumber") or comm.get("salesOfficePhone")  
        )
        item["Model hours"] = self._stringify(comm.get("salesOfficeHours"))

        item["status"] = self._stringify(
            data.get("status") or data.get("salesStatus") or comm.get("status")
        )
        item["Overall Description of the Community"] = self._strip_html(comm.get("marketingDescription"))
        item["Attributes/Features"] = self._stringify(comm.get("marketingFeatures"))
        amen_desc = self._strip_html(comm.get("marketingAmenitiesDescription"))
        item["Amenities Available"] = "Y" if amen_desc else ""
        item["Amenity Type"] = amen_desc[:500] if amen_desc else ""

        tax_rate = comm.get("marketingTaxRate")
        item["City/Town/Property Tax %"] = self._stringify(tax_rate) if tax_rate not in (None, "") else ""

        item["Sold Out Date"] = self._stringify(data.get("soldDate"))
        item["Incentives"] = ""
        item["QMI Incentives"] = ""

        item["QMI Model Name"] = self._stringify(data.get("planName"))
        item["QMI # of Garages"] = self._stringify(data.get("garageSpaces"))
        item["QMI # of BR"] = self._stringify(data.get("beds"))
        item["QMI # of BA"] = self._stringify(data.get("fullBaths"))
        item["QMI # of 1/2 BA"] = self._stringify(data.get("halfBaths"))
        sqft_val = self._to_int(data.get("sqFt"))
        price_val = self._to_int(data.get("price"))
        item["QMI Model SQFT"] = self._stringify(sqft_val) if sqft_val > 0 else ""
        item["QMI Model Price"] = self._money(price_val) if price_val > 0 else ""
        item["QMI Availability Date"] = self._stringify(data.get("availabilityDate"))
        # Per requirement, keep these blank when not consistently available.
        item["QMI Lot SQFT"] = ""
        item["Avg Lot Size"] = ""

        desc_parts = [
            self._strip_html(data.get("marketingDescription")),
        ]
        plan_desc = " ".join(p for p in desc_parts if p)
        item["Plan Description"] = plan_desc
        item["QMI Interior/Exterior Attributes"] = ""

        item["Model Name"] = item["QMI Model Name"]
        item["# BR"] = item["QMI # of BR"]
        item["# BA"] = item["QMI # of BA"]
        item["# 1/2 BA"] = item["QMI # of 1/2 BA"]
        item["# of Floors"] = self._stringify(data.get("stories"))
        item["# Garages"] = item["QMI # of Garages"]
        item["# of Garages"] = item["QMI # of Garages"]
        garages = self._to_int(data.get("garageSpaces"))
        item["Garages (Y/N)"] = "Y" if garages and garages > 0 else "N"
        item["Parking Type"] = "Garage" if item["Garages (Y/N)"] == "Y" else ""

        entry = self._stringify(data.get("garageEntry"))
        item["Plan Garage Entry (Front load)"] = entry.title() if entry else ""

        item["Minimum SQFT"] = item["QMI Model SQFT"]
        item["Maximum SQFT"] = ""
        item["Minimum Base Price (Current)"] = item["QMI Model Price"]
        item["Maximum Base Price/All In Price"] = ""

        orig = data.get("originalPrice")
        if self._to_int(orig) > 0:
            item["Previous Price"] = self._money(orig)

        item["Last Updated"] = self._stringify(data.get("updatedAt"))
        item["Model Details"] = ""
        item["Model/Product Types Available"] = "Y" if item["Model Name"] else "N"
        item["Product Type (SFD/SFA/CO)"] = self._product_type_from_inventory(data, comm)

        master = (data.get("masterBedroomLocation") or "").lower()
        if "downstairs" in master or "first" in master or "main" in master:
            item["First Floor Master (Y/N)"] = "Y"
        elif master:
            item["First Floor Master (Y/N)"] = "N"

        item["images"] = self._collect_image_urls(data)

        yield item

    @staticmethod
    def _parse_quick_move_in_url(url):
        if not url:
            return None
        path = [p for p in urlparse(url).path.split("/") if p]
        try:
            idx = path.index("communities")
        except ValueError:
            return None
        if idx + 5 >= len(path):
            return None
        if path[idx + 4] != "quick-move-in-homes":
            return None
        state = path[idx + 1]
        region = path[idx + 2]
        community = path[idx + 3]
        address = path[idx + 5]
        return {"state": state, "region": region, "community": community, "address": address}


        if not url:
            return ""
        marker = "/quick-move-in-homes/"
        if marker not in url:
            return ""
        return url.split(marker, 1)[0].rstrip("/")

    @staticmethod
    def _strip_html(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _full_address_line(street, city, state, zip_code):
        street = str(street or "").strip()
        city = str(city or "").strip()
        state = str(state or "").strip()
        zip_code = str(zip_code or "").strip()
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
        return f"{street}, {city_state_zip}" if city_state_zip else street

    @staticmethod
    def _stringify(value):
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()
    
    @staticmethod
    def lat_long_roundof(value):
        if value is None:
            return ""
        try:
            num = float(value)
            return f"{num:.4f}"
        except (ValueError, TypeError):
            return str(value).strip()

    @staticmethod
    def _to_int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _money(value):
        if value in (None, ""):
            return ""
        try:
            n = int(round(float(value)))
        except (TypeError, ValueError):
            return ""
        return f"{n}"

    @staticmethod
    def _format_phone(raw):
        digits = re.sub(r"\D", "", str(raw or ""))
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return str(raw or "").strip()

    @staticmethod
    def _product_type_from_inventory(data, comm):
        """
        Prefer explicit planType signal because listingType is often 'new_home'
        for both single-family and townhomes.
        """
        plan_type = data.get("planType") if isinstance(data, dict) else None
        if isinstance(plan_type, dict):
            tokens = " ".join(
                str(plan_type.get(k) or "").lower() for k in ("name", "label")
            ).strip()
            if "town" in tokens:
                return "SFA"
            if "single" in tokens:
                return "SFD"
            if "condo" in tokens:
                return "CO"

        # Fallback from marketing copy.
        text_parts = [
            str((comm or {}).get("marketingDescription") or "").lower(),
            str((comm or {}).get("marketingHeadline") or "").lower(),
            str((data or {}).get("marketingDescription") or "").lower(),
        ]
        blob = " ".join(text_parts)
        if "townhome" in blob or "townhomes" in blob or "town home" in blob:
            return "SFA"
        if "single family" in blob:
            return "SFD"
        if "condo" in blob:
            return "CO"

        # Last fallback.
        listing_type = str((data or {}).get("listingType") or "").lower()
        if "town" in listing_type:
            return "SFA"
        if "condo" in listing_type:
            return "CO"
        if "single" in listing_type or "new_home" in listing_type or "home" in listing_type:
            return "SFD"
        return ""

    def _collect_image_urls(self, data):
        urls = []
        for img in data.get("images") or []:
            if isinstance(img, dict) and img.get("url"):
                urls.append(str(img["url"]).split("?")[0])
        for key in ("floorplanImages", "exteriorImages", "interiorImages"):
            for img in data.get(key) or []:
                if isinstance(img, dict) and img.get("url"):
                    urls.append(str(img["url"]).split("?")[0])
        seen = set()
        out = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out
