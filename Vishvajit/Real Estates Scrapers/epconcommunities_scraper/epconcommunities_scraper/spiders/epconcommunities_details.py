import csv
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlparse

import scrapy

from .epconcommunities_listing import API_HEADERS, BY_NAME_URL

API_ORIGIN = "https://www.epconcommunities.com"
INVENTORY_URL = "https://api.epconcommunities.com/api/v1/public-epcon/inventory"

DEFAULT_API_HEADERS = {
    **API_HEADERS,
}


class _HTMLToPlainText(HTMLParser):
    """Extract visible text; handles ``>`` inside attributes (e.g. ``&gt;``)."""

    __slots__ = ("_parts", "_ignore_depth")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignore_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._ignore_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._ignore_depth:
            self._ignore_depth -= 1

    def handle_data(self, data):
        if self._ignore_depth or not data:
            return
        self._parts.append(data)

    def plain(self) -> str:
        return " ".join(" ".join(self._parts).split())


def _garage_stall_total(data):
    """Sum full garageSpaces + 0.5 * garageSpacesHalf (Epcon API)."""
    if not isinstance(data, dict):
        return 0.0
    try:
        full = int(float(data.get("garageSpaces") or 0))
    except (TypeError, ValueError):
        full = 0
    try:
        half = int(float(data.get("garageSpacesHalf") or 0))
    except (TypeError, ValueError):
        half = 0
    return full + 0.5 * half


def _garage_count_display(data):
    """String for CSV/JSON, e.g. ``2`` or ``2.5`` when half stalls exist."""
    total = _garage_stall_total(data)
    if total <= 0:
        return ""
    if total == int(total):
        return str(int(total))
    return f"{total:.2f}".rstrip("0").rstrip(".")


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


class EpconCommunitiesDetailsSpider(scrapy.Spider):
    name = "epconcommunities_details"
    allowed_domains = ["epconcommunities.com", "www.epconcommunities.com", "api.epconcommunities.com"]

    # custom_settings = {
    #     "HTTPERROR_ALLOWED_CODES": [400, 404],
    #     "FEEDS": {
    #         "epconcommunities_details.csv": {
    #             "format": "csv",
    #             "encoding": "utf-8",
    #             "overwrite": True,
    #             "fields": DETAIL_FIELDS,
    #         }
    #     },
    # }
    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [400, 404],
        "FEEDS": {
            "epconcommunities_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            },
            "epconcommunities_details.json": {
                "format": "json",
                "encoding": "utf-8",
                "overwrite": True,
                "indent": 4,
            },
        },
    }

    def __init__(self, input_csv="epconcommunities_listings.csv", *args, **kwargs):
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
                    detail_url = f"{INVENTORY_URL}/{inv_raw}"
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

                params = self._parse_community_qmi_url(house_url)
                if not params:
                    self.logger.warning("Could not parse API params from URL: %s", house_url)
                    continue
                by_name_full = f"{BY_NAME_URL}?{urlencode(params)}"
                yield scrapy.Request(
                    by_name_full,
                    method="GET",
                    headers=DEFAULT_API_HEADERS,
                    callback=self.parse_by_name,
                    cb_kwargs={"house_url": house_url, "params": params},
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

        detail_url = f"{INVENTORY_URL}/{inv_id}"
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
        item["Builder"] = "Epcon Communities"
        item["Community Details"] = ""

        addr = data.get("address") or {}
        comm = data.get("community") or {}

        item["Community Name"] = self._stringify(
            data.get("communityName") or comm.get("name")
        )

        street = self._stringify(addr.get("address1"))
        city = self._stringify(addr.get("city"))
        state_code = self._stringify(addr.get("stateName") or addr.get("stateLabel"))
        zip_code = self._stringify(addr.get("zip"))
        item["Street Address"] = self._full_address_line(street, city, state_code, zip_code)
        item["City"] = city
        item["State"] = state_code
        item["Zip Code"] = zip_code
        county = self._stringify(addr.get("countyName"))
        item["County"] = (
            f"{county} County" if county and "county" not in county.lower() else county
        )

        item["latitude"] = self.lat_long_roundof(addr.get("latitude"))
        item["longitude"] = self.lat_long_roundof(addr.get("longitude"))

        item["Phone number"] = self._format_phone(
            data.get("callTrackingNumber")
            or comm.get("callTrackingPhone")
            or comm.get("salesOfficePhone")
        )
        item["Model hours"] = self._stringify(comm.get("salesOfficeHours"))

        item["status"] = self._stringify(
            data.get("salesStatus") or data.get("status") or comm.get("status")
        )

        item["Overall Description of the Community"] = self._strip_html(
            comm.get("marketingDescription")
        )
        item["Attributes/Features"] = self._strip_html(comm.get("marketingFeatures"))
        amen_desc = self._strip_html(comm.get("marketingAmenitiesDescription"))
        item["Amenities Available"] = "Y" if amen_desc else ""
        item["Amenity Type"] = amen_desc[:500] if amen_desc else ""

        tax_rate = comm.get("marketingTaxRate")
        if tax_rate in (None, ""):
            tax_rate = data.get("communityTaxRate")
        item["City/Town/Property Tax %"] = (
            self._stringify(tax_rate) if tax_rate not in (None, "") else ""
        )

        item["Sold Out Date"] = self._stringify(data.get("soldDate"))
        item["Incentives"] = ""
        item["QMI Incentives"] = ""

        item["QMI Model Name"] = self._stringify(data.get("planName"))
        item["QMI # of Garages"] = _garage_count_display(data)
        item["QMI # of BR"] = self._stringify(data.get("beds"))
        item["QMI # of BA"] = self._stringify(data.get("fullBaths"))
        item["QMI # of 1/2 BA"] = self._stringify(data.get("halfBaths"))
        sqft_val = self._to_int(data.get("sqFt"))
        price_val = self._to_int(data.get("price"))
        item["QMI Model SQFT"] = self._stringify(sqft_val) if sqft_val > 0 else ""
        item["QMI Model Price"] = self._money(price_val) if price_val > 0 else ""
        item["QMI Availability Date"] = self._stringify(data.get("availabilityDate"))

        lot_sz_raw = data.get("lotSize")
        lot_units = (data.get("lotUnits") or "").strip().lower()
        try:
            lot_num = float(lot_sz_raw)
        except (TypeError, ValueError):
            lot_num = 0.0
        if lot_num > 0:
            lot_sz = self._stringify(lot_sz_raw).strip()
            if lot_units == "sq_ft":
                item["QMI Lot SQFT"] = lot_sz
                item["Avg Lot Size"] = lot_sz
            elif lot_sz:
                combo = f"{lot_sz} {lot_units}".strip()
                item["QMI Lot SQFT"] = combo
                item["Avg Lot Size"] = combo
        else:
            item["QMI Lot SQFT"] = ""
            item["Avg Lot Size"] = ""

        desc_parts = [
            self._strip_html(data.get("marketingDescription")),
            self._strip_html(data.get("marketingHeadline")),
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
        item["Garages (Y/N)"] = "Y" if _garage_stall_total(data) > 0 else "N"
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

        blob = " ".join(
            [
                item["Overall Description of the Community"].lower(),
                self._strip_html(comm.get("marketingHeadline") or "").lower(),
            ]
        )
        if any(
            t in blob
            for t in ("55+", "55 plus", "active adult", "adult community", "age-qualified")
        ):
            item["Adult Community (Y/N)"] = "Y"
        else:
            item["Adult Community (Y/N)"] = ""

        item["images"] = self._collect_image_urls(data)

        yield item

    @staticmethod
    def _parse_community_qmi_url(url):
        if not url:
            return None
        clean = url.split("?")[0].strip().rstrip("/")
        if clean.lower().endswith("/floorplan"):
            clean = clean[: -len("/floorplan")].rstrip("/")
        parts = [p for p in urlparse(clean).path.split("/") if p]
        try:
            idx = parts.index("communities")
        except ValueError:
            return None
        if len(parts) < idx + 8:
            return None
        if parts[idx + 6] != "quick-move-in-homes":
            return None
        return {
            "state": parts[idx + 1],
            "region": parts[idx + 2],
            "county": parts[idx + 3],
            "city": parts[idx + 4],
            "community": parts[idx + 5],
            "address": parts[idx + 7],
        }

    @staticmethod
    def _strip_html(value):
        if value is None:
            return ""
        raw = str(value).strip()
        if not raw:
            return ""
        if "<" not in raw:
            return re.sub(r"\s+", " ", unescape(raw)).strip()
        parser = _HTMLToPlainText()
        try:
            parser.feed(raw)
            parser.close()
        except Exception:
            text = re.sub(r"(?is)<[^>]+>", " ", unescape(raw))
            return re.sub(r"\s+", " ", text).strip()
        return re.sub(r"\s+", " ", parser.plain()).strip()

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

        text_parts = [
            str((comm or {}).get("marketingDescription") or "").lower(),
            str((comm or {}).get("marketingHeadline") or "").lower(),
            str((data or {}).get("marketingDescription") or "").lower(),
        ]
        blob = " ".join(text_parts)
        if "townhome" in blob or "townhomes" in blob or "town home" in blob:
            return "SFA"
        if "single family" in blob or "single-family" in blob:
            return "SFD"
        if "condo" in blob:
            return "CO"

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
