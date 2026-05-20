import csv
import re
from urllib.parse import parse_qs, unquote, urlparse
from pathlib import Path
from urllib.parse import urldefrag

import scrapy

BASE_WEBSITE = "https://www.marondahomes.com/"

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


class MarondaHomesDetailsSpider(scrapy.Spider):
    name = "marondahomes_details"
    allowed_domains = ["marondahomes.com", "www.marondahomes.com"]

    custom_settings = {
        "FEEDS": {
            "marondahomes_details.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            }
        }
    }
    MAX_COMMUNITY_DESC_LEN = 2500

    def __init__(self, input_csv="marondahomes_listings.csv", *args, **kwargs):
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
                house_url = self._normalize_url(row.get("house_url") or row.get("url"))
                community_url = self._normalize_url(row.get("community_url") or "")
                state = str(row.get("state") or "").strip().lower()
                if not house_url:
                    continue
                if not community_url:
                    community_url = self._community_url_from_house_url(house_url)
                yield scrapy.Request(
                    url=house_url,
                    callback=self.parse_house,
                    meta={
                        "state": state,
                        "community_url": community_url,
                        "house_url": house_url,
                    },
                )

    def parse_house(self, response):
        house_url = response.meta.get("house_url") or self._normalize_url(response.url)
        community_url = response.meta.get("community_url") or ""
        state_hint = response.meta.get("state") or ""

        item = {field: "" for field in DETAIL_FIELDS}
        item["url"] = house_url
        item["Website"] = BASE_WEBSITE
        item["Builder"] = "Maronda Homes"

        body_text = self._clean_join(response.xpath("//body//text()").getall())
        h1 = self._clean_join(response.xpath("//h1//text()").getall())
        if " in " in h1:
            parts = h1.split(" in ", 1)
            item["QMI Model Name"] = parts[0].replace("The ", "").strip()
            item["Community Name"] = parts[1].strip()
        else:
            item["QMI Model Name"] = h1.replace("The ", "").strip()

        address_line = self._address_from_maps_link(response)
        street, city, state_code, zip_code = self._parse_address(address_line)
        if not city:
            city = self._city_from_url(house_url)
        street = self._strip_city_suffix(street, city)
        item["Street Address"] = self._full_address_line(street, city, state_code, zip_code)
        item["City"] = city
        item["State"] = state_code or self._state_code_from_slug(state_hint)
        item["Zip Code"] = zip_code
        item["County"] = self._extract_county(body_text)

        phone = self._clean_join(
            response.xpath('//a[starts-with(@href, "tel:")]/text()').getall()
        )
        if not phone:
            phone = response.xpath('//a[starts-with(@href, "tel:")]/@href').get() or ""
        item["Phone number"] = self._normalize_phone(phone)

        # Keep blank; status text is not explicit/reliable enough.
        item["status"] = ""
        item["Plan Description"] = self._cap_text(self._extract_description(response), 1200)
        item["Overall Description of the Community"] = item["Plan Description"]

        house_stats = self._extract_house_stats(body_text)
        item["Model Name"] = item["QMI Model Name"]
        item["# BR"] = house_stats.get("beds", "")
        item["# BA"] = house_stats.get("full_ba", "")
        item["# 1/2 BA"] = house_stats.get("half_ba", "")
        item["# Garages"] = house_stats.get("garages", "")
        item["# of Garages"] = house_stats.get("garages", "")
        item["Minimum SQFT"] = house_stats.get("sqft", "")
        item["Minimum Base Price (Current)"] = self._extract_price(body_text)
        item["Garages (Y/N)"] = "Y" if self._to_int(item["# of Garages"]) > 0 else ""
        item["Parking Type"] = "Garage" if item["Garages (Y/N)"] == "Y" else ""
        item["images"] = self._collect_house_images(response)
        self._clear_qmi_fields(item)
        self._blank_zero_numeric_fields(item)

        # Keep house-level fields from house URL; enrich only selected community fields.
        if not community_url:
            yield item
            return

        yield scrapy.Request(
            url=community_url,
            callback=self.parse_community,
            meta={"item": item},
            dont_filter=True,
        )

    def parse_community(self, response):
        item = dict(response.meta["item"])
        text = self._clean_join(response.xpath("//body//text()").getall())

        if not item.get("Community Name"):
            item["Community Name"] = self._clean_join(response.xpath("//h1//text()").getall())

        location_line = self._clean_join(
            response.xpath(
                '//h1/following::*[contains(normalize-space(.), "County")][1]//text()'
            ).getall()
        )
        county_from_location = self._extract_county(location_line)
        if county_from_location:
            item["County"] = county_from_location

        if not item.get("Street Address"):
            model_address = self._address_from_maps_link(response)
            street, city, state_code, zip_code = self._parse_address(model_address)
            if not city:
                city = self._city_from_url(item.get("url"))
            street = self._strip_city_suffix(street, city)
            item["Street Address"] = self._full_address_line(street, city, state_code, zip_code)
            item["City"] = city
            item["State"] = state_code or item.get("State")
            item["Zip Code"] = zip_code

        community_details = self._extract_community_details(response)
        if community_details:
            item["Overall Description of the Community"] = self._cap_text(
                community_details, self.MAX_COMMUNITY_DESC_LEN
            )
        else:
            item["Overall Description of the Community"] = self._cap_text(
                item.get("Overall Description of the Community"),
                self.MAX_COMMUNITY_DESC_LEN,
            )

        item["Model hours"] = self._extract_hours(response, text)
        item["Amenities Available"] = "Y" if "Nearby Amenities" in text else ""
        item["Amenity Type"] = self._extract_amenities(text)
        item["Product Type (SFD/SFA/CO)"] = self._extract_product_type(text)
        item["Model/Product Types Available"] = "Y" if "Buildable Home Plans" in text else ""

        plan_stats = self._extract_plan_ranges(text)
        if not item.get("# BR"):
            item["# BR"] = plan_stats.get("beds", "")
        if not item.get("# BA"):
            item["# BA"] = plan_stats.get("baths", "")
        if not item.get("# Garages"):
            item["# Garages"] = plan_stats.get("garages", "")
        if not item.get("# of Garages"):
            item["# of Garages"] = plan_stats.get("garages", "")
        if not item.get("Minimum SQFT"):
            item["Minimum SQFT"] = plan_stats.get("min_sqft", "")
        item["Maximum SQFT"] = plan_stats.get("max_sqft", "")
        if not item.get("Minimum Base Price (Current)"):
            item["Minimum Base Price (Current)"] = plan_stats.get("min_price", "")
        item["Maximum Base Price/All In Price"] = plan_stats.get("max_price", "")

        # Keep only house-page images; do not append community-level galleries.

        yield item

    @staticmethod
    def _extract_plan_ranges(text):
        out = {"min_sqft": "", "max_sqft": "", "beds": "", "baths": "", "garages": "", "min_price": "", "max_price": ""}
        sqft_vals = [int(v.replace(",", "")) for v in re.findall(r"([\d,]+)\s*sqft", text, re.I)]
        if sqft_vals:
            out["min_sqft"] = str(min(sqft_vals))
            out["max_sqft"] = str(max(sqft_vals))

        price_vals = [int(v.replace(",", "")) for v in re.findall(r"\$(\d[\d,]*)", text)]
        if price_vals:
            out["min_price"] = str(min(price_vals))
            out["max_price"] = str(max(price_vals))

        bed_vals = [int(v) for v in re.findall(r"(\d+)(?:-\d+)?\s*bed", text, re.I)]
        bath_vals = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)\s*ba", text, re.I)]
        garage_vals = [int(v) for v in re.findall(r"(\d+)(?:-\d+)?\s*car", text, re.I)]
        if bed_vals:
            out["beds"] = f"{min(bed_vals)}-{max(bed_vals)}" if min(bed_vals) != max(bed_vals) else str(min(bed_vals))
        if bath_vals:
            out["baths"] = f"{min(bath_vals)}-{max(bath_vals)}" if min(bath_vals) != max(bath_vals) else str(min(bath_vals))
        if garage_vals:
            out["garages"] = f"{min(garage_vals)}-{max(garage_vals)}" if min(garage_vals) != max(garage_vals) else str(min(garage_vals))
        return out

    @staticmethod
    def _extract_amenities(text):
        m = re.search(r"Nearby Amenities(.*?)(?:Community Overview|Frequently Asked Questions)", text, re.I)
        if not m:
            return ""
        block = m.group(1)
        tokens = re.findall(r"\b(Shopping|Dining|Parks|Golf|Fitness|Hospitals)\b", block, re.I)
        return " | ".join(dict.fromkeys(t.title() for t in tokens))

    @staticmethod
    def _extract_product_type(text):
        low = str(text or "").lower()
        if "townhome" in low:
            return "SFA"
        if "single-family" in low or "single family" in low:
            return "SFD"
        if "condo" in low:
            return "CO"
        return ""

    def _extract_hours(self, response, text):
        if not text:
            return ""
        # Prefer the explicit "Full Hours" section when present.
        block = self._clean_join(
            response.xpath(
                '//*[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "full hours")]'
                '/following::*[position()<=30]//text()'
            ).getall()
        )
        if not block:
            m = re.search(
                r"Full Hours(.*?)(?:Schedule a Tour|Buildable Home Plans|Quick Move-In Homes|Community Details|Frequently Asked Questions|$)",
                text,
                re.I,
            )
            block = m.group(1).strip() if m else text

        entries = self._parse_day_hours_entries(block)
        if entries:
            return " | ".join(entries)

        # Fallback to global text parse, still strict on valid hour values.
        entries = self._parse_day_hours_entries(text)
        return " | ".join(entries)

    @staticmethod
    def _extract_move_in_date(text):
        m = re.search(r"Approx\.\s*move-in:\s*([0-9/.-]+)", text, re.I)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_description(response):
        # Preferred source for house-level long description when available.
        nodes = response.xpath('//div[@class="desc_long"]//text()').getall()
        text = re.sub(r"\s+", " ", " ".join(nodes)).strip()
        if text:
            return text
        # Fallback for pages without desc_long block.
        nodes = response.xpath("//h1/following::p[1]//text()").getall()
        return re.sub(r"\s+", " ", " ".join(nodes)).strip()

    @staticmethod
    def _cap_text(value, max_len):
        text = str(value or "").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 3].rstrip() + "..."

    @staticmethod
    def _extract_status(text):
        t = str(text or "")
        if re.search(r"\bMOVE IN READY\b", t, re.I):
            return "For Sale"
        if re.search(r"Approx\.\s*move-in", t, re.I):
            return "Coming Soon"
        if re.search(r"\bSold\b", t, re.I):
            return "Sold Out"
        return ""

    @staticmethod
    def _extract_measure(text, pattern):
        m = re.search(pattern, text or "", re.I)
        if not m:
            return ""
        return m.group(1).replace(",", "").strip()

    @staticmethod
    def _extract_price(text):
        m = re.search(r"\$(\d[\d,]*)", text or "")
        return m.group(1).replace(",", "") if m else ""

    @staticmethod
    def _split_baths(value):
        if not value:
            return "", ""
        try:
            n = float(value)
        except ValueError:
            return "", ""
        full = int(n)
        half = 1 if (n - full) >= 0.49 else 0
        return str(full) if full > 0 else "", str(half) if half > 0 else ""

    @staticmethod
    def _extract_county(text):
        m = re.search(r"\b([A-Z][A-Za-z'&.-]*(?:\s+[A-Z][A-Za-z'&.-]*){0,2}\s+County)\b", str(text or ""))
        return m.group(1).strip() if m else ""

    @staticmethod
    def _parse_address(raw):
        s = str(raw or "").strip()
        if not s:
            return "", "", "", ""
        s = re.sub(r"\blocation_pin\b", " ", s, flags=re.I)
        s = re.sub(r"\s+", " ", s).strip()
        m = re.match(r"^(.*?),\s*([^,]+),\s*([A-Za-z]{2})\s+(\d{5})", s)
        if m:
            return m.group(1).strip(), m.group(2).strip(), m.group(3).upper(), m.group(4)
        m1 = re.match(r"^(.*?)\s+([A-Za-z]{2})\s+(\d{5})$", s)
        if m1:
            left = m1.group(1).strip()
            return left, "", m1.group(2).upper(), m1.group(3)
        m2 = re.match(r"^(.*?)\s+([A-Za-z]{2})\s+(\d{5})$", s)
        if m2:
            return m2.group(1).strip(), "", m2.group(2).upper(), m2.group(3)
        return s, "", "", ""

    def _address_from_maps_link(self, response):
        href = response.xpath('//a[contains(@href, "google.com/maps/search")][1]/@href').get()
        if href:
            full = response.urljoin(href)
            parsed = urlparse(full)
            query = parse_qs(parsed.query)
            raw = ""
            if query.get("query"):
                raw = query["query"][0]
            elif query.get("q"):
                raw = query["q"][0]
            if raw:
                return self._clean_join([unquote(raw).replace("+", " ")])
        return self._clean_join(
            response.xpath('//a[contains(@href, "google.com/maps/search")][1]//text()').getall()
        )

    @staticmethod
    def _city_from_url(url):
        parts = [p for p in urlparse(str(url or "")).path.split("/") if p]
        if len(parts) < 2:
            return ""
        return parts[1].replace("-", " ").title()

    @staticmethod
    def _strip_city_suffix(street, city):
        s = str(street or "").strip().rstrip(",")
        c = str(city or "").strip().rstrip(",")
        if not s or not c:
            return s
        pattern = re.compile(rf"(.*?)(?:,\s*)?{re.escape(c)}$", re.I)
        m = pattern.match(s)
        if m:
            trimmed = m.group(1).strip().rstrip(",")
            if trimmed:
                return trimmed
        return s

    def _extract_community_details(self, response):
        direct = self._clean_community_blob(
            self._clean_join(
                response.xpath('//div[contains(@class,"community-description")]//text()').getall()
            )
        )
        if direct:
            return direct

        body = self._clean_join(response.xpath("//body//text()").getall())
        if not body:
            return ""

        m = re.search(
            r"Community Details(.*?)(?:Nearby Schools|Nearby Amenities|Frequently Asked Questions|Available Homesites|Your New Home is Waiting|$)",
            body,
            re.I,
        )
        if m:
            cleaned = self._clean_community_blob(m.group(1))
            if cleaned:
                return cleaned

        selectors = [
            '//*[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "community details")]'
            '/following::p[position()<=6]//text()',
            '//*[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "community details")]'
            '/following::*[contains(@class, "read-more")][1]//text()',
        ]
        for xp in selectors:
            text = self._clean_community_blob(self._clean_join(response.xpath(xp).getall()))
            if text:
                return text
        return ""

    @staticmethod
    def _clean_community_blob(text):
        t = str(text or "")
        t = re.sub(r"\b(Read More|Read Less|View Community Map)\b", " ", t, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _parse_day_hours_entries(self, text):
        day_map = {
            "sunday": "Sun",
            "monday": "Mon",
            "tuesday": "Tue",
            "wednesday": "Wed",
            "thursday": "Thu",
            "friday": "Fri",
            "saturday": "Sat",
            "sun": "Sun",
            "mon": "Mon",
            "tue": "Tue",
            "wed": "Wed",
            "thu": "Thu",
            "fri": "Fri",
            "sat": "Sat",
        }
        value_pat = (
            r"(?:Appointments only|Appointments Only|Closed|Closed Now|"
            r"\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M)"
        )
        out = {}
        for m in re.finditer(
            rf"\b(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sun|Mon|Tue|Wed|Thu|Fri|Sat)\b"
            rf"\s*:?\s*({value_pat})",
            str(text or ""),
            re.I,
        ):
            day = day_map.get(m.group(1).lower())
            val = m.group(2).strip()
            if day and day not in out:
                out[day] = val
        ordered = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        return [f"{d}: {out[d]}" for d in ordered if d in out]

    def _extract_house_stats(self, text):
        t = str(text or "")
        m = re.search(
            r"([\d,]+)\s*sqft\s*[•|]\s*(\d+)\s*bed\s*[•|]\s*([\d.]+)\s*ba\s*[•|]\s*(\d+)(?:-\d+)?\s*car",
            t,
            re.I,
        )
        if not m:
            return {"sqft": "", "beds": "", "full_ba": "", "half_ba": "", "garages": ""}
        full_ba, half_ba = self._split_baths(m.group(3))
        return {
            "sqft": m.group(1).replace(",", ""),
            "beds": m.group(2),
            "full_ba": full_ba,
            "half_ba": half_ba,
            "garages": m.group(4),
        }

    @staticmethod
    def _blank_zero_numeric_fields(item):
        for key in [
            "# BR",
            "# BA",
            "# 1/2 BA",
            "# Garages",
            "# of Garages",
            "Minimum SQFT",
            "Minimum Base Price (Current)",
        ]:
            value = str(item.get(key, "")).strip()
            if value in {"0", "0.0"}:
                item[key] = ""

    @staticmethod
    def _clear_qmi_fields(item):
        for key in [
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
        ]:
            item[key] = ""

    @staticmethod
    def _state_code_from_slug(state_slug):
        m = {
            "alabama": "AL",
            "florida": "FL",
            "georgia": "GA",
            "indiana": "IN",
            "kentucky": "KY",
            "maryland": "MD",
            "ohio": "OH",
            "pennsylvania": "PA",
            "south-carolina": "SC",
            "virginia": "VA",
            "west-virginia": "WV",
        }
        return m.get(str(state_slug or "").lower(), "")

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

    def _collect_house_images(self, response):
        urls = []
        # House gallery images from the slick carousel on house pages.
        gallery_sources = response.xpath(
            '//div[@class="main-site-container"]//div[@class="slick-list"]/div/div/img/@src | '
            '//div[@class="main-site-container"]//div[@class="slick-list"]/div/div/img/@data-src | '
            '//div[@class="main-site-container"]//div[@class="slick-list"]/div/div/img/@srcset'
        ).getall()
        for src in gallery_sources:
            token = str(src or "").strip()
            if not token:
                continue
            # srcset may contain "url 1x, url2 2x" -> pick first URL
            token = token.split(",")[0].strip().split(" ")[0].strip()
            full = response.urljoin(token).split("?")[0].strip()
            if not (full and full.startswith("http")):
                continue
            urls.append(full)

        # Fallback for JS/lazy-loaded house photos embedded in page source.
        if not urls:
            text = (response.text or "").replace("\\/", "/")
            for m in re.findall(r'https?://[^"\']+/moveinready-photos/[^"\']+\.(?:jpg|jpeg|png|webp)', text, re.I):
                full = str(m).split("?")[0].strip()
                if full.startswith("http"):
                    urls.append(full)
        return self._dedupe(urls)

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
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return raw

    @staticmethod
    def _to_int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _clean_join(values):
        if isinstance(values, str):
            values = [values]
        text = " ".join(str(v or "").strip() for v in values if str(v or "").strip())
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _normalize_url(url):
        raw = str(url or "").strip()
        if not raw:
            return ""
        clean, _ = urldefrag(raw)
        if clean.startswith("http://") or clean.startswith("https://"):
            return clean.split("?")[0].rstrip("/")
        return f"{BASE_WEBSITE.rstrip('/')}/{clean.lstrip('/').split('?')[0]}".rstrip("/")

    @staticmethod
    def _community_url_from_house_url(house_url):
        if not house_url:
            return ""
        return re.sub(r"/job-[^/]+\.html$", ".html", house_url, flags=re.I)
