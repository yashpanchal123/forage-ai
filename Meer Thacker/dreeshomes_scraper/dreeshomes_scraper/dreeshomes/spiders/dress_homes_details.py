import csv
import html as html_lib
import json
import os
import re
from urllib.parse import urlparse

import scrapy


class DressHomesDetailsSpider(scrapy.Spider):
    name = "dress_homes_details"
    allowed_domains = ["dreeshomes.com"]

    # Defaults (can be overridden via -a input_csv=... -a output_csv=...)
    input_csv = "dreeshomes_output.csv"
    output_csv = "dress_homes_details_ouput.csv"

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
        "FEEDS": {
            output_csv: {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": FIELDS,
            }
        },
        "ROBOTSTXT_OBEY": True,
        "DEFAULT_REQUEST_HEADERS": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    def start_requests(self):
        csv_path = self._resolve_input_csv_path(self.input_csv)
        if not os.path.exists(csv_path):
            raise FileNotFoundError(
                f"Input CSV not found: {csv_path}. "
                f"Pass -a input_csv=... or place it at project root."
            )
        self.logger.info("Reading input CSV: %s", csv_path)

        total_rows = 0
        queued_rows = 0
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_rows += 1
                house_url = (row.get("house_url") or "").strip()
                if not house_url:
                    self.logger.debug("Skipping input row %s: missing house_url", total_rows)
                    continue
                normalized_url = self._normalize_url(house_url)
                if not normalized_url:
                    self.logger.debug("Skipping input row %s: invalid URL '%s'", total_rows, house_url)
                    continue
                queued_rows += 1
                self.logger.info("Queueing URL %s: %s", queued_rows, normalized_url)
                yield scrapy.Request(
                    url=normalized_url,
                    callback=self.parse_details,
                    errback=self.errback_details,
                    meta={"source_row": row},
                )
        self.logger.info("Input rows read: %s | requests queued: %s", total_rows, queued_rows)

    def errback_details(self, failure):
        url = getattr(getattr(failure, "request", None), "url", "") or ""
        self.logger.warning("Request failed for URL: %s | error: %s", url, failure.value)
        out = {k: "" for k in self.FIELDS}
        out["url"] = url
        out["status"] = f"request_failed: {failure.value!s}"
        yield out

    def parse_details(self, response):
        self.logger.info("Parsing details URL: %s", response.url)
        blocks = self._parse_all_vbind_json(response)

        base = blocks.get("neighborhood-details-base-product-block") or {}
        base_details = (base.get("details") or {}) if isinstance(base, dict) else {}

        highlight = blocks.get("home-plan-highlight-product-block") or {}
        location = blocks.get("neighborhood-details-location-info") or {}
        community_data = {}
        if isinstance(location, dict) and isinstance(location.get("communityData"), dict):
            community_data = location["communityData"]

        # Basic identity/location
        url = response.url
        community_name = (
            self._first_nonempty(
                self._as_str(community_data.get("marketingName")),
                self._as_str(base_details.get("communityName")),
            )
            or ""
        )
        neighborhood_name = self._as_str(base_details.get("neighborhoodName"))

        latitude = self._first_nonempty(
            self._as_str(highlight.get("latitude")),
            self._as_str(community_data.get("latitude")),
        )
        longitude = self._first_nonempty(
            self._as_str(highlight.get("longitude")),
            self._as_str(community_data.get("longitude")),
        )

        street_address = self._first_nonempty(
            self._as_str(highlight.get("address")),
            self._as_str(community_data.get("streetAddress")),
        )

        city = self._first_nonempty(
            self._as_str(community_data.get("city")),
            self._as_str(highlight.get("cityName")),
            self._as_str(base_details.get("cityName")),
        )
        state = self._first_nonempty(
            self._as_str(community_data.get("state")),
            self._as_str(highlight.get("stateAbbr")),
            self._as_str(base_details.get("stateInitials")),
        )
        zip_code = self._first_nonempty(
            self._as_str(community_data.get("zipCode")),
            self._as_str(highlight.get("zipCode")),
            self._as_str(base_details.get("zipCode")),
        )

        # Hours / contact from:
        # 1) communityData.officehoursList
        # 2) salesOfficeLocation.officehoursList
        office_hours = self._resolve_model_hours_from_json_paths(blocks, community_data, location)
        phone = self._first_nonempty(self._as_str(location.get("phoneNumber")), self._as_str(location.get("modelPhone")))

        website = self._domain_url(url)

        # Description: prefer highlight.description (HTML), fallback to any long-form HTML sections.
        description_html = self._as_str(highlight.get("description"))
        plan_description_text = self._clean_ws(self._strip_tags(description_html))

        # Product/plan/QMI basics from base_details
        home_type = self._as_str(base_details.get("homeType"))
        plan_name = self._as_str(base_details.get("planName"))

        bed_low = base_details.get("bedLow")
        bath_low = base_details.get("bathLow")
        half_bath_low = base_details.get("halfBathLow")
        stories_low = base_details.get("storiesLow")
        garages_low = base_details.get("garagesLow")

        # Images: merge image URLs from known blocks
        image_urls = []
        for img in base_details.get("images") or []:
            if isinstance(img, dict):
                image_urls.append(img.get("path") or img.get("imagePath") or img.get("url"))
            elif isinstance(img, str):
                image_urls.append(img)
        # Some pages also have standard <img> tags we can fallback to (hero/gallery)
        if not image_urls:
            image_urls = response.xpath("//img/@src").getall()[:25]
        image_urls = [u for u in (self._normalize_media_url(u, response) for u in image_urls) if u]

        # County: not consistently exposed in JSON; fallback to HTML label search.
        county = self._extract_labeled_value(response, label_regex=r"County")

        adult_community = "Y" if self._looks_like_55_plus(url, neighborhood_name, community_name) else "N"

        product_type_code = self._home_type_short(home_type)
        gl_i = self._as_int(garages_low)
        garages_num = self._garages_display_string(garages_low, base_details.get("garagesHigh"))
        garages_yes_no = "Y" if gl_i is not None and gl_i > 0 else "N"

        home_features_text = self._home_features_from_html(response)
        # Skip pages with no attributes/features per requirement.
        if not home_features_text:
            self.logger.info("Skipping URL (empty Attributes/Features): %s", url)
            return
        min_price = self._resolve_minimum_base_price(base_details, highlight)
        max_price = self._resolve_maximum_base_price(base_details, highlight)
        previous_price = self._first_positive_price_string(base_details.get("originalPrice"))
        max_price_out = self._blank_if_zero(max_price)
        # If max and previous are same, keep previous and leave max blank.
        if max_price_out and previous_price and max_price_out == previous_price:
            max_price_out = ""

        # Output row (blank for fields not available on this page type)
        out = {k: "" for k in self.FIELDS}
        out.update(
            {
                "url": url,
                "Community Details": "",
                "Community Name": community_name,
                "latitude": self._round_coord(latitude, 4),
                "longitude": self._round_coord(longitude, 4),
                "Street Address": street_address,
                "City": city,
                "State": state,
                "Zip Code": zip_code,
                "County": county,
                "Model hours": office_hours,
                "Phone number": phone,
                "Website": website,
                "Builder": "Drees Homes",
                "Product Type (SFD/SFA/CO)": product_type_code,
                "Model/Product Types Available": "YES" if product_type_code else "NO",
                "Garages (Y/N)": garages_yes_no,
                "# of Garages": garages_num,
                "Adult Community (Y/N)": adult_community,
                "Attributes/Features": home_features_text,
                "Overall Description of the Community": "",
                "Model Details": "",
                "Model Name": plan_name,
                "Plan Description": plan_description_text or plan_name,
                "# BR": self._as_str(bed_low),
                "# BA": self._as_str(bath_low),
                "# 1/2 BA": self._as_str(half_bath_low),
                "# of Floors": self._as_str(stories_low),
                "Parking Type": "Garage" if garages_yes_no == "Y" else "",
                "# Garages": garages_num,
                "Minimum SQFT": self._as_str(base_details.get("sqFtLow")),
                "Maximum SQFT": self._blank_if_zero(base_details.get("sqFtHigh")),
                "Minimum Base Price (Current)": min_price,
                "Maximum Base Price/All In Price": max_price_out,
                "Previous Price": previous_price,
                "Last Updated": "",
                "First Floor Master (Y/N)": "Y" if self._mentions_first_floor_master(plan_description_text) else "",
                "Plan Features": home_features_text,
                "images": " | ".join(image_urls),
            }
        )

        self.logger.info("Saving URL to CSV: %s", url)
        yield out

    def _home_features_from_html(self, response):
        parts = response.xpath(
            "//h4[contains(text(),'Home Features')]/following-sibling::ul//text()"
        ).getall()
        clean = [self._clean_ws(x) for x in parts if x and str(x).strip()]
        return " | ".join(clean)

    @staticmethod
    def _as_float(v):
        try:
            if v is None:
                return None
            if isinstance(v, bool):
                return None
            s = str(v).strip()
            if not s:
                return None
            return float(s)
        except (TypeError, ValueError):
            return None

    def _first_positive_price_string(self, *candidates):
        for c in candidates:
            n = self._as_float(c)
            if n is not None and n > 0:
                i = int(round(n))
                return str(i) if abs(n - i) < 0.01 else str(n)
        return ""

    @staticmethod
    def _highlight_nested_dicts(highlight):
        """QMI / home highlight JSON often nests pricing under home, listing, etc."""
        if not isinstance(highlight, dict):
            return []
        found = [highlight]
        for key in ("home", "homeDetails", "property", "listing", "model", "details"):
            sub = highlight.get(key)
            if isinstance(sub, dict):
                found.append(sub)
        return found

    def _resolve_minimum_base_price(self, base_details, highlight):
        bd = base_details if isinstance(base_details, dict) else {}
        cands = [
            bd.get("priceLow"),
            bd.get("discountedPrice"),
        ]
        for hl in self._highlight_nested_dicts(highlight):
            cands.extend(
                [
                    hl.get("priceLow"),
                    hl.get("price"),
                    hl.get("minPrice"),
                    hl.get("basePrice"),
                    hl.get("listingPrice"),
                    hl.get("homePrice"),
                    hl.get("startingPrice"),
                ]
            )
        return self._first_positive_price_string(*cands)

    def _resolve_maximum_base_price(self, base_details, highlight):
        bd = base_details if isinstance(base_details, dict) else {}
        cands = [
            bd.get("priceHigh"),
            bd.get("originalPrice"),
        ]
        for hl in self._highlight_nested_dicts(highlight):
            cands.extend(
                [
                    hl.get("priceHigh"),
                    hl.get("maxPrice"),
                    hl.get("price"),
                    hl.get("listingPrice"),
                ]
            )
        return self._first_positive_price_string(*cands)

    @staticmethod
    def _round_coord(val, ndigits=4):
        if val is None or val == "":
            return ""
        try:
            return f"{float(str(val).strip()):.{ndigits}f}"
        except (TypeError, ValueError):
            return str(val).strip()

    @staticmethod
    def _domain_url(page_url: str) -> str:
        if not page_url:
            return ""
        p = urlparse(page_url.strip())
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/"
        return ""

    @staticmethod
    def _home_type_short(home_type: str) -> str:
        if not home_type:
            return ""
        t = str(home_type).strip().lower()
        if "single" in t:
            return "SFD"
        if "town" in t:
            return "SFA"
        if "condo" in t:
            return "CO"
        return str(home_type).strip()

    def _eff_high_garage(self, low, high):
        hi = self._as_int(high)
        lo = self._as_int(low)
        if hi is None or hi == 0:
            return lo
        return hi

    def _garages_display_string(self, low, high):
        gl = self._as_int(low)
        ge = self._eff_high_garage(low, high)
        if gl is None:
            return ""
        if gl == 0 and (ge is None or ge == 0):
            return ""
        if ge is not None and ge != gl:
            return f"{gl}-{ge}"
        return str(gl)

    @staticmethod
    def _format_time_compact(t: str) -> str:
        if not t:
            return ""
        return re.sub(r"\s+", "", str(t).strip())

    _DAY_SLOTS = (
        (0, "Mon", ("monday", "mon")),
        (1, "Tue", ("tuesday", "tue")),
        (2, "Wed", ("wednesday", "wed")),
        (3, "Thu", ("thursday", "thu")),
        (4, "Fri", ("friday", "fri")),
        (5, "Sat", ("saturday", "sat")),
        (6, "Sun", ("sunday", "sun")),
    )

    def _day_order_abbr(self, day_str: str):
        d = (day_str or "").strip().lower()
        for order, abbr, keys in self._DAY_SLOTS:
            for k in keys:
                if d == k or d.startswith(k):
                    return order, abbr
        return 99, (day_str or "")[:3].title()

    def _format_model_hours_pipe(self, officehours_list):
        if not isinstance(officehours_list, list) or not officehours_list:
            return ""
        rows = []
        for x in officehours_list:
            if not x:
                continue
            if isinstance(x, dict):
                order, abbr = self._day_order_abbr(x.get("dayOfWeek") or "")
                start = self._format_time_compact(x.get("startTimeString") or "")
                end = self._format_time_compact(x.get("endTimeString") or "")
                closed = bool(x.get("closedOnThisDay"))
                if closed:
                    seg = f"{abbr}: Closed"
                elif start and end:
                    seg = f"{abbr}: {start} - {end}"
                else:
                    seg = ""
                rows.append((order, seg))
            else:
                rows.append((99, self._clean_ws(self._strip_tags(self._as_str(x)))))
        rows.sort(key=lambda r: r[0])
        return " | ".join(r[1] for r in rows if r[1])

    def _resolve_model_hours_from_json_paths(self, blocks, community_data, location):
        # Primary exact paths from parsed location/community objects.
        if isinstance(community_data, dict):
            formatted = self._format_model_hours_pipe(
                community_data.get("officehoursList") or community_data.get("officeHoursList")
            )
            if formatted:
                return formatted

        if isinstance(location, dict):
            sales_office = location.get("salesOfficeLocation")
            if isinstance(sales_office, dict):
                formatted = self._format_model_hours_pipe(
                    sales_office.get("officehoursList") or sales_office.get("officeHoursList")
                )
                if formatted:
                    return formatted

        # Fallback: traverse all v-bind JSON blocks and resolve the same two paths.
        def walk_dicts(obj):
            if isinstance(obj, dict):
                yield obj
                for v in obj.values():
                    yield from walk_dicts(v)
            elif isinstance(obj, list):
                for item in obj:
                    yield from walk_dicts(item)

        for d in walk_dicts(blocks if isinstance(blocks, dict) else {}):
            cd = d.get("communityData")
            if isinstance(cd, dict):
                formatted = self._format_model_hours_pipe(
                    cd.get("officehoursList") or cd.get("officeHoursList")
                )
                if formatted:
                    return formatted
            sol = d.get("salesOfficeLocation")
            if isinstance(sol, dict):
                formatted = self._format_model_hours_pipe(
                    sol.get("officehoursList") or sol.get("officeHoursList")
                )
                if formatted:
                    return formatted
        return ""

    def _resolve_model_hours(self, response, community_data, location):
        schedule = self._format_model_hours_pipe((community_data or {}).get("officehoursList"))
        if schedule:
            return schedule

        # Some pages expose only an appointment note instead of structured day/time rows.
        for obj in (community_data, location):
            if not isinstance(obj, dict):
                continue
            for key in (
                "officeHours",
                "officeHoursText",
                "officeHoursDescription",
                "hours",
                "modelHours",
                "appointmentMessage",
            ):
                text = self._clean_ws(self._strip_tags(self._as_str(obj.get(key))))
                if text and "appointment" in text.lower():
                    return text

        appt_text = response.xpath(
            "//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'appointment only')]/text()"
        ).getall()
        for t in appt_text:
            val = self._clean_ws(t)
            if val and "appointment" in val.lower():
                return val
        return ""

    def _blank_if_zero(self, value):
        s = self._as_str(value)
        if not s:
            return ""
        n = self._as_float(s)
        if n is None:
            return s
        return "" if n <= 0 else s

    @staticmethod
    def _resolve_input_csv_path(path_value: str) -> str:
        raw = (path_value or "").strip()
        if not raw:
            return raw
        if os.path.isabs(raw):
            return raw
        # project root is typically dreeshomes_scraper/dreeshomes_scraper (same level as scrapy.cfg)
        return os.path.abspath(raw)

    @staticmethod
    def _normalize_url(url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        clean = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if not (clean.startswith("http://") or clean.startswith("https://")):
            return ""
        return clean

    def _parse_all_vbind_json(self, response):
        out = {}
        # we want tag name -> parsed json (dict)
        for node in response.xpath("//*[@v-bind]"):
            raw = node.xpath("@v-bind").get()
            if not raw:
                continue
            try:
                data = json.loads(html_lib.unescape(raw))
            except Exception:
                continue
            tag = node.root.tag if hasattr(node, "root") else node.xpath("name()").get()
            tag = tag or node.xpath("name()").get() or ""
            tag = str(tag)
            out[tag] = data
        return out

    @staticmethod
    def _strip_tags(s: str) -> str:
        if not s:
            return ""
        return re.sub(r"<[^>]+>", " ", s)

    @staticmethod
    def _clean_ws(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "")).strip()

    @staticmethod
    def _as_str(v):
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return str(v)
        return str(v).strip()

    @staticmethod
    def _first_nonempty(*vals: str) -> str:
        for v in vals:
            if v and str(v).strip():
                return str(v).strip()
        return ""

    @staticmethod
    def _as_int(v):
        try:
            if v is None:
                return None
            if isinstance(v, bool):
                return None
            if isinstance(v, int):
                return v
            s = str(v).strip()
            if not s:
                return None
            # Handle malformed values like "2/4/2026" by taking the first number.
            # We only need a garage-count integer and should not drop the row.
            m = re.search(r"\d+", s)
            if m and m.group(0) != s:
                return int(m.group(0))
            return int(float(s))
        except Exception:
            return None

    @staticmethod
    def _looks_like_55_plus(url: str, neighborhood_name: str, community_name: str) -> bool:
        hay = " ".join([url or "", neighborhood_name or "", community_name or ""]).lower()
        return "55" in hay or "active adult" in hay or "active-adult" in hay

    @staticmethod
    def _mentions_first_floor_master(text: str) -> bool:
        t = (text or "").lower()
        return "first-floor" in t and "primary" in t or "first floor" in t and "primary" in t or "first floor" in t and "master" in t

    @staticmethod
    def _normalize_media_url(u: str, response) -> str:
        if not u:
            return ""
        u = str(u).strip()
        if not u:
            return ""
        if u.startswith("//"):
            return "https:" + u
        if u.startswith("/"):
            return response.urljoin(u)
        return u

    @staticmethod
    def _extract_labeled_value(response, label_regex: str) -> str:
        # Tries patterns like: <dt>County</dt><dd>XYZ</dd> or "County: XYZ"
        # Keep conservative to avoid noise.
        label_xpath = (
            f"//*[self::dt or self::div or self::span or self::p]"
            f"[re:test(normalize-space(string(.)), '{label_regex}', 'i')]"
        )
        try:
            nodes = response.xpath(label_xpath, namespaces={"re": "http://exslt.org/regular-expressions"})
        except Exception:
            nodes = []
        for n in nodes[:10]:
            # dt/dd layout
            dd = n.xpath("following-sibling::dd[1]//text()").getall()
            if dd:
                val = " ".join(x.strip() for x in dd if x and x.strip())
                val = re.split(r"\s{2,}", val)[0].strip()
                if val:
                    return val
            # inline "County: XYZ"
            txt = " ".join(x.strip() for x in n.xpath(".//text()").getall() if x and x.strip())
            m = re.search(rf"{label_regex}\s*[:\-]\s*(.+)$", txt, re.I)
            if m:
                return m.group(1).strip()
        return ""

