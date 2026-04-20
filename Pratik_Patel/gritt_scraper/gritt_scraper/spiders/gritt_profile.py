import calendar
import csv
import os
import re
import time
from email.utils import parsedate
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import scrapy

from ..items import GRITT_PROFILE_EXPORT_FIELDS, GrittInvestorProfileItem

API_BASE = "https://x8zx-3roh-u9yx.e2.xano.io/api:lVInP5FG/profile"
DEFAULT_INPUT_CSV = Path(__file__).resolve().parents[1] / "investors.csv"
FALLBACK_INPUT_CSV = Path(__file__).resolve().parents[2] / "investors.csv"

DEFAULT_HEADERS = {
    "sec-ch-ua-platform": '"Windows"',
    "Referer": "https://www.gritt.io/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"',
    "Content-Type": "application/json",
    "sec-ch-ua-mobile": "?0",
}

_FORAGE_ZYTE_SMART_PROXY_KEY = "7916eb9714394ae9a160c862c3e3da93"


def _zyte_smart_proxy_url() -> str | None:
    key = (
        os.environ.get("ZYTE_SMART_PROXY_API_KEY")
        or os.environ.get("ZYTE_SMARTPROXY_APIKEY")
        or os.environ.get("CRAWLERA_API_KEY")
        or _FORAGE_ZYTE_SMART_PROXY_KEY
    )
    if not key:
        return None
    user = quote(key.strip(), safe="")
    return f"http://{user}:@proxy.zyte.com:8011"

# Retry backoff (seconds before each retry); index = next attempt number.
_RETRY_DELAYS = [0, 5, 15, 30, 60]
# Stronger ladder for HTTP 429 (rate limit) when Retry-After is absent.
_RETRY_DELAYS_429 = [0, 25, 60, 120, 180, 300]

_RETRY_AFTER_CAP_SEC = 600


def _parse_retry_after_sec(response) -> int | None:
    """Honor Retry-After on 429 (seconds or HTTP-date). Returns None if missing/invalid."""
    raw = response.headers.get("Retry-After") or response.headers.get(b"Retry-After")
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        raw = raw[0]
    if isinstance(raw, bytes):
        s = raw.decode("latin-1", errors="replace").strip()
    else:
        s = str(raw).strip()
    if not s:
        return None
    try:
        sec = int(s)
        return max(1, min(sec, _RETRY_AFTER_CAP_SEC))
    except ValueError:
        pass
    t = parsedate(s)
    if t is None:
        return None
    try:
        unix = calendar.timegm(t)
        wait = int(unix - time.time())
        return max(1, min(wait, _RETRY_AFTER_CAP_SEC))
    except (TypeError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_text(value) -> str:
    return str(value or "").strip()


# --- firm_role / firm_name from bio (investor_description): strict-only flow ---


_AT_SPLIT = re.compile(r"\s+at\s+", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"\.(?=\s|$)")
_PAREN_AT_END_RE = re.compile(r"\([^)]+\)\s*$")
_AT_SIGN_SEPARATOR_RE = re.compile(r"\s@")

# Pattern C: title token(s) at start of text before ``(``, then firm remainder.
_P_C_TITLE_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "Co-Founder",
            "Managing Director",
            "Managing Partner",
            "General Partner",
            "Portfolio Manager",
            "Chairman",
            "President",
            "Founder",
            "Director",
            "Principal",
            "Partner",
            "CEO",
            "CIO",
            "COO",
            "CFO",
            "Senior",
            "Junior",
            "Executive",
            "Vice",
            "Head of",
        ),
        key=len,
        reverse=True,
    )
)

_P_C_ROLE_MIDDLE = frozenset(
    {
        "and",
        "or",
        "the",
        "of",
        "in",
        "to",
        "for",
        "a",
        "an",
        "&",
        "-",
        "/",
        ",",
        "co",
        "de",
        "la",
        "le",
        "senior",
        "junior",
        "managing",
        "general",
        "executive",
        "portfolio",
        "manager",
        "chair",
        "chairman",
        "chairwoman",
        "chairperson",
        "vice",
        "president",
        "chief",
        "head",
        "global",
        "regional",
        "group",
        "lead",
        "associate",
        "principal",
        "deputy",
        "assistant",
        "acting",
        "interim",
        "former",
        "ex",
        "non-executive",
        "nonexecutive",
        "partner",
        "partners",
        "product",
        "marketing",
        "sales",
        "technology",
        "operations",
        "business",
        "development",
        "people",
        "talent",
        "strategy",
        "digital",
        "finance",
        "legal",
        "growth",
    }
)

_P_C_BAD_FIRM_START = frozenset(
    {"and", "or", "the", "of", "at", "in", "to", "for", "a", "an"}
)


def _p_c_word_key(word: str) -> str:
    return re.sub(r"[^\w]+", "", word or "", flags=re.UNICODE).lower()


def _p_c_middle_words_ok(ws: list[str]) -> bool:
    for w in ws:
        key = _p_c_word_key(w)
        if not key:
            continue
        if key in _P_C_ROLE_MIDDLE:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9&.-]*", w) and w.isupper():
            continue
        if w.replace("-", "").isalpha() and w[0].isupper() and w[1:].islower():
            if key in _P_C_ROLE_MIDDLE:
                continue
        return False
    return True


def _p_c_leading_title_word_count(words: list[str]) -> int:
    if not words:
        return 0
    head = " ".join(words).lower()
    for p in _P_C_TITLE_PREFIXES:
        pl = p.lower()
        if head.startswith(pl) and (
            len(head) == len(pl) or head[len(pl) : len(pl) + 1].isspace()
        ):
            return len(p.split())
    return 0


def _clean_firm_name_after_at(right: str) -> str:
    """Stop at first `,` or `|` or `(`; strip ws and edge punctuation."""
    if not right:
        return ""
    end = len(right)
    for ch in (",", "|", "("):
        j = right.find(ch)
        if j >= 0:
            end = min(end, j)
    fn = right[:end].strip()
    fn = re.sub(r"^[\s.,;:!?]+|[\s.,;:!?]+$", "", fn).strip()
    return fn


def _clean_firm_role_before_at(left: str) -> str:
    """Trim ws; strip trailing commas, dashes, pipes (and spaces before them)."""
    s = left.strip()
    s = re.sub(r"[\s,\-|]+$", "", s).strip()
    return s


def _is_valid_at_firm_name(firm_name: str) -> bool:
    """
    Pattern-B firm_name guard:
    - valid if starts with capital letter
    - valid if starts with digit and is not just a number/age phrase
    """
    fn = _clean_text(firm_name)
    if not fn:
        return False
    if re.match(r"^[A-Z]", fn):
        return True
    if not re.match(r"^\d", fn):
        return False
    if re.fullmatch(r"\d+", fn):
        return False
    if re.match(r"^\d+\s*(?:years?\s+old|year\s+old|yo)\b", fn, re.IGNORECASE):
        return False
    # Accept digit-led names only when there is at least one alpha token.
    return bool(re.search(r"[A-Za-z]", fn))


def _pattern_c_split_role_firm(before_paren: str) -> tuple[str | None, str | None]:
    """Text before first ``(``: title prefix + role-continuation words, then firm."""
    words = before_paren.split()
    if len(words) < 2:
        return None, None
    n_kw = _p_c_leading_title_word_count(words)
    if n_kw < 1:
        return None, None
    for i in range(len(words) - 1, n_kw - 1, -1):
        role_words = words[:i]
        firm_words = words[i:]
        if not firm_words or firm_words[0].lower() in _P_C_BAD_FIRM_START:
            continue
        if not _p_c_middle_words_ok(role_words[n_kw:]):
            continue
        role = " ".join(role_words).strip()
        firm = " ".join(firm_words).strip()
        if role and firm:
            return role, firm
    return None, None


def _parse_firm_from_investor_description(bio: str) -> tuple[str | None, str | None]:
    """
    Rejection-first flow:
    1) pipe count > 1 -> null
    2) sentence count > 1 -> null
    3) '@' count > 1 -> null
    Then patterns:
      A) '@' count == 1 -> split at first '@'
      B) no '@', first ' at ' -> split there
      C) no '@' and no ' at ', but parenthesis at end -> role+firm before '('
    """
    raw = _clean_text(bio)
    if not raw:
        return None, None

    if raw.count("|") > 1:
        return None, None
    if len(_SENTENCE_END_RE.findall(raw)) > 1:
        return None, None
    # Pattern A: '@' role separator.
    # Allow a comma-separated list of @firm handles on the right side as one combined firm_name.
    if "@" in raw and not _AT_SPLIT.search(raw):
        left, right = raw.split("@", 1)
        firm_role = _clean_firm_role_before_at(left)
        firm_name = right.strip()
        right_has_handle_list = ", @" in right
        if firm_role and firm_name and (raw.count("@") == 1 or right_has_handle_list):
            return firm_role, firm_name
        if raw.count("@") > 1:
            return None, None

    # Pattern B: first " at " (only when no "@")
    m_at = _AT_SPLIT.search(raw)
    if m_at:
        left = raw[: m_at.start()].strip()
        right = raw[m_at.end() :].strip()
        firm_role = _clean_firm_role_before_at(left)
        firm_name = _clean_firm_name_after_at(right)
        if firm_name and not _is_valid_at_firm_name(firm_name):
            return None, None
        if firm_role and firm_name:
            return firm_role, firm_name
        return None, None

    # Pattern C: parenthesis format only when ending parenthesis exists.
    if "(" in raw and _PAREN_AT_END_RE.search(raw):
        cut = raw.find("(")
        before_paren = raw[:cut].strip()
        if before_paren and "(" not in before_paren:
            firm_role, firm_name = _pattern_c_split_role_firm(before_paren)
            if firm_role and firm_name:
                return firm_role, firm_name

    return None, None


def _to_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_profile_id(investor_url: str) -> str:
    u = _clean_text(investor_url)
    if not u:
        return ""
    parsed = urlparse(u)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return ""
    if len(parts) >= 2 and parts[0].lower() == "investor":
        return unquote(parts[1].strip())
    return unquote(parts[-1].strip())


def _profile_id_for_url(investor_url: str, row_index: int) -> str:
    """Profile slug for the API query — always non-empty so no CSV row is dropped."""
    u = _clean_text(investor_url)
    if not u:
        return f"_empty_url_row_{row_index}"
    parsed = urlparse(u)
    pid = _extract_profile_id(u)
    if pid:
        return pid
    segments = [p for p in (parsed.path or "").split("/") if p]
    if segments:
        return unquote(segments[-1].strip())
    qs = parse_qs(parsed.query)
    for key in ("profile", "id", "investor", "investor_id"):
        vals = qs.get(key)
        if vals and _clean_text(vals[0]):
            return _clean_text(vals[0])
    host = _clean_text(parsed.netloc).replace(":", "_")
    if host:
        return host[:200]
    return f"_unparsed_row_{row_index}"


def _safe_join(parts: list[str]) -> str:
    return ", ".join([p for p in parts if p])


def _full_name(first: str, last: str) -> str:
    return " ".join([p for p in (_clean_text(first), _clean_text(last)) if p])


def _strip_domain_query(value: str) -> str:
    """API sometimes returns domains with ?utm_* — normalize to hostname only."""
    raw = _clean_text(value)
    if not raw:
        return ""
    raw = raw.split("?")[0].split("#")[0].strip().rstrip("/")
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        return host
    first = raw.split("/")[0].lower()
    if first.startswith("www."):
        first = first[4:]
    return first


def _normalize_stage(value: str) -> str:
    raw = _clean_text(value).lower().replace("-", " ").replace("_", " ")
    if "early" in raw:
        return "early_stage"
    if "mid" in raw:
        return "mid_stage"
    if "late" in raw:
        return "late_stage"
    if "not relevant" in raw:
        return "not_relevant"
    return "unknown"


def _extract_stage_value(exp: dict) -> str:
    candidates = [
        exp.get("stage"),
        exp.get("investmentStage"),
        exp.get("investment_stage"),
        exp.get("roundStage"),
        exp.get("round_stage"),
        exp.get("fundingStage"),
        exp.get("funding_stage"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            nested = _first_non_empty(
                candidate.get("name"),
                candidate.get("label"),
                candidate.get("value"),
                candidate.get("stage"),
            )
            if nested:
                return nested
        else:
            flat = _clean_text(candidate)
            if flat:
                return flat
    return ""


def _first_non_empty(*values) -> str:
    for v in values:
        s = _clean_text(v)
        if s:
            return s
    return ""


def _gritt_company_link(exp: dict) -> str:
    c = exp.get("_companies") if isinstance(exp.get("_companies"), dict) else {}
    return _first_non_empty(
        c.get("link"),
        exp.get("companyLink"),
        exp.get("link"),
    )


def _extract_company_location(company: dict) -> tuple[str, str]:
    city = _clean_text(company.get("city"))
    country = _clean_text(company.get("country"))
    address = _clean_text(company.get("address"))

    if city and not country and "," in city:
        city_parts = [_clean_text(part) for part in city.split(",") if _clean_text(part)]
        if len(city_parts) >= 2:
            city = city_parts[0]
            country = city_parts[-1]

    if address and (not city or not country):
        address_parts = [_clean_text(part) for part in address.split(",") if _clean_text(part)]
        if len(address_parts) >= 2:
            tail_country = address_parts[-1]
            tail_city = address_parts[-2]
            if not country and tail_country and not re.search(r"\d", tail_country):
                country = tail_country
            if not city and tail_city:
                city = tail_city
        elif len(address_parts) == 1 and not city and not country:
            city = address_parts[0]

    if not city or not country:
        region = _clean_text(company.get("region"))
        description = _clean_text(company.get("description"))
        text = _safe_join([description, region, address])
        inferred_city, inferred_country = _infer_location_from_text(text)
        if not city and inferred_city:
            city = inferred_city
        if not country and inferred_country:
            country = inferred_country

    return city, country


def _infer_location_from_text(text: str) -> tuple[str, str]:
    """
    Infer city/country from unstructured company text, e.g. 'in Amarillo, Texas'.
    """
    raw = _clean_text(text)
    if not raw:
        return "", ""

    us_states = {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming", "district of columbia",
    }
    known_countries = {
        "united states", "usa", "india", "united kingdom", "uk", "canada",
        "france", "germany", "italy", "spain", "australia", "singapore",
        "netherlands", "switzerland", "japan", "china", "israel", "denmark",
    }

    m = re.search(
        r"\bin\s+"
        r"([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,4})"
        r",\s*"
        r"([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,2})"
        r"(?=[\s\.,;:)]|$)",
        raw,
    )
    if not m:
        return "", ""
    city = _clean_text(m.group(1))
    place = _clean_text(m.group(2))
    place_l = place.lower()
    # Trim over-captured tails like "Texas is expected ..."
    place_l = re.split(r"\b(?:is|are|was|were|will|with|and|for|to)\b", place_l, maxsplit=1)[0].strip()
    # Keep only first 1-3 words if needed, and prefer known state/country phrase.
    words = [w for w in place_l.split() if w]
    if words:
        for n in range(min(3, len(words)), 0, -1):
            candidate = " ".join(words[:n]).strip()
            if candidate in us_states or candidate in known_countries:
                place_l = candidate
                break
    if place_l in us_states:
        return city, "United States"
    if place_l in known_countries:
        if place_l == "usa":
            return city, "United States"
        if place_l == "uk":
            return city, "United Kingdom"
        return city, place.title()
    return city, place


# ---------------------------------------------------------------------------
# Profile mapper
# ---------------------------------------------------------------------------

class ProfileMapper:
    @staticmethod
    def _experience_company_id(exp: dict) -> str:
        c = exp.get("_companies") if isinstance(exp.get("_companies"), dict) else {}
        return _clean_text(c.get("id") or exp.get("companyId"))

    @staticmethod
    def _experience_company_key(exp: dict, row_index: int | None = None) -> str:
        """Stable join key for export rows; last resort `_row_{n}` so every experience line is kept."""
        cid = ProfileMapper._experience_company_id(exp)
        if cid:
            return cid
        exp_id = _clean_text(exp.get("id"))
        if exp_id:
            return f"_exp_{exp_id}"
        c = exp.get("_companies") if isinstance(exp.get("_companies"), dict) else {}
        name = _first_non_empty(c.get("name"), exp.get("companyName"))
        if name:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:120]
            if slug:
                return f"_name_{slug}"
        if row_index is not None:
            return f"_row_{row_index}"
        return ""

    @staticmethod
    def build_investor(profile_pic: dict) -> dict:
        """Fields consumed by pipelines.iter_gritt_profile_flat_rows only."""
        first_name = _clean_text(profile_pic.get("ffirstname"))
        last_name = _clean_text(profile_pic.get("flastname"))
        return {
            "full_name": _full_name(first_name, last_name),
            "bio": _clean_text(profile_pic.get("bio")),
            "location_city": _clean_text(profile_pic.get("fcity")),
            "location_country": _clean_text(profile_pic.get("fcountry")),
        }

    @staticmethod
    def build_portfolio_investments(experience: list[dict]) -> list[dict]:
        rows: list[dict] = []
        for idx, exp in enumerate(experience):
            if not isinstance(exp, dict):
                continue
            year = _to_int(exp.get("year"))
            company_id = ProfileMapper._experience_company_key(exp, row_index=idx)
            rows.append({
                "company_id": company_id,
                "role": _clean_text(exp.get("role")),
                "investment_year": year,
                "stage": _normalize_stage(_extract_stage_value(exp)),
                "gritt_url": _gritt_company_link(exp),
            })
        return rows

    @staticmethod
    def build_companies(
        experience: list[dict], company_cache: dict[str, dict] | None = None
    ) -> list[dict]:
        rows: list[dict] = []
        seen_ids: set[str] = set()
        for idx, exp in enumerate(experience):
            if not isinstance(exp, dict):
                continue
            c = exp.get("_companies") if isinstance(exp.get("_companies"), dict) else {}
            company_id = ProfileMapper._experience_company_key(exp, row_index=idx)
            if not company_id or company_id in seen_ids:
                continue
            seen_ids.add(company_id)
            cached = company_cache.get(company_id, {}) if company_cache else {}
            merged_loc_source = {
                "city": _first_non_empty(c.get("city"), cached.get("city")),
                "country": _first_non_empty(c.get("country"), cached.get("country")),
                "address": _first_non_empty(c.get("address"), cached.get("address")),
                "region": _first_non_empty(c.get("region"), cached.get("region")),
                "description": _first_non_empty(c.get("pitch"), cached.get("description")),
            }
            city, country = _extract_company_location(merged_loc_source)
            domain_merged = _first_non_empty(c.get("domain"), cached.get("website_domain"))
            row = {
                "id": company_id,
                "name": _first_non_empty(c.get("name"), cached.get("name"), exp.get("companyName")),
                "website_domain": _strip_domain_query(domain_merged),
                "linkedin_url": _first_non_empty(c.get("linkedinUrl"), cached.get("linkedin_url")),
                "gritt_url": _first_non_empty(_gritt_company_link(exp), cached.get("gritt_url")),
                "description": _first_non_empty(c.get("pitch"), cached.get("description")),
                "city": city,
                "country": country,
            }
            rows.append(row)
            if company_cache is not None:
                company_cache[company_id] = row
        return rows


# ---------------------------------------------------------------------------
# Spider
# ---------------------------------------------------------------------------

class GrittProfileSpider(scrapy.Spider):
    name = "gritt_profile"
    # Each object in profile_data.json uses these keys (spider yields nested items; pipelines flatten).
    json_export_row_keys = GRITT_PROFILE_EXPORT_FIELDS
    allowed_domains = ["x8zx-3roh-u9yx.e2.xano.io", "www.gritt.io"]

    custom_settings = {
        "ITEM_PIPELINES": {
            "gritt_scraper.pipelines.ProfileDataJsonPipeline": 50,
        },
        "LOG_LEVEL": "INFO",

        # HTTPERROR_ALLOW_ALL — every HTTP response (including 429) comes
        # straight to parse_profile so we can handle it ourselves.
        "HTTPERROR_ALLOW_ALL": True,

        # Scrapy-level retries only for hard server errors.
        # 429 is handled manually: Zyte proxy on retry + backoff below.
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504],

        # Pace requests to the API — main pain point is HTTP 429.
        "DOWNLOAD_DELAY": 2.0,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 2.0,
        "AUTOTHROTTLE_MAX_DELAY": 60.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 0.5,
        "AUTOTHROTTLE_DEBUG": False,

        "DOWNLOAD_TIMEOUT": 60,
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
    }

    def __init__(self, csv_path=None, limit=None, *args, **kwargs):
        use_zyte_always = kwargs.pop("use_zyte_always", None)
        super().__init__(*args, **kwargs)
        _env_zyte = (os.environ.get("GRITT_USE_ZYTE_ALWAYS") or "").strip().lower()
        if use_zyte_always is not None or _env_zyte in ("1", "true", "yes", "on"):
            self.logger.warning(
                "use_zyte_always / GRITT_USE_ZYTE_ALWAYS are ignored — first request is always direct"
            )
        self.csv_path = csv_path or str(DEFAULT_INPUT_CSV)
        self.limit = int(limit) if limit not in (None, "") else None
        self._requested = 0
        self._parsed = 0
        self._errors = 0
        self._target_total = 0
        self._run_start_ts = 0.0
        self._company_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Request factory — direct when via_zyte=False; retries after errors use True.
    # ------------------------------------------------------------------

    def _build_request(
        self,
        api_url: str,
        profile_id: str,
        source_url: str,
        investor_seq_id: int,
        attempt: int = 0,
        via_zyte: bool = False,
    ) -> scrapy.Request:
        meta = {}
        # Only mark via_zyte in cb_kwargs when proxy is actually attached — avoids
        # stuck JSON/errback paths that think they are on Zyte while hitting origin direct.
        effective_via_zyte = False
        if via_zyte:
            proxy_url = _zyte_smart_proxy_url()
            if proxy_url:
                meta["proxy"] = proxy_url
                effective_via_zyte = True
            else:
                self.logger.warning(
                    "Zyte requested for %s but no API key — request goes direct (URL not skipped)",
                    profile_id,
                )

        return scrapy.Request(
            url=api_url,
            headers=DEFAULT_HEADERS,
            callback=self.parse_profile,
            errback=self.on_error,
            # Every CSV row is requested; same API URL may repeat — never dupe-filter.
            dont_filter=True,
            meta=meta,
            cb_kwargs={
                "profile_id": profile_id,
                "source_url": source_url,
                "api_url": api_url,
                # 1-based input URL index; stored as investor_id in exports (1 → first URL, 2 → second, …).
                "investor_seq_id": investor_seq_id,
                "attempt": attempt,
                "via_zyte": effective_via_zyte,
            },
        )

    # ------------------------------------------------------------------
    # start_requests
    # ------------------------------------------------------------------

    def start_requests(self):
        self._run_start_ts = time.monotonic()
        csv_file = Path(self.csv_path)
        if not csv_file.exists():
            fallback = FALLBACK_INPUT_CSV
            if csv_file.resolve() != fallback.resolve() and fallback.exists():
                self.logger.warning(
                    "CSV not found at %s, using fallback %s",
                    csv_file.resolve(),
                    fallback.resolve(),
                )
                csv_file = fallback
            else:
                self.logger.error("CSV not found: %s", csv_file.resolve())
                return

        urls = self._load_investor_urls(csv_file)
        if not urls:
            self.logger.warning("No investor URLs found in %s", csv_file)
            return

        # investor_id in output = 1-based index of this URL in the input list (1st URL → 1, 2nd → 2, …).
        self._target_total = 0
        for row_idx, investor_url in enumerate(urls, start=1):
            if self.limit is not None and self._target_total >= self.limit:
                break
            profile_id = _profile_id_for_url(investor_url, row_idx)
            if profile_id.startswith("_empty_url_row_") or profile_id.startswith(
                "_unparsed_row_"
            ):
                self.logger.warning(
                    "Weak profile id %r for row %d — still requesting: %s",
                    profile_id,
                    row_idx,
                    investor_url,
                )
            self._requested += 1
            self._target_total += 1
            api_url = f"{API_BASE}?{urlencode({'profile': profile_id})}"
            yield self._build_request(
                api_url,
                profile_id,
                investor_url,
                row_idx,
                attempt=0,
                via_zyte=False,
            )

        self.logger.info(
            "Queued %d profile requests (direct first; Zyte only on retry after error)",
            self._requested,
        )

    # ------------------------------------------------------------------
    # Retry helper — never gives up, just keeps increasing the delay
    # ------------------------------------------------------------------

    def _schedule_retry(
        self,
        profile_id: str,
        source_url: str,
        api_url: str,
        investor_seq_id: int,
        attempt: int,
        reason: str,
        via_zyte: bool = False,
        http_status: int | None = None,
        retry_after_sec: int | None = None,
    ):
        next_attempt = attempt + 1
        if retry_after_sec is not None and retry_after_sec > 0:
            delay = retry_after_sec
            delay_note = " (Retry-After)"
        elif http_status == 429:
            delay = _RETRY_DELAYS_429[min(next_attempt, len(_RETRY_DELAYS_429) - 1)]
            delay_note = " (429 backoff)"
        else:
            delay = _RETRY_DELAYS[min(next_attempt, len(_RETRY_DELAYS) - 1)]
            delay_note = ""
        via = "Zyte proxy" if via_zyte else "direct"
        self.logger.warning(
            "%s for %s (attempt %d) — retrying via %s in %ds%s",
            reason,
            profile_id,
            attempt,
            via,
            delay,
            delay_note,
        )
        if delay > 0:
            time.sleep(delay)
        yield self._build_request(
            api_url,
            profile_id,
            source_url,
            investor_seq_id,
            attempt=next_attempt,
            via_zyte=via_zyte,
        )

    # ------------------------------------------------------------------
    # Main callback — receives ALL HTTP responses (HTTPERROR_ALLOW_ALL)
    # ------------------------------------------------------------------

    def parse_profile(
        self,
        response,
        profile_id: str,
        source_url: str,
        api_url: str,
        investor_seq_id: int,
        attempt: int = 0,
        via_zyte: bool = False,
    ):
        # ----------------------------------------------------------------
        # Non-200 → retry via Zyte when a key exists (first hop was always direct).
        # Zyte tunnel/upstream failures retry direct once.
        # ----------------------------------------------------------------
        if response.status != 200:
            if via_zyte and response.status in (407, 502, 503, 504):
                use_zyte = False
                self.logger.warning(
                    "HTTP %s via Zyte for %s — retrying direct (URL not skipped)",
                    response.status,
                    profile_id,
                )
            else:
                use_zyte = True if _zyte_smart_proxy_url() else False
            ra = _parse_retry_after_sec(response) if response.status == 429 else None
            if response.status == 429 and ra:
                self.logger.info("HTTP 429 for %s — sleeping %ds per Retry-After", profile_id, ra)
            yield from self._schedule_retry(
                profile_id=profile_id,
                source_url=source_url,
                api_url=api_url,
                investor_seq_id=investor_seq_id,
                attempt=attempt,
                reason=f"HTTP {response.status}",
                via_zyte=use_zyte,
                http_status=response.status,
                retry_after_sec=ra,
            )
            return

        # ----------------------------------------------------------------
        # 200 OK — process normally
        # ----------------------------------------------------------------
        via_label = f" (Zyte)" if via_zyte else ""
        try:
            payload = response.json()
        except Exception as exc:
            # JSON parse failure — retry as well, it may be a fluke
            self.logger.error(
                "JSON parse failed for %s%s: %s — retrying", profile_id, via_label, exc
            )
            yield from self._schedule_retry(
                profile_id=profile_id,
                source_url=source_url,
                api_url=api_url,
                investor_seq_id=investor_seq_id,
                attempt=attempt,
                reason="JSON parse error",
                via_zyte=bool(_zyte_smart_proxy_url()),
            )
            return

        profile = payload.get("profile") if isinstance(payload, dict) else {}
        profile = profile if isinstance(profile, dict) else {}
        profile_pic = (
            profile.get("_getprofilepic")
            if isinstance(profile.get("_getprofilepic"), dict)
            else {}
        )
        experience = payload.get("experience") if isinstance(payload, dict) else []
        experience = experience if isinstance(experience, list) else []

        investor = ProfileMapper.build_investor(profile_pic)

        # firm_name / firm_role: `` at `` split (patterns 1–3) or parenthesis pattern (4) only.
        firm_role, firm_name = _parse_firm_from_investor_description(
            _clean_text(profile_pic.get("bio"))
        )

        # One export row per API `experience` entry (invest true or false), in list order.
        portfolio_experience = [e for e in experience if isinstance(e, dict)]
        companies = ProfileMapper.build_companies(
            portfolio_experience, company_cache=self._company_cache
        )
        investments = ProfileMapper.build_portfolio_investments(portfolio_experience)

        item = GrittInvestorProfileItem({
            "gritt_source_url": source_url,
            "profile_data": {
                "investor_details": investor,
                "portfolio_investment": investments,
                "portfolio_company_details": companies,
                "investor_metadata": {
                    "investor_id": investor_seq_id,
                    "firm_name": firm_name,
                    "firm_role": firm_role,
                },
            },
        })

        self._parsed += 1
        done = self._parsed + self._errors
        total = self._target_total or self._requested
        elapsed = max(0.0, time.monotonic() - self._run_start_ts)
        avg_sec = (elapsed / done) if done else 0.0
        remaining = max(total - done, 0)
        eta_sec = int(avg_sec * remaining)
        eta_min, eta_rem_sec = divmod(eta_sec, 60)
        self.logger.info(
            "%d profile scraped ✅%s | ETA full run: %02d:%02d (%d/%d)",
            self._parsed,
            via_label,
            eta_min,
            eta_rem_sec,
            done,
            total,
        )
        yield item

    # ------------------------------------------------------------------
    # Errback — network-level failures (timeout, DNS, connection refused)
    # These also retry forever — no URL is ever permanently dropped
    # ------------------------------------------------------------------

    def on_error(self, failure):
        request = failure.request
        cb = request.cb_kwargs or {}
        profile_id = cb.get("profile_id", request.url)
        attempt = cb.get("attempt", 0)
        api_url = cb.get("api_url", request.url)
        source_url = cb.get("source_url", "")
        investor_seq_id = cb.get("investor_seq_id", 0)
        via_zyte = cb.get("via_zyte", False)
        if via_zyte:
            self.logger.warning(
                "Network error via Zyte for %s — retrying direct (URL not skipped): %r",
                profile_id,
                failure,
            )
            next_via_zyte = False
        else:
            self.logger.error(
                "Network error for %s (attempt %d): %r — will retry via proxy if configured",
                profile_id,
                attempt,
                failure,
            )
            next_via_zyte = bool(_zyte_smart_proxy_url())
        yield from self._schedule_retry(
            profile_id=profile_id,
            source_url=source_url,
            api_url=api_url,
            investor_seq_id=investor_seq_id,
            attempt=attempt,
            reason="Network error",
            via_zyte=next_via_zyte,
        )

    # ------------------------------------------------------------------

    def close(self, reason):
        self.logger.info(
            "Done reason=%s requested=%d parsed=%d errors=%d",
            reason,
            self._requested,
            self._parsed,
            self._errors,
        )

    def _load_investor_urls(self, csv_file: Path) -> list[str]:
        out: list[str] = []
        with csv_file.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                return out

            header_l = [_clean_text(h).lower() for h in header]
            url_idx = None
            for candidate in ("investor_url", "url", "link"):
                if candidate in header_l:
                    url_idx = header_l.index(candidate)
                    break

            if url_idx is None:
                url_idx = 0
                first = _clean_text(header[url_idx]) if header else ""
                if first:
                    out.append(first)

            for row in reader:
                if not row or url_idx >= len(row):
                    continue
                value = _clean_text(row[url_idx])
                if value:
                    out.append(value)
        return out