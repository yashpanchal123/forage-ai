import csv
import json
import re
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

import scrapy


class MihomesDetailsSpider(scrapy.Spider):
    name = "mihomes_details"
    allowed_domains = ["mihomes.com"]

    def __init__(self, csv_file: str = "mihomes_new_homes_urls.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.csv_file = csv_file
        self.seen_urls = set()

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

    def _resolve_csv_path(self, csv_file: str) -> Path | None:
        provided = Path(csv_file)
        if provided.is_absolute() and provided.exists():
            return provided

        # Support running from either project root or package folder.
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

    def parse(self, response):
        if "This home is no longer available" in response.text:
            self.logger.info("Skipping unavailable home: %s", response.url)
            return

        json_ld_scripts = response.xpath(
            "//script[@type='application/ld+json'][contains(text(),'image')]/text()"
        ).getall()
        json_objects = self._parse_json_ld(json_ld_scripts)
        product_json = self._first_by_type(json_objects, "Product")

        data = self._new_item(response.url)
        data["Community Name"] = self._clean_text(
            response.xpath(
                "normalize-space((//div[contains(@class,'product-header-centered')]/div/a/text())[1])"
            ).get()
        ) or self._clean_text(
            response.xpath("normalize-space((//h6[contains(@class,'strong')][1])/text())").get()
        ) or self._clean_text(
            response.xpath(
                "normalize-space((//a[contains(@href,'/new-homes/') and contains(@class,'button-plain')][1])/text())"
            ).get()
        )
        data["Community Name"] = data["Community Name"] or self._clean_text(
            self._to_text(self._dig(product_json, "brand", "name"))
        )
        data["Website"] = self._domain_url(response.url)
        data["Phone number"] = self._clean_text(
            response.xpath(
                "normalize-space((//a[contains(@class,'gami-tel')])[1]/text())"
            ).get()
        ) or self._clean_text(
            self._get_first(self._search_values(json_objects, ["telephone", "phone"]))
        )
        data["Overall Description of the Community"] = self._clean_text(
            " ".join(
                response.xpath(
                    "//h3[contains(text(),'About the Community')]/following-sibling::p/text()"
                ).getall()
            )
        )
        plan_desc_primary = self._clean_text(
            " ".join(
                response.xpath(
                    "//div[@class='overview-table']/following-sibling::div[contains(@class,'content-inner-content')]//*[not(ancestor-or-self::*[contains(@class,'community') or contains(@class,'Community')]) and not(preceding-sibling::*[contains(text(),'About the Community')] or self::*[contains(text(),'About the Community')]) and not(contains(text(),'Learn more about this community')) ]/text()"
                ).getall()
            )
        )
        data["Plan Description"] = plan_desc_primary or self._clean_text(
            " ".join(
                response.xpath(
                    "//div[@class='overview-table']/following-sibling::div[contains(@class,'content-inner-content')]//text()"
                ).getall()
            )
        )
        extra_features_text = self._clean_text(
            " | ".join(
                [
                    t.strip()
                    for t in response.xpath(
                        '//div[contains(@class,"content-inner-content__capped-mobile-content")]//ul/li//text()'
                    ).getall()
                    if t.strip()
                ]
            )
        )
        if extra_features_text:
            data["Plan Description"] = self._clean_text(
                " | ".join(filter(None, [data["Plan Description"], extra_features_text]))
            )

        data["images"] = self._collect_images(response, json_objects)

        geo = self._find_geo(json_objects)
        data["latitude"] = self._to_text(geo.get("latitude"))
        data["longitude"] = self._to_text(geo.get("longitude"))

        address = self._find_address(json_objects)
        data["Street Address"] = self._to_text(address.get("streetAddress"))
        data["City"] = self._to_text(address.get("addressLocality"))
        data["State"] = self._to_text(address.get("addressRegion"))
        data["Zip Code"] = self._to_text(address.get("postalCode"))
        if not any([data["Street Address"], data["City"], data["State"], data["Zip Code"]]):
            street_full = self._clean_text(
                response.xpath("normalize-space((//h1)[1])").get()
            )
            # Plan pages can include a trailing "Favorite" label in the H1.
            street_full = re.sub(r"\s+Favorite\s*$", "", street_full, flags=re.IGNORECASE)
            street, city, state, zipcode = self._split_us_address(street_full)
            data["Street Address"] = street
            data["City"] = city
            data["State"] = state
            data["Zip Code"] = zipcode

        data["County"] = self._overview_value(response, "County")
        data["Foundation Type"] = self._overview_value(response, "Foundation Type")
        data["Product Type (SFD/SFA/CO)"] = self._overview_value(response, "Home Type")
        data["Model/Product Types Available"] = (
            "Y" if data["Product Type (SFD/SFA/CO)"] else "N"
        )
        data["Model Name"] = self._overview_value(response, "Home Plan")
        data["Model Details"] = ""
        data["Avg Lot - Width/Depth"] = self._get_first(
            [
                self._overview_value(response, "Base Plan Width & Depth"),
                self._overview_value(response, "Width & Depth"),
            ]
        )
        data["Adult Community (Y/N)"] = "N"

        sqft_raw = self._spec_value(response, "square feet")
        min_sqft, max_sqft = self._split_numeric_range(sqft_raw)
        data["Minimum SQFT"] = min_sqft
        data["Maximum SQFT"] = max_sqft
        data["# of Floors"] = self._normalize_range_text(self._spec_value(response, "stories"))
        data["# BR"] = self._normalize_range_text(self._spec_value(response, "bedrooms"))
        data["# BA"] = self._normalize_range_text(self._spec_value(response, "full bathrooms"))
        data["# 1/2 BA"] = self._normalize_range_text(self._spec_value(response, "half bathrooms"))

        garages_raw = self._spec_value(response, "garages")
        data["# Garages"] = self._extract_number(garages_raw)
        data["# of Garages"] = data["# Garages"]
        data["Garages (Y/N)"] = "Y" if data["# Garages"] else ""
        data["Parking Type"] = self._extract_parenthetical(garages_raw)
        data["Plan Garage Entry (Front load)"] = (
            "N" if data["Parking Type"].lower() == "rear load detached"
            else "Y" if "front load" in data["Parking Type"].lower()
            else ""
        )

        qmi_availability = self._clean_text(
            response.xpath(
                "normalize-space(//dt[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'ready date')]/following-sibling::dd[1])"
            ).get()
        )
        min_price, max_price = self._extract_price_range_from_html(response)
        data["Minimum Base Price (Current)"] = min_price
        data["Maximum Base Price/All In Price"] = max_price
        old_price_html = self._clean_text(
            response.xpath("normalize-space((//span[contains(@class,'home-card-price-old')])[1])").get()
        )
        data["Previous Price"] = self._digits_only(old_price_html) if old_price_html else ""

        data["status"] = ""
        data["Builder"] = self._clean_text(
            self._to_text(self._dig(product_json, "brand", "parentOrganization", "name"))
        ) or "M/I Homes"

        schedule = self._collect_model_hours(response)
        data["Model hours"] = schedule
        data["Amenity Type"] = self._extract_amenities(
            data["Overall Description of the Community"]
        )
        data["Amenities Available"] = "Y" if data["Amenity Type"] else "N"
        data["Plan Features"] = self._clean_text(
            " | ".join(
                [
                    t.strip()
                    for t in response.xpath('//div[@data-slate-type="paragraph"]/following-sibling::ul/li//text()').getall()
                    if t.strip()
                ]
            )
        )
        if extra_features_text:
            data["Plan Features"] = self._clean_text(
                " | ".join(filter(None, [data["Plan Features"], extra_features_text]))
            )
        data["Attributes/Features"] = data["Plan Features"]
        data["Interior Specifications Descriptions"] = data["Plan Description"]
        data["City/Town/Property Tax %"] = self._clean_text(
            response.xpath("normalize-space((//*[@data-taxrate])[1]/@data-taxrate)").get()
        )

        data["Last Updated"] = ""
        data["First Floor Master (Y/N)"] = ""
        self._clear_qmi_fields(data)

        yield data

    def _new_item(self, url: str) -> dict[str, str]:
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
            "Maximum Base Price/All In Price",
            "Previous Price",
            "Last Updated",
            "Plan Features",
            "Interior Specifications Descriptions",
            "First Floor Master (Y/N)",
            "images",
        ]
        return {field: (url if field == "url" else "") for field in fields}

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
            flat = []
            for item in obj:
                flat.extend(self._flatten_json_ld(item))
            return flat
        if isinstance(obj, dict) and isinstance(obj.get("@graph"), list):
            flat = []
            for item in obj["@graph"]:
                flat.extend(self._flatten_json_ld(item))
            return flat
        return [obj]

    def _search_values(self, data: Any, keys: list[str]) -> list[Any]:
        keys_lower = {k.lower() for k in keys}
        results: list[Any] = []

        def visit(node: Any):
            if isinstance(node, dict):
                for k, v in node.items():
                    if str(k).lower() in keys_lower:
                        results.append(v)
                    visit(v)
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
            if isinstance(obj, dict):
                address = obj.get("address")
                if isinstance(address, dict):
                    return address
        return {}

    def _join_values(self, values: list[Any]) -> str:
        parts: list[str] = []
        for value in values:
            if isinstance(value, list):
                for nested in value:
                    text = self._to_text(nested)
                    if text:
                        parts.append(text)
            else:
                text = self._to_text(value)
                if text:
                    parts.append(text)
        deduped = []
        seen = set()
        for part in parts:
            if part in seen:
                continue
            seen.add(part)
            deduped.append(part)
        return " | ".join(deduped)

    def _collect_images(self, response: scrapy.http.Response, json_objects: list[Any]) -> str:
        urls: list[str] = []
        urls.extend(self._listify_image_values(self._search_values(json_objects, ["image"])))

        srcset_values = response.xpath(
            "//ul[@class='gallery-modal__list with-transition']/li//img/@srcset | "
            "//div[contains(@class,'gallery-slide__image')]//img/@srcset"
        ).getall()
        for srcset in srcset_values:
            best_url = self._extract_best_srcset_url(srcset)
            if best_url:
                urls.append(best_url)

        direct_img_urls = response.xpath(
            "//ul[@class='gallery-modal__list with-transition']/li//img/@src | "
            "//div[contains(@class,'gallery-slide__image')]//img/@src"
        ).getall()
        urls.extend([self._clean_text(url) for url in direct_img_urls if self._clean_text(url)])

        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            cleaned = self._clean_text(url)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            deduped.append(cleaned)
        return " | ".join(deduped)

    def _listify_image_values(self, values: list[Any]) -> list[str]:
        items: list[str] = []
        for value in values:
            if isinstance(value, list):
                for nested in value:
                    text = self._clean_text(self._to_text(nested))
                    if text:
                        items.append(text)
            else:
                text = self._clean_text(self._to_text(value))
                if text:
                    items.append(text)
        return items

    def _extract_best_srcset_url(self, srcset: str) -> str:
        # srcset format: "url1 320w, url2 640w, ..."; keep highest-resolution URL.
        parts = [part.strip() for part in (srcset or "").split(",") if part.strip()]
        if not parts:
            return ""
        last_candidate = parts[-1].split()[0].strip()
        return self._clean_text(last_candidate)

    def _spec_value(self, response: scrapy.http.Response, label: str) -> str:
        return self._clean_text(
            response.xpath(
                "normalize-space(//section[contains(@class,'plan-specification')]//span[contains(@class,'plan-specification-item__label') and translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=$label_lower]/following-sibling::strong[1])",
                label_lower=label.lower(),
            ).get()
        )

    def _overview_value(self, response: scrapy.http.Response, label: str) -> str:
        return self._clean_text(
            response.xpath(
                "normalize-space((//div[contains(@class,'overview-table')]//dt[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=$label_lower]/following-sibling::dd[1])[1])",
                label_lower=label.lower(),
            ).get()
        )

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

    def _dig(self, data: Any, *path: str) -> Any:
        cur = data
        for key in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur

    def _first_by_type(self, json_objects: list[Any], schema_type: str) -> dict[str, Any]:
        target = schema_type.lower()
        for obj in json_objects:
            if isinstance(obj, dict):
                t = str(obj.get("@type", "")).lower()
                if t == target:
                    return obj
        return {}

    def _split_us_address(self, full_address: str) -> tuple[str, str, str, str]:
        if not full_address:
            return "", "", "", ""
        match = re.match(r"^(.*?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})", full_address)
        if not match:
            return full_address, "", "", ""
        return match.group(1).strip(), match.group(2).strip(), match.group(3).strip(), match.group(4).strip()

    def _extract_number(self, text: str) -> str:
        match = re.search(r"\d+(?:\.\d+)?", text or "")
        return match.group(0) if match else ""

    def _extract_parenthetical(self, text: str) -> str:
        match = re.search(r"\(([^)]+)\)", text or "")
        return match.group(1).strip() if match else ""

    def _map_product_type(self, home_type: str) -> str:
        value = (home_type or "").lower()
        if "townhome" in value:
            return "SFA"
        if "single" in value:
            return "SFD"
        if "condo" in value or "condominium" in value:
            return "CO"
        return home_type

    def _collect_model_hours(self, response: scrapy.http.Response) -> str:
        days = response.xpath(
            "(//ul[contains(@class,'contact-card__schedule')])[1]/li"
        )
        rows = []
        for day in days:
            label = self._clean_text(day.xpath("normalize-space(span[1])").get())
            hours = self._clean_text(day.xpath("normalize-space(span[contains(@class,'contact-card__time')])").get())
            if label and hours:
                rows.append(f"{label} {hours}")
        return " | ".join(rows)

    def _extract_amenities(self, text: str) -> str:
        if not text:
            return ""
        known = ["pool", "cabana", "park", "playground", "pond"]
        found = []
        lowered = text.lower()
        for amenity in known:
            if amenity in lowered:
                found.append(amenity.title())
        return " | ".join(found)

    def _domain_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}/"

    def _normalize_range_text(self, value: str) -> str:
        if not value:
            return ""
        value = value.replace(",", "")
        return re.sub(r"\s*-\s*", " to ", value)

    def _clear_qmi_fields(self, data: dict[str, str]):
        qmi_fields = [
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
        ]
        for field in qmi_fields:
            data[field] = ""

    def _split_numeric_range(self, value: str) -> tuple[str, str]:
        cleaned = (value or "").replace(",", "").strip()
        if not cleaned:
            return "", ""
        parts = [part.strip() for part in re.split(r"\s*-\s*", cleaned) if part.strip()]
        if len(parts) >= 2:
            return parts[0], parts[1]
        return cleaned, ""

    def _digits_only(self, value: str) -> str:
        return re.sub(r"[^\d.]", "", value or "")

    def _extract_price_range_from_html(self, response: scrapy.http.Response) -> tuple[str, str]:
        # Priority:
        # 1) product-header-price-new (used on QMI & some pages)
        # 2) product-header-price (container for "Starting at: $X" on plan pages)
        price_text = self._clean_text(
            response.xpath(
                "normalize-space((//span[contains(@class,'product-header-price-new')])[1])"
            ).get()
        )
        if price_text:
            return self._split_price_range(price_text)

        container_text = self._clean_text(
            " ".join(
                response.xpath(
                    "(//*[contains(@class,'product-header-price')])[1]//text()"
                ).getall()
            )
        )
        if not container_text:
            return "", ""

        return self._split_price_range(container_text)

    def _split_price_range(self, value: str) -> tuple[str, str]:
        matches = re.findall(r"\$\s*[\d,]+(?:\.\d+)?", value or "")
        if len(matches) >= 2:
            return self._digits_only(matches[0]), self._digits_only(matches[1])
        if len(matches) == 1:
            return self._digits_only(matches[0]), ""

        numeric_value = self._clean_text(value)
        parts = [part.strip() for part in re.split(r"\s*-\s*", numeric_value) if part.strip()]
        if len(parts) >= 2:
            return self._digits_only(parts[0]), self._digits_only(parts[1])
        digits = self._digits_only(numeric_value)
        return digits, ""

    def _get_first(self, values: list[Any] | Any, fallback: Any = "") -> str:
        if not isinstance(values, list):
            values = [values]
        for value in values:
            text = self._to_text(value)
            if text:
                return text
        return self._to_text(fallback)

    def _to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            # Compact nested dict data so it can be stored in CSV-friendly output.
            chunks = []
            for key, val in value.items():
                text = self._to_text(val)
                if text:
                    chunks.append(f"{key}: {text}")
            return "; ".join(chunks)
        if isinstance(value, list):
            return " | ".join(filter(None, [self._to_text(v) for v in value]))
        return str(value).strip()
