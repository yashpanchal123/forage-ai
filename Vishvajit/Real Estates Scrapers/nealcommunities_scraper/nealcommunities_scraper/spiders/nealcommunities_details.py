import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import scrapy

BASE_WEBSITE = "https://www.nealcommunities.com"

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
    "Status (Active, Upcoming, Sold Out)",
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
    
]


class NealCommunitiesDetailsSpider(scrapy.Spider):
    name = "nealcommunities_details"
    allowed_domains = ["nealcommunities.com", "www.nealcommunities.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [404, 500],
        "FEEDS": {
            "nealcommunities_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            },
            "nealcommunities_details.json": {
                "format": "json",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
                "indent": 4,
            },
        },
    }

    def __init__(self, input_csv="nealcommunities_listings.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv
        self._community_cache: dict[str, dict] = {}
        self._pending_by_community: dict[str, list[dict]] = {}
        self._scheduled_community: set[str] = set()

    # ------------------------------------------------------------------ #
    #  Entry point — async start() replaces deprecated start_requests()   #
    # ------------------------------------------------------------------ #
    async def start(self):
        csv_path = Path(self.input_csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        if not csv_path.exists():
            raise FileNotFoundError(f"Input CSV not found: {csv_path}")

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                house_url = self._ensure_trailing_slash(
                    self._normalize_url(row.get("house_url") or row.get("url") or "")
                )
                if not house_url:
                    continue
                community_url = self._ensure_trailing_slash(
                    self._normalize_url(row.get("community_url") or "")
                    or self._community_url_from_house(house_url)
                )
                yield scrapy.Request(
                    house_url,
                    callback=self.parse_home,
                    dont_filter=True,
                    cb_kwargs={"community_url": community_url, "house_url": house_url},
                )

    # ------------------------------------------------------------------ #
    #  Home detail page                                                    #
    # ------------------------------------------------------------------ #
    def parse_home(self, response, community_url, house_url):
        item = {field: "" for field in DETAIL_FIELDS}
        item["url"] = self._ensure_trailing_slash(self._normalize_url(house_url))
        item["Website"] = BASE_WEBSITE
        item["Builder"] = "Neal Communities"

        # Some listing URLs redirect to the community hub (no home/spec data there).
        # Skip those rows entirely.
        resolved_url = self._ensure_trailing_slash(self._normalize_url(response.url))
        community_url_norm = self._ensure_trailing_slash(self._normalize_url(community_url or ""))
        if self._is_community_hub_url(resolved_url) or (
            community_url_norm and resolved_url == community_url_norm
        ):
            return

        self._fill_from_spec_page(response, item)

        community_url = self._ensure_trailing_slash(
            self._normalize_url(community_url or "")
        )
        if not community_url:
            yield item
            return

        cached = self._community_cache.get(community_url)
        if cached is not None:
            self._apply_cached_community(item, cached)
            yield item
            return

        self._pending_by_community.setdefault(community_url, []).append(item)
        if community_url not in self._scheduled_community:
            self._scheduled_community.add(community_url)
            yield scrapy.Request(
                community_url,
                callback=self.parse_community,
                dont_filter=False,
                cb_kwargs={"community_url": community_url},
            )

    # ------------------------------------------------------------------ #
    #  Community hub page                                                  #
    # ------------------------------------------------------------------ #
    def parse_community(self, response, community_url):
        community_url = self._ensure_trailing_slash(
            self._normalize_url(community_url or response.url)
        )

        extracted = self._extract_community_fields(response)
        self._community_cache[community_url] = extracted

        pending = self._pending_by_community.pop(community_url, [])
        for item in pending:
            self._apply_cached_community(item, extracted)
            yield item

    # ------------------------------------------------------------------ #
    #  Community field helpers                                             #
    # ------------------------------------------------------------------ #
    def _apply_cached_community(self, item: dict, extracted: dict) -> None:
        for k, v in (extracted or {}).items():
            if not v:
                continue
            if not item.get(k):
                item[k] = v

    def _extract_community_fields(self, response) -> dict:
        out = {field: "" for field in DETAIL_FIELDS}
        body_text = " ".join(response.xpath("//body//text()").getall())

        title = response.xpath("//title/text()").get() or ""
        ct_name = self._community_title_community_name(title)
        if ct_name:
            out["Community Name"] = ct_name

        # Use the explicit community intro block only.
        desc = self._community_intro_text(response, body_text)
        if desc:
            out["Overall Description of the Community"] = self._cap_text(desc, 3500)

        amenities = self._community_amenities(response)
        out["Amenities Available"] = "Y" if amenities else "N"

        # Per requirement: do not infer/guess these fields.
        out["HOA Fee"] = ""
        out["Other Fees (i.e. CDD)"] = ""
        out["School District"] = ""

        return out

    # ------------------------------------------------------------------ #
    #  Home detail page field helpers                                      #
    # ------------------------------------------------------------------ #
    def _fill_from_spec_page(self, response, item):
        raw = response.text or ""

        lat, lng = self._extract_house_page_coords_xpath(response)
        item["Latitude"] = lat
        item["Longitude"] = lng

        modified = self._json_ld_modified(raw)
        if modified:
            item["Last Updated"] = modified

        ld_desc = self._json_ld_description(raw)
        if ld_desc:
            item["Plan Description"] = self._cap_text(ld_desc, 2000)

        title_community = self._spec_title_community_name(response)
        if title_community:
            item["Community Name"] = title_community

        lines = self._lines_from_selector(response, ".home-detail-info")
        detail_block = (
            response.css(".home-detail-info").xpath("string(.)").get() or ""
        )
        parsed_info = self._parse_detail_info_lines(lines, detail_block)
        if parsed_info.get("model_name"):
            item["Model Name"] = parsed_info["model_name"]
        if parsed_info.get("sqft"):
            item["Minimum SQFT"] = parsed_info["sqft"]
        if parsed_info.get("price"):
            item["Minimum Base Price (Current)"] = self._normalize_price(parsed_info["price"])
        if parsed_info.get("product_type"):
            item["Product Type"] = parsed_info["product_type"]
        descriptor = response.css("div.house-frame-descriptor::text").get() or ""
        mapped_descriptor = self._map_product_type(descriptor)
        if mapped_descriptor:
            item["Product Type"] = mapped_descriptor
        status_text = (
            response.css("div.price-details-top div.move-in-date::text").get()
            or parsed_info.get("status")
            or ""
        )
        mapped_status = self._map_status(status_text)
        if mapped_status:
            item["Status (Active, Upcoming, Sold Out)"] = mapped_status
        if parsed_info.get("street"):
            item["Street Address"] = parsed_info["street"]
        if parsed_info.get("city"):
            item["City"] = parsed_info["city"]
        if parsed_info.get("state"):
            item["State"] = parsed_info["state"]
        if parsed_info.get("zip"):
            item["Zip Code"] = parsed_info["zip"]

        spec_lines = self._hd_specs_lines(response)
        stats = self._parse_hd_specs(spec_lines)
        if stats.get("beds"):
            item["# BR"] = stats["beds"]
        item["# BA"], item["# 1/2 BA"] = stats.get("full_ba", ""), stats.get("half_ba", "")
        if stats.get("garages"):
            item["# of Garages"] = stats["garages"]
        if stats.get("sqft"):
            item["Minimum SQFT"] = stats["sqft"]
            item["QMI Model SQFT"] = stats["sqft"]
        if stats.get("floors"):
            item["# of Floors"] = stats["floors"]
            item["QMI Model # of Floor"] = stats["floors"]

        if parsed_info.get("price"):
            item["QMI Model Price"] = self._normalize_price(parsed_info["price"])
        if parsed_info.get("model_name"):
            item["QMI Model Name"] = parsed_info["model_name"]
        if stats.get("garages"):
            item["QMI # of Garages"] = stats["garages"]
        if stats.get("beds"):
            item["QMI # of BR"] = stats["beds"]
        if stats.get("full_ba"):
            item["QMI # of BA"] = stats["full_ba"]
        if stats.get("half_ba"):
            item["QMI # of 1/2 BA"] = stats["half_ba"]

        full_body = " ".join(response.xpath("//body//text()").getall())
        plan_desc = self._plan_description_from_spec(response)
        if plan_desc:
            item["Plan Description"] = self._cap_text(plan_desc, 3000)

        features_only = self._plan_features_from_spec(response)
        item["Plan Features"] = features_only
        item["Attributes/Features"] = features_only

        # Keep community description sourced from community URL (.comm-desc-intro).
        # Phone number is frequently not explicit/reliable on this site context.
        item["Phone number"] = ""

        item["Model hours"] = self._extract_office_hours(full_body)

        ff = self._first_floor_master_hint(full_body)
        if ff:
            item["First Floor Master (Y/N)"] = ff

        adult = self._adult_community_hint(full_body)
        if adult:
            item["Adult Community (Y/N)"] = adult

        status_txt = (item.get("Status (Active, Upcoming, Sold Out)") or "").lower()
        if "quick move" in status_txt or "move-in" in full_body.lower():
            item["QMI Availability Date"] = ""

    # ------------------------------------------------------------------ #
    #  Static / class helpers                                              #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_url(url: str) -> str:
        clean = (url or "").strip().split("#")[0].split("?")[0].rstrip("/")
        if clean.startswith("//"):
            clean = "https:" + clean
        return clean

    @staticmethod
    def _ensure_trailing_slash(url: str) -> str:
        u = (url or "").strip()
        if not u:
            return ""
        return u if u.endswith("/") else (u + "/")

    @staticmethod
    def _community_url_from_house(house_url: str) -> str:
        parts = [p for p in urlparse(house_url).path.strip("/").split("/") if p]
        if len(parts) >= 2 and parts[0] == "new-homes":
            return f"{BASE_WEBSITE}/new-homes/{parts[1]}/"
        return ""

    @staticmethod
    def _is_community_hub_url(url: str) -> bool:
        parts = [p for p in urlparse((url or "")).path.strip("/").split("/") if p]
        return len(parts) == 2 and parts[0] == "new-homes"

    @staticmethod
    def _extract_house_page_coords_xpath(response) -> tuple[str, str]:
        """
        Coordinates must come only from house page map div via XPath:
        //div[@id='comm-map']/@data-latitude and @data-longitude
        No fallback to any other source.
        """
        lat = (response.xpath('//div[@id="comm-map"]/@data-latitude').get() or "").strip()
        lng = (response.xpath('//div[@id="comm-map"]/@data-longitude').get() or "").strip()
        return lat, lng

    @staticmethod
    def _json_ld_modified(html: str) -> str:
        for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html or "",
            re.I | re.S,
        ):
            raw = m.group(1).strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            graph = data.get("@graph") if isinstance(data, dict) else None
            nodes = graph if isinstance(graph, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                dm = node.get("dateModified")
                if dm:
                    return str(dm)
        return ""

    @staticmethod
    def _json_ld_description(html: str) -> str:
        for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html or "",
            re.I | re.S,
        ):
            raw = m.group(1).strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            graph = data.get("@graph") if isinstance(data, dict) else None
            nodes = graph if isinstance(graph, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("@type") == "WebPage" and node.get("description"):
                    return str(node["description"])
        return ""

    @staticmethod
    def _spec_title_community_name(response) -> str:
        title = (response.xpath("//title/text()").get() or "").strip()
        m = re.match(r"^(.+?)\s+Quick Move-in Home", title, re.I)
        if m:
            return m.group(1).strip()
        return ""

    @staticmethod
    def _community_title_community_name(title: str) -> str:
        t = (title or "").strip()
        m = re.match(r"^(.+?)\s+Real Estate", t, re.I)
        if m:
            return m.group(1).strip()
        return ""

    @staticmethod
    def _lines_from_selector(response, sel) -> list[str]:
        block = response.css(sel).xpath("string(.)").get() or ""
        return [ln.strip() for ln in re.split(r"[\r\n]+", block) if ln.strip()]

    def _parse_detail_info_lines(self, lines: list[str], detail_block: str) -> dict:
        out = {
            "model_name": "",
            "sqft": "",
            "price": "",
            "product_type": "",
            "status": "",
            "street": "",
            "city": "",
            "state": "",
            "zip": "",
        }
        if not lines:
            return out

        idx_sqft = next(
            (i for i, ln in enumerate(lines) if re.search(r"SQ\.?\s*FT", ln, re.I)),
            None,
        )
        if idx_sqft is not None and idx_sqft >= 1:
            out["model_name"] = lines[idx_sqft - 1]

        for ln in lines:
            m_sq = re.search(r"([\d,]+)\s*SQ\.?\s*FT\.?", ln.replace(",", ""), re.I)
            if m_sq:
                out["sqft"] = m_sq.group(1).replace(",", "")
            if "priced at" in ln.lower():
                mp = re.search(r"\$[\d,]+", ln)
                if mp:
                    out["price"] = mp.group(0)
            if "|" in ln and "series" in ln.lower() and not out["product_type"]:
                out["product_type"] = self._map_product_type(ln.split("|")[0].strip())
            if "move-in" in ln.lower() or "available" in ln.lower():
                out["status"] = ln

        # Prefer explicit descriptor block for product type.
        descriptor = self._clean_join(lines)
        if descriptor and not out["product_type"]:
            out["product_type"] = self._map_product_type(descriptor)

        for ln in lines:
            if "homesite" in ln.lower():
                street_part = re.split(r"\s+-\s+Homesite", ln, flags=re.I)[0].strip()
                if street_part:
                    out["street"] = street_part
                break

        for ln in reversed(lines):
            m_line = re.match(
                r"^(.+),\s*(FL|[A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$",
                ln.strip(),
            )
            if not m_line:
                continue
            city_raw = m_line.group(1).strip()
            st = m_line.group(2).strip()
            z = m_line.group(3).strip()
            if st != "FL" and not re.match(r"^[A-Z]{2}$", st):
                continue
            if "homesite" in city_raw.lower():
                tail = re.split(r"Homesite\s*#[\w\-]+\s*", city_raw, flags=re.I)[-1]
                city_raw = tail.strip()
            city_raw = re.sub(r"\s+", " ", city_raw)
            out["city"] = city_raw
            out["state"] = st
            out["zip"] = z
            break

        if not out["city"] and detail_block:
            found = re.findall(
                r"([A-Za-z][^,\n]{1,80}?),\s*FL\s+(\d{5}(?:-\d{4})?)",
                detail_block,
            )
            if found:
                city_guess, z_guess = found[-1]
                if "homesite" in city_guess.lower():
                    city_guess = re.split(
                        r"Homesite\s*#[\w\-]+\s*", city_guess, flags=re.I
                    )[-1].strip()
                city_guess = re.sub(r"\s+", " ", city_guess).strip()
                out["city"] = city_guess
                out["state"] = "FL"
                out["zip"] = z_guess

        return out

    @staticmethod
    def _hd_specs_lines(response) -> list[str]:
        for sel in (".hd-home-specs", ".comm-homes-specs"):
            block = response.css(sel).xpath("string(.)").get() or ""
            part = [ln.strip() for ln in re.split(r"[\r\n]+", block) if ln.strip()]
            if part:
                return part
        return []

    @staticmethod
    def _parse_hd_specs(lines: list[str]) -> dict:
        out = {"beds": "", "full_ba": "", "half_ba": "", "garages": "", "sqft": "", "floors": ""}
        for ln in lines:
            if m := re.match(r"(\d+)\s*Beds?", ln, re.I):
                out["beds"] = m.group(1)
            if m := re.match(r"([\d.]+)\s*Baths?", ln, re.I):
                val = float(m.group(1))
                whole = int(val)
                frac = val - whole
                out["full_ba"] = str(whole)
                if frac >= 0.4:
                    out["half_ba"] = "1" if abs(frac - 0.5) < 0.21 else ""
            if m := re.match(r"(\d+)\s*Car\s+Garage", ln, re.I):
                out["garages"] = m.group(1)
            if m := re.search(r"([\d,]+)\s*Sq\s*Ft", ln, re.I):
                out["sqft"] = m.group(1).replace(",", "")
            if "story" in ln.lower():
                if m := re.match(r"(\d+)\s*Story", ln, re.I):
                    out["floors"] = m.group(1)
        return out

    @classmethod
    def _harvest_phones(cls, response) -> list[str]:
        found = []
        for href in response.xpath('//a[starts-with(@href,"tel:")]/@href').getall():
            raw = href.replace("tel:", "").strip()
            digits = re.sub(r"\D", "", raw)
            if len(digits) >= 10:
                found.append(raw)
        text = response.text or ""
        for m in re.findall(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text):
            found.append(m.strip())
        for m in re.findall(
            r"\b(?:239|321|352|386|407|561|727|754|772|813|850|863|904|941|954)\d{7}\b",
            re.sub(r"\D", "", text),
        ):
            if len(m) == 10:
                found.append(f"{m[:3]}-{m[3:6]}-{m[6:]}")
        seen: set[str] = set()
        uniq = []
        for p in found:
            k = re.sub(r"\D", "", p)
            if len(k) >= 10 and k not in seen:
                seen.add(k)
                uniq.append(p)
        return uniq

    @staticmethod
    def _prefer_sales_phone(phones: list[str]) -> str:
        if not phones:
            return ""
        normalized = []
        for p in phones:
            dig = re.sub(r"\D", "", p)
            if len(dig) == 11 and dig.startswith("1"):
                dig = dig[1:]
            if len(dig) >= 10:
                normalized.append((dig, p))
        for dig, raw in normalized:
            if dig.startswith(("888", "800", "877", "866", "855")):
                continue
            return raw
        return phones[0]

    @staticmethod
    def _extract_office_hours(text: str) -> str:
        m = re.search(r"(Office\s+Hours[^\n]{5,120}|Hours[^\n]{5,120})", text or "", re.I)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _first_floor_master_hint(text: str) -> str:
        low = (text or "").lower()
        if "first floor master" in low or "first-floor master" in low:
            return "Y"
        if "second floor master" in low:
            return "N"
        return ""

    @staticmethod
    def _adult_community_hint(text: str) -> str:
        low = (text or "").lower()
        if re.search(r"\b55\+\b", low):
            return "Y"
        if "age-restricted" in low or "age restricted community" in low:
            return "Y"
        return ""

    @staticmethod
    def _community_detail_paragraph(response, body_text: str) -> str:
        for txt in response.xpath(
            '//main//*[contains(@class,"detail") or contains(@class,"desc")]//text()'
        ).getall():
            t = (txt or "").strip()
            if len(t) > 120 and not t.startswith("{") and '"items"' not in t:
                return t
        m = re.search(
            r"(Discover\s+.+?)(?:\n|\r|Learn More|Schedule)",
            body_text,
            re.I | re.S,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        return ""

    @staticmethod
    def _community_intro_text(response, body_text: str) -> str:
        txt = response.css("div.comm-desc-intro").xpath("string(.)").get() or ""
        return re.sub(r"\s+", " ", txt).strip()

    @staticmethod
    def _parse_promotion_banner(text: str) -> tuple[str, str, str]:
        t = text or ""
        pct, dollars, promo_text = "", "", ""
        m_usd = re.search(r"(Up to\s+\$[\d,]+[^.\n]{0,80})", t, re.I)
        if m_usd:
            promo_text = re.sub(r"\s+", " ", m_usd.group(1)).strip()
            m_amt = re.search(r"\$([\d,]+)", promo_text)
            if m_amt:
                dollars = "$" + m_amt.group(1)
        m_pct = re.search(
            r"((?:save|receive)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*%[^.\n]{0,60})",
            t,
            re.I,
        )
        if m_pct and not re.search(r"\bAPR\b|interest rate", m_pct.group(1), re.I):
            pct = m_pct.group(2) + "%"
            if not promo_text:
                promo_text = re.sub(r"\s+", " ", m_pct.group(1)).strip()
        return pct, dollars, promo_text

    @staticmethod
    def _starting_price_range(text: str) -> tuple[str, str]:
        m = re.search(
            r"(?:FROM|Starting)\s*\$([\d,]+)(?:\s*-\s*\$([\d,]+))?",
            text or "",
            re.I,
        )
        if not m:
            return "", ""
        return "$" + m.group(1), ("$" + m.group(2) if m.group(2) else "")

    @staticmethod
    def _fee_snippets(text: str) -> tuple[str, str]:
        t = text or ""
        hoa, other = "", ""
        m_hoa = re.search(
            r"(HOA[^.\n]{0,120}(?:\$[\d,]+[^.\n]{0,80}|fee[^.\n]{0,80}))", t, re.I
        )
        if m_hoa:
            hoa = re.sub(r"\s+", " ", m_hoa.group(1)).strip()
        m_cdd = re.search(r"((?:CDD|bond|assessment)[^.\n]{0,160})", t, re.I)
        if m_cdd:
            other = re.sub(r"\s+", " ", m_cdd.group(1)).strip()
        return hoa, other

    @staticmethod
    def _school_district_hint(text: str) -> str:
        m = re.search(
            r"((?:school|education)[^.]{10,180}(?:district|schools)[^.]{0,120})",
            text or "",
            re.I,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()[:400]
        return ""

    @staticmethod
    def _cap_text(value: str, max_len: int) -> str:
        v = (value or "").strip()
        return (v[: max_len - 1] + "…") if len(v) > max_len else v

    @staticmethod
    def _clean_join(values: list[str]) -> str:
        return re.sub(r"\s+", " ", " ".join(v for v in values if v)).strip()

    @staticmethod
    def _map_product_type(raw: str) -> str:
        r = (raw or "").strip().lower()
        if not r:
            return ""
        if "single" in r and "family" in r:
            return "SFD"
        if "townhome" in r or "villa" in r:
            return "SFA"
        if "condo" in r or "condominium" in r:
            return "CO"
        return ""

    @staticmethod
    def _normalize_price(value: str) -> str:
        return (value or "").replace("$", "").replace(",", "").strip()

    @staticmethod
    def _map_status(text: str) -> str:
        t = (text or "").strip().lower()
        if not t:
            return ""
        if "sold out" in t:
            return "Sold Out"
        if (
            "under construction" in t
            or "home in progress" in t
            or "move in date" in t
            or "summer " in t
            or "fall " in t
            or "winter " in t
            or "spring " in t
        ):
            return "Upcoming"
        if "move in ready" in t or "quick move" in t or "available" in t:
            return "Active"
        return ""

    @staticmethod
    def _plan_description_from_spec(response) -> str:
        paragraphs = response.css("div.container-12 div.div-block-9 p::text").getall()
        cleaned = [re.sub(r"\s+", " ", p).strip() for p in paragraphs if p and p.strip()]
        return "\n".join(cleaned).strip()

    @staticmethod
    def _plan_features_from_spec(response) -> str:
        feats = response.css("div.floor-plan-features-col ul li::text").getall()
        cleaned = [re.sub(r"\s+", " ", f).strip() for f in feats if f and f.strip()]
        return ", ".join(cleaned)

    @staticmethod
    def _community_amenities(response) -> str:
        """
        Extract explicit amenities listed under the community AMENITIES section.
        """
        heading = response.xpath(
            '//*[self::h2 or self::h3][contains(translate(normalize-space(.),'
            '"abcdefghijklmnopqrstuvwxyz","ABCDEFGHIJKLMNOPQRSTUVWXYZ"),"AMENITIES")][1]'
        )
        if not heading:
            return ""

        raw_lines = heading.xpath(
            'following-sibling::*[position()<=40]//text()'
        ).getall()
        cleaned = [re.sub(r"\s+", " ", t).strip() for t in raw_lines if t and t.strip()]
        if not cleaned:
            return ""

        stop_words = {
            "SCHEDULE AN APPOINTMENT",
            "PLAN YOUR VISIT TODAY",
            "DIRECTIONS TO",
            "MAP DIRECTIONS",
            "SIGN UP FOR OUR VIP LIST",
            "VIEW HOMES INVENTORY HOMES",
            "GREAT SCHOOLS",
        }
        out = []
        for t in cleaned:
            up = t.upper()
            if any(sw in up for sw in stop_words):
                break
            # keep concise amenity labels only
            if len(t) > 40:
                continue
            if any(ch.isdigit() for ch in t):
                continue
            if t in out:
                continue
            out.append(t)
        return ", ".join(out)