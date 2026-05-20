"""
Fischer Homes detail spider.

Reads listing CSV (house_url, optional card_id for API gallery).
Uses JSON-LD on move-in detail pages plus community microsite pages when linked.
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path
from urllib.parse import urldefrag, urlparse

import scrapy

BASE = "https://www.fischerhomes.com"
BASE_WEBSITE = "https://www.fischerhomes.com/"

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


class FischerHomesDetailsSpider(scrapy.Spider):
    name = "fischerhomes_details"
    allowed_domains = ["fischerhomes.com", "www.fischerhomes.com"]

    custom_settings = {
        "FEEDS": {
            "fischerhomes_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            }
        }
    }

    def __init__(self, input_csv="fischerhomes_listings.csv", urls=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv
        self.urls_arg = urls

    def start_requests(self):
        if self.urls_arg:
            for raw_url in str(self.urls_arg).split(","):
                house_url = self._normalize_url(raw_url.strip())
                if not house_url:
                    continue
                yield scrapy.Request(
                    url=self._json_detail_url(house_url),
                    callback=self.parse_house_json,
                    meta={
                        "state_slug": "",
                        "community_url": "",
                        "house_url": house_url,
                        "card_id": "",
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
                house_url = self._normalize_url(row.get("house_url") or row.get("url"))
                if not house_url:
                    continue
                community_url = self._normalize_url(row.get("community_url") or "")
                state_slug = str(row.get("state") or "").strip().lower()
                card_id = str(row.get("card_id") or "").strip()
                yield scrapy.Request(
                    url=self._json_detail_url(house_url),
                    callback=self.parse_house_json,
                    meta={
                        "state_slug": state_slug,
                        "community_url": community_url,
                        "house_url": house_url,
                        "card_id": card_id,
                    },
                )

    def parse_house_json(self, response):
        house_url = response.meta.get("house_url") or self._normalize_url(response.url)
        try:
            payload = response.json()
        except Exception:
            yield scrapy.Request(
                url=house_url,
                callback=self.parse_house,
                meta=response.meta,
                dont_filter=True,
            )
            return

        if not isinstance(payload, dict) or not isinstance(payload.get("home"), dict):
            yield scrapy.Request(
                url=house_url,
                callback=self.parse_house,
                meta=response.meta,
                dont_filter=True,
            )
            return

        home = payload.get("home") or {}
        region = payload.get("region") or {}
        community = home.get("community") if isinstance(home.get("community"), dict) else {}
        plan = home.get("plan") if isinstance(home.get("plan"), dict) else {}

        item = {field: "" for field in DETAIL_FIELDS}
        item["url"] = house_url
        item["Website"] = BASE_WEBSITE
        item["Builder"] = "Fischer Homes"

        item["Community Name"] = self._clean_join([community.get("name")])
        item["latitude"] = self._clean_join([home.get("latitude") or community.get("latitude")])
        item["longitude"] = self._clean_join([home.get("longitude") or community.get("longitude")])

        street = self._clean_join([home.get("address")])
        city = self._clean_join([home.get("city")])
        state = self._clean_join([home.get("state")])
        postal = self._clean_join([home.get("zip")])
        item["Street Address"] = self._full_address_line(street, city, state, postal)
        item["City"] = city
        item["State"] = state.upper() if state else ""
        item["Zip Code"] = postal
        item["County"] = self._clean_join([home.get("county") or community.get("county")])
        item["Phone number"] = self._normalize_phone(
            community.get("sales_office_phone") or region.get("sales_phone") or ""
        )

        item["QMI Availability Date"] = self._clean_join([home.get("occupancy")])
        # Per requirement: site status labels are not reliable; drive status from occupancy only.
        item["status"] = self._normalize_status_from_occupancy(item.get("QMI Availability Date"))
        item["Minimum Base Price (Current)"] = self._digits_only(
            home.get("sale_price") or home.get("price")
        )
        item["QMI Model Price"] = item["Minimum Base Price (Current)"]
        item["Previous Price"] = self._digits_only(home.get("old_price"))

        item["QMI Model Name"] = self._clean_join([plan.get("name")])
        item["Model Name"] = item["QMI Model Name"]
        item["Plan Description"] = self._cap_text(
            self._clean_join(
                [
                    home.get("preferred_marketing_description")
                    or home.get("description")
                    or plan.get("preferred_marketing_description")
                    or plan.get("description")
                ]
            ),
            2500,
        )
        item["Overall Description of the Community"] = self._cap_text(
            self._clean_join(
                [
                    self._strip_html(community.get("description")),
                    self._strip_html(community.get("marketing_description")),
                    self._strip_html(community.get("headline")),
                ]
            ),
            3500,
        )

        item["# BR"] = self._digits_only(home.get("beds"))
        item["# BA"] = self._digits_only(home.get("baths"))
        item["# 1/2 BA"] = self._digits_only(home.get("baths_partial"))
        item["# of Floors"] = self._digits_only(home.get("floors"))
        item["# Garages"] = self._digits_only(home.get("garage"))
        item["# of Garages"] = item["# Garages"]
        item["Garages (Y/N)"] = "Y" if self._to_int(item["# Garages"]) > 0 else "N"
        item["Parking Type"] = "Garage" if item["Garages (Y/N)"] == "Y" else ""
        item["Minimum SQFT"] = self._digits_only(home.get("sqft"))
        item["QMI Model SQFT"] = item["Minimum SQFT"]
        item["QMI # of Garages"] = item["# Garages"]
        item["QMI # of BR"] = item["# BR"]
        item["QMI # of BA"] = item["# BA"]
        item["QMI # of 1/2 BA"] = item["# 1/2 BA"]
        item["QMI Lot SQFT"] = ""

        item["Model hours"] = self._region_hours(region)

        amenity_text = self._clean_join([self._strip_html(community.get("amenities_description"))])
        item["Amenities Available"] = "Y" if amenity_text else "N"
        item["Amenity Type"] = amenity_text
        item["Attributes/Features"] = self._cap_text(
            self._clean_join(
                [
                    community.get("notable_features"),
                    home.get("propertyType"),
                    plan.get("notable_features"),
                ]
            ),
            1200,
        )
        item["Product Type (SFD/SFA/CO)"] = self._map_product_label(self._clean_join([home.get("propertyType")]))
        item["Model/Product Types Available"] = "Y" if item["Product Type (SFD/SFA/CO)"] else "N"

        item["QMI Interior/Exterior Attributes"] = ""
        item["Community Details"] = ""

        images = self._images_from_json_payload(response, home)
        item["images"] = images

        if not item.get("status"):
            item["status"] = self._normalize_status_from_occupancy(item.get("QMI Availability Date"))
        self._blank_zero_numeric_fields(item)
        yield item

    def parse_house(self, response):
        house_url = response.meta.get("house_url") or self._normalize_url(response.url)
        community_url = response.meta.get("community_url") or ""
        card_id = str(response.meta.get("card_id") or "").strip()
        state_slug = str(response.meta.get("state_slug") or "").strip().lower()

        item = {field: "" for field in DETAIL_FIELDS}
        item["url"] = house_url
        item["Website"] = BASE_WEBSITE
        item["Builder"] = "Fischer Homes"

        ld_objs = self._json_ld_objects(response)
        house_ld = self._pick_ld(ld_objs, ("house", "product", "residence"))
        plan_ld = house_ld.get("accommodationFloorPlan") if isinstance(house_ld, dict) else {}

        addr = house_ld.get("address") if isinstance(house_ld, dict) else {}
        if not isinstance(addr, dict):
            addr = {}

        item["latitude"] = self._first_scalar(house_ld, ("latitude",))
        item["longitude"] = self._first_scalar(house_ld, ("longitude",))
        street = self._clean_join([addr.get("streetAddress")])
        locality = self._clean_join([addr.get("addressLocality")])
        region = self._clean_join([addr.get("addressRegion")])
        postal = self._clean_join([addr.get("postalCode")])
        item["Street Address"] = self._full_address_line(street, locality, region, postal)
        item["City"] = locality or ""
        item["State"] = region.upper() if region else ""
        item["Zip Code"] = postal or ""

        item["Model Name"] = plan_ld.get("name") if isinstance(plan_ld, dict) else ""
        item["Plan Description"] = self._cap_text(self._clean_join([house_ld.get("description")]), 2500)

        item["# BR"] = self._digits_only(house_ld.get("numberOfBedrooms"))
        item["# BA"] = self._digits_only(house_ld.get("numberOfFullBathrooms"))
        item["# 1/2 BA"] = self._digits_only(house_ld.get("numberOfPartialBathrooms"))

        floor_size = house_ld.get("floorSize") if isinstance(house_ld, dict) else {}
        sqft_val = ""
        if isinstance(floor_size, dict):
            sqft_val = self._digits_only(floor_size.get("value"))
        item["Minimum SQFT"] = sqft_val
        item["Maximum SQFT"] = ""

        garages = ""
        props = house_ld.get("additionalProperty") if isinstance(house_ld, dict) else []
        if isinstance(props, list):
            for prop in props:
                if not isinstance(prop, dict):
                    continue
                name = str(prop.get("name") or "").strip().lower()
                if name == "garage":
                    garages = self._digits_only(prop.get("value"))
                if name == "floors":
                    item["# of Floors"] = self._digits_only(prop.get("value"))

        item["# Garages"] = garages
        item["# of Garages"] = garages
        item["Garages (Y/N)"] = "Y" if self._to_int(garages) > 0 else "N"
        item["Parking Type"] = "Garage" if item["Garages (Y/N)"] == "Y" else ""

        phone = self._clean_join([house_ld.get("telephone")])
        item["Phone number"] = self._normalize_phone(phone)

        sale_price = self._extract_sale_price_html(response)
        if not sale_price:
            sale_price = self._first_money_from_text(response.text or "")
        item["Minimum Base Price (Current)"] = sale_price
        item["QMI Model Price"] = sale_price

        avail = self._extract_availability_html(response)
        item["QMI Availability Date"] = avail
        item["status"] = self._normalize_status_from_occupancy(item.get("QMI Availability Date"))

        item["QMI Model Name"] = item["Model Name"]
        item["QMI # of Garages"] = item["# Garages"]
        item["QMI # of BR"] = item["# BR"]
        item["QMI # of BA"] = item["# BA"]
        item["QMI # of 1/2 BA"] = item["# 1/2 BA"]
        item["QMI Model SQFT"] = item["Minimum SQFT"]
        item["QMI Interior/Exterior Attributes"] = ""

        item["County"] = self._extract_county(response.text or "")

        if not community_url:
            community_url = self._community_path_from_house_html(response)
        community_url = self._normalize_url(community_url)

        img_urls = self._collect_page_images(response)
        item["images"] = img_urls

        base_meta = {
            "item": item,
            "community_url": community_url,
            "house_url": house_url,
            "card_id": card_id,
            "state_slug": state_slug,
        }

        if card_id.isdigit():
            yield scrapy.Request(
                url=f"{BASE}/api/region-revamped/home/{card_id}",
                callback=self.parse_home_api,
                meta=base_meta,
                dont_filter=True,
            )
            return

        if community_url:
            yield scrapy.Request(
                url=community_url,
                callback=self.parse_community,
                meta={"item": dict(base_meta["item"]), "community_url": community_url, "house_url": house_url},
                dont_filter=True,
            )
            return

        self._blank_zero_numeric_fields(item)
        yield item

    def parse_home_api(self, response):
        meta = response.meta
        item = dict(meta["item"])
        card_id = meta.get("card_id") or ""
        community_url = meta.get("community_url") or ""
        house_url = meta.get("house_url") or ""

        if response.status == 200:
            try:
                data = response.json()
            except Exception:
                data = {}
            if isinstance(data, dict):
                imgs = data.get("images") or []
                urls = []
                if isinstance(imgs, list):
                    for im in imgs:
                        if isinstance(im, dict):
                            blob = im.get("image")
                            url_part = ""
                            if isinstance(blob, dict):
                                # Per requirement: keep only `image.large`.
                                url_part = blob.get("large") or ""
                            if url_part:
                                urls.append(self._normalize_media_url(response, url_part))
                        elif isinstance(im, str):
                            urls.append(self._normalize_media_url(response, im))
                if urls:
                    merged = item.get("images") or []
                    if isinstance(merged, str):
                        existing = [x.strip() for x in merged.split(" | ") if x.strip()]
                    elif isinstance(merged, list):
                        existing = [str(x).strip() for x in merged if str(x).strip()]
                    else:
                        existing = []
                    item["images"] = self._dedupe(existing + urls)

        next_meta = {"item": item, "community_url": community_url, "house_url": house_url}

        if community_url:
            yield scrapy.Request(
                url=community_url,
                callback=self.parse_community,
                meta=next_meta,
                dont_filter=True,
            )
            return

        yield item

    def parse_community(self, response):
        item = dict(response.meta["item"])
        community_url = response.meta.get("community_url") or self._normalize_url(response.url)

        ld_objs = self._json_ld_objects(response)
        biz = self._pick_ld(ld_objs, ("localbusiness", "place"))

        name = self._clean_join([biz.get("name")])
        if name:
            item["Community Name"] = name

        geo = biz.get("geo") if isinstance(biz, dict) else {}
        if isinstance(geo, dict):
            if not item.get("latitude"):
                item["latitude"] = self._clean_join([geo.get("latitude")])
            if not item.get("longitude"):
                item["longitude"] = self._clean_join([geo.get("longitude")])

        addr = biz.get("address") if isinstance(biz, dict) else {}
        if isinstance(addr, dict):
            street = self._clean_join([addr.get("streetAddress")])
            locality = self._clean_join([addr.get("addressLocality")])
            region = self._clean_join([addr.get("addressRegion")])
            postal = self._clean_join([addr.get("postalCode")])
            item["Street Address"] = item.get("Street Address") or self._full_address_line(
                street, locality, region, postal
            )
            item["City"] = item.get("City") or locality
            item["State"] = item.get("State") or (region.upper() if region else "")
            item["Zip Code"] = item.get("Zip Code") or postal

        desc = self._clean_join([self._strip_html(biz.get("description"))])
        if desc:
            item["Overall Description of the Community"] = self._cap_text(desc, 3500)

        oh = biz.get("openingHours") if isinstance(biz, dict) else ""
        if oh:
            item["Model hours"] = self._clean_join([oh])

        cp = biz.get("contactPoint") if isinstance(biz, dict) else {}
        if isinstance(cp, dict) and cp.get("telephone"):
            item["Phone number"] = item.get("Phone number") or self._normalize_phone(cp.get("telephone"))

        if not item.get("County"):
            item["County"] = self._extract_county(response.text or "")
        self._fill_product_fallback(item, response.text or "")

        comm_id = self._community_numeric_id(community_url)
        if comm_id:
            yield scrapy.Request(
                url=f"{BASE}/api/region-revamped/community/{comm_id}",
                callback=self.parse_community_api,
                meta={"item": item, "community_url": community_url},
                dont_filter=True,
            )
            return

        self._fill_product_fallback(item, response.text or "")
        self._blank_zero_numeric_fields(item)
        yield item

    def parse_community_api(self, response):
        item = dict(response.meta["item"])
        if response.status != 200:
            yield item
            return
        try:
            data = response.json()
        except Exception:
            yield item
            return
        if not isinstance(data, dict):
            yield item
            return

        feats = data.get("notable_features") or ""
        if feats:
            item["Attributes/Features"] = self._cap_text(str(feats), 1200)

        label = self._clean_join([data.get("property_types_label")])
        item["Product Type (SFD/SFA/CO)"] = item.get("Product Type (SFD/SFA/CO)") or self._map_product_label(label)
        item["Model/Product Types Available"] = "Y" if label else "N"

        # Do not use community availability labels for status; they can be inaccurate.
        # Keep status derived only from QMI occupancy/timeline.

        self._blank_zero_numeric_fields(item)
        yield item

    @staticmethod
    def _community_numeric_id(url: str) -> str:
        path = urlparse(str(url or "")).path.strip("/")
        parts = [p for p in path.split("/") if p]
        try:
            idx = parts.index("communities")
            if idx + 1 < len(parts) and parts[idx + 1].isdigit():
                return parts[idx + 1]
        except ValueError:
            pass
        return ""

    @staticmethod
    def _normalize_media_url(response, raw: str) -> str:
        s = str(raw or "").strip().split("?")[0].strip()
        if not s:
            return ""
        return response.urljoin(s)

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
            tl = [t.lower() for t in types]
            for sub in type_substrings:
                if any(sub in t for t in tl):
                    return obj
        return {}

    @staticmethod
    def _ld_types(obj):
        t = obj.get("@type")
        if isinstance(t, list):
            return [str(x) for x in t]
        return [str(t or "")]

    @staticmethod
    def _first_scalar(obj, keys):
        if not isinstance(obj, dict):
            return ""
        for key in keys:
            val = obj.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""

    @staticmethod
    def _digits_only(value):
        if value is None:
            return ""
        s = str(value).strip()
        m = re.search(r"\d+", s)
        return m.group(0) if m else ""

    @staticmethod
    def _to_int(value):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return 0

    def _community_path_from_house_html(self, response):
        hrefs = response.xpath('//a[contains(@href,"/find-new-homes/")][contains(@href,"/communities/")]/@href').getall()
        for href in hrefs:
            path = str(href or "").strip()
            if "/communities/" in path.lower():
                return path.split("?")[0].strip()
        text = response.text or ""
        m = re.search(
            r'href="(/find-new-homes/[^"?]+/communities/[^"?]+)',
            text,
            re.I,
        )
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_sale_price_html(response):
        txt = response.xpath('//div[contains(@class,"sale-price")]//text()').getall()
        blob = " ".join(str(t or "").strip() for t in txt if str(t or "").strip())
        m = re.search(r"\$\s*([\d,]+)", blob)
        return m.group(1).replace(",", "") if m else ""

    @staticmethod
    def _extract_availability_html(response):
        span = response.xpath(
            '//div[contains(@class,"availability")]//span[contains(@class,"red")]//text()'
        ).get()
        if span:
            return str(span).strip()
        blob = response.xpath('//div[contains(@class,"availability")]//text()').getall()
        text = " ".join(str(t or "").strip() for t in blob if str(t or "").strip())
        m = re.search(r"Availability:\s*([^\n]+)", text, re.I)
        if not m:
            return ""
        return m.group(1).strip().split()[0] if m.group(1).strip() else ""

    @staticmethod
    def _availability_to_status(avail: str):
        low = str(avail or "").lower()
        if not low:
            return ""
        if "sold" in low:
            return "Sold Out"
        if "immediate" in low or "move" in low or "ready" in low:
            return "Move-In Ready"
        if "soon" in low:
            return "Coming Soon"
        return avail

    @staticmethod
    def _normalize_status_from_occupancy(occupancy: str) -> str:
        """Status is derived only from occupancy."""
        occ = str(occupancy or "").strip()
        low = occ.lower()
        if not low:
            return ""
        if low == "tbd":
            return "TBD"
        if (
            "move-in ready" in low
            or "move in ready" in low
            or "immediate" in low
            or "ready now" in low
            or re.search(r"\b\d+\s*days?\b", low)
        ):
            return "available"
        return occ

    @staticmethod
    def _first_money_from_text(text: str) -> str:
        m = re.search(r"\$\s*([\d,]+)", text or "")
        return m.group(1).replace(",", "") if m else ""

    @staticmethod
    def _extract_county(text: str) -> str:
        m = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\s+County)\b", text or "")
        return m.group(1).strip() if m else ""

    @staticmethod
    def _map_product_label(label: str) -> str:
        low = str(label or "").lower()
        if "town" in low:
            return "SFA"
        if "condo" in low:
            return "CO"
        if "single" in low:
            return "SFD"
        return ""

    @staticmethod
    def _fill_product_fallback(item, html: str):
        low = str(html or "").lower()
        if item.get("Product Type (SFD/SFA/CO)"):
            return
        if "townhome" in low:
            item["Product Type (SFD/SFA/CO)"] = "SFA"
        elif "condo" in low:
            item["Product Type (SFD/SFA/CO)"] = "CO"
        elif "single family" in low:
            item["Product Type (SFD/SFA/CO)"] = "SFD"

    @staticmethod
    def _collect_page_images(response):
        urls = []
        for src in response.xpath("//img/@src").getall():
            s = str(src or "").strip()
            if "/images/site/" in s or "fb-share" in s.lower():
                continue
            full = response.urljoin(s).split("?")[0].strip()
            if full.startswith("http"):
                urls.append(full)
        return FischerHomesDetailsSpider._dedupe(urls)

    @staticmethod
    def _dedupe(values):
        seen = set()
        out = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    @staticmethod
    def _normalize_phone(value):
        raw = str(value or "").replace("tel:", "").strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return raw

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
    def _cap_text(value, max_len: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 3].rstrip() + "..."

    @staticmethod
    def _clean_join(values):
        if isinstance(values, str):
            values = [values]
        text = " ".join(str(v or "").strip() for v in values if str(v or "").strip())
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _strip_html(value):
        text = str(value or "")
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

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
    def _json_detail_url(house_url: str) -> str:
        base = str(house_url or "").split("?")[0].strip()
        if not base:
            return ""
        return f"{base}?json=true"

    @staticmethod
    def _region_hours(region: dict) -> str:
        if not isinstance(region, dict):
            return ""
        val = region.get("detailed_hours")
        if not val:
            return ""
        if isinstance(val, str):
            return re.sub(r"\s+", " ", val).strip()
        if isinstance(val, list):
            bits = []
            for row in val:
                if isinstance(row, dict):
                    day = str(row.get("day") or row.get("label") or "").strip()
                    hrs = str(row.get("hours") or row.get("value") or "").strip()
                    if day and hrs:
                        bits.append(f"{day}: {hrs}")
                    elif hrs:
                        bits.append(hrs)
                elif isinstance(row, str):
                    bits.append(row.strip())
            return " | ".join([b for b in bits if b])
        return ""

    def _images_from_json_payload(self, response, home: dict) -> list[str]:
        urls: list[str] = []
        if not isinstance(home, dict):
            return urls
        assets = home.get("assets") or []
        if isinstance(assets, list):
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                image = asset.get("image")
                if isinstance(image, dict):
                    # Per requirement: keep only `image.large`.
                    val = image.get("large") or ""
                    if val:
                        urls.append(self._normalize_media_url(response, val))
        return self._dedupe([u for u in urls if u])

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
