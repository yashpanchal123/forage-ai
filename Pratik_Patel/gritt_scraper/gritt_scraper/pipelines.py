import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem

from .items import GRITT_PROFILE_EXPORT_FIELDS

# --- Shared helpers for profile export (16 flat fields only) -----------------

_NA_TOKENS = frozenset({"na", "n/a", "none", "null", "-"})


def _clean_field_text(value: object) -> str:
    """Single-line text for CSV/JSON: line breaks and stray control chars -> spaces."""
    s = str(value or "").strip()
    if not s:
        return ""
    if s.upper() in ("NA", "N/A"):
        return ""
    if s.lower() in _NA_TOKENS:
        return ""
    s = s.replace("\x00", "")
    # Newlines, Unicode line/paragraph separators, tabs -> space
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = s.replace("\u2028", " ").replace("\u2029", " ")
    s = s.replace("\t", " ")
    s = re.sub(r" +", " ", s).strip()
    return s


def _strip_url_line_breaks(value: object) -> str:
    """URLs: remove line-break characters only (do not insert spaces into the host/path)."""
    s = str(value or "").strip()
    s = re.sub(r"[\r\n\u2028\u2029]+", "", s).strip()
    return s


_FLAT_ROW_URL_KEYS = frozenset({
    "investor_profile_url",
    "portfolio_company_website",
    "portfolio_linkedin",
    "portfolio_gritt_link",
})


def _finalize_flat_export_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize every cell: text fields single-line (\\n -> space); URL fields strip breaks only."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if v is None or isinstance(v, int):
            out[k] = v
            continue
        if isinstance(v, str):
            if k in _FLAT_ROW_URL_KEYS:
                t = _strip_url_line_breaks(v)
            else:
                t = _clean_field_text(v)
            out[k] = t if t else None
            continue
        out[k] = v
    return out


def _optional_str(value: object) -> Optional[str]:
    s = _clean_field_text(value)
    return s if s else None


def _optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_investment_stage(value: object) -> Optional[str]:
    raw = _clean_field_text(value).lower().replace("-", "_")
    if not raw or raw == "unknown":
        return None
    mapping = {
        "early_stage": "Early Stage",
        "mid_stage": "Mid Stage",
        "late_stage": "Late Stage",
        "not_relevant": "Not Relevant",
    }
    return mapping.get(raw, _clean_field_text(value) or None)


def _sanitize_portfolio_location(country: object, city: object) -> Optional[str]:
    cty = _clean_field_text(city)
    ctry = _clean_field_text(country)
    if cty and ctry:
        return f"{cty}, {ctry}"
    if ctry:
        return ctry
    if cty:
        return cty
    return None


_GENERIC_SHORTENER_HOSTS = frozenset(
    {
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "buff.ly",
        "short.link",
    }
)


def _is_generic_shortener_website(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host in _GENERIC_SHORTENER_HOSTS
    except Exception:
        return False


def _row_for_csv(row: dict[str, Any]) -> dict[str, Any]:
    """CSV cells: None -> empty string."""
    return {k: ("" if v is None else v) for k, v in row.items()}


def _safe_lower(s: object) -> str:
    return str(s or "").strip().lower()


class ProfileDataJsonPipeline:
    """
    Writes profile_data.json as a pretty-printed JSON array (indent=2).

    Uses str.split('\\n') — not splitlines() — when re-indenting each object so
    Unicode line separators inside string values cannot break the file.
    """

    OUTPUT_FILENAME = "profile_data.json"

    def __init__(self):
        self._fh = None
        self._active = False
        self._first_row = True
        self._crawler = None
        self._seen_sources: set[str] = set()
        self._written = 0
        self._dupes = 0

    @classmethod
    def from_crawler(cls, crawler):
        pipe = cls()
        pipe._crawler = crawler
        return pipe

    @staticmethod
    def _clean_text(value: object) -> str:
        return _clean_field_text(value)

    def open_spider(self, spider=None):
        active_spider = spider or getattr(self._crawler, "spider", None)
        if active_spider is None or active_spider.name != "gritt_profile":
            return
        self._active = True
        self._first_row = True
        self._written = 0
        self._dupes = 0
        self._seen_sources.clear()
        out_path = (Path(__file__).resolve().parent / self.OUTPUT_FILENAME).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(out_path, "w", encoding="utf-8", newline="\n")
        self._fh.write("[\n")
        active_spider.logger.info("ProfileDataJsonPipeline: writing to %s", out_path)

    def process_item(self, item, spider=None):
        if not self._active or self._fh is None:
            return item

        payload = ItemAdapter(item).asdict()
        source_url = self._clean_text(payload.get("gritt_source_url"))
        if source_url and source_url in self._seen_sources:
            self._dupes += 1
            return item
        if source_url:
            self._seen_sources.add(source_url)

        for row in iter_gritt_profile_flat_rows(payload):
            if not self._first_row:
                self._fh.write(",\n")
            block = json.dumps(row, ensure_ascii=False, indent=2, allow_nan=False)
            # split("\\n") only — not splitlines() — U+2028/U+2029 in values stay inside quotes
            indented = "\n".join("  " + ln for ln in block.split("\n"))
            self._fh.write(indented)
            self._first_row = False
            self._written += 1
        self._fh.flush()
        return item

    def close_spider(self, spider=None):
        if not self._active or self._fh is None:
            return
        self._fh.write("\n]\n")
        self._fh.flush()
        self._fh.close()
        self._fh = None
        logger_owner = spider or getattr(self._crawler, "spider", None)
        if logger_owner is not None:
            logger_owner.logger.info(
                "ProfileDataJsonPipeline: written rows=%d, duplicate_profiles_skipped=%d",
                self._written,
                self._dupes,
            )


class ProfileDataCsvPipeline:
    """
    Writes gritt_profile rows as flat CSV (one row per portfolio company/investment).
    Output: gritt_scraper/gritt_scraper/gritt_profile_data.csv
    """

    fieldnames = list(GRITT_PROFILE_EXPORT_FIELDS)
    OUTPUT_FILENAME = "gritt_profile_data.csv"

    def __init__(self):
        self._fh = None
        self._writer = None
        self._active = False
        self._crawler = None
        self._written = 0

    @classmethod
    def from_crawler(cls, crawler):
        pipe = cls()
        pipe._crawler = crawler
        return pipe

    @staticmethod
    def _clean_text(value: object) -> str:
        return _clean_field_text(value)

    @staticmethod
    def _gritt_link_value(company: dict, investment: dict) -> str:
        direct = _strip_url_line_breaks(
            company.get("gritt_url") or investment.get("gritt_url") or ""
        )
        if direct:
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", direct):
                parsed = urlparse(direct)
                path = (parsed.path or "").strip("/")
                if path:
                    parts = [p for p in path.split("/") if p]
                    if len(parts) >= 2 and parts[0].lower() == "company":
                        slug = parts[1]
                        return f"https://www.gritt.io/company/{slug}/"
            else:
                direct = direct.lstrip("/")
                if direct:
                    return f"https://www.gritt.io/company/{direct}/"

        # If API gives LinkedIn company URL (e.g. /company/rainxyz), use that slug.
        linkedin = _strip_url_line_breaks(company.get("linkedin_url") or "")
        if linkedin:
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", linkedin):
                linkedin = f"https://{linkedin.lstrip('/')}"
            parsed_li = urlparse(linkedin)
            li_parts = [p for p in (parsed_li.path or "").split("/") if p]
            if len(li_parts) >= 2 and li_parts[0].lower() == "company":
                li_slug = re.sub(r"[^a-z0-9-]+", "-", li_parts[1].lower()).strip("-")
                if li_slug:
                    return f"https://www.gritt.io/company/{li_slug}/"

        # Prefer website domain slug from API company domain:
        # salesforce.com -> salesforce, www.rain.xyz -> rain
        website = _strip_url_line_breaks(company.get("website_domain") or "").lower()
        if website:
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", website):
                website = f"https://{website.lstrip('/')}"
            parsed_site = urlparse(website)
            host = parsed_site.netloc.lower()
            host = re.sub(r"^www\.", "", host)
            labels = [label for label in host.split(".") if label]
            if labels:
                noisy_subdomains = {
                    "www", "careers", "jobs", "job", "blog", "news", "app", "api",
                    "m", "en", "us", "uk", "eu", "in", "ca", "au"
                }
                domain_slug = labels[0]
                if domain_slug in noisy_subdomains and len(labels) > 1:
                    domain_slug = labels[1]
                domain_slug = re.sub(r"[^a-z0-9-]+", "-", domain_slug).strip("-")
                if domain_slug:
                    return f"https://www.gritt.io/company/{domain_slug}/"
        return ""

    @staticmethod
    def _clean(value: object) -> str:
        return ProfileDataCsvPipeline._clean_text(value)

    @staticmethod
    def _normalize_website_url(value: object) -> str:
        raw = _strip_url_line_breaks(value)
        if not raw:
            return ""
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", raw):
            raw = f"https://{raw.lstrip('/')}"
        parsed = urlparse(raw)
        if not parsed.netloc:
            return ""
        host = parsed.netloc.lower()
        if not host.startswith("www."):
            host = f"www.{host}"
        path = parsed.path.rstrip("/")
        normalized = f"https://{host}{path}"
        if parsed.query:
            normalized = f"{normalized}?{parsed.query}"
        if parsed.fragment:
            normalized = f"{normalized}#{parsed.fragment}"
        if not path and not parsed.query and not parsed.fragment:
            normalized = f"{normalized}/"
        return normalized

    @staticmethod
    def _normalize_linkedin_company_url(value: object) -> str:
        raw = _strip_url_line_breaks(value)
        if not raw:
            return ""
        raw = raw.rstrip("/")
        if "linkedin.com" in raw.lower():
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", raw):
                raw = f"https://{raw.lstrip('/')}"
            parsed = urlparse(raw)
            if not parsed.netloc:
                return ""
            host = "www.linkedin.com"
            path = parsed.path or ""
            normalized = f"https://{host}{path}"
            if parsed.query:
                normalized = f"{normalized}?{parsed.query}"
            if parsed.fragment:
                normalized = f"{normalized}#{parsed.fragment}"
            return f"{normalized}/" if not normalized.endswith("/") else normalized

        slug = raw
        slug = re.sub(r"^company/", "", slug, flags=re.IGNORECASE).strip("/")
        if not slug:
            return ""
        return f"https://www.linkedin.com/company/{slug}/"

    def open_spider(self, spider=None):
        active_spider = spider or getattr(self._crawler, "spider", None)
        if active_spider is None or active_spider.name != "gritt_profile":
            return
        self._active = True
        self._written = 0
        out_path = (Path(__file__).resolve().parent / self.OUTPUT_FILENAME).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(out_path, "w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.fieldnames)
        self._writer.writeheader()
        self._fh.flush()
        active_spider.logger.info("ProfileDataCsvPipeline: writing to %s", out_path)

    def process_item(self, item, spider=None):
        if not self._active or self._fh is None or self._writer is None:
            return item

        payload = ItemAdapter(item).asdict()
        for row in iter_gritt_profile_flat_rows(payload):
            self._writer.writerow(_row_for_csv(row))
            self._written += 1

        self._fh.flush()
        return item

    def close_spider(self, spider=None):
        if not self._active or self._fh is None:
            return
        self._fh.flush()
        self._fh.close()
        self._fh = None
        logger_owner = spider or getattr(self._crawler, "spider", None)
        if logger_owner is not None:
            logger_owner.logger.info("ProfileDataCsvPipeline: written rows=%d", self._written)


def iter_gritt_profile_flat_rows(payload: dict):
    """
    One row per portfolio investment; keys match ProfileDataCsvPipeline.fieldnames.
    Uses JSON null semantics (None); CSV pipeline maps None to empty cells.
    """
    P = ProfileDataCsvPipeline
    profile_data = payload.get("profile_data") or {}
    investor_details = profile_data.get("investor_details") or {}
    investor_metadata = profile_data.get("investor_metadata") or {}
    investments = profile_data.get("portfolio_investment") or []
    companies = profile_data.get("portfolio_company_details") or []

    companies_by_id = {
        P._clean(company.get("id")): company
        for company in companies
        if isinstance(company, dict) and P._clean(company.get("id"))
    }

    investor_location = _optional_str(
        investor_details.get("location_country") or investor_details.get("location_city")
    )

    for investment in investments:
        if not isinstance(investment, dict):
            continue
        company = companies_by_id.get(P._clean(investment.get("company_id")), {})
        if not isinstance(company, dict):
            company = {}

        website_norm = P._normalize_website_url(company.get("website_domain"))
        portfolio_website: Optional[str] = None
        if website_norm and not _is_generic_shortener_website(website_norm):
            portfolio_website = website_norm

        linkedin_norm = P._normalize_linkedin_company_url(company.get("linkedin_url"))
        portfolio_linkedin = _optional_str(linkedin_norm)

        gritt_raw = P._gritt_link_value(company, investment)
        portfolio_gritt_link = _optional_str(gritt_raw)

        desc = company.get("description")

        row = {
            "investor_id": _optional_int(investor_metadata.get("investor_id")),
            "investor_name": _optional_str(investor_details.get("full_name")),
            "investor_profile_url": _optional_str(
                _strip_url_line_breaks(payload.get("gritt_source_url"))
            ),
            "investor_location": investor_location,
            "investor_description": _optional_str(investor_details.get("bio")),
            "firm_name": _optional_str(investor_metadata.get("firm_name")),
            "firm_role": _optional_str(investor_metadata.get("firm_role")),
            "portfolio_role": _optional_str(investment.get("role")),
            "portfolio_company_name": _optional_str(company.get("name")),
            "portfolio_company_website": portfolio_website,
            "portfolio_linkedin": portfolio_linkedin,
            "portfolio_description": _optional_str(desc),
            "portfolio_location": _sanitize_portfolio_location(
                company.get("country"), company.get("city")
            ),
            "investment_date": _optional_int(investment.get("investment_year")),
            "investment_stage": _normalize_investment_stage(investment.get("stage")),
            "portfolio_gritt_link": portfolio_gritt_link,
        }
        yield _finalize_flat_export_row(row)


class DuplicateFilterPipeline:
    """
    Drops items if their investor_url already exists in investors.csv.

    The CSV is treated as append-only storage; we load existing URLs once at spider start
    and keep an in-memory set for O(1) checks.
    """

    def __init__(self, csv_path: str = "investors.csv"):
        self.csv_path = csv_path
        self._existing_urls: set[str] = set()
        self.skipped_existing = 0

    @classmethod
    def from_crawler(cls, crawler):
        csv_path = crawler.settings.get("INVESTORS_CSV_PATH", "investors.csv")
        return cls(csv_path=csv_path)

    def open_spider(self, spider):
        self._existing_urls = _load_existing_urls(self.csv_path)
        spider.logger.info(
            f"DuplicateFilterPipeline: loaded {len(self._existing_urls)} existing URLs from {self.csv_path}"
        )

    def process_item(self, item, spider):
        a = ItemAdapter(item)
        url = (a.get("investor_url") or "").strip()
        if url and url in self._existing_urls:
            self.skipped_existing += 1
            raise DropItem("url_already_in_csv")

        if url:
            self._existing_urls.add(url)
        return item

    def close_spider(self, spider):
        spider.logger.info(
            f"DuplicateFilterPipeline: skipped_existing={self.skipped_existing}"
        )


class CsvInvestorsPipeline:
    """
    Appends investors to investors.csv one-by-one as items stream in.

    Schema:
      - investor_url
    """

    fieldnames = ["investor_url"]

    def __init__(self, csv_path: str = "investors.csv"):
        self.csv_path = csv_path
        self._fh = None
        self._writer: Optional[csv.DictWriter] = None
        self.written = 0

    @classmethod
    def from_crawler(cls, crawler):
        csv_path = crawler.settings.get("INVESTORS_CSV_PATH", "investors.csv")
        return cls(csv_path=csv_path)

    def open_spider(self, spider):
        os.makedirs(os.path.dirname(self.csv_path) or ".", exist_ok=True)
        file_exists = os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0

        self._fh = open(self.csv_path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.fieldnames)

        if not file_exists:
            self._writer.writeheader()
            self._fh.flush()

        spider.logger.info(
            f"CsvInvestorsPipeline: writing to {self.csv_path} (schema={self.fieldnames})"
        )

    def process_item(self, item, spider):
        a = ItemAdapter(item)
        row = {"investor_url": (a.get("investor_url") or "").strip()}
        self._writer.writerow(row)
        self._fh.flush()  # one-by-one append durability
        self.written += 1
        return item

    def close_spider(self, spider):
        try:
            if self._fh:
                self._fh.flush()
                self._fh.close()
        finally:
            spider.logger.info(
                f"CsvInvestorsPipeline: written={self.written} (schema={self.fieldnames})"
            )


def _load_existing_urls(csv_path: str) -> set[str]:
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
        return set()

    existing: set[str] = set()
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return set()

        header_l = [_safe_lower(h) for h in header]
        url_idx = None
        for candidate in ("investor_url", "url", "link"):
            if candidate in header_l:
                url_idx = header_l.index(candidate)
                break

        # If no header match, assume first column is URL (backward compatibility)
        if url_idx is None:
            url_idx = 0
            # also treat first row as data (not header) in that case
            first_row = header
            if first_row and first_row[url_idx].strip():
                existing.add(first_row[url_idx].strip())

        for row in reader:
            if not row:
                continue
            if url_idx < len(row):
                url = row[url_idx].strip()
                if url:
                    existing.add(url)

    return existing