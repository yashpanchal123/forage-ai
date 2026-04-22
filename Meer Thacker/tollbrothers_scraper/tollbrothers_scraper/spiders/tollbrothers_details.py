import csv
import json
import re
from pathlib import Path

import scrapy


def _norm_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _to_text(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    return ""


def _extract_numeric_from_text(text):
    if not text:
        return ""
    value_match = re.search(r"value\s*:\s*(-?\d+(?:\.\d+)?)", text, flags=re.I)
    if value_match:
        return value_match.group(1)
    number_match = re.search(r"-?\d+(?:\.\d+)?", text)
    return number_match.group(0) if number_match else ""


def _walk_values(data, target_keys):
    matches = []
    normalized_targets = {_norm_key(key) for key in target_keys}

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if _norm_key(key) in normalized_targets:
                    matches.append(value)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return matches


def _flatten_scalars(node):
    if isinstance(node, (str, int, float)):
        text = _to_text(node)
        return [text] if text else []
    if isinstance(node, list):
        values = []
        for item in node:
            values.extend(_flatten_scalars(item))
        return values
    if isinstance(node, dict):
        values = []
        for item in node.values():
            values.extend(_flatten_scalars(item))
        return values
    return []


def _first_value(data, keys, default=""):
    for value in _walk_values(data, keys):
        if isinstance(value, (str, int, float)):
            text = _to_text(value)
            if text:
                return text
    return default


def _first_number(data, keys, default=""):
    for value in _walk_values(data, keys):
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            numeric = _extract_numeric_from_text(value.strip())
            if numeric:
                return numeric
    return default


def _first_dict(data, keys):
    for value in _walk_values(data, keys):
        if isinstance(value, dict):
            return value
    return {}


def _all_text_values(data, keys):
    values = []
    for value in _walk_values(data, keys):
        values.extend(_flatten_scalars(value))
    deduped = []
    seen = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return " | ".join(deduped)


class TollbrothersDetailsSpider(scrapy.Spider):
    name = "tollbrothers_details"
    allowed_domains = ["tollbrothers.com"]

    fields = [
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
        "Maximum Base Price / All In Price",
        "Previous Price",
        "Last Updated",
        "Plan Features",
        "Interior Specifications Descriptions",
        "First Floor Master (Y/N)",
        "Images",
    ]

    custom_settings = {
        "FEEDS": {"tollbrothers_qmi_details.csv": {"format": "csv", "overwrite": True}},
        "FEED_EXPORT_FIELDS": fields,
    }

    def start_requests(self):
        csv_path = Path(__file__).resolve().parents[2] / "tollbrothers_luxury_homes_urls.csv"
        with csv_path.open("r", encoding="utf-8-sig", newline="") as infile:
            for row in csv.DictReader(infile):
                url = (row.get("url") or "").strip()
                if url and "/Quick-Move-In/" in url:
                    yield scrapy.Request(url=url, callback=self.parse, dont_filter=True)

    def parse(self, response):
        next_data = self._extract_next_data(response)
        json_ld = self._extract_json_ld(response)
        combined = {"next_data": next_data, "json_ld": json_ld}

        data = {field: "" for field in self.fields}
        data["url"] = response.url
        data["Website"] = response.url
        data["Builder"] = _first_value(combined, ["builderName", "builder"]) or "Toll Brothers"
        data["status"] = ""

        address = self._extract_address(combined)
        geo = self._extract_geo(combined)
        data["Street Address"] = address["Street Address"]
        data["City"] = address["City"]
        data["State"] = address["State"]
        data["Zip Code"] = address["Zip Code"]
        data["County"] = address["County"]
        data["latitude"] = geo["latitude"]
        data["longitude"] = geo["longitude"]

        data["Community Details"] = _first_value(combined, ["communityDetails", "communityDescription", "communityOverview", "overview"])
        data["Community Name"] = _first_value(combined, ["communityName", "developmentName", "neighborhoodName", "communityNameLabel"])
        data["Model hours"] = _first_value(combined, ["salesHours", "hours", "modelHours"])
        data["Phone number"] = _first_value(combined, ["telephone", "phone", "salesPhone"])
        data["Product Type (SFD/SFA/CO)"] = _first_value(combined, ["productType", "propertyType", "homeType"])
        data["Model/Product Types Available"] = _all_text_values(combined, ["collectionName", "seriesName", "modelName", "homeDesignName", "planName"])
        data["Avg Lot Size"] = _first_value(combined, ["avgLotSize", "averageLotSize"])
        data["Avg Lot - Width/Depth"] = _first_value(combined, ["lotDimensions", "lotWidthDepth", "lotWidthAndDepth"])
        data["# of Garages"] = _first_number(combined, ["garages", "garageSpaces", "numGarage"])
        data["Garages (Y/N)"] = "Y" if data["# of Garages"] else ""
        data["Adult Community (Y/N)"] = _first_value(combined, ["adultCommunity", "activeAdult", "ageRestricted"])
        data["Amenities Available"] = _all_text_values(combined, ["amenities"])
        data["Amenity Type"] = _all_text_values(combined, ["amenityType", "amenityTypes"])
        data["Attributes/Features"] = _all_text_values(combined, ["features", "highlights", "attributes"])
        data["Overall Description of the Community"] = _first_value(combined, ["description", "communityDescription"])
        data["Foundation Type"] = _first_value(combined, ["foundationType"])
        data["Exterior Specifications Available"] = _all_text_values(combined, ["exteriorFeatures", "exteriorSpecifications"])
        data["Interior Specifications Available"] = _all_text_values(combined, ["interiorFeatures", "interiorSpecifications"])
        data["HOA Fee"] = _first_value(combined, ["hoaFee", "hoaDues"])
        data["HOA Services"] = _all_text_values(combined, ["hoaServices"])
        data["Other Fees (i.e. CDD)"] = _first_value(combined, ["cddFee", "otherFees"])
        data["City/Town/Property Tax %"] = _first_value(combined, ["taxRate", "propertyTax"])
        data["Sales Start Date"] = _first_value(combined, ["salesStartDate"])
        data["Sold Out Date"] = _first_value(combined, ["soldOutDate"])
        data["Total Lots Sold"] = _first_number(combined, ["totalLotsSold"])
        data["Incentives"] = _all_text_values(combined, ["incentives"])
        data["QMI Incentives"] = _all_text_values(combined, ["qmiIncentives", "quickMoveInIncentives"])
        data["QMI Model Name"] = _first_value(combined, ["quickMoveInName", "homeDesignName", "modelName"])
        data["QMI # of Garages"] = _first_number(combined, ["garages", "garageSpaces"])
        data["QMI # of BR"] = _first_number(combined, ["bedrooms", "beds"])
        data["QMI # of BA"] = _first_number(combined, ["bathrooms", "baths"])
        data["QMI # of 1/2 BA"] = _first_number(combined, ["halfBaths", "halfBathrooms"])
        data["QMI Model SQFT"] = _first_number(combined, ["squareFeet", "sqft", "livingArea"])
        data["QMI Model Price"] = _first_value(combined, ["price", "salesPrice", "currentPrice"])
        data["QMI Availability Date"] = _first_value(combined, ["availableDate", "availabilityDate"])
        data["QMI Lot SQFT"] = _first_number(combined, ["lotSquareFeet", "lotSqft"])
        data["QMI Interior/Exterior Attributes"] = _all_text_values(combined, ["interiorFeatures", "exteriorFeatures", "features"])
        data["Model Details"] = _first_value(combined, ["modelDetails", "homeDesignDescription"])
        data["Model Name"] = _first_value(combined, ["modelName", "homeDesignName", "planName"])
        data["Plan Description"] = _first_value(combined, ["planDescription", "description"])
        data["# BR"] = _first_number(combined, ["bedrooms", "beds"])
        data["# BA"] = _first_number(combined, ["bathrooms", "baths"])
        data["# 1/2 BA"] = _first_number(combined, ["halfBaths", "halfBathrooms"])
        data["# of Floors"] = _first_number(combined, ["stories", "floors"])
        data["Parking Type"] = _first_value(combined, ["parkingType"])
        data["Plan Garage Entry (Front load)"] = _first_value(combined, ["garageEntry"])
        data["# Garages"] = _first_number(combined, ["garages", "garageSpaces"])
        data["Minimum SQFT"] = _first_number(combined, ["minSquareFeet", "minimumSqft", "floorSize"])
        data["Maximum SQFT"] = _first_number(combined, ["maxSquareFeet", "maximumSqft"])
        data["Minimum Base Price (Current)"] = _first_value(combined, ["basePrice", "startingPrice"])
        data["Maximum Base Price / All In Price"] = _first_value(combined, ["maxPrice", "allInPrice", "endingPrice"])
        data["Previous Price"] = _first_value(combined, ["previousPrice"])
        data["Last Updated"] = _first_value(combined, ["lastUpdated", "updatedAt", "modifiedDate"])
        data["Plan Features"] = _all_text_values(combined, ["planFeatures", "features"])
        data["Interior Specifications Descriptions"] = _all_text_values(combined, ["interiorDescription", "interiorSpecifications"])
        data["First Floor Master (Y/N)"] = _first_value(combined, ["firstFloorMaster", "mainLevelPrimaryBedroom"])
        data["Images"] = self._collect_images_from_xpath(response)
        yield data

    def _extract_next_data(self, response):
        script_text = response.css("script#__NEXT_DATA__::text").get()
        if not script_text:
            return {}
        try:
            return json.loads(script_text)
        except json.JSONDecodeError:
            return {}

    def _extract_json_ld(self, response):
        data = []
        for chunk in response.xpath("//script[@type='application/ld+json']/text()").getall():
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                data.extend(parsed)
            else:
                data.append(parsed)
        return data

    def _extract_geo(self, combined):
        geo = _first_dict(combined, ["geo", "coordinates", "location"])
        latitude = geo.get("latitude") or geo.get("lat") or ""
        longitude = geo.get("longitude") or geo.get("lng") or geo.get("lon") or ""
        return {"latitude": _to_text(latitude), "longitude": _to_text(longitude)}

    def _extract_address(self, combined):
        address = _first_dict(combined, ["address", "salesCenterAddress", "streetAddress"])
        return {
            "Street Address": _to_text(address.get("streetAddress") or address.get("addressLine1") or address.get("line1") or address.get("street")),
            "City": _to_text(address.get("addressLocality") or address.get("city")),
            "State": _to_text(address.get("addressRegion") or address.get("state")),
            "Zip Code": _to_text(address.get("postalCode") or address.get("zip") or address.get("zipCode")),
            "County": _to_text(address.get("addressCounty") or address.get("county")),
        }

    def _collect_images_from_xpath(self, response):
        image_urls = []
        for src in response.xpath("//figure[contains(@class,'GalleryMedia')]/img/@src").getall():
            src = src.strip()
            if not src:
                continue
            if src.startswith("//"):
                src = f"https:{src}"
            elif src.startswith("/"):
                src = response.urljoin(src)
            image_urls.append(src)

        deduped = []
        seen = set()
        for url in image_urls:
            if url not in seen:
                deduped.append(url)
                seen.add(url)
        return " | ".join(deduped)
import csv
import json
import re
from pathlib import Path

import scrapy


def _norm_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _to_text(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    return ""


def _extract_numeric_from_text(text):
    if not text:
        return ""
    value_match = re.search(r"value\s*:\s*(-?\d+(?:\.\d+)?)", text, flags=re.I)
    if value_match:
        return value_match.group(1)
    number_match = re.search(r"-?\d+(?:\.\d+)?", text)
    return number_match.group(0) if number_match else ""


def _walk_values(data, target_keys):
    matches = []
    normalized_targets = {_norm_key(key) for key in target_keys}

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if _norm_key(key) in normalized_targets:
                    matches.append(value)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return matches


def _flatten_scalars(node):
    if isinstance(node, (str, int, float)):
        text = _to_text(node)
        return [text] if text else []
    if isinstance(node, list):
        values = []
        for item in node:
            values.extend(_flatten_scalars(item))
        return values
    if isinstance(node, dict):
        values = []
        for item in node.values():
            values.extend(_flatten_scalars(item))
        return values
    return []


def _first_value(data, keys, default=""):
    for value in _walk_values(data, keys):
        if isinstance(value, (str, int, float)):
            text = _to_text(value)
            if text:
                return text
    return default


def _first_number(data, keys, default=""):
    for value in _walk_values(data, keys):
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            numeric = _extract_numeric_from_text(value.strip())
            if numeric:
                return numeric
    return default


def _first_dict(data, keys):
    for value in _walk_values(data, keys):
        if isinstance(value, dict):
            return value
    return {}


def _all_text_values(data, keys):
    values = []
    for value in _walk_values(data, keys):
        values.extend(_flatten_scalars(value))
    deduped = []
    seen = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return " | ".join(deduped)


class TollbrothersDetailsSpider(scrapy.Spider):
    name = "tollbrothers_details"
    allowed_domains = ["tollbrothers.com"]

    fields = [
        "url", "Community Details", "Community Name", "latitude", "longitude", "Street Address",
        "City", "State", "Zip Code", "County", "Model hours", "Phone number", "Website", "status",
        "Builder", "Product Type (SFD/SFA/CO)", "Model/Product Types Available", "Avg Lot Size",
        "Avg Lot - Width/Depth", "Garages (Y/N)", "# of Garages", "Adult Community (Y/N)",
        "Amenities Available", "Amenity Type", "Attributes/Features", "Overall Description of the Community",
        "Foundation Type", "Exterior Specifications Available", "Interior Specifications Available",
        "HOA Fee", "HOA Services", "Other Fees (i.e. CDD)", "City/Town/Property Tax %",
        "Sales Start Date", "Sold Out Date", "Total Lots Sold", "Incentives", "QMI Incentives",
        "QMI Model Name", "QMI # of Garages", "QMI # of BR", "QMI # of BA", "QMI # of 1/2 BA",
        "QMI Model SQFT", "QMI Model Price", "QMI Availability Date", "QMI Lot SQFT",
        "QMI Interior/Exterior Attributes", "Model Details", "Model Name", "Plan Description",
        "# BR", "# BA", "# 1/2 BA", "# of Floors", "Parking Type", "Plan Garage Entry (Front load)",
        "# Garages", "Minimum SQFT", "Maximum SQFT", "Minimum Base Price (Current)",
        "Maximum Base Price / All In Price", "Previous Price", "Last Updated", "Plan Features",
        "Interior Specifications Descriptions", "First Floor Master (Y/N)", "Images",
    ]

    custom_settings = {
        "FEEDS": {"tollbrothers_qmi_details.csv": {"format": "csv", "overwrite": True}},
        "FEED_EXPORT_FIELDS": fields,
    }

    def start_requests(self):
        csv_path = Path(__file__).resolve().parents[2] / "tollbrothers_luxury_homes_urls.csv"
        with csv_path.open("r", encoding="utf-8-sig", newline="") as infile:
            for row in csv.DictReader(infile):
                url = (row.get("url") or "").strip()
                if url and "/Quick-Move-In/" in url:
                    yield scrapy.Request(url=url, callback=self.parse, dont_filter=True)

    def parse(self, response):
        next_data = self._extract_next_data(response)
        json_ld = self._extract_json_ld(response)
        combined = {"next_data": next_data, "json_ld": json_ld}

        data = {field: "" for field in self.fields}
        data["url"] = response.url
        data["Website"] = response.url
        data["Builder"] = _first_value(combined, ["builderName", "builder"]) or "Toll Brothers"
        data["status"] = ""

        address = self._extract_address(combined)
        geo = self._extract_geo(combined)
        data["Street Address"] = address["Street Address"]
        data["City"] = address["City"]
        data["State"] = address["State"]
        data["Zip Code"] = address["Zip Code"]
        data["County"] = address["County"]
        data["latitude"] = geo["latitude"]
        data["longitude"] = geo["longitude"]

        data["Community Details"] = _first_value(combined, ["communityDetails", "communityDescription", "communityOverview", "overview"])
        data["Community Name"] = _first_value(combined, ["communityName", "developmentName", "neighborhoodName", "communityNameLabel"])
        data["Model hours"] = _first_value(combined, ["salesHours", "hours", "modelHours"])
        data["Phone number"] = _first_value(combined, ["telephone", "phone", "salesPhone"])
        data["Product Type (SFD/SFA/CO)"] = _first_value(combined, ["productType", "propertyType", "homeType"])
        data["Model/Product Types Available"] = _all_text_values(combined, ["collectionName", "seriesName", "modelName", "homeDesignName", "planName"])
        data["Avg Lot Size"] = _first_value(combined, ["avgLotSize", "averageLotSize"])
        data["Avg Lot - Width/Depth"] = _first_value(combined, ["lotDimensions", "lotWidthDepth", "lotWidthAndDepth"])
        data["# of Garages"] = _first_number(combined, ["garages", "garageSpaces", "numGarage"])
        data["Garages (Y/N)"] = "Y" if data["# of Garages"] else ""
        data["Adult Community (Y/N)"] = _first_value(combined, ["adultCommunity", "activeAdult", "ageRestricted"])
        data["Amenities Available"] = _all_text_values(combined, ["amenities"])
        data["Amenity Type"] = _all_text_values(combined, ["amenityType", "amenityTypes"])
        data["Attributes/Features"] = _all_text_values(combined, ["features", "highlights", "attributes"])
        data["Overall Description of the Community"] = _first_value(combined, ["description", "communityDescription"])
        data["Foundation Type"] = _first_value(combined, ["foundationType"])
        data["Exterior Specifications Available"] = _all_text_values(combined, ["exteriorFeatures", "exteriorSpecifications"])
        data["Interior Specifications Available"] = _all_text_values(combined, ["interiorFeatures", "interiorSpecifications"])
        data["HOA Fee"] = _first_value(combined, ["hoaFee", "hoaDues"])
        data["HOA Services"] = _all_text_values(combined, ["hoaServices"])
        data["Other Fees (i.e. CDD)"] = _first_value(combined, ["cddFee", "otherFees"])
        data["City/Town/Property Tax %"] = _first_value(combined, ["taxRate", "propertyTax"])
        data["Sales Start Date"] = _first_value(combined, ["salesStartDate"])
        data["Sold Out Date"] = _first_value(combined, ["soldOutDate"])
        data["Total Lots Sold"] = _first_number(combined, ["totalLotsSold"])
        data["Incentives"] = _all_text_values(combined, ["incentives"])
        data["QMI Incentives"] = _all_text_values(combined, ["qmiIncentives", "quickMoveInIncentives"])
        data["QMI Model Name"] = _first_value(combined, ["quickMoveInName", "homeDesignName", "modelName"])
        data["QMI # of Garages"] = _first_number(combined, ["garages", "garageSpaces"])
        data["QMI # of BR"] = _first_number(combined, ["bedrooms", "beds"])
        data["QMI # of BA"] = _first_number(combined, ["bathrooms", "baths"])
        data["QMI # of 1/2 BA"] = _first_number(combined, ["halfBaths", "halfBathrooms"])
        data["QMI Model SQFT"] = _first_number(combined, ["squareFeet", "sqft", "livingArea"])
        data["QMI Model Price"] = _first_value(combined, ["price", "salesPrice", "currentPrice"])
        data["QMI Availability Date"] = _first_value(combined, ["availableDate", "availabilityDate"])
        data["QMI Lot SQFT"] = _first_number(combined, ["lotSquareFeet", "lotSqft"])
        data["QMI Interior/Exterior Attributes"] = _all_text_values(combined, ["interiorFeatures", "exteriorFeatures", "features"])
        data["Model Details"] = _first_value(combined, ["modelDetails", "homeDesignDescription"])
        data["Model Name"] = _first_value(combined, ["modelName", "homeDesignName", "planName"])
        data["Plan Description"] = _first_value(combined, ["planDescription", "description"])
        data["# BR"] = _first_number(combined, ["bedrooms", "beds"])
        data["# BA"] = _first_number(combined, ["bathrooms", "baths"])
        data["# 1/2 BA"] = _first_number(combined, ["halfBaths", "halfBathrooms"])
        data["# of Floors"] = _first_number(combined, ["stories", "floors"])
        data["Parking Type"] = _first_value(combined, ["parkingType"])
        data["Plan Garage Entry (Front load)"] = _first_value(combined, ["garageEntry"])
        data["# Garages"] = _first_number(combined, ["garages", "garageSpaces"])
        data["Minimum SQFT"] = _first_number(combined, ["minSquareFeet", "minimumSqft", "floorSize"])
        data["Maximum SQFT"] = _first_number(combined, ["maxSquareFeet", "maximumSqft"])
        data["Minimum Base Price (Current)"] = _first_value(combined, ["basePrice", "startingPrice"])
        data["Maximum Base Price / All In Price"] = _first_value(combined, ["maxPrice", "allInPrice", "endingPrice"])
        data["Previous Price"] = _first_value(combined, ["previousPrice"])
        data["Last Updated"] = _first_value(combined, ["lastUpdated", "updatedAt", "modifiedDate"])
        data["Plan Features"] = _all_text_values(combined, ["planFeatures", "features"])
        data["Interior Specifications Descriptions"] = _all_text_values(combined, ["interiorDescription", "interiorSpecifications"])
        data["First Floor Master (Y/N)"] = _first_value(combined, ["firstFloorMaster", "mainLevelPrimaryBedroom"])
        data["Images"] = self._collect_images_from_xpath(response)
        yield data

    def _extract_next_data(self, response):
        script_text = response.css("script#__NEXT_DATA__::text").get()
        if not script_text:
            return {}
        try:
            return json.loads(script_text)
        except json.JSONDecodeError:
            return {}

    def _extract_json_ld(self, response):
        data = []
        for chunk in response.xpath("//script[@type='application/ld+json']/text()").getall():
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                data.extend(parsed)
            else:
                data.append(parsed)
        return data

    def _extract_geo(self, combined):
        geo = _first_dict(combined, ["geo", "coordinates", "location"])
        latitude = geo.get("latitude") or geo.get("lat") or ""
        longitude = geo.get("longitude") or geo.get("lng") or geo.get("lon") or ""
        return {"latitude": _to_text(latitude), "longitude": _to_text(longitude)}

    def _extract_address(self, combined):
        address = _first_dict(combined, ["address", "salesCenterAddress", "streetAddress"])
        return {
            "Street Address": _to_text(address.get("streetAddress") or address.get("addressLine1") or address.get("line1") or address.get("street")),
            "City": _to_text(address.get("addressLocality") or address.get("city")),
            "State": _to_text(address.get("addressRegion") or address.get("state")),
            "Zip Code": _to_text(address.get("postalCode") or address.get("zip") or address.get("zipCode")),
            "County": _to_text(address.get("addressCounty") or address.get("county")),
        }

    def _collect_images_from_xpath(self, response):
        image_urls = []
        for src in response.xpath("//figure[contains(@class,'GalleryMedia')]/img/@src").getall():
            src = src.strip()
            if not src:
                continue
            if src.startswith("//"):
                src = f"https:{src}"
            elif src.startswith("/"):
                src = response.urljoin(src)
            image_urls.append(src)

        deduped = []
        seen = set()
        for url in image_urls:
            if url not in seen:
                deduped.append(url)
                seen.add(url)
        return " | ".join(deduped)
import csv
import json
import re
from pathlib import Path

import scrapy


def _norm_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _to_text(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    return ""


def _extract_numeric_from_text(text):
    if not text:
        return ""
    value_match = re.search(r"value\s*:\s*(-?\d+(?:\.\d+)?)", text, flags=re.I)
    if value_match:
        return value_match.group(1)
    number_match = re.search(r"-?\d+(?:\.\d+)?", text)
    return number_match.group(0) if number_match else ""


def _walk_values(data, target_keys):
    matches = []
    normalized_targets = {_norm_key(key) for key in target_keys}

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if _norm_key(key) in normalized_targets:
                    matches.append(value)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return matches


def _flatten_scalars(node):
    if isinstance(node, (str, int, float)):
        text = _to_text(node)
        return [text] if text else []
    if isinstance(node, list):
        values = []
        for item in node:
            values.extend(_flatten_scalars(item))
        return values
    if isinstance(node, dict):
        values = []
        for item in node.values():
            values.extend(_flatten_scalars(item))
        return values
    return []


def _first_value(data, keys, default=""):
    for value in _walk_values(data, keys):
        if isinstance(value, (str, int, float)):
            text = _to_text(value)
            if text:
                return text
    return default


def _first_number(data, keys, default=""):
    for value in _walk_values(data, keys):
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            numeric = _extract_numeric_from_text(value.strip())
            if numeric:
                return numeric
    return default


def _first_dict(data, keys):
    for value in _walk_values(data, keys):
        if isinstance(value, dict):
            return value
    return {}


def _all_text_values(data, keys):
    values = []
    for value in _walk_values(data, keys):
        values.extend(_flatten_scalars(value))

    deduped = []
    seen = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return " | ".join(deduped)


class TollbrothersDetailsSpider(scrapy.Spider):
    name = "tollbrothers_details"
    allowed_domains = ["tollbrothers.com"]

    fields = [
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
        "Maximum Base Price / All In Price",
        "Previous Price",
        "Last Updated",
        "Plan Features",
        "Interior Specifications Descriptions",
        "First Floor Master (Y/N)",
        "Images",
    ]

    custom_settings = {
        "FEEDS": {
            "tollbrothers_qmi_details.csv": {
                "format": "csv",
                "overwrite": True,
            }
        },
        "FEED_EXPORT_FIELDS": fields,
    }

    def start_requests(self):
        csv_path = Path(__file__).resolve().parents[2] / "tollbrothers_luxury_homes_urls.csv"
        if not csv_path.exists():
            self.logger.error("Input CSV not found: %s", csv_path)
            return

        with csv_path.open("r", encoding="utf-8-sig", newline="") as infile:
            for row in csv.DictReader(infile):
                url = (row.get("url") or "").strip()
                if not url or "/Quick-Move-In/" not in url:
                    continue
                yield scrapy.Request(url=url, callback=self.parse_details, dont_filter=True)

    def parse_details(self, response):
        next_data = self._extract_next_data(response)
        json_ld = self._extract_json_ld(response)
        combined = {"next_data": next_data, "json_ld": json_ld}

        data = {field: "" for field in self.fields}
        data["url"] = response.url
        data["Website"] = response.url
        data["Builder"] = _first_value(combined, ["builderName", "builder"]) or "Toll Brothers"

        address = self._extract_address(combined)
        geo = self._extract_geo(combined)

        data["Community Details"] = _first_value(combined, ["communityDetails", "communityDescription", "communityOverview", "overview"])
        data["Community Name"] = _first_value(combined, ["communityName", "developmentName", "neighborhoodName", "communityNameLabel"])
        data["latitude"] = geo["latitude"]
        data["longitude"] = geo["longitude"]
        data["Street Address"] = address["Street Address"]
        data["City"] = address["City"]
        data["State"] = address["State"]
        data["Zip Code"] = address["Zip Code"]
        data["County"] = address["County"]
        data["Model hours"] = _first_value(combined, ["salesHours", "hours", "modelHours"])
        data["Phone number"] = _first_value(combined, ["telephone", "phone", "salesPhone"])
        data["status"] = ""
        data["Product Type (SFD/SFA/CO)"] = _first_value(combined, ["productType", "propertyType", "homeType"])
        data["Model/Product Types Available"] = _all_text_values(combined, ["collectionName", "seriesName", "modelName", "homeDesignName", "planName"])
        data["Avg Lot Size"] = _first_value(combined, ["avgLotSize", "averageLotSize"])
        data["Avg Lot - Width/Depth"] = _first_value(combined, ["lotDimensions", "lotWidthDepth", "lotWidthAndDepth"])
        data["# of Garages"] = _first_number(combined, ["garages", "garageSpaces", "numGarage"])
        data["Garages (Y/N)"] = "Y" if data["# of Garages"] else ""
        data["Adult Community (Y/N)"] = _first_value(combined, ["adultCommunity", "activeAdult", "ageRestricted"])
        data["Amenities Available"] = _all_text_values(combined, ["amenities"])
        data["Amenity Type"] = _all_text_values(combined, ["amenityType", "amenityTypes"])
        data["Attributes/Features"] = _all_text_values(combined, ["features", "highlights", "attributes"])
        data["Overall Description of the Community"] = _first_value(combined, ["description", "communityDescription"])
        data["Foundation Type"] = _first_value(combined, ["foundationType"])
        data["Exterior Specifications Available"] = _all_text_values(combined, ["exteriorFeatures", "exteriorSpecifications"])
        data["Interior Specifications Available"] = _all_text_values(combined, ["interiorFeatures", "interiorSpecifications"])
        data["HOA Fee"] = _first_value(combined, ["hoaFee", "hoaDues"])
        data["HOA Services"] = _all_text_values(combined, ["hoaServices"])
        data["Other Fees (i.e. CDD)"] = _first_value(combined, ["cddFee", "otherFees"])
        data["City/Town/Property Tax %"] = _first_value(combined, ["taxRate", "propertyTax"])
        data["Sales Start Date"] = _first_value(combined, ["salesStartDate"])
        data["Sold Out Date"] = _first_value(combined, ["soldOutDate"])
        data["Total Lots Sold"] = _first_number(combined, ["totalLotsSold"])
        data["Incentives"] = _all_text_values(combined, ["incentives"])
        data["QMI Incentives"] = _all_text_values(combined, ["qmiIncentives", "quickMoveInIncentives"])
        data["QMI Model Name"] = _first_value(combined, ["quickMoveInName", "homeDesignName", "modelName"])
        data["QMI # of Garages"] = _first_number(combined, ["garages", "garageSpaces"])
        data["QMI # of BR"] = _first_number(combined, ["bedrooms", "beds"])
        data["QMI # of BA"] = _first_number(combined, ["bathrooms", "baths"])
        data["QMI # of 1/2 BA"] = _first_number(combined, ["halfBaths", "halfBathrooms"])
        data["QMI Model SQFT"] = _first_number(combined, ["squareFeet", "sqft", "livingArea"])
        data["QMI Model Price"] = _first_value(combined, ["price", "salesPrice", "currentPrice"])
        data["QMI Availability Date"] = _first_value(combined, ["availableDate", "availabilityDate"])
        data["QMI Lot SQFT"] = _first_number(combined, ["lotSquareFeet", "lotSqft"])
        data["QMI Interior/Exterior Attributes"] = _all_text_values(combined, ["interiorFeatures", "exteriorFeatures", "features"])
        data["Model Details"] = _first_value(combined, ["modelDetails", "homeDesignDescription"])
        data["Model Name"] = _first_value(combined, ["modelName", "homeDesignName", "planName"])
        data["Plan Description"] = _first_value(combined, ["planDescription", "description"])
        data["# BR"] = _first_number(combined, ["bedrooms", "beds"])
        data["# BA"] = _first_number(combined, ["bathrooms", "baths"])
        data["# 1/2 BA"] = _first_number(combined, ["halfBaths", "halfBathrooms"])
        data["# of Floors"] = _first_number(combined, ["stories", "floors"])
        data["Parking Type"] = _first_value(combined, ["parkingType"])
        data["Plan Garage Entry (Front load)"] = _first_value(combined, ["garageEntry"])
        data["# Garages"] = _first_number(combined, ["garages", "garageSpaces"])
        data["Minimum SQFT"] = _first_number(combined, ["minSquareFeet", "minimumSqft", "floorSize"])
        data["Maximum SQFT"] = _first_number(combined, ["maxSquareFeet", "maximumSqft"])
        data["Minimum Base Price (Current)"] = _first_value(combined, ["basePrice", "startingPrice"])
        data["Maximum Base Price / All In Price"] = _first_value(combined, ["maxPrice", "allInPrice", "endingPrice"])
        data["Previous Price"] = _first_value(combined, ["previousPrice"])
        data["Last Updated"] = _first_value(combined, ["lastUpdated", "updatedAt", "modifiedDate"])
        data["Plan Features"] = _all_text_values(combined, ["planFeatures", "features"])
        data["Interior Specifications Descriptions"] = _all_text_values(combined, ["interiorDescription", "interiorSpecifications"])
        data["First Floor Master (Y/N)"] = _first_value(combined, ["firstFloorMaster", "mainLevelPrimaryBedroom"])
        data["Images"] = self._collect_images_from_xpath(response)

        community_url = response.url.split("/Quick-Move-In/")[0]
        if community_url and community_url != response.url:
            yield scrapy.Request(
                url=community_url,
                callback=self.parse_with_community,
                dont_filter=True,
                meta={"qmi_data": data},
            )
            return

        yield data

    def parse_with_community(self, response):
        data = dict(response.meta.get("qmi_data", {}))
        next_data = self._extract_next_data(response)
        json_ld = self._extract_json_ld(response)
        combined = {"next_data": next_data, "json_ld": json_ld}

        self._fill_if_empty(data, "Community Details", _first_value(combined, ["communityDetails", "communityDescription", "overview"]))
        self._fill_if_empty(data, "Community Name", _first_value(combined, ["communityName", "developmentName", "neighborhoodName"]))
        self._fill_if_empty(data, "Model/Product Types Available", _all_text_values(combined, ["collectionName", "seriesName", "modelName", "homeDesignName"]))
        self._fill_if_empty(data, "Avg Lot Size", _first_value(combined, ["avgLotSize", "averageLotSize"]))
        self._fill_if_empty(data, "Avg Lot - Width/Depth", _first_value(combined, ["lotDimensions", "lotWidthDepth", "lotWidthAndDepth"]))
        self._fill_if_empty(data, "Amenities Available", _all_text_values(combined, ["amenities"]))
        self._fill_if_empty(data, "Amenity Type", _all_text_values(combined, ["amenityType", "amenityTypes"]))
        self._fill_if_empty(data, "Overall Description of the Community", _first_value(combined, ["description", "communityDescription"]))
        self._fill_if_empty(data, "HOA Fee", _first_value(combined, ["hoaFee", "hoaDues"]))
        self._fill_if_empty(data, "HOA Services", _all_text_values(combined, ["hoaServices"]))
        self._fill_if_empty(data, "Other Fees (i.e. CDD)", _first_value(combined, ["cddFee", "otherFees"]))
        self._fill_if_empty(data, "City/Town/Property Tax %", _first_value(combined, ["taxRate", "propertyTax"]))
        self._fill_if_empty(data, "Sales Start Date", _first_value(combined, ["salesStartDate"]))
        self._fill_if_empty(data, "Sold Out Date", _first_value(combined, ["soldOutDate"]))
        self._fill_if_empty(data, "Total Lots Sold", _first_number(combined, ["totalLotsSold"]))
        self._fill_if_empty(data, "Incentives", _all_text_values(combined, ["incentives"]))
        self._fill_if_empty(data, "Images", self._collect_images_from_xpath(response))
        data["status"] = ""

        yield data

    def _extract_next_data(self, response):
        script_text = response.css("script#__NEXT_DATA__::text").get()
        if not script_text:
            return {}
        try:
            return json.loads(script_text)
        except json.JSONDecodeError:
            return {}

    def _extract_json_ld(self, response):
        data = []
        for chunk in response.xpath("//script[@type='application/ld+json']/text()").getall():
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                data.extend(parsed)
            else:
                data.append(parsed)
        return data

    def _extract_geo(self, combined):
        geo = _first_dict(combined, ["geo", "coordinates", "location"])
        latitude = geo.get("latitude") or geo.get("lat") or ""
        longitude = geo.get("longitude") or geo.get("lng") or geo.get("lon") or ""
        return {"latitude": _to_text(latitude), "longitude": _to_text(longitude)}

    def _extract_address(self, combined):
        address = _first_dict(combined, ["address", "salesCenterAddress", "streetAddress"])
        return {
            "Street Address": _to_text(address.get("streetAddress") or address.get("addressLine1") or address.get("line1") or address.get("street")),
            "City": _to_text(address.get("addressLocality") or address.get("city")),
            "State": _to_text(address.get("addressRegion") or address.get("state")),
            "Zip Code": _to_text(address.get("postalCode") or address.get("zip") or address.get("zipCode")),
            "County": _to_text(address.get("addressCounty") or address.get("county")),
        }

    def _collect_images_from_xpath(self, response):
        image_urls = []
        for src in response.xpath("//figure[contains(@class,'GalleryMedia')]/img/@src").getall():
            src = src.strip()
            if not src:
                continue
            if src.startswith("//"):
                src = f"https:{src}"
            elif src.startswith("/"):
                src = response.urljoin(src)
            image_urls.append(src)

        deduped = []
        seen = set()
        for url in image_urls:
            if url not in seen:
                deduped.append(url)
                seen.add(url)
        return " | ".join(deduped)

    def _fill_if_empty(self, data, key, value):
        if not data.get(key) and value:
            data[key] = value
import csv
import json
import re
from pathlib import Path

import scrapy


def _norm_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return value
    return " ".join(str(value).split()).strip()


def _to_text(value):
    value = _clean(value)
    if isinstance(value, list):
        parts = [_to_text(item) for item in value]
        return " | ".join([part for part in parts if part])
    if isinstance(value, dict):
        return ""
    return value


def _walk_values(data, target_keys):
    matches = []
    normalized_targets = {_norm_key(key) for key in target_keys}

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if _norm_key(key) in normalized_targets:
                    matches.append(value)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return matches


def _first_value(data, keys, default=""):
    for value in _walk_values(data, keys):
        if isinstance(value, (str, int, float)):
            text = _to_text(value)
            if text:
                return text
    return default


def _first_number(data, keys, default=""):
    for value in _walk_values(data, keys):
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            text = value.strip()
            if re.fullmatch(r"-?\d+(\.\d+)?", text):
                return text
    return default


def _first_dict(data, keys):
    for value in _walk_values(data, keys):
        if isinstance(value, dict):
            return value
    return {}


def _all_text_values(data, keys):
    def _flatten_scalars(node):
        if isinstance(node, (str, int, float)):
            text = _to_text(node)
            return [text] if text else []
        if isinstance(node, list):
            values = []
            for item in node:
                values.extend(_flatten_scalars(item))
            return values
        if isinstance(node, dict):
            values = []
            for item in node.values():
                values.extend(_flatten_scalars(item))
            return values
        return []

    values = []
    for value in _walk_values(data, keys):
        values.extend(_flatten_scalars(value))
    # Preserve order, remove duplicates.
    deduped = []
    seen = set()
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return " | ".join(deduped)


class TollbrothersDetailsSpider(scrapy.Spider):
    name = "tollbrothers_details"
    allowed_domains = ["tollbrothers.com"]

    custom_settings = {
        "FEEDS": {
            "tollbrothers_qmi_details.csv": {
                "format": "csv",
                "overwrite": True,
            }
        }
    }

    output_columns = [
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
        "Maximum Base Price / All In Price",
        "Previous Price",
        "Last Updated",
        "Plan Features",
        "Interior Specifications Descriptions",
        "First Floor Master (Y/N)",
        "Images",
    ]

    def start_requests(self):
        csv_path = Path(__file__).resolve().parents[2] / "tollbrothers_luxury_homes_urls.csv"
        if not csv_path.exists():
            self.logger.error("Input CSV not found: %s", csv_path)
            return

        with csv_path.open("r", encoding="utf-8-sig", newline="") as infile:
            for row in csv.DictReader(infile):
                url = (row.get("url") or "").strip()
                if not url or "/Quick-Move-In/" not in url:
                    continue
                yield scrapy.Request(url=url, callback=self.parse_details, dont_filter=True)

    def parse_details(self, response):
        next_data = self._extract_next_data(response)
        json_ld = self._extract_json_ld(response)

        combined = {"next_data": next_data, "json_ld": json_ld}

        address = self._extract_address(combined)
        geo = self._extract_geo(combined)

        row = {column: "" for column in self.output_columns}
        row["url"] = response.url
        row["Website"] = response.url
        row["Builder"] = _first_value(combined, ["builderName", "builder"]) or "Toll Brothers"

        row["Community Details"] = _first_value(
            combined,
            ["communityDetails", "communityDescription", "communityOverview", "overview"],
        )
        row["Community Name"] = _first_value(
            combined,
            ["communityName", "developmentName", "neighborhoodName", "community"],
        )
        row["latitude"] = geo.get("latitude", "")
        row["longitude"] = geo.get("longitude", "")
        row["Street Address"] = address.get("Street Address", "")
        row["City"] = address.get("City", "")
        row["State"] = address.get("State", "")
        row["Zip Code"] = address.get("Zip Code", "")
        row["County"] = address.get("County", "")
        row["Model hours"] = _first_value(combined, ["salesHours", "hours", "modelHours"])
        row["Phone number"] = _first_value(combined, ["telephone", "phone", "salesPhone"])
        row["status"] = _first_value(
            combined,
            ["status", "homeStatus", "availabilityStatus", "constructionStatus"],
        )
        row["Product Type (SFD/SFA/CO)"] = _first_value(
            combined,
            ["productType", "propertyType", "homeType"],
        )
        row["Model/Product Types Available"] = _all_text_values(
            combined,
            ["collectionName", "seriesName", "modelName", "homeDesignName", "planName"],
        )
        row["Avg Lot Size"] = _first_value(combined, ["avgLotSize", "averageLotSize"])
        row["Avg Lot - Width/Depth"] = _first_value(
            combined,
            ["lotDimensions", "lotWidthDepth", "lotWidthAndDepth"],
        )
        row["# of Garages"] = _first_number(combined, ["garages", "garageSpaces", "numGarage"])
        row["Garages (Y/N)"] = "Y" if row["# of Garages"] else ""
        row["Adult Community (Y/N)"] = _first_value(
            combined,
            ["adultCommunity", "activeAdult", "ageRestricted"],
        )
        row["Amenities Available"] = _all_text_values(combined, ["amenities"])
        row["Amenity Type"] = _all_text_values(combined, ["amenityType", "amenityTypes"])
        row["Attributes/Features"] = _all_text_values(
            combined,
            ["features", "highlights", "attributes"],
        )
        row["Overall Description of the Community"] = _first_value(
            combined,
            ["description", "communityDescription", "overview"],
        )
        row["Foundation Type"] = _first_value(combined, ["foundationType"])
        row["Exterior Specifications Available"] = _all_text_values(
            combined,
            ["exterior", "exteriorFeatures", "exteriorSpecifications"],
        )
        row["Interior Specifications Available"] = _all_text_values(
            combined,
            ["interior", "interiorFeatures", "interiorSpecifications"],
        )
        row["HOA Fee"] = _first_value(combined, ["hoaFee", "hoaDues"])
        row["HOA Services"] = _all_text_values(combined, ["hoaServices"])
        row["Other Fees (i.e. CDD)"] = _first_value(combined, ["cddFee", "otherFees"])
        row["City/Town/Property Tax %"] = _first_value(combined, ["taxRate", "propertyTax"])
        row["Sales Start Date"] = _first_value(combined, ["salesStartDate"])
        row["Sold Out Date"] = _first_value(combined, ["soldOutDate"])
        row["Total Lots Sold"] = _first_number(combined, ["totalLotsSold"])
        row["Incentives"] = _all_text_values(combined, ["incentives"])
        row["QMI Incentives"] = _all_text_values(combined, ["qmiIncentives", "quickMoveInIncentives"])
        row["QMI Model Name"] = _first_value(combined, ["quickMoveInName", "homeDesignName", "modelName"])
        row["QMI # of Garages"] = _first_number(combined, ["garages", "garageSpaces"])
        row["QMI # of BR"] = _first_number(combined, ["bedrooms", "beds"])
        row["QMI # of BA"] = _first_number(combined, ["bathrooms", "baths"])
        row["QMI # of 1/2 BA"] = _first_number(combined, ["halfBaths", "halfBathrooms"])
        row["QMI Model SQFT"] = _first_number(combined, ["squareFeet", "sqft", "livingArea"])
        row["QMI Model Price"] = _first_value(combined, ["price", "salesPrice", "currentPrice"])
        row["QMI Availability Date"] = _first_value(combined, ["availableDate", "availabilityDate"])
        row["QMI Lot SQFT"] = _first_number(combined, ["lotSquareFeet", "lotSqft"])
        row["QMI Interior/Exterior Attributes"] = _all_text_values(
            combined,
            ["interiorFeatures", "exteriorFeatures", "features", "highlights"],
        )
        row["Model Details"] = _first_value(combined, ["modelDetails", "homeDesignDescription"])
        row["Model Name"] = _first_value(combined, ["modelName", "homeDesignName", "planName"])
        row["Plan Description"] = _first_value(combined, ["planDescription", "description"])
        row["# BR"] = _first_number(combined, ["bedrooms", "beds"])
        row["# BA"] = _first_number(combined, ["bathrooms", "baths"])
        row["# 1/2 BA"] = _first_number(combined, ["halfBaths", "halfBathrooms"])
        row["# of Floors"] = _first_number(combined, ["stories", "floors"])
        row["Parking Type"] = _first_value(combined, ["parkingType"])
        row["Plan Garage Entry (Front load)"] = _first_value(combined, ["garageEntry"])
        row["# Garages"] = _first_number(combined, ["garages", "garageSpaces"])
        row["Minimum SQFT"] = _first_number(combined, ["minSquareFeet", "minimumSqft"])
        row["Maximum SQFT"] = _first_number(combined, ["maxSquareFeet", "maximumSqft"])
        row["Minimum Base Price (Current)"] = _first_value(combined, ["basePrice", "startingPrice"])
        row["Maximum Base Price / All In Price"] = _first_value(
            combined,
            ["maxPrice", "allInPrice", "endingPrice"],
        )
        row["Previous Price"] = _first_value(combined, ["previousPrice"])
        row["Last Updated"] = _first_value(combined, ["lastUpdated", "updatedAt", "modifiedDate"])
        row["Plan Features"] = _all_text_values(combined, ["planFeatures", "features"])
        row["Interior Specifications Descriptions"] = _all_text_values(
            combined,
            ["interiorDescription", "interiorSpecifications", "interiorFeatures"],
        )
        row["First Floor Master (Y/N)"] = _first_value(
            combined,
            ["firstFloorMaster", "mainLevelPrimaryBedroom"],
        )
        row["Images"] = _all_text_values(combined, ["images", "image", "gallery"])

        yield row

    def _extract_next_data(self, response):
        script_text = response.css("script#__NEXT_DATA__::text").get()
        if not script_text:
            return {}
        try:
            return json.loads(script_text)
        except json.JSONDecodeError:
            self.logger.warning("Failed to parse __NEXT_DATA__ for %s", response.url)
            return {}

    def _extract_json_ld(self, response):
        data = []
        for chunk in response.xpath("//script[@type='application/ld+json']/text()").getall():
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                parsed = json.loads(chunk)
                if isinstance(parsed, list):
                    data.extend(parsed)
                else:
                    data.append(parsed)
            except json.JSONDecodeError:
                continue
        return data

    def _extract_geo(self, combined):
        geo = _first_dict(combined, ["geo", "coordinates", "location"])
        latitude = geo.get("latitude") or geo.get("lat") or ""
        longitude = geo.get("longitude") or geo.get("lng") or geo.get("lon") or ""
        return {"latitude": _to_text(latitude), "longitude": _to_text(longitude)}

    def _extract_address(self, combined):
        address = _first_dict(combined, ["address", "salesCenterAddress", "streetAddress"])
        street = _to_text(
            address.get("streetAddress")
            or address.get("addressLine1")
            or address.get("line1")
            or address.get("street")
        )
        city = _to_text(address.get("addressLocality") or address.get("city"))
        state = _to_text(address.get("addressRegion") or address.get("state"))
        postal = _to_text(address.get("postalCode") or address.get("zip") or address.get("zipCode"))
        county = _to_text(address.get("addressCounty") or address.get("county"))
        return {
            "Street Address": street,
            "City": city,
            "State": state,
            "Zip Code": postal,
            "County": county,
        }
import csv
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import scrapy


class TollbrothersDetailsSpider(scrapy.Spider):
    name = "tollbrothers_details"
    allowed_domains = ["tollbrothers.com"]
    FIELDS = [
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
    custom_settings = {
        "FEED_EXPORT_FIELDS": FIELDS,
    }

    def __init__(self, csv_file: str = "tollbrothers_luxury_homes_urls.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.csv_file = csv_file
        self.seen_urls: set[str] = set()

    def start_requests(self):
        csv_path = self._resolve_csv_path(self.csv_file)
        if csv_path is None:
            self.logger.error("CSV file not found: %s", self.csv_file)
            return

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                url = (row.get("url") or "").strip()
                if not url or url in self.seen_urls:
                    continue
                self.seen_urls.add(url)
                yield scrapy.Request(url=url, callback=self.parse)

    def parse(self, response: scrapy.http.Response):
        data = self._new_item(response.url)

        json_ld_scripts = response.xpath("//script[@type='application/ld+json']/text()").getall()
        json_objects = self._parse_json_ld(json_ld_scripts)
        product_json = self._first_by_type(json_objects, "Product")
        next_data = self._parse_next_data(response)

        data["Community Details"] = self._clean_text(
            " ".join(
                response.xpath(
                    "//section[contains(@class,'product-overview') or contains(@class,'overview')]//text()"
                ).getall()
            )
        )
        data["Community Name"] = self._get_first(
            [
                self._clean_text(
                    response.xpath(
                        "normalize-space((//a[contains(@href,'/luxury-homes-for-sale/')])[1]/text())"
                    ).get()
                ),
                self._clean_text(
                    response.xpath("normalize-space((//h1)[1]/text())").get()
                ),
                self._to_text(self._dig(product_json, "brand", "name")),
                self._search_next_data_value(next_data, ["communityName", "community", "neighborhoodName"]),
            ]
        )

        geo = self._find_geo(json_objects) or self._find_geo_from_next_data(next_data)
        data["latitude"] = self._to_text(geo.get("latitude"))
        data["longitude"] = self._to_text(geo.get("longitude"))

        address = self._find_address(json_objects) or self._find_address_from_next_data(next_data)
        data["Street Address"] = self._to_text(address.get("streetAddress"))
        data["City"] = self._to_text(address.get("addressLocality"))
        data["State"] = self._to_text(address.get("addressRegion"))
        data["Zip Code"] = self._to_text(address.get("postalCode"))

        if not data["Street Address"]:
            address_text = self._clean_text(
                response.xpath(
                    "normalize-space((//div[contains(@class,'contact-card') or contains(@class,'address')]//*[contains(@class,'address')]/text())[1])"
                ).get()
            )
            if address_text:
                street, city, state, zipcode = self._split_us_address(address_text)
                data["Street Address"] = street
                data["City"] = city
                data["State"] = state
                data["Zip Code"] = zipcode

        data["County"] = self._overview_value(response, "County")
        data["Model hours"] = self._collect_model_hours(response)
        data["Phone number"] = self._get_first(
            [
                self._clean_text(
                    response.xpath(
                        "normalize-space((//a[starts-with(@href,'tel:')])[1]/text())"
                    ).get()
                ),
                self._search_next_data_value(next_data, ["phone", "phoneNumber", "telephone"]),
                self._search_json_values(json_objects, ["telephone", "phone"]),
            ]
        )
        data["Website"] = self._domain_url(response.url)
        data["status"] = self._get_first(
            [
                self._search_next_data_value(next_data, ["status", "availabilityStatus"]),
                self._to_text(self._dig(product_json, "offers", "availability")),
            ]
        )
        data["Builder"] = self._get_first(
            [
                self._to_text(self._dig(product_json, "brand", "name")),
                "Toll Brothers",
            ]
        )

        home_type = self._get_first(
            [
                self._overview_value(response, "Home Type"),
                self._search_next_data_value(next_data, ["homeType", "productType", "propertyType"]),
            ]
        )
        data["Product Type (SFD/SFA/CO)"] = self._map_product_type(home_type)
        data["Model/Product Types Available"] = "Y" if data["Product Type (SFD/SFA/CO)"] else ""
        data["Avg Lot Size"] = self._get_first(
            [
                self._overview_value(response, "Lot Size"),
                self._search_next_data_value(next_data, ["lotSize", "averageLotSize"]),
            ]
        )
        data["Avg Lot - Width/Depth"] = self._get_first(
            [
                self._overview_value(response, "Width & Depth"),
                self._overview_value(response, "Base Plan Width & Depth"),
            ]
        )

        garages_raw = self._get_first(
            [
                self._spec_value(response, "garages"),
                self._search_next_data_value(next_data, ["garages", "garage", "garageSpaces"]),
            ]
        )
        data["# Garages"] = self._extract_number(garages_raw)
        data["# of Garages"] = data["# Garages"]
        data["Garages (Y/N)"] = "Y" if data["# Garages"] else ""
        data["Parking Type"] = self._extract_parenthetical(garages_raw)
        data["Plan Garage Entry (Front load)"] = "Y" if "front" in data["Parking Type"].lower() else ""
        data["Adult Community (Y/N)"] = self._guess_adult_community(response.text, data["Community Name"])

        amenities = self._extract_amenities(response)
        data["Amenity Type"] = amenities
        data["Amenities Available"] = "Y" if amenities else ""

        data["Overall Description of the Community"] = self._clean_text(
            " ".join(
                response.xpath(
                    "//h2[contains(.,'Community') or contains(.,'Neighborhood')]/following-sibling::*[self::p or self::div]//text()"
                ).getall()
            )
        )
        data["Attributes/Features"] = self._clean_text(
            " | ".join(
                [
                    t.strip()
                    for t in response.xpath("//ul/li//text()").getall()
                    if t and t.strip()
                ]
            )
        )
        data["Foundation Type"] = self._overview_value(response, "Foundation Type")
        data["Exterior Specifications Available"] = self._search_next_data_value(
            next_data, ["exterior", "exteriorFeatures", "elevation"]
        )
        data["Interior Specifications Available"] = self._search_next_data_value(
            next_data, ["interior", "interiorFeatures", "designFeatures"]
        )
        data["HOA Fee"] = self._get_first(
            [
                self._overview_value(response, "HOA"),
                self._search_next_data_value(next_data, ["hoaFee", "hoaDues"]),
            ]
        )
        data["HOA Services"] = self._search_next_data_value(next_data, ["hoaServices", "hoaIncludes"])
        data["Other Fees (i.e. CDD)"] = self._search_next_data_value(next_data, ["cddFee", "otherFees"])
        data["City/Town/Property Tax %"] = self._get_first(
            [
                self._clean_text(
                    response.xpath("normalize-space((//*[@data-taxrate])[1]/@data-taxrate)").get()
                ),
                self._search_next_data_value(next_data, ["taxRate", "propertyTax"]),
            ]
        )

        data["Sales Start Date"] = self._search_next_data_value(next_data, ["salesStartDate", "startDate"])
        data["Sold Out Date"] = self._search_next_data_value(next_data, ["soldOutDate"])
        data["Total Lots Sold"] = self._search_next_data_value(next_data, ["lotsSold", "totalLotsSold"])
        data["Incentives"] = self._search_next_data_value(next_data, ["incentives", "promotion"])

        # QMI-specific columns stay blank for plan pages unless identified.
        if self._is_qmi_page(response.url, next_data):
            data["QMI Model Name"] = self._get_first(
                [
                    self._clean_text(response.xpath("normalize-space((//h1)[1])").get()),
                    self._search_next_data_value(next_data, ["modelName", "planName"]),
                ]
            )
            data["QMI # of Garages"] = data["# Garages"]
            data["QMI # of BR"] = self._spec_value(response, "bedrooms")
            data["QMI # of BA"] = self._spec_value(response, "full bathrooms")
            data["QMI # of 1/2 BA"] = self._spec_value(response, "half bathrooms")
            data["QMI Model SQFT"] = self._spec_value(response, "square feet")
            data["QMI Model Price"] = self._extract_price(response)
            data["QMI Availability Date"] = self._search_next_data_value(next_data, ["availabilityDate", "readyDate"])
            data["QMI Lot SQFT"] = self._search_next_data_value(next_data, ["lotSqft", "lotSizeSqft"])
            data["QMI Interior/Exterior Attributes"] = self._clean_text(
                " | ".join(filter(None, [data["Interior Specifications Available"], data["Exterior Specifications Available"]]))
            )

        data["Model Details"] = self._search_next_data_value(next_data, ["modelDetails", "floorPlan"])
        data["Model Name"] = self._get_first(
            [
                self._overview_value(response, "Home Plan"),
                self._clean_text(response.xpath("normalize-space((//h1)[1])").get()),
                self._search_next_data_value(next_data, ["modelName", "planName"]),
            ]
        )
        data["Plan Description"] = self._clean_text(
            " ".join(
                response.xpath(
                    "//section[contains(@class,'plan') or contains(@class,'overview')]//p//text()"
                ).getall()
            )
        )

        data["# BR"] = self._spec_value(response, "bedrooms")
        data["# BA"] = self._spec_value(response, "full bathrooms")
        data["# 1/2 BA"] = self._spec_value(response, "half bathrooms")
        data["# of Floors"] = self._spec_value(response, "stories")

        sqft_raw = self._spec_value(response, "square feet")
        min_sqft, max_sqft = self._split_numeric_range(sqft_raw)
        data["Minimum SQFT"] = min_sqft
        data["Maximum SQFT"] = max_sqft

        price_min, price_max = self._extract_price_range(response)
        data["Minimum Base Price (Current)"] = price_min
        data["Maximum Base Price/All In Price"] = price_max
        data["Previous Price"] = self._extract_old_price(response)
        data["Last Updated"] = self._search_next_data_value(next_data, ["lastUpdated", "updatedAt"])
        data["Plan Features"] = data["Attributes/Features"]
        data["Interior Specifications Descriptions"] = self._clean_text(
            " | ".join(filter(None, [data["Plan Description"], data["Interior Specifications Available"]]))
        )
        data["First Floor Master (Y/N)"] = self._guess_first_floor_master(
            data["Plan Description"], data["Attributes/Features"]
        )
        data["images"] = self._collect_images(response, json_objects)

        yield data

    def _new_item(self, url: str) -> dict[str, str]:
        return {field: (url if field == "url" else "") for field in self.FIELDS}

    def _resolve_csv_path(self, csv_file: str) -> Path | None:
        provided = Path(csv_file)
        if provided.is_absolute() and provided.exists():
            return provided

        current = Path.cwd()
        spider_file = Path(__file__).resolve()
        candidates = [
            current / csv_file,
            current.parent / csv_file,
            current.parent.parent / csv_file,
            spider_file.parents[3] / csv_file,
            spider_file.parents[2] / csv_file,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _parse_json_ld(self, scripts: list[str]) -> list[Any]:
        parsed: list[Any] = []
        for script in scripts:
            script = (script or "").strip()
            if not script:
                continue
            try:
                obj = json.loads(script)
            except json.JSONDecodeError:
                continue
            parsed.extend(self._flatten_json_ld(obj))
        return parsed

    def _flatten_json_ld(self, obj: Any) -> list[Any]:
        if isinstance(obj, list):
            out: list[Any] = []
            for entry in obj:
                out.extend(self._flatten_json_ld(entry))
            return out
        if isinstance(obj, dict) and isinstance(obj.get("@graph"), list):
            out: list[Any] = []
            for entry in obj["@graph"]:
                out.extend(self._flatten_json_ld(entry))
            return out
        return [obj]

    def _parse_next_data(self, response: scrapy.http.Response) -> dict[str, Any]:
        payload = response.xpath("//script[@id='__NEXT_DATA__']/text()").get()
        if not payload:
            return {}
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        page_props = data.get("props", {}).get("pageProps")
        return page_props if isinstance(page_props, dict) else {}

    def _first_by_type(self, json_objects: list[Any], schema_type: str) -> dict[str, Any]:
        target = schema_type.lower()
        for obj in json_objects:
            if isinstance(obj, dict) and str(obj.get("@type", "")).lower() == target:
                return obj
        return {}

    def _find_geo(self, json_objects: list[Any]) -> dict[str, Any]:
        for obj in json_objects:
            if isinstance(obj, dict):
                geo = obj.get("geo")
                if isinstance(geo, dict):
                    return geo
        return {}

    def _find_address(self, json_objects: list[Any]) -> dict[str, Any]:
        for obj in json_objects:
            if isinstance(obj, dict):
                address = obj.get("address")
                if isinstance(address, dict):
                    return address
        return {}

    def _find_geo_from_next_data(self, data: dict[str, Any]) -> dict[str, Any]:
        latitude = self._search_next_data_value(data, ["latitude", "lat"])
        longitude = self._search_next_data_value(data, ["longitude", "lng", "lon"])
        if latitude or longitude:
            return {"latitude": latitude, "longitude": longitude}
        return {}

    def _find_address_from_next_data(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "streetAddress": self._search_next_data_value(data, ["streetAddress", "address1"]),
            "addressLocality": self._search_next_data_value(data, ["city", "addressLocality"]),
            "addressRegion": self._search_next_data_value(data, ["state", "addressRegion"]),
            "postalCode": self._search_next_data_value(data, ["zip", "postalCode"]),
        }

    def _search_next_data_value(self, data: Any, keys: list[str]) -> str:
        values = self._search_values(data, keys)
        for value in values:
            text = self._clean_text(self._to_text(value))
            if text:
                return text
        return ""

    def _search_json_values(self, data: Any, keys: list[str]) -> str:
        values = self._search_values(data, keys)
        for value in values:
            text = self._clean_text(self._to_text(value))
            if text:
                return text
        return ""

    def _search_values(self, data: Any, keys: list[str]) -> list[Any]:
        keys_lower = {k.lower() for k in keys}
        results: list[Any] = []

        def visit(node: Any):
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() in keys_lower:
                        results.append(value)
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(data)
        return results

    def _collect_model_hours(self, response: scrapy.http.Response) -> str:
        day_rows = response.xpath(
            "//ul[contains(@class,'schedule') or contains(@class,'hours')]/li"
        )
        parts = []
        for row in day_rows:
            text = self._clean_text(" ".join(row.xpath(".//text()").getall()))
            if text:
                parts.append(text)
        return " | ".join(parts)

    def _collect_images(self, response: scrapy.http.Response, json_objects: list[Any]) -> str:
        urls: list[str] = []
        urls.extend(self._listify_image_values(self._search_values(json_objects, ["image"])))
        urls.extend(
            [
                self._clean_text(url)
                for url in response.xpath("//img/@src | //img/@data-src").getall()
                if self._clean_text(url).startswith("http")
            ]
        )

        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return " | ".join(deduped)

    def _listify_image_values(self, values: list[Any]) -> list[str]:
        out: list[str] = []
        for value in values:
            if isinstance(value, list):
                for nested in value:
                    text = self._clean_text(self._to_text(nested))
                    if text:
                        out.append(text)
            else:
                text = self._clean_text(self._to_text(value))
                if text:
                    out.append(text)
        return out

    def _spec_value(self, response: scrapy.http.Response, label: str) -> str:
        return self._clean_text(
            response.xpath(
                "normalize-space(//*[contains(@class,'plan-specification') or contains(@class,'specification')]//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), $label_lower)]/following::strong[1])",
                label_lower=label.lower(),
            ).get()
        )

    def _overview_value(self, response: scrapy.http.Response, label: str) -> str:
        return self._clean_text(
            response.xpath(
                "normalize-space((//dt[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=$label_lower]/following-sibling::dd[1])[1])",
                label_lower=label.lower(),
            ).get()
        )

    def _extract_amenities(self, response: scrapy.http.Response) -> str:
        text = self._clean_text(
            " ".join(
                response.xpath(
                    "//section[contains(@class,'amenit') or contains(., 'Amenities')]//text()"
                ).getall()
            )
        ).lower()
        if not text:
            return ""
        known = [
            "pool",
            "clubhouse",
            "fitness",
            "playground",
            "park",
            "trails",
            "tennis",
            "pickleball",
            "golf",
            "gated",
        ]
        found = [name.title() for name in known if name in text]
        return " | ".join(found)

    def _extract_price_range(self, response: scrapy.http.Response) -> tuple[str, str]:
        text = self._clean_text(
            " ".join(
                response.xpath(
                    "//*[contains(@class,'price') or contains(text(),'Starting at')]/text()"
                ).getall()
            )
        )
        prices = re.findall(r"\$\s*[\d,]+(?:\.\d+)?", text or "")
        if len(prices) >= 2:
            return self._digits_only(prices[0]), self._digits_only(prices[1])
        if len(prices) == 1:
            return self._digits_only(prices[0]), ""
        return "", ""

    def _extract_price(self, response: scrapy.http.Response) -> str:
        min_price, _ = self._extract_price_range(response)
        return min_price

    def _extract_old_price(self, response: scrapy.http.Response) -> str:
        old_price = self._clean_text(
            " ".join(
                response.xpath(
                    "//*[contains(@class,'old') and contains(@class,'price')]//text()"
                ).getall()
            )
        )
        return self._digits_only(old_price)

    def _split_numeric_range(self, value: str) -> tuple[str, str]:
        cleaned = (value or "").replace(",", "").strip()
        if not cleaned:
            return "", ""
        parts = [part.strip() for part in re.split(r"\s*-\s*", cleaned) if part.strip()]
        if len(parts) >= 2:
            return self._digits_only(parts[0]), self._digits_only(parts[1])
        return self._digits_only(cleaned), ""

    def _extract_number(self, text: str) -> str:
        match = re.search(r"\d+(?:\.\d+)?", text or "")
        return match.group(0) if match else ""

    def _extract_parenthetical(self, text: str) -> str:
        match = re.search(r"\(([^)]+)\)", text or "")
        return match.group(1).strip() if match else ""

    def _split_us_address(self, full_address: str) -> tuple[str, str, str, str]:
        if not full_address:
            return "", "", "", ""
        match = re.match(r"^(.*?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})", full_address)
        if not match:
            return full_address, "", "", ""
        return (
            match.group(1).strip(),
            match.group(2).strip(),
            match.group(3).strip(),
            match.group(4).strip(),
        )

    def _map_product_type(self, value: str) -> str:
        lowered = (value or "").lower()
        if "town" in lowered or "duplex" in lowered:
            return "SFA"
        if "condo" in lowered or "condominium" in lowered:
            return "CO"
        if "single" in lowered or "detached" in lowered:
            return "SFD"
        return value

    def _guess_adult_community(self, html: str, community_name: str) -> str:
        text = f"{html} {community_name}".lower()
        return "Y" if "55+" in text or "active adult" in text else "N"

    def _guess_first_floor_master(self, *texts: str) -> str:
        merged = " ".join([t.lower() for t in texts if t])
        if not merged:
            return ""
        if "first-floor primary bedroom" in merged or "first floor primary bedroom" in merged:
            return "Y"
        if "first-floor master" in merged or "first floor master" in merged:
            return "Y"
        return ""

    def _is_qmi_page(self, url: str, next_data: dict[str, Any]) -> bool:
        if any(token in url.lower() for token in ["quick-move", "move-in-ready", "inventory"]):
            return True
        return bool(self._search_next_data_value(next_data, ["readyDate", "availabilityDate"]))

    def _domain_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}/"

    def _dig(self, data: Any, *path: str) -> Any:
        current = data
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _get_first(self, values: list[Any] | Any, fallback: Any = "") -> str:
        if not isinstance(values, list):
            values = [values]
        for value in values:
            text = self._clean_text(self._to_text(value))
            if text:
                return text
        return self._clean_text(self._to_text(fallback))

    def _digits_only(self, value: str) -> str:
        return re.sub(r"[^\d.]", "", value or "")

    def _clean_text(self, value: Any) -> str:
        text = self._to_text(value)
        text = (
            text.replace("â€”", " ")
            .replace("â€“", " ")
            .replace("â€™", "'")
            .replace("â€œ", '"')
            .replace("â€", '"')
            .replace("Â", " ")
        )
        return " ".join(text.replace("\xa0", " ").split())

    def _to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            parts: list[str] = []
            for key, val in value.items():
                text = self._to_text(val)
                if text:
                    parts.append(f"{key}: {text}")
            return "; ".join(parts)
        if isinstance(value, list):
            return " | ".join(filter(None, [self._to_text(v) for v in value]))
        return str(value).strip()
import csv
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import scrapy


class TollbrothersDetailsSpider(scrapy.Spider):
    name = "tollbrothers_details"
    allowed_domains = ["tollbrothers.com", "www.tollbrothers.com"]

    REDIRECT_STATUSES = {301, 302, 303, 307, 308}
    FIELDNAMES = [
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
        "Model Hours",
        "Phone Number",
        "Website",
        "Status",
        "Builder",
        "Product Type (SFD/SFA/CO)",
        "Model/Product Types Available",
        "Avg Lot Size",
        "Avg Lot Width/Depth",
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
        "Other Fees (i.e., CDD)",
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
        "Plan Garage Entry (Front Load)",
        "Minimum SQFT",
        "Maximum SQFT",
        "Minimum Base Price (Current)",
        "Maximum Base Price / All-In Price",
        "Previous Price",
        "Last Updated",
        "Plan Features",
        "Interior Specifications Descriptions",
        "First Floor Master (Y/N)",
        "Images",
    ]

    def __init__(self, csv_file: str = "tollbrothers_luxury_homes_urls.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.csv_file = csv_file
        self.seen_urls: set[str] = set()

    def start_requests(self):
        csv_path = self._resolve_csv_path(self.csv_file)
        if csv_path is None:
            self.logger.error("CSV file not found: %s", self.csv_file)
            return

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                url = (row.get("url") or "").strip()
                if not url or url in self.seen_urls:
                    continue
                self.seen_urls.add(url)
                yield scrapy.Request(
                    url=url,
                    callback=self.parse,
                    dont_filter=True,
                    meta={
                        "dont_redirect": True,
                        "handle_httpstatus_list": list(self.REDIRECT_STATUSES),
                    },
                )

    def parse(self, response: scrapy.http.Response):
        if response.status in self.REDIRECT_STATUSES:
            self.logger.info("Skipping redirected URL (%s): %s", response.status, response.url)
            return

        page_text = (response.text or "").lower()
        has_bedroom_or_bathroom = any(
            keyword in page_text
            for keyword in ("bedroom", "bedrooms", "bathroom", "bathrooms", "baths")
        )
        if not has_bedroom_or_bathroom:
            print(f"Skipping URL without bedroom/bathroom details: {response.url}")
            self.logger.info("Skipping URL without bedroom/bathroom details: %s", response.url)
            return

        data = self._new_item(response.url)
        json_objects = self._parse_page_json(response)
        page_props = self._get_page_props(response)
        page_model = self._dig(page_props, "pageData", "modelComponent")
        if not isinstance(page_model, dict) or not page_model:
            alt = self._dig(page_props, "modelComponent")
            page_model = alt if isinstance(alt, dict) else {}
        product_json = self._first_by_type(json_objects, "Product")
        residence_json = self._first_by_type(json_objects, "Residence")
        place_json = self._first_by_type(json_objects, "Place")
        body_text = self._clean_text(" ".join(response.xpath("//body//text()").getall()))

        data["Website"] = self._domain_url(response.url)
        data["Builder"] = self._first_non_empty(
            [
                self._to_text(self._dig(product_json, "brand", "name")),
                self._to_text(self._dig(product_json, "brand", "parentOrganization", "name")),
                "Toll Brothers",
            ]
        )
        data["status"] = ""
        data["Community Name"] = self._first_non_empty(
            [
                self._clean_text(
                    response.xpath(
                        "normalize-space((//a[contains(@href,'/luxury-homes-for-sale/')][position()=last()]/text())[1])"
                    ).get()
                ),
                self._clean_text(
                    response.xpath("normalize-space((//h1)[1])").get()
                ),
                self._clean_text(self._to_text(self._dig(place_json, "name"))),
                self._clean_text(self._to_text(self._dig(residence_json, "name"))),
            ]
        )
        data["Community Details"] = ""

        data["Phone Number"] = self._first_non_empty(
            [
                self._clean_text(response.xpath("normalize-space((//a[starts-with(@href,'tel:')])[1])").get()),
                self._clean_text(self._to_text(self._get_first(self._search_values(json_objects, ["telephone", "phone"])))),
            ]
        )

        geo = self._find_geo(json_objects)
        data["latitude"] = self._round_coord(geo.get("latitude"))
        data["longitude"] = self._round_coord(geo.get("longitude"))

        xpath_addr = self._xpath_house_address_fields(response)
        address = self._find_home_address(json_objects)
        data["Street Address"] = self._clean_text(xpath_addr.get("street") or self._to_text(address.get("streetAddress")))
        data["City"] = self._clean_text(xpath_addr.get("city") or self._to_text(address.get("addressLocality")))
        data["State"] = self._clean_text(xpath_addr.get("state") or self._to_text(address.get("addressRegion")))
        data["Zip Code"] = self._clean_text(xpath_addr.get("zip") or self._to_text(address.get("postalCode")))
        if not data["Street Address"]:
            data["Street Address"] = self._extract_street_from_text(body_text)
        if not data["Zip Code"]:
            data["Zip Code"] = self._extract_json_value(json_objects, ["zip", "zipcode", "postalCode", "postal"])
        if self._looks_like_sales_center_address(data["Street Address"], body_text) or self._looks_like_online_sales_address(
            data["Street Address"], body_text
        ):
            data["Street Address"] = ""
            data["City"] = ""
            data["State"] = ""
            data["Zip Code"] = ""
        data["County"] = self._first_non_empty(
            [
                self._clean_text(self._to_text(address.get("addressCounty"))),
                self._extract_json_value(json_objects, ["county", "addressCounty"]),
                self._find_labeled_value(response, ["County"]),
                self._extract_county_from_text(body_text),
            ]
        )
        if data["County"] and not data["County"].lower().endswith("county"):
            data["County"] = f"{data['County']} County"
        if not data["City"] or not data["State"]:
            city, state = self._extract_city_state_from_text(body_text)
            data["City"] = data["City"] or city
            data["State"] = data["State"] or state

        data["Model Hours"] = self._extract_model_hours(response, page_props)
        data["Product Type (SFD/SFA/CO)"] = self._map_product_type(
            self._first_non_empty(
                [
                    self._find_labeled_value(response, ["Home Type", "Property Type", "Residence Type"]),
                    self._clean_text(self._to_text(self._get_first(self._search_values(json_objects, ["homeType", "propertyType"])))),
                ]
            )
        )
        data["Model/Product Types Available"] = "YES" if data["Product Type (SFD/SFA/CO)"] else "NO"
        data["Avg Lot Size"] = self._find_labeled_value(response, ["Lot Size", "Average Lot Size"])
        data["Avg Lot Width/Depth"] = self._find_labeled_value(
            response, ["Width & Depth", "Base Plan Width & Depth", "Lot Width/Depth"]
        )
        garages_raw = self._first_non_empty(
            [
                self._find_labeled_value(response, ["Garages", "Garage"]),
                self._clean_text(self._to_text(self._get_first(self._search_values(json_objects, ["numberOfGarageSpaces"])))),
            ]
        )
        data["# of Garages"] = self._extract_number(garages_raw)
        data["Garages (Y/N)"] = "Y" if data["# of Garages"] else "N"
        data["Parking Type"] = self._find_labeled_value(response, ["Parking", "Parking Type"])
        data["Plan Garage Entry (Front Load)"] = self._garage_entry_value(data["Parking Type"])

        data["Overall Description of the Community"] = ""

        data["Foundation Type"] = self._find_labeled_value(response, ["Foundation Type"])
        elevations = self._dig(page_model, "elevations")
        data["Exterior Specifications Available"] = self._yes_no_from_nonempty(elevations)
        data["Interior Specifications Available"] = self._yn_from_text(
            self._find_labeled_value(response, ["Interior", "Interior Features"])
        )
        data["HOA Fee"] = self._find_labeled_value(response, ["HOA", "HOA Fee", "Association Fee"])
        data["HOA Services"] = self._find_labeled_value(response, ["HOA Services", "Association Services"])
        data["Other Fees (i.e., CDD)"] = self._find_labeled_value(response, ["CDD", "Other Fees", "Special Tax"])
        data["City/Town/Property Tax %"] = self._find_labeled_value(response, ["Tax Rate", "Property Tax", "City/Town/Property Tax %"])
        data["Sales Start Date"] = self._find_labeled_value(response, ["Sales Start Date", "Start Date"])
        data["Sold Out Date"] = self._find_labeled_value(response, ["Sold Out Date"])
        data["Total Lots Sold"] = self._find_labeled_value(response, ["Total Lots Sold", "Lots Sold"])
        data["Incentives"] = self._find_labeled_value(response, ["Incentives", "Promotion"])

        model_desc = self._dig(page_model, "description")
        if isinstance(page_model, dict) and "description" in page_model:
            data["Model Details"] = self._clean_text(self._to_text(page_model.get("description")))
        else:
            data["Model Details"] = self._clean_text(
                " ".join(
                    response.xpath(
                        "//section//*[contains(@class,'overview') or contains(@class,'spec')]/descendant::text()"
                    ).getall()
                )
            )
        data["Model Name"] = self._first_non_empty(
            [
                self._clean_text(response.xpath("normalize-space((//h1)[1])").get()),
                self._clean_text(self._to_text(self._dig(product_json, "name"))),
            ]
        )
        if isinstance(page_model, dict) and "description" in page_model:
            data["Plan Description"] = self._clean_text(self._to_text(page_model.get("description")))
        else:
            data["Plan Description"] = self._clean_text(
                " ".join(
                    response.xpath(
                        "//section//*[self::p or self::li][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),'plan')]/text()"
                    ).getall()
                )
            )
        data["# BR"] = self._normalize_range_text(self._find_labeled_value(response, ["Bedrooms", "Beds"]))
        data["# BA"] = self._normalize_range_text(self._find_labeled_value(response, ["Bathrooms", "Baths"]))
        data["# 1/2 BA"] = self._normalize_range_text(self._find_labeled_value(response, ["Half Bathrooms", "Half Baths", "1/2 BA"]))
        data["# of Floors"] = self._normalize_range_text(self._find_labeled_value(response, ["Stories", "Floors"]))
        if not data["# BR"]:
            data["# BR"] = self._normalize_range_text(
                self._extract_metric_from_text(body_text, ["Bedrooms", "Beds"])
            )
        if not data["# BA"]:
            data["# BA"] = self._normalize_range_text(
                self._extract_metric_from_text(body_text, ["Bathrooms", "Baths"])
            )
        if not data["# 1/2 BA"]:
            data["# 1/2 BA"] = self._normalize_range_text(
                self._extract_metric_from_text(body_text, ["Half Bathrooms", "Half Baths", "Half Bath", "1/2 BA"])
            )
        if not data["# of Floors"]:
            data["# of Floors"] = self._normalize_range_text(
                self._extract_metric_from_text(body_text, ["Stories", "Floors"])
            )
        min_sqft, max_sqft = self._split_numeric_range(
            self._find_labeled_value(response, ["Square Feet", "Sq Ft", "SQFT", "Living Area"])
        )
        if not min_sqft and not max_sqft:
            sqft_text = self._extract_metric_from_text(body_text, ["Square Feet", "Sq Ft", "SQFT", "Living Area"])
            min_sqft, max_sqft = self._split_numeric_range(sqft_text)
        if not min_sqft and not max_sqft:
            sqft_text = self._extract_sqft_from_json(json_objects)
            min_sqft, max_sqft = self._split_numeric_range(sqft_text)
        data["Minimum SQFT"] = min_sqft
        data["Maximum SQFT"] = max_sqft
        if not data["# of Garages"]:
            data["# of Garages"] = self._extract_number(
                self._extract_metric_from_text(body_text, ["Garage", "Garages"])
            )
            data["Garages (Y/N)"] = "Y" if data["# of Garages"] else data["Garages (Y/N)"]
        min_price, max_price, previous = self._extract_prices(response)
        data["Minimum Base Price (Current)"] = min_price
        data["Maximum Base Price / All-In Price"] = max_price
        data["Previous Price"] = previous
        data["Last Updated"] = self._clean_text(
            response.xpath("normalize-space((//time/@datetime)[1])").get()
        )
        feature_items = [
            self._clean_text(t)
            for t in response.xpath("//h4[contains(@class,'CommunityFloorplan')]/following-sibling::ul/li//text()").getall()
            if self._clean_text(t)
        ]
        features_from_xpath = " | ".join(dict.fromkeys(feature_items))
        model_bullets = self._extract_model_bullets(page_model)
        data["Attributes/Features"] = self._first_non_empty([features_from_xpath, model_bullets])
        data["Plan Features"] = data["Attributes/Features"]
        data["Interior Specifications Descriptions"] = self._find_labeled_value(
            response, ["Interior", "Interior Specifications", "Interior Features"]
        )
        data["First Floor Master (Y/N)"] = self._master_bedroom_first_floor_value(response, json_objects)

        amenity_text = self._clean_text(
            " | ".join(
                [t.strip() for t in response.xpath("//*[contains(@class,'amenit')]/descendant::text()").getall() if t.strip()]
            )
        )
        data["Amenity Type"] = amenity_text
        data["Amenities Available"] = "Y" if amenity_text else "N"
        data["Adult Community (Y/N)"] = "Y" if "55+" in (data["Community Details"] + " " + data["Overall Description of the Community"]) else "N"
        data["Images"] = self._collect_images(response, json_objects)

        if self._is_qmi_page(response.url, response):
            data["QMI Incentives"] = data["Incentives"]
            data["QMI Model Name"] = data["Model Name"]
            data["QMI # of Garages"] = data["# of Garages"]
            data["QMI # of BR"] = data["# BR"]
            data["QMI # of BA"] = data["# BA"]
            data["QMI # of 1/2 BA"] = data["# 1/2 BA"]
            qmi_sqft = self._extract_quantitative_value_sqft_ftk(page_props) or self._extract_quantitative_value_sqft_ftk(
                json_objects
            )
            data["QMI Model SQFT"] = qmi_sqft or (data["Maximum SQFT"] or data["Minimum SQFT"])
            data["QMI Model Price"] = self._extract_price_from_xpath(response)
            data["QMI Availability Date"] = self._find_labeled_value(response, ["Availability", "Move-In Date", "Ready Date"])
            data["QMI Lot SQFT"] = data["Avg Lot Size"]
            data["QMI Interior/Exterior Attributes"] = self._first_non_empty(
                [data["Plan Features"], data["Interior Specifications Descriptions"]]
            )

        yield data

    def _get_page_props(self, response: scrapy.http.Response) -> dict[str, Any]:
        raw = response.xpath("//script[@id='__NEXT_DATA__']/text()").get()
        if not raw:
            return {}
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        props = blob.get("props")
        if not isinstance(props, dict):
            return {}
        page_props = props.get("pageProps")
        return page_props if isinstance(page_props, dict) else {}

    def _xpath_house_address_fields(self, response: scrapy.http.Response) -> dict[str, str]:
        not_sales = (
            "[not(ancestor::*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sales center')])]"
            "[not(ancestor::*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'online sales team')])]"
            "[not(ancestor::*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'online sales')])]"
        )
        street = response.xpath(f"//span[contains(@class,'streetAddress')]{not_sales}/text()").get()
        city = response.xpath(
            f"//span[contains(@class,'addressLocality') or contains(@class,'locality')]{not_sales}/text()"
        ).get()
        state = response.xpath(
            f"//span[contains(@class,'addressRegion') or contains(@class,'region')]{not_sales}/text()"
        ).get()
        zipcode = response.xpath(f"//span[contains(@class,'postalCode')]{not_sales}/text()").get()
        return {
            "street": self._clean_text(street) if street else "",
            "city": self._clean_text(city) if city else "",
            "state": self._clean_text(state) if state else "",
            "zip": self._clean_text(zipcode) if zipcode else "",
        }

    def _yes_no_from_nonempty(self, data: Any) -> str:
        if data is None:
            return "No"
        if isinstance(data, list):
            return "Yes" if len(data) > 0 else "No"
        if isinstance(data, dict):
            return "Yes" if len(data) > 0 else "No"
        if isinstance(data, str):
            return "Yes" if self._clean_text(data) else "No"
        return "Yes" if data else "No"

    def _extract_model_bullets(self, page_model: dict[str, Any]) -> str:
        if not isinstance(page_model, dict):
            return ""
        gallery = page_model.get("gallery")
        if not isinstance(gallery, dict):
            return ""
        walk_throughs = gallery.get("walkThroughs")
        if not isinstance(walk_throughs, list) or not walk_throughs:
            return ""
        first = walk_throughs[0]
        if not isinstance(first, dict):
            return ""
        bullets = first.get("modelBullets")
        if isinstance(bullets, list):
            parts = [self._clean_text(self._to_text(b)) for b in bullets]
            return " | ".join(p for p in parts if p)
        return self._clean_text(self._to_text(bullets))

    def _extract_quantitative_value_sqft_ftk(self, root: Any) -> str:
        found: list[str] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                raw_type = node.get("@type")
                if isinstance(raw_type, list):
                    type_text = " ".join(str(t) for t in raw_type).lower()
                else:
                    type_text = str(raw_type or "").lower()
                unit = str(node.get("unitCode", "") or "").upper()
                if "quantitativevalue" in type_text and unit == "FTK":
                    val = node.get("value")
                    if val is not None and str(val).strip():
                        found.append(re.sub(r"[^\d.]", "", str(val).strip()) or self._clean_text(self._to_text(val)))
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        if isinstance(root, list):
            for item in root:
                visit(item)
        else:
            visit(root)
        return found[0] if found else ""

    def _collect_schedule_entries(self, page_props: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                if "timeString" in node and "day" in node and isinstance(node.get("timeString"), str):
                    entries.append(
                        {
                            "day": node.get("day"),
                            "timeString": node.get("timeString"),
                            "appointmentOnly": node.get("appointmentOnly"),
                        }
                    )
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(page_props)
        return entries

    def _parse_schedule_from_raw_json_text(self, text: str) -> dict[str, str]:
        by_day: dict[str, str] = {}
        for day_code, time_string in re.findall(
            r'"day"\s*:\s*"([^"]+)"\s*,\s*"timeString"\s*:\s*"([^"]+)"',
            text,
            flags=re.IGNORECASE,
        ):
            full_day = self._day_code_to_name(day_code)
            if full_day:
                normalized = self._normalize_time_range_text(time_string)
                if normalized:
                    by_day[full_day] = normalized
        for time_string, day_code in re.findall(
            r'"timeString"\s*:\s*"([^"]+)"\s*,\s*"day"\s*:\s*"([^"]+)"',
            text,
            flags=re.IGNORECASE,
        ):
            full_day = self._day_code_to_name(day_code)
            if full_day:
                normalized = self._normalize_time_range_text(time_string)
                if normalized:
                    by_day[full_day] = normalized
        return by_day

    def _new_item(self, url: str) -> dict[str, str]:
        return {field: (url if field == "url" else "") for field in self.FIELDNAMES}

    def _resolve_csv_path(self, csv_file: str) -> Path | None:
        provided = Path(csv_file)
        if provided.is_absolute() and provided.exists():
            return provided

        current = Path.cwd()
        spider_file = Path(__file__).resolve()
        candidates = [
            current / csv_file,
            current.parent / csv_file,
            current.parent.parent / csv_file,
            spider_file.parents[3] / csv_file,
            spider_file.parents[2] / csv_file,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _parse_page_json(self, response: scrapy.http.Response) -> list[Any]:
        scripts = response.xpath("//script[@type='application/ld+json']/text()").getall()
        next_data = response.xpath("//script[@id='__NEXT_DATA__']/text()").get()
        if next_data:
            scripts.append(next_data)

        parsed: list[Any] = []
        for script in scripts:
            raw = (script or "").strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            parsed.extend(self._flatten_json(obj))
        return parsed

    def _flatten_json(self, obj: Any) -> list[Any]:
        if isinstance(obj, list):
            flat: list[Any] = []
            for item in obj:
                flat.extend(self._flatten_json(item))
            return flat
        if isinstance(obj, dict):
            flat = [obj]
            graph = obj.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    flat.extend(self._flatten_json(item))
            return flat
        return [obj]

    def _search_values(self, data: Any, keys: list[str]) -> list[Any]:
        keys_lower = {k.lower() for k in keys}
        results: list[Any] = []

        def visit(node: Any):
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() in keys_lower:
                        results.append(value)
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(data)
        return results

    def _find_geo(self, json_objects: list[Any]) -> dict[str, Any]:
        for obj in json_objects:
            if isinstance(obj, dict):
                geo = obj.get("geo")
                if isinstance(geo, dict):
                    return geo
        return {}

    def _find_address(self, json_objects: list[Any]) -> dict[str, Any]:
        for obj in json_objects:
            if not isinstance(obj, dict):
                continue
            address = obj.get("address")
            if isinstance(address, dict):
                return address
        return {}

    def _find_home_address(self, json_objects: list[Any]) -> dict[str, Any]:
        preferred_types = {"residence", "product", "house", "singlefamilyresidence"}
        for obj in json_objects:
            if not isinstance(obj, dict):
                continue
            schema_type = str(obj.get("@type", "")).lower()
            address = obj.get("address")
            if schema_type in preferred_types and isinstance(address, dict):
                obj_text = self._clean_text(self._to_text(obj)).lower()
                if "sales center" not in obj_text:
                    return address
        for obj in json_objects:
            if not isinstance(obj, dict):
                continue
            address = obj.get("address")
            if isinstance(address, dict):
                obj_text = self._clean_text(self._to_text(obj)).lower()
                if "sales center" not in obj_text:
                    return address
        return {}

    def _first_by_type(self, json_objects: list[Any], schema_type: str) -> dict[str, Any]:
        target = schema_type.lower()
        for obj in json_objects:
            if not isinstance(obj, dict):
                continue
            current_type = str(obj.get("@type", "")).lower()
            if current_type == target:
                return obj
        return {}

    def _find_labeled_value(self, response: scrapy.http.Response, labels: list[str]) -> str:
        for label in labels:
            value = self._clean_text(
                response.xpath(
                    "normalize-space((//*[self::dt or self::th or self::span or self::strong][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), $label)]/following::*[self::dd or self::td or self::span][1])[1])",
                    label=label.lower(),
                ).get()
            )
            if value:
                return value
        return ""

    def _extract_status(self, response: scrapy.http.Response) -> str:
        status_tokens = ["sold out", "coming soon", "now selling", "new phase", "available", "quick move-in", "quick move in"]
        text = self._clean_text(" ".join(response.xpath("//body//text()").getall())).lower()
        for token in status_tokens:
            if token in text:
                return token.title()
        return "Available"

    def _extract_address_line(self, response: scrapy.http.Response) -> str:
        candidates = response.xpath(
            "//a[contains(@href,'maps') or contains(@href,'google.com/maps')]/text() | "
            "//*[contains(@class,'address')]/text() | "
            "//p[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),' , ') and contains(., ',')]/text()"
        ).getall()
        for candidate in candidates:
            text = self._clean_text(candidate)
            if re.search(r",\s*[A-Z]{2}\s+\d{5}", text):
                return text
        return ""

    def _split_us_address(self, full_address: str) -> tuple[str, str, str, str]:
        if not full_address:
            return "", "", "", ""
        match = re.match(r"^(.*?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", full_address)
        if not match:
            return "", "", "", ""
        return (
            match.group(1).strip(),
            match.group(2).strip(),
            match.group(3).strip(),
            match.group(4).strip(),
        )

    def _extract_model_hours(self, response: scrapy.http.Response, page_props: dict[str, Any]) -> str:
        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        by_day: dict[str, str] = {}

        for entry in self._collect_schedule_entries(page_props):
            day_code = entry.get("day")
            time_string = entry.get("timeString")
            if not isinstance(time_string, str):
                continue
            full_day = self._day_code_to_name(str(day_code or ""))
            if not full_day:
                continue
            normalized_time = self._normalize_time_range_text(time_string)
            if normalized_time:
                by_day[full_day] = normalized_time

        if not by_day:
            by_day = self._parse_schedule_from_raw_json_text(response.text or "")

        if by_day:
            return self._format_daily_hours(by_day, day_order)

        rows = response.xpath(
            "//*[contains(@class,'hours') or contains(@class,'schedule') or contains(@class,'model-hours')]//*[self::li or self::p or self::div]"
        )
        parts = []
        for row in rows:
            text = self._clean_text(" ".join(row.xpath(".//text()").getall()))
            if text and re.search(r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, flags=re.IGNORECASE):
                parts.append(text)
        if not parts:
            body_text = self._clean_text(" ".join(response.xpath("//body//text()").getall()))
            matches = re.findall(
                r"((?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)[^|]{0,80}(?:am|pm))",
                body_text,
                flags=re.IGNORECASE,
            )
            parts.extend([self._clean_text(match) for match in matches if self._clean_text(match)])
        return " | ".join(dict.fromkeys(parts))

    def _extract_model_product_types(self, response: scrapy.http.Response) -> str:
        collection_text = [
            self._clean_text(t)
            for t in response.xpath(
                "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'collection')]/text()"
            ).getall()
            if self._clean_text(t)
        ]
        return " | ".join(dict.fromkeys(collection_text))

    def _garage_entry_value(self, parking_text: str) -> str:
        lowered = (parking_text or "").lower()
        if not lowered:
            return ""
        if "front" in lowered:
            return "Y"
        if "rear" in lowered:
            return "N"
        return ""

    def _collect_images(self, response: scrapy.http.Response, json_objects: list[Any]) -> str:
        urls: list[str] = []
        gallery_urls = response.xpath("//figure[contains(@class,'GalleryMedia')]/img/@src").getall()
        for image_url in gallery_urls:
            cleaned = self._clean_text(image_url)
            if not cleaned:
                continue
            urls.append(response.urljoin(cleaned))

        deduped = []
        seen = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        if not deduped:
            og_image = self._clean_text(response.xpath("//meta[@property='og:image'][1]/@content").get())
            if og_image:
                deduped.append(response.urljoin(og_image))
        return " | ".join(deduped)

    def _extract_prices(self, response: scrapy.http.Response) -> tuple[str, str, str]:
        xpath_price = self._extract_price_from_xpath(response)
        return xpath_price, "", ""

    def _extract_price_from_xpath(self, response: scrapy.http.Response) -> str:
        price_text = self._clean_text(" ".join(response.xpath("//span[@class='price']/text()").getall()))
        if not price_text:
            return ""
        match = re.search(r"\$\s*[\d,]+(?:\.\d+)?", price_text)
        if not match:
            return ""
        return self._digits_only(match.group(0))

    def _round_coord(self, value: Any) -> str:
        if value is None:
            return ""
        raw = self._clean_text(str(value))
        if not raw:
            return ""
        try:
            return f"{float(raw):.4f}"
        except (TypeError, ValueError):
            return raw

    def _extract_metric_from_text(self, text: str, labels: list[str]) -> str:
        cleaned_text = self._clean_text(text)
        if not cleaned_text:
            return ""
        metric_pattern = r"(\d[\d,]*(?:\.\d+)?(?:\s*-\s*\d[\d,]*(?:\.\d+)?)?(?:\+)?)"
        for label in labels:
            escaped = re.escape(label)
            pattern = rf"\b{escaped}\b\s*[:\-]?\s*{metric_pattern}"
            match = re.search(pattern, cleaned_text, flags=re.IGNORECASE)
            if match:
                return self._clean_text(match.group(1))
        return ""

    def _extract_county_from_text(self, text: str) -> str:
        cleaned_text = self._clean_text(text)
        if not cleaned_text:
            return ""
        match = re.search(r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\s+County\b", cleaned_text)
        if match:
            return self._clean_text(f"{match.group(1)} County")
        return ""

    def _extract_city_state_from_text(self, text: str) -> tuple[str, str]:
        cleaned_text = self._clean_text(text)
        if not cleaned_text:
            return "", ""
        match = re.search(r"\b([A-Za-z][A-Za-z.\- ]+),\s*([A-Z]{2})\s*\|\s*[A-Za-z][A-Za-z.\- ]+\s+County\b", cleaned_text)
        if match:
            return self._clean_text(match.group(1)), self._clean_text(match.group(2))
        match = re.search(r"\b([A-Za-z][A-Za-z.\- ]+),\s*([A-Z]{2})\b", cleaned_text)
        if match:
            return self._clean_text(match.group(1)), self._clean_text(match.group(2))
        return "", ""

    def _day_code_to_name(self, day_code: str) -> str:
        code = self._clean_text(day_code).lower()
        mapping = {
            "mon": "Monday",
            "tue": "Tuesday",
            "wed": "Wednesday",
            "thu": "Thursday",
            "fri": "Friday",
            "sat": "Saturday",
            "sun": "Sunday",
            "monday": "Monday",
            "tuesday": "Tuesday",
            "wednesday": "Wednesday",
            "thursday": "Thursday",
            "friday": "Friday",
            "saturday": "Saturday",
            "sunday": "Sunday",
        }
        return mapping.get(code, "")

    def _extract_hours_time_only(self, value: str) -> str:
        text = self._clean_text(value)
        if not text:
            return ""
        if "|" in text:
            left, right = [self._clean_text(part) for part in text.split("|", 1)]
            if re.search(r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", left, flags=re.IGNORECASE):
                text = right
            elif re.search(r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", right, flags=re.IGNORECASE):
                text = left
            else:
                text = left
        return self._normalize_time_range_text(text)

    def _normalize_clock_token(self, token: str) -> str:
        text = self._clean_text(token)
        if not text:
            return ""
        match = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*([AP]M)$", text, flags=re.IGNORECASE)
        if match:
            hour = int(match.group(1))
            minute = match.group(2) or "00"
            suffix = match.group(3).upper()
            return f"{hour:02d}:{minute}{suffix}"
        match_simple = re.match(r"^(\d{1,2})\s*([AP]M)$", text, flags=re.IGNORECASE)
        if match_simple:
            return f"{int(match_simple.group(1)):02d}:00{match_simple.group(2).upper()}"
        return text

    def _normalize_time_range_text(self, value: str) -> str:
        text = self._clean_text(value)
        if not text:
            return ""
        if "|" in text:
            text = text.split("|", 1)[0].strip()
        text = re.sub(r"\s+to\s+", " - ", text, flags=re.IGNORECASE)
        parts = re.split(r"\s*-\s*", text, maxsplit=1)
        if len(parts) == 2:
            left = self._normalize_clock_token(parts[0].strip())
            right = self._normalize_clock_token(parts[1].strip())
            return f"{left} - {right}"
        return self._normalize_clock_token(text)

    def _format_daily_hours(self, by_day: dict[str, str], day_order: list[str]) -> str:
        short_day = {
            "Monday": "Mon",
            "Tuesday": "Tue",
            "Wednesday": "Wed",
            "Thursday": "Thu",
            "Friday": "Fri",
            "Saturday": "Sat",
            "Sunday": "Sun",
        }
        chunks: list[str] = []
        for day in day_order:
            time_value = by_day.get(day, "")
            if not time_value:
                continue
            chunks.append(f"{short_day[day]}: {time_value}")
        return " | ".join(chunks)

    def _format_day_list(self, days: list[str], day_order: list[str]) -> str:
        idx = {day: i for i, day in enumerate(day_order)}
        ordered_days = sorted([d for d in days if d in idx], key=lambda d: idx[d])
        if not ordered_days:
            return ""

        ranges: list[list[str]] = []
        current = [ordered_days[0]]
        for day in ordered_days[1:]:
            if idx[day] == idx[current[-1]] + 1:
                current.append(day)
            else:
                ranges.append(current)
                current = [day]
        ranges.append(current)

        pieces = []
        for day_range in ranges:
            if len(day_range) >= 3:
                pieces.append(f"{day_range[0]} - {day_range[-1]}")
            elif len(day_range) == 2:
                pieces.append(f"{day_range[0]} & {day_range[1]}")
            else:
                pieces.append(day_range[0])
        return ", ".join(pieces)

    def _extract_sqft_from_json(self, json_objects: list[Any]) -> str:
        for key in ["floorSize", "livingArea", "sqft", "squareFeet", "size"]:
            for value in self._search_values(json_objects, [key]):
                text = self._clean_text(self._to_text(value))
                if re.search(r"\d", text):
                    return text
        return ""

    def _master_bedroom_first_floor_value(self, response: scrapy.http.Response, json_objects: list[Any]) -> str:
        labeled = self._find_labeled_value(response, ["First Floor Master", "Primary Bedroom Main Floor"])
        if self._clean_text(labeled):
            return "Y"
        for value in self._search_values(json_objects, ["masterBedroomLocation"]):
            text = self._clean_text(self._to_text(value)).lower()
            if not text:
                continue
            if any(token in text for token in ["first", "1st", "main"]):
                return "Y"
            return "N"
        return "N"

    def _looks_like_sales_center_address(self, street_address: str, body_text: str) -> bool:
        street = self._clean_text(street_address).lower()
        if not street:
            return False
        if "sales center" in street:
            return True
        return False

    def _looks_like_online_sales_address(self, street_address: str, body_text: str) -> bool:
        street = self._clean_text(street_address).lower()
        if not street:
            return False
        if "online sales" in street:
            return True
        return False

    def _extract_street_from_text(self, text: str) -> str:
        cleaned = self._clean_text(text)
        if not cleaned:
            return ""
        match = re.search(
            r"\b(\d{2,6}\s+[A-Za-z0-9.\- ]+?)\s*,\s*[A-Za-z][A-Za-z.\- ]+,\s*[A-Z]{2}\s*(?:\||\d{5})",
            cleaned,
        )
        if match:
            return self._clean_text(match.group(1))
        return ""

    def _extract_json_value(self, json_objects: list[Any], keys: list[str]) -> str:
        for value in self._search_values(json_objects, keys):
            text = self._clean_text(self._to_text(value))
            if text:
                return text
        return ""

    def _split_numeric_range(self, value: str) -> tuple[str, str]:
        cleaned = (value or "").replace(",", "").strip()
        if not cleaned:
            return "", ""
        nums = re.findall(r"\d+(?:\.\d+)?", cleaned)
        if not nums:
            return "", ""
        if len(nums) >= 2 and re.search(r"\bto\b|\-", cleaned, flags=re.IGNORECASE):
            return nums[0], nums[1]
        return nums[0], ""

    def _map_product_type(self, raw: str) -> str:
        value = (raw or "").lower()
        if "townhome" in value or "duplex" in value:
            return "SFA"
        if "single" in value or "single-family" in value:
            return "SFD"
        if "condo" in value or "condominium" in value:
            return "CO"
        return ""

    def _normalize_range_text(self, value: str) -> str:
        text = self._clean_text(value).replace(",", "")
        if not text:
            return ""
        return re.sub(r"\s*-\s*", " to ", text)

    def _is_qmi_page(self, url: str, response: scrapy.http.Response) -> bool:
        if "/quick-move-in/" in (url or "").lower():
            return True
        title_text = self._clean_text(
            response.xpath("normalize-space((//h1)[1])").get()
        ).lower()
        return "quick move-in" in title_text or "quick move in" in title_text

    def _yn_from_text(self, value: str) -> str:
        return "Y" if self._clean_text(value) else "N"

    def _digits_only(self, value: str) -> str:
        return re.sub(r"[^\d.]", "", value or "")

    def _extract_number(self, text: str) -> str:
        match = re.search(r"\d+(?:\.\d+)?", text or "")
        return match.group(0) if match else ""

    def _first_non_empty(self, values: list[str]) -> str:
        for value in values:
            cleaned = self._clean_text(value)
            if cleaned:
                return cleaned
        return ""

    def _domain_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}/"

    def _dig(self, data: Any, *path: str) -> Any:
        current = data
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _get_first(self, values: list[Any]) -> Any:
        for value in values:
            text = self._clean_text(self._to_text(value))
            if text:
                return value
        return ""

    def _to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            pieces = []
            for key, item in value.items():
                text = self._to_text(item)
                if text:
                    pieces.append(f"{key}: {text}")
            return "; ".join(pieces)
        if isinstance(value, list):
            return " | ".join(filter(None, [self._to_text(item) for item in value]))
        return str(value).strip()

    def _clean_text(self, value: Any) -> str:
        text = self._to_text(value)
        text = (
            text.replace("â€”", " ")
            .replace("â€“", " ")
            .replace("â€™", "'")
            .replace("â€œ", '"')
            .replace("â€", '"')
            .replace("Â", " ")
        )
        return " ".join(text.replace("\xa0", " ").split())
