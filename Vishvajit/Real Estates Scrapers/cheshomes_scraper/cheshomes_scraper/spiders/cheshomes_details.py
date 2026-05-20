import csv
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

import scrapy

BASE_WEBSITE = "https://www.cheshomes.com"

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
    "Minimum Base Price (Current)",
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


class CheshomesDetailsSpider(scrapy.Spider):
    name = "cheshomes_details"
    allowed_domains = ["cheshomes.com", "www.cheshomes.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [404, 410, 500],
        "FEEDS": {
            "cheshomes_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            }
        },
    }

    def __init__(self, input_csv="cheshomes_listings.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv
        self._community_cache: dict[str, dict] = {}
        self._pending_by_community: dict[str, list[dict]] = {}
        self._scheduled_community: set[str] = set()

    async def start(self):
        csv_path = Path(self.input_csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        if not csv_path.exists():
            raise FileNotFoundError(f"Input CSV not found: {csv_path}")

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                house_raw = row.get("house_url") or row.get("url") or ""
                house_url = self._slash_url(self._normalize_url(house_raw))
                if not house_url:
                    continue
                yield scrapy.Request(
                    house_url,
                    callback=self.parse_home,
                    dont_filter=True,
                    cb_kwargs={"house_url": house_url},
                )

    def parse_home(self, response, house_url: str):
        item = {field: ([] if field == "images" else "") for field in DETAIL_FIELDS}
        item["url"] = self._slash_url(self._normalize_url(house_url))
        item["Website"] = BASE_WEBSITE
        item["Builder"] = "Chesapeake Homes"

        resolved = self._slash_url(self._normalize_url(response.url))
        if not self._is_inventory_home_url(resolved):
            self.logger.warning("skip_non_inventory url=%s", resolved)
            return

        self._fill_inventory_page(response, item)

        comm_href = (
            response.xpath(
                '//span[contains(text(), "Community")]/following-sibling::a/@href'
            ).get()
            or ""
        ).strip()
        community_url_final = ""
        if comm_href:
            community_url_final = self._slash_url(
                self._normalize_url(response.urljoin(comm_href))
            )

        if not community_url_final:
            self._normalize_price_fields(item)
            yield item
            return

        cached = self._community_cache.get(community_url_final)
        if cached is not None:
            self._apply_cached_community(item, cached)
            self._normalize_price_fields(item)
            yield item
            return

        self._pending_by_community.setdefault(community_url_final, []).append(item)
        if community_url_final not in self._scheduled_community:
            self._scheduled_community.add(community_url_final)
            yield scrapy.Request(
                community_url_final,
                callback=self.parse_community,
                dont_filter=False,
                cb_kwargs={"community_url": community_url_final},
            )

    def parse_community(self, response, community_url: str):
        community_url = self._slash_url(
            self._normalize_url(community_url or response.url)
        )
        extracted = self._extract_community_fields(response)
        self._community_cache[community_url] = extracted
        pending = self._pending_by_community.pop(community_url, [])
        for row in pending:
            self._apply_cached_community(row, extracted)
            self._normalize_price_fields(row)
            yield row

    # ------------------------------------------------------------------
    # Community — overview text only (no other fields from this page)
    # ------------------------------------------------------------------
    def _apply_cached_community(self, item: dict, extracted: dict) -> None:
        desc = (extracted or {}).get("Overall Description of the Community") or ""
        if not desc:
            return
        if not (item.get("Overall Description of the Community") or "").strip():
            item["Overall Description of the Community"] = desc

    def _extract_community_fields(self, response) -> dict:
        text = self._long_community_descriptions(response)
        if not text:
            return {}
        return {"Overall Description of the Community": text}

    @staticmethod
    def _normalize_price_fields(item: dict) -> None:
        price_fields = (
            "Minimum Base Price (Current)",
            "Previous Price",
            "Maximum Price",
            "QMI Model Price",
            "Incentive $",
            "QMI Incentive $",
        )
        for field in price_fields:
            value = str(item.get(field) or "").strip()
            if not value:
                item[field] = ""
                continue
            item[field] = re.sub(r"[^\d.]", "", value)

    # ------------------------------------------------------------------
    # Inventory (house) page
    # ------------------------------------------------------------------
    def _fill_inventory_page(self, response, item: dict) -> None:
        comm_a = response.xpath(
            '//span[contains(text(), "Community")]/following-sibling::a'
        )
        community_name = (
            (comm_a.xpath("normalize-space(string(.))").get() or "").strip()
        )
        if community_name:
            item["Community Name"] = community_name

        item["Model Name"] = ""
        item["Incentive %"] = ""
        item["Incentive $"] = ""
        item["Current Incentive Type"] = ""

        item["Latitude"], item["Longitude"] = "", ""
        maps_href = response.xpath(
            '//a[contains(@class, "oi-directions-click")]/@href'
        ).get()
        if maps_href:
            item["Latitude"], item["Longitude"] = self._coords_from_maps_url(maps_href)

        street_line = "".join(
            response.xpath(
                '//section[contains(@class, "detail-page-hero")]'
                '//h1[contains(@class, "page")]//text()'
            ).getall()
        ).strip()
        subtitle = "".join(
            response.xpath(
                '//section[contains(@class, "detail-page-hero")]'
                '//p[contains(@class, "page-subtitle")]//text()'
            ).getall()
        ).strip()

        city, state_abbr = self._split_city_state(subtitle)
        zip_code = self._zip_from_hero_address(response)
        if street_line:
            item["Street Address"] = street_line
        item["City"] = city or ""
        item["State"] = state_abbr or ""
        item["Zip Code"] = zip_code

        if city and state_abbr and zip_code:
            item["Street Address"] = (
                f"{street_line}, {city}, {state_abbr} {zip_code}".strip()
            )

        specs = self._stats_from_li(response)

        baths = specs.get("bathrooms")
        full_ba, half_ba = self._split_bathrooms(baths)
        item["# BA"] = full_ba
        item["# 1/2 BA"] = half_ba
        item["# BR"] = specs.get("bedrooms", "")
        item["Minimum SQFT"] = specs.get("sqft", "")
        item["Maximum SQFT"] = ""
        item["# of Floors"] = specs.get("stories", "")
        item["# of Garages"] = specs.get("garages", "")

        banner_texts = response.xpath(
            '//*[contains(@class, "custom-spec-banners")]'
            '//*[contains(@class, "banner-two")]//text()'
        ).getall()
        item["Status"] = self._clean_ws(
            " ".join(t.strip() for t in banner_texts if t and str(t).strip())
        )

        hero_price_txt = (
            (
                response.xpath(
                    '//section[contains(@class, "detail-page-hero")]'
                    '//*[contains(@class, "page-price")]'
                ).xpath("string(.)").get()
                or ""
            ).strip()
        )
        mprice = re.search(r"\$\s*([\d,]+)", hero_price_txt or "")
        norm_lo = self._digits_price(
            f"${mprice.group(1)}" if mprice else ""
        )
        item["Minimum Base Price (Current)"] = norm_lo or ""
        item["Maximum Price"] = ""

        plan_body = (
            response.xpath(
                '//section[contains(@class, "location")]'
                '//*[contains(@class, "article")]'
            ).xpath("string(.)").get()
            or ""
        ).strip()

        gall = response.xpath(
            '//section[@id="photos-videos"]/div/div/a/@href'
        ).getall()
        if not gall:
            gall = response.xpath(
                '//div[contains(@class,"main-img")]/a/@href'
            ).getall()
        item["images"] = [
            response.urljoin(h.strip()) for h in gall if (h or "").strip()
        ]
        plan_body = self._clean_ws(plan_body)
        if plan_body:
            item["Plan Description"] = self._cap_text(plan_body, 4000)

        item["Plan Features"] = ""
        item["Interior Specifications Descriptions"] = ""
        item["Exterior Specifications Available"] = ""
        item["Interior Specifications Available"] = ""

        item["QMI Incentive %"] = ""
        item["QMI Incentive $"] = ""
        item["QMI Current Incentive Type"] = ""
        item["QMI Model Name"] = ""
        item["QMI # of Garages"] = item["# of Garages"]
        item["QMI # of BR"] = item["# BR"]
        item["QMI # of BA"] = item["# BA"]
        item["QMI # of 1/2 BA"] = item["# 1/2 BA"]
        item["QMI Model SQFT"] = item["Minimum SQFT"]
        item["QMI Model # of Floor"] = item["# of Floors"]
        item["QMI Model Price"] = norm_lo or ""
        item["Attributes/Features"] = ""
        item["QMI Interior/Exterior Attributes"] = ""

        sd_parts = response.xpath(
            '//h6[contains(text(), "School District")]/following-sibling::text()'
        ).getall()
        school_district = self._clean_ws(" ".join(sd_parts))
        if school_district:
            item["School District"] = school_district

        item["County"] = ""

        item["Foundation Type"] = ""
        item["Avg Lot Size"] = ""
        item["Previous Price"] = ""
        item["Model hours"] = ""
        item["First Floor Master (Y/N)"] = ""
        item["Adult Community (Y/N)"] = ""
        item["Last Updated"] = ""
        item["HOA Fee"] = ""
        item["HOA Services"] = ""
        item["Other Fees (i.e. CDD)"] = ""
        item["Phone number"] = ""
        item["Website"] = BASE_WEBSITE
        item["Product Type"] = ""

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_url(url: str) -> str:
        clean = (url or "").strip().split("#")[0].split("?")[0].rstrip("/")
        if clean.startswith("//"):
            clean = "https:" + clean
        return clean

    @staticmethod
    def _slash_url(url: str) -> str:
        u = (url or "").strip()
        return u if u.endswith("/") else (u + "/")

    @staticmethod
    def _is_inventory_home_url(url: str) -> bool:
        if not url or "cheshomes.com" not in url.lower():
            return False
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) != 7:
            return False
        if parts[0] != "new-homes":
            return False
        if not parts[4].isdigit() or not parts[6].isdigit():
            return False
        if parts[5].isdigit():
            return False
        return True

    @staticmethod
    def _coords_from_maps_url(href: str) -> tuple[str, str]:
        if not href:
            return "", ""
        h = href.strip()
        if h.startswith("//"):
            h = "https:" + h
        h = unquote(h)
        parsed = urlparse(h)
        qs = parse_qs(parsed.query)
        coords = ""
        if "q" in qs and qs["q"]:
            coords = qs["q"][0].strip()

        coords = coords.replace("+", "").strip()
        m = re.match(
            r"(-?\d+(?:\.\d+)?)\s*,?\s*(-?\d+(?:\.\d+)?)", coords.strip()
        )
        if not m:
            return "", ""
        return m.group(1).strip(), m.group(2).strip()

    @staticmethod
    def _stats_from_li(response) -> dict:
        out = {
            "bedrooms": "",
            "bathrooms": "",
            "sqft": "",
            "stories": "",
            "garages": "",
        }
        for li in response.xpath(
            '//section[contains(@class, "detail-page-hero")]'
            '//ul[contains(@class, "stats")]//li'
        ):
            label_raw = "".join(
                li.xpath('.//span[contains(@class, "item")]//text()').getall()
            ).strip().lower()
            num_raw = "".join(
                li.xpath('.//span[contains(@class, "number")]//text()').getall()
            ).strip()
            if not label_raw or not num_raw:
                continue
            if label_raw.startswith("bed"):
                out["bedrooms"] = num_raw.split("-")[0].strip()
            elif label_raw.startswith("bath"):
                out["bathrooms"] = num_raw
            elif "sq" in label_raw and "ft" in label_raw.replace(".", ""):
                out["sqft"] = re.sub(r"\D", "", num_raw.split("-")[0])
            elif "stor" in label_raw:
                out["stories"] = num_raw.split("-")[0].strip()
            elif "garage" in label_raw:
                digits = re.sub(r"\D", "", num_raw)
                if digits:
                    out["garages"] = digits
        return out

    @staticmethod
    def _split_bathrooms(value: str) -> tuple[str, str]:
        if not value:
            return "", ""
        try:
            fv = float(value.replace(",", "").strip())
        except ValueError:
            return "", ""
        whole = int(fv)
        frac = fv - whole
        half = "1" if 0.3 < frac < 0.7 else ""
        return str(whole), half

    @staticmethod
    def _digits_price(display: str) -> str:
        if not display:
            return ""
        return (display or "").replace("$", "").replace(",", "").strip()

    def _split_city_state(self, subtitle: str) -> tuple[str, str]:
        s = (subtitle or "").strip()
        if not s:
            return "", ""
        m = re.match(r"^([^,]+),\s*([A-Z]{2})$", s)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return s, ""

    @staticmethod
    def _zip_from_hero_address(response) -> str:
        """ZIP only when it appears in the listing hero (visible address), not page-wide."""
        blob = (
            response.xpath('//section[contains(@class, "detail-page-hero")]')
            .xpath("string(.)")
            .get()
            or ""
        )
        m = re.search(r"\b[A-Z]{2}\s+(\d{5}(?:-\d{4})?)\b", blob)
        return m.group(1) if m else ""

    @staticmethod
    def _cap_text(val: str, max_len: int) -> str:
        v = (val or "").strip()
        return (v[: max_len - 1] + "\u2026") if len(v) > max_len else v

    @staticmethod
    def _clean_ws(text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        return cleaned.replace("�", "-")

    def _long_community_descriptions(self, response) -> str:
        lines = []
        for txt in response.xpath('//section[@id="overview"]//text()').getall():
            t = self._clean_ws(txt)
            if not t:
                continue
            lines.append(t)

        out = []
        seen = set()
        for t in lines:
            key = t[:220].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return self._cap_text("\n\n".join(out), 4000)

