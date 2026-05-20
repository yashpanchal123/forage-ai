"""
Holiday Builders Move-In Ready listings: scrape listing HTML plus one overlay per
community page (deduped) for office hours.

Many requested columns are not published on the site; those are left blank.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import scrapy

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

def _blank_row() -> dict[str, str]:
    return {k: "" for k in DETAIL_FIELDS}


def _norm_community_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    path = (p.path or "").rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}/"


def _digits_price(text: str) -> str:
    if not text:
        return ""
    nums = re.sub(r"[^\d]", "", text)
    return nums if nums else ""


def _parse_city_state_zip(line: str) -> tuple[str, str, str]:
    line = re.sub(r"\s+", " ", (line or "").strip())
    # "Ocala, FL 34473"
    m = re.match(r"^(.+?),\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$", line, flags=re.I)
    if m:
        return m.group(1).strip(), m.group(2).upper(), m.group(3).strip()
    # ", FL 32583" (city missing in template)
    m2 = re.match(r"^,?\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$", line, flags=re.I)
    if m2:
        return "", m2.group(1).upper(), m2.group(2).strip()
    return "", "", ""


def _city_from_breadcrumb(response) -> str:
    city = (
        response.xpath('//p[@id="breadcrumbs"]//a[contains(@href,"/new-home-markets/")][1]/text()')
        .get(default="")
        .strip()
    )
    return city


def _parse_stat_grid(response) -> dict[str, str]:
    icons = {"bed": "", "bath": "", "car": "", "sqft": ""}
    for block in response.xpath(
        '//div[contains(@class,"background-gray-blue") and contains(@class,"uk-padding-small")]'
        '//div[contains(@class,"uk-flex") and contains(@class,"uk-flex-center")]'
    ):
        label_raw = "".join(block.xpath('.//span/text()').getall()).strip().lower()
        label = (
            "".join(label_raw.split())
            if isinstance(label_raw, str)
            else label_raw.strip().lower()
        )
        value = "".join(block.xpath('.//h3/text()').getall()).strip()
        label = "".join(label.split())
        if "beds" in label:
            icons["bed"] = value.replace(",", "").strip()
        elif "baths" in label:
            icons["bath"] = value.replace(",", "").strip()
        elif label == "car":
            icons["car"] = value.replace(",", "").strip()
        elif "sqft" in label:
            icons["sqft"] = value.replace(",", "").strip()
    return icons


def _overview_labeled_facts(response) -> dict[str, str]:
    """Map lowercased label text (e.g. 'school district') to value for #overview stats row."""
    out: dict[str, str] = {}
    for val_node in response.xpath(
        '//div[@id="overview"]//div[contains(@class,"background-gray-blue")]'
        '//div[contains(@class,"font-secondary") and contains(@class,"font15") and contains(@class,"lh1")]'
    ):
        label = " ".join(
            " ".join(val_node.xpath("following-sibling::div[1]//text()").getall()).split()
        ).lower()
        val = " ".join(" ".join(val_node.xpath(".//text()").getall()).split())
        if label and val:
            out[label] = val
    return out


def _join_paragraphs(selector) -> str:
    parts = []
    for p in selector:
        t = " ".join(x.strip() for x in p.xpath(".//text()").getall() if x.strip())
        if t:
            parts.append(t)
    return " ".join(parts).strip()


def _modified_time(response) -> str:
    m = (
        response.xpath('//meta[@property="article:modified_time"]/@content').get()
        or ""
    ).strip()
    if m:
        return m
    for script in response.xpath('//script[@type="application/ld+json"]/text()').getall():
        script = script.strip()
        if not script:
            continue
        try:
            data = json.loads(script)
        except json.JSONDecodeError:
            # Fallback for malformed JSON-LD blobs that still contain a dateModified token.
            m_raw = re.search(
                r'"dateModified"\s*:\s*"([^"]+)"',
                script,
                flags=re.I,
            )
            if m_raw:
                return m_raw.group(1).strip()
            continue
        nodes = []
        if isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                nodes.extend([n for n in data["@graph"] if isinstance(n, dict)])
            nodes.append(data)
        elif isinstance(data, list):
            nodes.extend([n for n in data if isinstance(n, dict)])
        for node in nodes:
            dm = node.get("dateModified")
            if dm:
                return str(dm).strip()
            wp = node.get("mainEntity")
            if isinstance(wp, dict) and wp.get("dateModified"):
                return str(wp.get("dateModified")).strip()
    # Final safety net on raw HTML.
    m_text = re.search(
        r'"dateModified"\s*:\s*"([^"]+)"',
        response.text or "",
        flags=re.I,
    )
    if m_text:
        return m_text.group(1).strip()
    return ""


def _extract_community_name(response) -> str:
    h2 = (
        response.xpath(
            "string(//div[@id='overview']//h2[contains(@class,'text-blue') and contains(@class,'title')])"
        ).get(default="")
        or ""
    )
    h2 = " ".join(h2.split())
    pm = re.search(r"New Construction Homes\s+in\s*(.+)", h2, flags=re.I)
    if pm:
        return " ".join(pm.group(1).split()).strip()
    crumbs = response.xpath('//p[@id="breadcrumbs"]//a[contains(@href,"new-home-communities")]/text()').getall()
    if crumbs:
        return str(crumbs[-1]).strip()
    return ""


def _community_hours_text(response) -> str:
    """Office hours from the first Sales Center clock block (community pages often duplicate the block)."""
    hours_div = response.xpath(
        '//i[contains(@class,"fa-clock")]/parent::*[1]/following-sibling::div[1]'
    )
    if not hours_div:
        return ""

    block = hours_div[0]
    lines: list[str] = []
    for p in block.xpath(".//p"):
        raw = p.get() or ""
        raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        plain = re.sub(r"<[^>]+>", " ", raw)
        for seg in plain.split("\n"):
            seg = " ".join(seg.split()).strip()
            seg = seg.replace("\xa0", " ")
            seg = seg.replace("–", "-").replace("—", "-")
            if seg:
                lines.append(seg)

    if not lines:
        raw = block.get() or ""
        raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        plain = re.sub(r"<[^>]+>", " ", raw)
        for seg in plain.split("\n"):
            seg = " ".join(seg.split()).strip()
            if seg:
                lines.append(seg)

    text = "\n".join(lines).strip()
    text = re.sub(r"(?i)\boffice hours\b", "", text).strip()
    return text[:800]


def _extract_community_url(response) -> str:
    links = [
        (
            url or ""
        ).strip()
        for url in response.xpath('//p[@id="breadcrumbs"]//a[contains(@href,"new-home-communities")]/@href').getall()
    ]
    if not links:
        return ""
    return _norm_community_url(response.urljoin(links[-1]))


class HolidayBuildersDetailsSpider(scrapy.Spider):
    name = "holidaybuilders_details"
    allowed_domains = ["holidaybuilders.com", "www.holidaybuilders.com"]

    custom_settings = {
        "HTTPERROR_ALLOWED_CODES": [404, 500, 502, 503],
        "FEEDS": {
            "holidaybuilders_sample_data.csv": {
                "format": "csv",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
            },
            "holidaybuilders_sample_data.json": {
                "format": "json",
                "encoding": "utf-8",
                "overwrite": True,
                "fields": DETAIL_FIELDS,
                "indent": 4,
            },
        },
    }

    def __init__(self, input_csv="holidaybuilders_listings.csv", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_csv = input_csv
        self._community_waiting: dict[str, list[dict[str, str]]] = {}
        self._scheduled_community: set[str] = set()
        self._community_cache: dict[str, dict[str, str]] = {}

    def start_requests(self):
        csv_path = Path(self.input_csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        if not csv_path.exists():
            raise FileNotFoundError(f"Input CSV not found: {csv_path}")

        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                raw = row.get("house_url") or row.get("url") or ""
                house_url = str(raw).strip().rstrip("/") + "/"
                if "/new-homes-for-sale/" not in house_url:
                    continue
                yield scrapy.Request(
                    house_url,
                    callback=self.parse_listing,
                    errback=self._listing_err,
                    dont_filter=True,
                )

    def _listing_err(self, failure):
        self.logger.error("listing request failed: %s", failure.value)

    def _flush_comm_queue(self, comm_key: str, overlay: dict[str, str]):
        waiters = self._community_waiting.pop(comm_key, [])
        if not overlay and comm_key in self._community_cache:
            overlay = self._community_cache[comm_key]
        for base in waiters:
            row = {**base}
            if overlay.get("model_hours"):
                row["Model hours"] = overlay["model_hours"]
            yield row

    def parse_listing(self, response):
        if response.status != 200:
            self.logger.warning("listing HTTP %s %s", response.status, response.url)
            return

        row = _blank_row()
        row["url"] = response.url.split("?")[0].rstrip("/") + "/"
        row["Website"] = "https://holidaybuilders.com/"
        row["Builder"] = "Holiday Builders"
        row["Last Updated"] = _modified_time(response)

        badge = "".join(
            response.xpath(
                '//div[contains(@class,"uk-position-relative")]'
                '//div[contains(@class,"uk-position-bottom-center")]//text()'
            ).getall()
        ).strip()
        badge_norm = re.sub(r"[^a-z0-9]+", " ", badge.lower()).strip()
        if "move in ready" in badge_norm:
            row["Status (Active, Upcoming, Sold Out)"] = "Active"
        elif "coming soon" in badge_norm:
            row["Status (Active, Upcoming, Sold Out)"] = "Upcoming"
        elif "sold out" in badge_norm:
            row["Status (Active, Upcoming, Sold Out)"] = "Sold Out"
        elif badge.strip().lower() in {"active", "upcoming", "sold out"}:
            row["Status (Active, Upcoming, Sold Out)"] = badge.strip()
        else:
            row["Status (Active, Upcoming, Sold Out)"] = ""

        row["Model Name"] = (
            response.xpath('//h1[contains(@class,"text-blue") and contains(@class,"font-tertiary")]//span/text()').get(default="").strip()
        )
        stats = _parse_stat_grid(response)
        br = stats.get("bed", "")
        ba = stats.get("bath", "")
        garages = stats.get("car", "")
        sqft = stats.get("sqft", "").replace(",", "")

        paras = response.xpath(
            '//div[@class="uk-section uk-section-default uk-section-small"]'
            '//div[@class="uk-container-alt"]/p[position()<=20]'
        )
        plan_story = _join_paragraphs(paras)
        row["Plan Description"] = plan_story

        row["# BR"] = br
        row["# BA"] = ba
        row["# 1/2 BA"] = ""
        row["# of Floors"] = ""
        row["# of Garages"] = garages
        row["First Floor Master (Y/N)"] = ""

        price = (
            response.xpath(
                '//div[contains(@class,"uk-section-default") and contains(@class,"uk-section-small")]'
                '//div[contains(@class,"uk-text-right")]/h3[contains(@class,"text-red")]/text()'
            )
            .get(default="")
            .strip()
        )
        listing_price_digits = _digits_price(price)
        row["Minimum Base Price (Current)"] = listing_price_digits
        row["QMI Model Price"] = listing_price_digits

        row["Minimum SQFT"] = sqft
        row["Maximum SQFT"] = sqft
        row["QMI Model SQFT"] = sqft
        row["QMI # of Garages"] = garages
        row["QMI # of BR"] = br
        row["QMI # of BA"] = ba
        row["QMI # of 1/2 BA"] = ""
        row["QMI Model Name"] = row["Model Name"]
        row["QMI Model # of Floor"] = ""
        row["QMI Current Incentive Type"] = ""
        row["QMI Incentive %"] = ""
        row["QMI Incentive $"] = ""

        row["Current Incentive Type"] = ""
        row["QMI Current Incentive Type"] = ""
        row["Incentive %"] = ""
        row["Incentive $"] = ""
        row["QMI Incentive %"] = ""
        row["QMI Incentive $"] = ""

        street = (
            response.xpath('//a[contains(@class,"google-map-link")]//h2[contains(@class,"text-blue")]/text()').get(default="").strip()
        )
        line2 = (
            response.xpath('//a[contains(@class,"google-map-link")]//h5[contains(@class,"text-ligh-gray")]/text()')
            .get(default="")
            .strip()
        )
        city, st, zp = _parse_city_state_zip(line2)
        if not city:
            city = _city_from_breadcrumb(response)

        row["Street Address"] = street
        row["City"] = city
        row["State"] = st
        row["Zip Code"] = zp

        lat = response.xpath('//div[@id="map"]//div[contains(@class,"marker")]/@data-lat').get()
        lng = response.xpath('//div[@id="map"]//div[contains(@class,"marker")]/@data-lng').get()
        row["Latitude"] = (lat or "").strip()
        row["Longitude"] = (lng or "").strip()

        facts = _overview_labeled_facts(response)
        sd = (facts.get("school district") or "").strip()
        row["School District"] = sd
        row["County"] = ""

        comm_name = _extract_community_name(response)
        row["Community Name"] = comm_name

        ov_desc = ""
        desc_box = response.xpath(
            '//div[@id="overview"]//div[contains(@class,"uk-overflow-auto") and contains(@style,"150px")]'
        )
        if desc_box:
            ov_desc = _join_paragraphs(desc_box.xpath(".//p"))
        row["Overall Description of the Community"] = ov_desc

        if listing_price_digits:
            row["Minimum Base Price (Current)"] = listing_price_digits

        row["Maximum Price"] = ""

        if sqft:
            row["Minimum SQFT"] = sqft
            row["Maximum SQFT"] = ""
            row["QMI Model SQFT"] = sqft

        row["Product Type"] = ""

        row["Phone number"] = ""

        row["Interior Specifications Available"] = ""
        row["Plan Features"] = ""
        row["Interior Specifications Descriptions"] = ""
        row["Attributes/Features"] = ""
        row["QMI Interior/Exterior Attributes"] = ""

        comm_url = _extract_community_url(response)

        row["Model hours"] = ""

        ck = comm_url.rstrip("/") if comm_url else ""
        self.logger.debug(
            "community_url=%s house=%s", comm_url[:80] + "..." if len(comm_url) > 80 else comm_url, row["url"]
        )

        if not comm_url:
            yield {k: row.get(k, "") for k in DETAIL_FIELDS}
            return

        if ck in self._community_cache:
            cached_ov = self._community_cache[ck]
            if cached_ov.get("model_hours"):
                row["Model hours"] = cached_ov["model_hours"]
            yield {k: row.get(k, "") for k in DETAIL_FIELDS}
            return

        self._community_waiting.setdefault(ck, []).append({k: row.get(k, "") for k in DETAIL_FIELDS})

        if ck not in self._scheduled_community:
            self._scheduled_community.add(ck)
            yield scrapy.Request(
                comm_url,
                callback=self.parse_community_overlay,
                errback=self._community_err,
                meta={"community_key": ck},
                dont_filter=True,
            )

    def parse_community_overlay(self, response):
        ck = response.meta.get("community_key") or _norm_community_url(response.url).rstrip("/")
        overlay = {
            "model_hours": _community_hours_text(response),
        }
        self._community_cache[ck] = overlay
        yield from self._flush_comm_queue(ck, overlay)

    def _community_err(self, failure):
        req = failure.request
        ck = (req.meta.get("community_key") or "").rstrip("/")
        self.logger.warning("community fetch failed %s", failure.value)
        yield from self._flush_comm_queue(ck, {})
