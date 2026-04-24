import csv
import json
import re
from pathlib import Path
from typing import Any

import scrapy


class CenturycommunitiesDetailsSpider(scrapy.Spider):
    name = "centurycommunities_details"
    allowed_domains = ["centurycommunities.com"]

    csv_columns = [
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

    def __init__(self, csv_file: str = "centurycommunities_lots_plans_urls.csv", url: str = "", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.csv_file = csv_file
        self.single_url = (url or "").strip()
        self.seen_urls = set()

    def start_requests(self):
        if self.single_url:
            yield scrapy.Request(url=self.single_url, callback=self.parse)
            return

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
        item = self._new_item(response.url)

        json_text = self._clean_text(
            "".join(response.xpath("//script[@type='application/ld+json'][contains(text(),'streetAddress')]/text()").getall())
        )
        if not json_text:
            json_text = self._clean_text("".join(response.xpath("//script[@type='application/ld+json']/text()").getall()))
        json_content = self._normalize_json_root(self._parse_json(json_text))

        about = json_content.get("about", {}) if isinstance(json_content, dict) else {}
        address = about.get("address", {}) if isinstance(about, dict) else {}
        geo = about.get("geo", {}) if isinstance(about, dict) else {}
        offers = json_content.get("offers", {}) if isinstance(json_content, dict) else {}
        floor_size = about.get("floorSize", {}) if isinstance(about, dict) else {}

        community_details = self._first_non_empty(
            self._join(response.xpath("//h2[contains(text(),'Community Information')]/parent::div/parent::div/following-sibling::div//text()").getall()),
            self._join(response.xpath("//div[contains(@class,'description')]//p/text()").getall()),
            self._clean_text(response.xpath("//meta[@name='description']/@content").get("")),
            self._to_text(json_content.get("description")) if isinstance(json_content, dict) else "",
        )
        breadcrumb_name = self._clean_text(
            response.xpath("//ol[contains(@class,'breadcrumb')]//li[last()]/a/text()").get("")
        ) or self._clean_text(response.xpath("//ol[contains(@class,'breadcrumb')]//li[last()]/span/text()").get(""))
        if "|" in breadcrumb_name:
            breadcrumb_name = ""

        community_name = self._clean_text(
            response.xpath("//span[contains(@class,'plan-name')]/span/text()").get("")
        )

        product_type = self._join(response.xpath("//div[@class='listing_details']/p/text()").getall())
        if not product_type:
            product_type = self._join(response.xpath("//div[@class='listing_details']/h2/text()").getall())

        garage_count = self._get(response.xpath("//li[contains(.,'bay')]/p/span/text()").getall())
        amenities_text = self._join(response.xpath("//div[@class='description']/p/text()").getall())
        plan_features = self._first_non_empty(
            self._join(response.xpath("//div[@class='paragraph_contain']//li/span/text()").getall(), sep=" | "),
            self._join(response.xpath("//div[contains(@class,'paragraph_contain')]//li//text()").getall(), sep=" | "),
            self._join(response.xpath("//ul[contains(@class,'features')]//li//text()").getall(), sep=" | "),
        )

        images = [
            response.urljoin(src.strip())
            for src in response.xpath("//div[@id='thumbnail-list']/button/img/@src").getall()
            if src.strip()
        ]
        if not images:
            images = [
                response.urljoin(src.strip())
                for src in response.xpath("//meta[@property='og:image']/@content").getall()
                if src.strip()
            ]

        metrics_text = self._join(
            response.xpath("//div[contains(@class,'paragraph_contain')]//text() | //*[contains(text(),'sqft') or contains(text(),'sq ft')]/text()").getall(),
            sep=" | ",
        )

        sqft_raw = ""
        if isinstance(floor_size, dict):
            sqft_raw = self._to_text(floor_size.get("value"))
        sqft_raw = self._first_non_empty(
            sqft_raw,
            self._get(response.xpath("//li[contains(.,'sq ft')]/p/span/text()").getall()),
            self._clean_text(
                response.xpath(
                    "normalize-space((//ul[contains(@class,'specs_contain')]//li[.//img[contains(@alt,'Square Footage')]]/p)[1])"
                ).get("")
            ),
            self._extract_sqft(metrics_text),
        )
        sqft_value = self._digits_only(sqft_raw)

        price_raw = self._first_non_empty(
            self._to_text(offers.get("price")) if isinstance(offers, dict) else "",
            self._get(response.xpath("//p[@class='price']/text()").getall()),
            self._clean_text(response.xpath("normalize-space(//*[contains(text(),'From:')])").get("")),
            self._clean_text(response.xpath("normalize-space((//*[contains(@class,'product-header-price') or contains(@class,'price')][1]))").get("")),
        )
        price_value = self._digits_only(price_raw)

        br_value = self._first_non_empty(
            self._sanitize_bedrooms(self._get(response.xpath("//li[contains(.,'beds')]/p/span/text()").getall())),
            self._sanitize_bedrooms(
                self._clean_text(
                    response.xpath(
                        "normalize-space((//ul[contains(@class,'specs_contain')]//li[.//img[contains(@alt,'Bedrooms')]]/p)[1])"
                    ).get("")
                )
            ),
            self._sanitize_bedrooms(self._clean_text(response.xpath("normalize-space((//span[contains(text(),' bed')])[1])").get(""))),
        )

        ba_raw = self._first_non_empty(
            self._get(response.xpath("//li[contains(.,'baths')]/p/span/text()").getall()),
            self._clean_text(
                response.xpath(
                    "normalize-space((//ul[contains(@class,'specs_contain')]//li[.//img[contains(@alt,'Bathrooms')]]/p)[1])"
                ).get("")
            ),
        )
        ba_value, half_ba_value = self._parse_bath_values(ba_raw)

        floors_value = self._first_non_empty(
            self._get(response.xpath("//small[contains(text(),'floor')]/preceding-sibling::span/text()").getall()),
            self._get(response.xpath("//li[contains(.,'stor')]/p/span/text()").getall()),
        )

        parking_type = "garage" if garage_count else self._get(
            response.xpath("//li[contains(.,'parking') or contains(.,'Parking')]/p/span/text()").getall()
        )

        item.update(
            {
                "Community Details": community_details,
                "Community Name": community_name,
                "latitude": self._to_text(geo.get("latitude")),
                "longitude": self._to_text(geo.get("longitude")),
                "Street Address": self._to_text(address.get("streetAddress")),
                "City": self._to_text(address.get("addressLocality")),
                "State": self._to_text(address.get("addressRegion")),
                "Zip Code": self._to_text(address.get("postalCode")),
                "County": self._join(response.xpath("//span[contains(@class,'county')]/text()").getall()),
                "Model hours": self._join(response.xpath("//p[contains(text(),'Hours')]/following-sibling::p//text()").getall()),
                "Phone number": self._first_non_empty(
                    self._join(response.xpath("//a[@class='cells phone']//span/text()").getall()),
                    self._clean_text(response.xpath("//a[starts-with(@href,'tel:')][1]/text()").get("")),
                    self._clean_text(response.xpath("substring-after((//a[starts-with(@href,'tel:')])[1]/@href, 'tel:')").get("")),
                ),
                "Website": "https://www.centurycommunities.com/",
                "status": "",
                "Builder": "Century Communities",
                "Product Type (SFD/SFA/CO)": product_type,
                "Model/Product Types Available": self._yn(product_type),
                "Avg Lot Size": self._join(response.xpath("//li[contains(.,'sq ft lot')]/p/span/text()").getall()),
                "Avg Lot - Width/Depth": self._join(response.xpath("//li[contains(.,'lot width') or contains(.,'lot depth')]/p/span/text()").getall()),
                "Garages (Y/N)": self._yn(garage_count),
                "# of Garages": garage_count,
                "Adult Community (Y/N)": self._yn(response.xpath("//span[contains(text(),'55+') or contains(text(),'Adult')]").getall()),
                "Amenities Available": self._yn(amenities_text),
                "Amenity Type": self._join(response.xpath("//div[@id='amenities-content']/div//text()").getall()),
                "Attributes/Features": plan_features,
                "Overall Description of the Community": community_details,
                "Foundation Type": self._join(response.xpath("//li[contains(.,'Foundation')]/p/span/text()").getall()),
                "Exterior Specifications Available": self._yn(response.xpath("//div[contains(@class,'exterior-spec')]").getall()),
                "Interior Specifications Available": self._yn(response.xpath("//div[contains(@class,'interior-spec')]").getall()),
                "HOA Fee": self._join(response.xpath("//li[contains(.,'HOA')]/p/span/text()").getall()),
                "HOA Services": self._join(response.xpath("//div[contains(@class,'hoa-services')]//text()").getall()),
                "Other Fees (i.e. CDD)": self._join(response.xpath("//li[contains(.,'CDD') or contains(.,'fee')]/p/span/text()").getall()),
                "City/Town/Property Tax %": self._join(response.xpath("//li[contains(.,'tax') or contains(.,'Tax')]/p/span/text()").getall()),
                "Sales Start Date": self._join(response.xpath("//li[contains(.,'Sales Start')]/p/span/text()").getall()),
                "Sold Out Date": self._join(response.xpath("//li[contains(.,'Sold Out')]/p/span/text()").getall()),
                "Total Lots Sold": self._join(response.xpath("//li[contains(.,'Lots Sold')]/p/span/text()").getall()),
                "Incentives": self._join(response.xpath("//div[contains(@class,'incentive')]//p/text()").getall(), sep=" | "),
                "QMI Incentives": "",
                "QMI Model Name": "",
                "QMI # of Garages": "",
                "QMI # of BR": "",
                "QMI # of BA": "",
                "QMI # of 1/2 BA": "",
                "QMI Model SQFT": "",
                "QMI Model Price": "",
                "QMI Availability Date": "",
                "QMI Lot SQFT": "",
                "QMI Interior/Exterior Attributes": "",
                "Model Details": self._join(response.xpath("//div[@class='listing_details']//text()").getall()),
                "Model Name": self._clean_text(response.xpath("//span[contains(@class,'plan-name')]/text()").get("")),
                "Plan Description": self._first_non_empty(
                    self._join(response.xpath("//p[contains(text(),'Upgrades include')]/following-sibling::ul/li//text()").getall(), sep=" | "),
                    self._join(response.xpath("//div[@class='paragraph_contain']//text()").getall(), sep=" | "),
                ),
                "# BR": br_value,
                "# BA": ba_value,
                "# 1/2 BA": half_ba_value,
                "# of Floors": floors_value,
                "Parking Type": parking_type,
                "Plan Garage Entry (Front load)": self._yn(response.xpath("//li[contains(.,'Front Load') or contains(.,'front load')]").getall()),
                "# Garages": garage_count,
                "Minimum SQFT": sqft_value,
                "Maximum SQFT": sqft_value,
                "Minimum Base Price (Current)": price_value,
                "Maximum Base Price/All In Price": price_value,
                "Previous Price": self._digits_only(self._get(response.xpath("//p[@class='wasPrice']/s/text()").getall())),
                "Last Updated": "",
                "Plan Features": plan_features,
                "Interior Specifications Descriptions": self._join(response.xpath("//div[contains(@class,'interior')]//li/text()").getall(), sep=" | "),
                "First Floor Master (Y/N)": self._yn(response.xpath("//li[contains(.,'First Floor Master') or contains(.,'Main Floor Master')]").getall()),
                "images": " | ".join(images),
            }
        )

        if item["Minimum Base Price (Current)"] == item["Maximum Base Price/All In Price"]:
            item["Maximum Base Price/All In Price"] = ""
        if item["Minimum SQFT"] == item["Maximum SQFT"]:
            item["Maximum SQFT"] = ""

        yield item

    def _new_item(self, url: str) -> dict[str, str]:
        return {field: (url if field == "url" else "") for field in self.csv_columns}

    def _resolve_csv_path(self, csv_file: str) -> Path | None:
        provided = Path(csv_file)
        if provided.is_absolute() and provided.exists():
            return provided
        current = Path.cwd()
        spider_file = Path(__file__).resolve()
        candidates = [current / csv_file, current.parent / csv_file, spider_file.parents[2] / csv_file, spider_file.parents[1] / csv_file]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _parse_json(self, text: str) -> Any:
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def _normalize_json_root(self, obj: Any) -> dict[str, Any]:
        if isinstance(obj, dict):
            if isinstance(obj.get("@graph"), list):
                for node in obj["@graph"]:
                    if isinstance(node, dict) and ("about" in node or "offers" in node):
                        return node
            return obj
        if isinstance(obj, list):
            for node in obj:
                if isinstance(node, dict) and ("about" in node or "offers" in node):
                    return node
        return {}

    def _to_text(self, value: Any) -> str:
        return str(value).strip() if value is not None else ""

    def _clean_text(self, value: Any) -> str:
        text = self._to_text(value)
        return " ".join(text.replace("\xa0", " ").split())

    def _get(self, values: list[str], default: str = "") -> str:
        for value in values:
            cleaned = self._clean_text(value)
            if cleaned:
                return cleaned
        return default

    def _join(self, values: list[str], sep: str = " ", default: str = "") -> str:
        cleaned = [self._clean_text(value) for value in values if self._clean_text(value)]
        return sep.join(cleaned) if cleaned else default

    def _yn(self, value: Any) -> str:
        if isinstance(value, list):
            return "YES" if any(self._clean_text(v) for v in value) else "NO"
        return "YES" if self._clean_text(value) else "NO"

    def _first_non_empty(self, *values: Any) -> str:
        for value in values:
            cleaned = self._clean_text(value)
            if cleaned:
                return cleaned
        return ""

    def _digits_only(self, value: Any) -> str:
        return "".join(ch for ch in self._to_text(value) if ch.isdigit())

    def _extract_sqft(self, text: Any) -> str:
        content = self._to_text(text)
        match = re.search(r"(\d[\d,]*)\s*sq\s*-?\s*ft\b|(\d[\d,]*)\s*sqft\b", content, flags=re.IGNORECASE)
        if not match:
            return ""
        return match.group(1) or match.group(2) or ""

    def _sanitize_bedrooms(self, value: Any) -> str:
        text = self._clean_text(value)
        if not text:
            return ""
        if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text):
            return ""
        match = re.search(r"(\d+)\s*(?:beds?|br)?\b", text, flags=re.IGNORECASE)
        return match.group(1) if match else ""

    def _parse_bath_values(self, value: Any) -> tuple[str, str]:
        text = self._clean_text(value)
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            return "", ""
        num = float(match.group(1))
        whole = str(int(num))
        half = "1" if abs(num - int(num) - 0.5) < 0.001 else ""
        return whole, half

    def _extract_community_name_from_details(self, details: str) -> str:
        text = self._clean_text(details)
        match = re.match(r"^(.*?)\s+Welcome to\b", text, flags=re.IGNORECASE)
        return self._clean_text(match.group(1)) if match else ""

    def _community_from_url(self, url: str) -> str:
        path = url.split("://", 1)[-1].split("/", 1)
        if len(path) < 2:
            return ""
        segments = [segment for segment in path[1].split("/") if segment]
        marker_idx = -1
        for marker in ("lots", "plans"):
            if marker in segments:
                marker_idx = segments.index(marker)
                break
        if marker_idx <= 0:
            return ""
        slug = segments[marker_idx - 1]
        return " ".join(part.capitalize() for part in slug.split("-") if part)
