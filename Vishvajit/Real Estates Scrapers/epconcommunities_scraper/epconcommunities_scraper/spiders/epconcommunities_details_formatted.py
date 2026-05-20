"""
Standalone Epcon Communities QMI details spider (formatted CSV).

Output columns are exactly ``FORMATTED_OUTPUT_FIELDS`` below.

**Left empty (no parsing / no inference):** community and QMI incentive % / $ /
type, HOA fee / services / CDD, adult community flag, and community
``Total Lots - Total Sold`` unless Epcon adds matching JSON keys later.

**Structured only:** school district (nested object or ``schoolDistrictName`` /
``schoolDistrictLabel``; no marketing copy). foundation type
(``foundationType`` on inventory or community only). Lot/sales counts only
from identically named fields when present (e.g. ``totalLots``).

**Product type (``Product Type`` column):** Each row is one QMI inventory item.
``Product Type`` is **this home’s** standardized type from the structured
``planType`` object only (``SFD`` = single-family detached, ``SFA`` = attached /
townhome-style, ``CO`` = condo). If ``planType`` is missing or unrecognized,
the cell is empty. When a community builds multiple product types, that mix
is not summarized on each row; use one row per listing and the plan’s
``planType`` for that row.
"""

from __future__ import annotations

import csv
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlparse

import scrapy

# --- API client (inlined from listing/details spiders) ---
API_ORIGIN = "https://www.epconcommunities.com"
BY_NAME_URL = (
    "https://api.epconcommunities.com/api/v1/public-epcon/inventory/"
    "state/region/county/city/by-name"
)
API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": API_ORIGIN,
    "referer": f"{API_ORIGIN}/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
INVENTORY_URL = "https://api.epconcommunities.com/api/v1/public-epcon/inventory"
DEFAULT_API_HEADERS = {**API_HEADERS}


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
    """Sum full ``garageSpaces`` + 0.5 × ``garageSpacesHalf`` (Epcon API)."""
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
    """Display string, e.g. ``2`` or ``2.5``."""
    total = _garage_stall_total(data)
    if total <= 0:
        return ""
    if total == int(total):
        return str(int(total))
    return f"{total:.2f}".rstrip("0").rstrip(".")


# Exact CSV columns, in order (only these are emitted).
FORMATTED_OUTPUT_FIELDS = (
    "url",
    "Community Name",
    "Street Address",
    "City",
    "State",
    "Zip Code",
    "County",
    "School District",
    "Latitude",
    "Longitude",
    "Phone number",
    "Model hours",
    "Website",
    "Status (Active, Upcoming, Sold Out)",
    "Builder",
    "Avg Lot Size",
    "Community - Open Date",
    "Community - Closed Date",
    "Total Lots",
    "Total Lots Sold",
    "Total Lots - Total Sold",
    "# of Sales per month",
    "HOA Fee",
    "HOA Services",
    "Other Fees (i.e. CDD)",
    "Incentive %",
    "Incentive $",
    "Incentive Type",
    "Amenities Available",
    "Exterior Specifications Available",
    "Interior Specifications Available",
    "QMI Model Name",
    "Model Name",
    "# of Garages",
    "QMI # of Garages",
    "# of BR",
    "# of BA",
    "# of 1/2 BA",
    "QMI # of BR",
    "QMI # of BA",
    "QMI # of 1/2 BA",
    "QMI Model # of Floor",
    "Foundation Type",
    "# of Floors",
    "Product Type",
    "QMI Model SQFT",
    "Minimum SQFT",
    "Maximum SQFT",
    "QMI Model Price",
    "Minimum Base Price (Current)",
    "Maximum Price",
    "QMI Availability Date",
    "QMI Lot SQFT",
    "QMI Incentive %",
    "QMI Incentive $",
    "QMI Incentive Type",
    "QMI Interior/Exterior Attributes",
    "Plan Features",
    "Interior Specifications Descriptions",
    "Overall Description of the Community",
    "Plan Description",
    "Attributes/Features",
    "First Floor Master (Y/N)",
    "Plan Garage Entry (Front load)",
    "Garages (Y/N)",
    "Adult Community (Y/N)",
    "Previous Price",
    "Last Updated",
)


class EpconCommunitiesDetailsFormattedSpider(scrapy.Spider):
    name = "epconcommunities_details_formatted"
    allowed_domains = ["epconcommunities.com", "www.epconcommunities.com", "api.epconcommunities.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [400, 404],
        "FEEDS": {
            "epconcommunities_dats_05_05_2026.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": list(FORMATTED_OUTPUT_FIELDS),
            }
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

        yield self._build_formatted_row(house_url, data)

    def _build_formatted_row(self, house_url, data):
        row = {k: "" for k in FORMATTED_OUTPUT_FIELDS}
        addr = data.get("address") or {}
        comm = data.get("community") or {}

        row["url"] = house_url
        row["Website"] = API_ORIGIN
        row["Builder"] = "Epcon Communities"

        row["Community Name"] = self._stringify(
            data.get("communityName") or comm.get("name")
        )

        street = self._stringify(addr.get("address1"))
        city = self._stringify(addr.get("city"))
        state_code = self._stringify(addr.get("stateName") or addr.get("stateLabel"))
        zip_code = self._stringify(addr.get("zip"))
        row["Street Address"] = self._full_address_line(street, city, state_code, zip_code)
        row["City"] = city
        row["State"] = state_code
        row["Zip Code"] = zip_code
        county = self._stringify(addr.get("countyName"))
        row["County"] = (
            f"{county} County" if county and "county" not in county.lower() else county
        )

        row["School District"] = self._school_district(data, comm)

        row["Latitude"] = self.lat_long_roundof(addr.get("latitude"))
        row["Longitude"] = self.lat_long_roundof(addr.get("longitude"))

        row["Phone number"] = self._format_phone(
            data.get("callTrackingNumber")
            or comm.get("callTrackingPhone")
            or comm.get("salesOfficePhone")
        )
        row["Model hours"] = self._stringify(comm.get("salesOfficeHours"))

        row["Status (Active, Upcoming, Sold Out)"] = self._stringify(
            data.get("salesStatus") or data.get("status") or comm.get("status")
        )

        row["Avg Lot Size"] = self._avg_lot_size(data)

        row["Community - Open Date"] = self._stringify(
            comm.get("openingDate") or comm.get("openDate")
        )
        row["Community - Closed Date"] = self._stringify(
            comm.get("closingDate")
            or comm.get("closedDate")
            or comm.get("communityClosedDate")
        )

        row["Total Lots"] = self._stringify(
            comm.get("totalLots")
            or comm.get("totalHomesites")
            or comm.get("totalInventory")
        )
        row["Total Lots Sold"] = self._stringify(
            comm.get("totalLotsSold") or comm.get("lotsSold") or comm.get("homesSold")
        )
        row["Total Lots - Total Sold"] = self._stringify(
            comm.get("totalLotsMinusSold")
            or comm.get("remainingLots")
            or comm.get("lotsRemaining")
        )
        row["# of Sales per month"] = self._stringify(
            comm.get("salesPerMonth") or comm.get("averageSalesPerMonth")
        )

        row["HOA Fee"] = ""
        row["HOA Services"] = ""
        row["Other Fees (i.e. CDD)"] = ""

        row["Incentive %"] = ""
        row["Incentive $"] = ""
        row["Incentive Type"] = ""

        amen_desc = self._strip_html(comm.get("marketingAmenitiesDescription"))
        row["Amenities Available"] = "Y" if amen_desc else ""

        row["Exterior Specifications Available"] = self._y_if_gallery(
            data.get("exteriorImages"), data.get("exteriorGalleryDescription")
        )
        row["Interior Specifications Available"] = self._y_if_gallery(
            data.get("interiorImages"),
            data.get("floorplanImages"),
        )

        plan_name = self._stringify(data.get("planName"))
        row["QMI Model Name"] = plan_name
        row["Model Name"] = plan_name

        g_str = _garage_count_display(data)
        row["# of Garages"] = g_str
        row["QMI # of Garages"] = g_str

        br = self._stringify(data.get("beds"))
        ba = self._stringify(data.get("fullBaths"))
        half = self._stringify(data.get("halfBaths"))
        row["# of BR"] = br
        row["# of BA"] = ba
        row["# of 1/2 BA"] = half
        row["QMI # of BR"] = br
        row["QMI # of BA"] = ba
        row["QMI # of 1/2 BA"] = half

        floors = self._stringify(data.get("stories"))
        row["QMI Model # of Floor"] = floors
        row["# of Floors"] = floors

        row["Foundation Type"] = self._stringify(
            data.get("foundationType") or comm.get("foundationType")
        )

        row["Product Type"] = self._standardized_product_type(data)

        sqft_val = self._to_int(data.get("sqFt"))
        price_val = self._to_int(data.get("price"))
        row["QMI Model SQFT"] = self._stringify(sqft_val) if sqft_val > 0 else ""
        row["Minimum SQFT"] = row["QMI Model SQFT"]
        row["Maximum SQFT"] = ""

        row["QMI Model Price"] = self._money(price_val) if price_val > 0 else ""
        row["Minimum Base Price (Current)"] = row["QMI Model Price"]
        row["Maximum Price"] = ""

        row["QMI Availability Date"] = self._stringify(data.get("availabilityDate"))

        lot_sz_raw = data.get("lotSize")
        lot_units = (data.get("lotUnits") or "").strip().lower()
        try:
            lot_num = float(lot_sz_raw)
        except (TypeError, ValueError):
            lot_num = 0.0
        if lot_num > 0:
            lot_sz = self._stringify(lot_sz_raw).strip()
            if lot_units == "sq_ft":
                row["QMI Lot SQFT"] = lot_sz
            elif lot_sz:
                row["QMI Lot SQFT"] = f"{lot_sz} {lot_units}".strip()
        else:
            row["QMI Lot SQFT"] = ""

        row["QMI Incentive %"] = ""
        row["QMI Incentive $"] = ""
        row["QMI Incentive Type"] = ""

        row["QMI Interior/Exterior Attributes"] = self._qmi_interior_exterior_attrs(data)

        row["Plan Features"] = self._plan_features_text(data, comm)
        row["Interior Specifications Descriptions"] = self._interior_spec_text(data, comm)

        row["Overall Description of the Community"] = self._strip_html(
            comm.get("marketingDescription")
        )

        desc_parts = [
            self._strip_html(data.get("marketingDescription")),
            self._strip_html(data.get("marketingHeadline")),
        ]
        row["Plan Description"] = " ".join(p for p in desc_parts if p)

        row["Attributes/Features"] = self._strip_html(comm.get("marketingFeatures"))

        master = (data.get("masterBedroomLocation") or "").lower()
        if "downstairs" in master or "first" in master or "main" in master:
            row["First Floor Master (Y/N)"] = "Y"
        elif master:
            row["First Floor Master (Y/N)"] = "N"

        entry = self._stringify(data.get("garageEntry"))
        row["Plan Garage Entry (Front load)"] = entry.title() if entry else ""

        row["Garages (Y/N)"] = "Y" if _garage_stall_total(data) > 0 else "N"

        row["Adult Community (Y/N)"] = ""

        orig = data.get("originalPrice")
        if self._to_int(orig) > 0:
            row["Previous Price"] = self._money(orig)

        row["Last Updated"] = self._stringify(data.get("updatedAt"))

        return row

    @staticmethod
    def _avg_lot_size(data):
        lot_sz_raw = data.get("lotSize")
        lot_units = (data.get("lotUnits") or "").strip().lower()
        try:
            lot_num = float(lot_sz_raw)
        except (TypeError, ValueError):
            lot_num = 0.0
        if lot_num <= 0:
            return ""
        lot_sz = EpconCommunitiesDetailsFormattedSpider._stringify(lot_sz_raw).strip()
        if lot_units == "sq_ft":
            return lot_sz
        return f"{lot_sz} {lot_units}".strip() if lot_sz else ""

    @staticmethod
    def _y_if_gallery(*sources):
        for src in sources:
            if isinstance(src, list) and src:
                for item in src:
                    if isinstance(item, dict) and item.get("url"):
                        return "Y"
                    if isinstance(item, str) and item.strip():
                        return "Y"
            if isinstance(src, str) and src.strip():
                return "Y"
        return ""

    @staticmethod
    def _qmi_interior_exterior_attrs(data):
        bits = []
        ct = data.get("courtyard")
        if ct:
            bits.append(f"Courtyard: {ct}")
        att = data.get("attachment")
        if att:
            bits.append(f"Attachment: {att}")
        gd = data.get("garageDetached")
        if gd:
            bits.append(f"Garage detached: {gd}")
        da = data.get("diningAreas")
        if da not in (None, ""):
            bits.append(f"Dining areas: {da}")
        bs = data.get("bonusSuites")
        if bs is True:
            bits.append("Bonus suite: Y")
        elif bs is False:
            bits.append("Bonus suite: N")
        hers = data.get("hersScore")
        if hers not in (None, ""):
            bits.append(f"HERS: {hers}")
        return " | ".join(bits)

    @staticmethod
    def _plan_features_text(data, comm):
        lines = []
        eg = data.get("exteriorGalleryDescription")
        if isinstance(eg, list):
            for line in eg:
                s = str(line).strip()
                if s:
                    lines.append(s)
        mlp = data.get("marketingLandingPageFeatures")
        if isinstance(mlp, str) and mlp.strip():
            lines.append(EpconCommunitiesDetailsFormattedSpider._strip_html(mlp))
        elif isinstance(mlp, list):
            for item in mlp:
                if isinstance(item, str) and item.strip():
                    lines.append(EpconCommunitiesDetailsFormattedSpider._strip_html(item))
        return " | ".join(lines)[:2000]

    @staticmethod
    def _interior_spec_text(data, comm):
        chunks = [
            EpconCommunitiesDetailsFormattedSpider._strip_html(
                comm.get("marketingHomeDesignDescription")
            ),
            EpconCommunitiesDetailsFormattedSpider._strip_html(
                comm.get("defaultPlanElevationsDescription")
            ),
            EpconCommunitiesDetailsFormattedSpider._strip_html(
                comm.get("defaultInventoryExteriorsDescription")
            ),
            EpconCommunitiesDetailsFormattedSpider._strip_html(
                data.get("marketingRequestATourDescription")
            ),
        ]
        return " | ".join(c for c in chunks if c)[:2000]

    def _school_district(self, data, comm):
        """Structured API fields only (no marketing-text parsing)."""
        sd = comm.get("schoolDistrict") or data.get("schoolDistrict")
        if isinstance(sd, dict):
            name = self._stringify(sd.get("name") or sd.get("label") or sd.get("title"))
            if name:
                return name
        return self._stringify(
            comm.get("schoolDistrictName")
            or data.get("schoolDistrictName")
            or comm.get("schoolDistrictLabel")
            or data.get("schoolDistrictLabel")
        )

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
    def _standardized_product_type(data):
        """
        Map Epcon ``planType`` only (no marketing or ``listingType`` inference).

        Returns ``SFD`` | ``SFA`` | ``CO`` for cross-source alignment. Equivalent
        labels used elsewhere: SF ≈ SFD, TH ≈ SFA, CO = CO.
        """
        plan_type = data.get("planType") if isinstance(data, dict) else None
        if not isinstance(plan_type, dict):
            return ""
        name = str(plan_type.get("name") or "").lower()
        label = str(plan_type.get("label") or "").lower()
        blob = f"{name} {label}"
        if not blob.strip():
            return ""
        if "condo" in blob:
            return "CO"
        if "town" in blob or "attached" in blob or "paired" in blob or "duplex" in blob:
            return "SFA"
        if "single" in blob or "detached" in blob:
            return "SFD"
        return ""
