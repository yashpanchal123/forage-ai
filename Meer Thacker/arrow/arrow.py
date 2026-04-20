import csv
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from lxml import html
import pandas as pd
import requests

EXCEL_FILE = r"C:\Users\Dell\OneDrive\Desktop\Crawl\arrow\20260409_Trial PN list_vShare.xlsx"
FOUND_CSV = r"C:\Users\Dell\OneDrive\Desktop\Crawl\arrow\arrow_found.csv"
NOT_FOUND_CSV = r"C:\Users\Dell\OneDrive\Desktop\Crawl\arrow\arrow_not_found.csv"
LOG_FILE = r"C:\Users\Dell\OneDrive\Desktop\Crawl\arrow\arrow_scraper.log"

MAX_WORKERS = 10
MAX_RETRIES = 2
PER_THREAD_DELAY_SEC = 0.35
HTTP_MAX_RETRIES = 3
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

HEADERS_AUTOCOMPLETE = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-GB,en;q=0.9",
    "priority": "u=1, i",
    "referer": "https://www.arrow.com/en/products/10117743.html",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}

# Optional proxy (keep empty to disable)
PROXY_HOST = "proxy.zyte.com"
PROXY_PORT = "8011"
PROXY_USER = "7916eb9714394ae9a160c862c3e3da93"
PROXY_PASS = ""

USE_PROXY = bool(PROXY_HOST and PROXY_PORT and PROXY_USER and PROXY_PASS)
PROXIES = (
    {
        "http": f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
        "https": f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}",
    }
    if USE_PROXY
    else None
)

PRINT_LOCK = threading.Lock()
LOGGER = logging.getLogger("arrow_scraper")


def tprint(*args, **kwargs):
    with PRINT_LOCK:
        print(*args, **kwargs)


def setup_logging():
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)


def log_part(index, total, part_number, message):
    LOGGER.info("[%s/%s] %s | %s", index, total, part_number, message)


def normalize_part_number(value):
    """
    Normalize part numbers from Excel/API for reliable matching.
    Examples: '35068218.0' -> '35068218', ' 3506-8218 ' -> '35068218'
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return re.sub(r"[\s\-_/]", "", text).upper()


def clean_text(value):
    """
    Clean mojibake/unwanted characters before CSV write.
    Example: 'MX64â„¢' -> 'MX64'
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return text

    # Try common mojibake repair (UTF-8 mis-decoded as Latin-1/CP1252).
    if any(token in text for token in ("Ã", "â", "Â")):
        for source_encoding in ("latin-1", "cp1252"):
            try:
                repaired = text.encode(source_encoding).decode("utf-8")
                if repaired:
                    text = repaired
                    break
            except Exception:
                continue

    replacements = {
        "â„¢": "",
        "™": "",
        "Â": "",
        "\u00ad": "",  # soft hyphen
        "\ufeff": "",  # BOM
        "â€“": "-",
        "â€”": "-",
        "â€˜": "'",
        "â€™": "'",
        "â€œ": '"',
        "â€\x9d": '"',
        "Ã©": "e",
        "Ã¨": "e",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)

    # Remove hidden control chars except standard whitespace.
    text = "".join(ch for ch in text if (ord(ch) >= 32 or ch in "\t\n\r"))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_row_for_csv(row):
    cleaned = {}
    for key, value in row.items():
        if isinstance(value, str):
            cleaned[key] = clean_text(value)
        else:
            cleaned[key] = value
    return cleaned


def to_int(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    cleaned = re.sub(r"[^\d]", "", str(value))
    return int(cleaned) if cleaned else None


def to_float(value):
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return float(value)
    cleaned = re.sub(r"[^\d.]", "", str(value))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def find_first_int_recursive(node, keys):
    if isinstance(node, dict):
        for key in keys:
            if key in node:
                parsed = to_int(node.get(key))
                if parsed is not None:
                    return parsed
        for child in node.values():
            found = find_first_int_recursive(child, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_first_int_recursive(child, keys)
            if found is not None:
                return found
    return None


def collect_price_bands_recursive(node, collector):
    if isinstance(node, dict):
        qty = to_int(
            node.get("minQty")
            or node.get("quantity")
            or node.get("qty")
            or node.get("breakQty")
            or node.get("orderQty")
        )
        price = to_float(
            node.get("unitPrice")
            or node.get("price")
            or node.get("resalePrice")
            or node.get("displayPrice")
            or node.get("unitListPrice")
        )
        if qty is not None and price is not None:
            collector.append({"quantity": qty, "price_usd": price})

        for child in node.values():
            collect_price_bands_recursive(child, collector)
    elif isinstance(node, list):
        for child in node:
            collect_price_bands_recursive(child, collector)


def read_excel_rows(path):
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    shutil.copy2(path, tmp.name)
    df = pd.read_excel(tmp.name, dtype=str)
    os.unlink(tmp.name)

    df.columns = df.columns.str.strip()
    if "Cleaned Part Number" not in df.columns:
        raise ValueError("Column 'Cleaned Part Number' not found in Excel.")

    df["Cleaned Part Number"] = df["Cleaned Part Number"].apply(normalize_part_number)
    df = df[df["Cleaned Part Number"].notna() & (df["Cleaned Part Number"] != "")]
    return df


def request_with_retry(url, *, method="GET", params=None, headers=None, timeout=20):
    """
    Retry wrapper for transient network failures and retryable HTTP status codes.
    """
    last_exc = None
    for attempt in range(HTTP_MAX_RETRIES + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                params=params,
                headers=headers,
                timeout=timeout,
                proxies=PROXIES,
                verify=False,
            )
            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt < HTTP_MAX_RETRIES:
                    backoff = (2 ** attempt) + 0.2
                    time.sleep(backoff)
                    continue
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < HTTP_MAX_RETRIES:
                backoff = (2 ** attempt) + 0.2
                time.sleep(backoff)
                continue
            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("Request failed after retry attempts.")


def get_mfr_seo(part_number):
    url = "https://www.arrow.com/experienceservices/search/autoComplete"
    params = {"q": part_number, "lang": "en", "currency": "USD"}
    response = request_with_retry(
        url,
        method="GET",
        params=params,
        headers=HEADERS_AUTOCOMPLETE,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    products = payload.get("autoCompleteTypes", {}).get("product", [])
    LOGGER.info("Autocomplete for %s returned %s product candidates", part_number, len(products))
    # If proxy is enabled and returns an empty product list, retry once directly.
    if not products and USE_PROXY:
        LOGGER.info("Proxy returned empty autocomplete for %s, retrying direct request", part_number)
        response = requests.get(
            url,
            params=params,
            headers=HEADERS_AUTOCOMPLETE,
            timeout=20,
            verify=False,
        )
        response.raise_for_status()
        payload = response.json()
        products = payload.get("autoCompleteTypes", {}).get("product", [])
        LOGGER.info("Direct retry autocomplete for %s returned %s product candidates", part_number, len(products))
    if not products:
        return None, None, None

    part_number_norm = normalize_part_number(part_number)
    for product in products:
        product_id = str(product.get("fullPart", "")).strip()
        if normalize_part_number(product_id) == part_number_norm:
            part_seo = str(product.get("partSeo", "")).strip()
            LOGGER.info(
                "Matched part %s with Arrow fullPart=%s partSeo=%s mfrSeo=%s",
                part_number, product_id, part_seo, product.get("mfrSeo")
            )
            return product.get("mfrSeo"), product_id, part_seo
    LOGGER.info("No matching fullPart found in autocomplete list for %s", part_number)
    return None, None, None


def get_initial_state(page_html):
    content = html.fromstring(page_html)
    json_text = ''.join(content.xpath('//script[@id="__INITIAL_STATE__"]/text()'))
    if not json_text:
        return None
    return json.loads(json_text)


def get_ssr_props(state):
    if not state:
        return None
    items = state.get("rootModel", {}).get(":items", {})
    grid = items.get("root", {}).get(":items", {}).get("responsivegrid", {}).get(":items", {})
    for key, value in grid.items():
        if "productdetail" in key and isinstance(value, dict):
            ssr = value.get("ssrProps")
            if isinstance(ssr, dict):
                return ssr

    # Fallback: search recursively if Arrow changes component key path.
    def find_ssr(node):
        if isinstance(node, dict):
            ssr = node.get("ssrProps")
            if isinstance(ssr, dict) and (ssr.get("fullPart") or ssr.get("mfrName")):
                return ssr
            for child in node.values():
                found = find_ssr(child)
                if found:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = find_ssr(child)
                if found:
                    return found
        return None

    return find_ssr(state)


def get_productdetail_node(state):
    """
    Returns the productdetail_* node under:
    rootModel.:items.root.:items.responsivegrid.:items.productdetail_*
    """
    if not state:
        return None
    items = state.get("rootModel", {}).get(":items", {})
    grid = items.get("root", {}).get(":items", {}).get("responsivegrid", {}).get(":items", {})
    for key, value in grid.items():
        if "productdetail" in key and isinstance(value, dict):
            return value
    return None


def extract_technical_specs(ssr_props):
    specs = {}
    for feature in ssr_props.get("features", []):
        name = str(feature.get("name", "")).strip()
        if not name:
            continue
        value = str(feature.get("value", "")).strip()
        uom = str(feature.get("uom", "")).strip()
        specs[name] = f"{value} {uom}".strip() if uom else value
    return specs


def extract_price_bands(ssr_props):
    price_bands = []

    # Primary source: list of pricing tiers in SSR payload
    tiers = ssr_props.get("resaleList", []) or ssr_props.get("priceBreaks", [])
    for tier in tiers:
        qty = to_int(tier.get("minQty") or tier.get("quantity") or tier.get("qty"))
        price = to_float(
            tier.get("unitPrice")
            or tier.get("price")
            or tier.get("resalePrice")
            or tier.get("displayPrice")
        )
        if qty is not None and price is not None:
            price_bands.append({"quantity": qty, "price_usd": price})

    # Fallback: lowest price if no band list available
    if not price_bands:
        lowest = to_float(ssr_props.get("lowestPrice"))
        min_qty = to_int(
            ssr_props.get("minOrderQty")
            or ssr_props.get("minimumOrderQuantity")
            or ssr_props.get("multOrderQty")
        )
        if lowest is not None and min_qty is not None:
            price_bands.append({"quantity": min_qty, "price_usd": lowest})

    price_bands = sorted(price_bands, key=lambda x: x["quantity"])
    unique = []
    seen = set()
    for row in price_bands:
        key = (row["quantity"], row["price_usd"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def extract_stock_status(productdetail_node, ssr_props, html_text):
    # Preferred source (updated path):
    # rootModel.:items.root.:items.responsivegrid.:items.productdetail_*.ssrProps
    # .groupByPrice.regionalBuyingOptions[0].availabilityMessage
    availability_message = ""
    if isinstance(productdetail_node, dict):
        availability_message = (
            productdetail_node.get("ssrProps", {})
            .get("groupByPrice", {})
            .get("regionalBuyingOptions", [{}])[0]
            .get("availabilityMessage", "")
        )
        availability_message = str(availability_message).strip()

    if availability_message:
        msg = availability_message.lower()
        if "no stock available" in msg or "out of stock" in msg:
            return "Out of Stock"
        if "in stock" in msg:
            return "In Stock"
        return availability_message

    # Legacy fallback path
    label = ""
    if isinstance(productdetail_node, dict):
        label = str(productdetail_node.get("outOfStockLabel", "")).strip()
    if label:
        if "out of stock" in label.lower():
            return "Out of Stock"
        if "in stock" in label.lower():
            return "In Stock"
        return label

    candidates = [
        ssr_props.get("stockStatus"),
        ssr_props.get("inventoryStatus"),
        ssr_props.get("availabilityStatus"),
    ]
    for value in candidates:
        if value:
            return str(value).strip()

    lowered = html_text.lower()
    if "in stock" in lowered:
        return "In Stock"
    if "out of stock" in lowered:
        return "Out of Stock"
    return ""


def extract_minimum_quantity(productdetail_node, ssr_props, state, html_text):
    # Preferred source (user-provided path):
    # rootModel.:items.root.:items.responsivegrid.:items.productdetail_*.ssrProps
    # .groupByPrice.regionalBuyingOptions[0].buyingOptions[0].orderMinimumQuantity
    if isinstance(productdetail_node, dict):
        qty = (
            productdetail_node.get("ssrProps", {})
            .get("groupByPrice", {})
            .get("regionalBuyingOptions", [{}])[0]
            .get("buyingOptions", [{}])[0]
            .get("orderMinimumQuantity")
        )
        qty = to_int(qty)
        if qty is not None:
            return qty

    ssr_min = to_int(
        ssr_props.get("minOrderQty")
        or ssr_props.get("minimumOrderQuantity")
        or ssr_props.get("multOrderQty")
        or ssr_props.get("orderMultiple")
    )
    if ssr_min is not None:
        return ssr_min

    state_min = find_first_int_recursive(
        state,
        keys={"minOrderQty", "minimumOrderQuantity", "multOrderQty", "orderMultiple", "minimum"},
    )
    if state_min is not None:
        return state_min

    min_match = re.search(r"Minimum\s*:\s*([\d,]+)", html_text, re.IGNORECASE)
    if min_match:
        return to_int(min_match.group(1))
    return None


def extract_total_stock_quantity(productdetail_node, ssr_props):
    """
    Preferred path:
    rootModel.:items.root.:items.responsivegrid.:items.productdetail_*.ssrProps.groupByPrice.totalStock
    """
    if isinstance(productdetail_node, dict):
        total_stock = (
            productdetail_node.get("ssrProps", {})
            .get("groupByPrice", {})
            .get("totalStock")
        )
        parsed = to_int(total_stock)
        if parsed is not None:
            return parsed

    # Fallback
    return to_int(ssr_props.get("totalStock"))


def extract_product_image(productdetail_node, ssr_props):
    """
    Preferred path:
    rootModel.:items.root.:items.responsivegrid.:items.productdetail_*.ssrProps.primaryImage.medias[2].url
    """
    medias = (
        productdetail_node.get("ssrProps", {})
        .get("primaryImage", {})
        .get("medias", [])
        if isinstance(productdetail_node, dict)
        else []
    )
    def normalize_image_url(url_value):
        url_value = str(url_value or "").strip()
        if not url_value:
            return ""
        if url_value.startswith("//"):
            return f"https:{url_value}"
        if url_value.startswith("/"):
            return f"https://www.arrow.com{url_value}"
        if url_value.startswith("http://") or url_value.startswith("https://"):
            return url_value
        return f"https:{url_value}"

    if isinstance(medias, list):
        if len(medias) > 2 and isinstance(medias[2], dict):
            url = normalize_image_url(medias[2].get("url", ""))
            if url:
                return url
        for media in medias:
            if isinstance(media, dict):
                url = normalize_image_url(media.get("url", ""))
                if url:
                    return url

    # Fallback
    medias = ssr_props.get("primaryImage", {}).get("medias", [])
    if isinstance(medias, list):
        for media in medias:
            if isinstance(media, dict):
                url = normalize_image_url(media.get("url", ""))
                if url:
                    return url
    return ""


def extract_product_category(productdetail_node, ssr_props):
    """
    Preferred path:
    rootModel.:items.root.:items.responsivegrid.:items.productdetail_*.ssrProps
    .taxonomy[0].categories[0].subCategories[0].subCategories[0].name
    """
    taxonomy = (
        productdetail_node.get("ssrProps", {}).get("taxonomy", [])
        if isinstance(productdetail_node, dict)
        else []
    )
    try:
        name = taxonomy[0]["categories"][0]["subCategories"][0]["subCategories"][0]["name"]
        if name:
            return str(name).strip()
    except Exception:
        pass

    # Fallback to last leaf in taxonomy
    taxonomy = ssr_props.get("taxonomy", [])
    try:
        cats = taxonomy[0]["categories"]
    except Exception:
        return ""

    leaf_name = ""
    stack = list(cats) if isinstance(cats, list) else []
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "")).strip()
        if node_name:
            leaf_name = node_name
        subs = node.get("subCategories", [])
        if isinstance(subs, list) and subs:
            stack = subs + stack
    return leaf_name


def extract_price_bands_enhanced(productdetail_node, ssr_props, state):
    # Preferred source (user-provided path):
    # rootModel.:items.root.:items.responsivegrid.:items.productdetail_*.ssrProps
    # .groupByPrice.regionalBuyingOptions[0].buyingOptions[0].priceBands
    if isinstance(productdetail_node, dict):
        exact_bands = (
            productdetail_node.get("ssrProps", {})
            .get("groupByPrice", {})
            .get("regionalBuyingOptions", [{}])[0]
            .get("buyingOptions", [{}])[0]
            .get("priceBands", [])
        )
        # Requested output format:
        # {4999: 0.3339, 5999: 0.3306, 50000: 0.3112}
        # key uses fromQuantity when present, otherwise fromQuantity.
        normalized_map = {}
        for band in exact_bands:
            qty = to_int(band.get("fromQuantity"))
            if qty is None:
                qty = to_int(
                    band.get("fromQuantity")
                    or band.get("quantity")
                    or band.get("minQty")
                    or band.get("qty")
                    or band.get("breakQty")
                )
            price = to_float(
                band.get("price")
                or band.get("unitPrice")
                or band.get("resalePrice")
                or band.get("displayPrice")
                or band.get("amount")
            )
            if qty is not None and price is not None:
                normalized_map[qty] = price
        if normalized_map:
            return dict(sorted(normalized_map.items(), key=lambda kv: kv[0]))

    price_bands = extract_price_bands(ssr_props)
    if price_bands:
        return {int(x["quantity"]): float(x["price_usd"]) for x in price_bands}

    collected = []
    collect_price_bands_recursive(state, collected)
    collected = sorted(collected, key=lambda x: (x["quantity"], x["price_usd"]))
    unique = []
    seen = set()
    for row in collected:
        key = (row["quantity"], row["price_usd"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return {int(x["quantity"]): float(x["price_usd"]) for x in unique}


def scrape_product(part_number):
    LOGGER.info("Starting scrape for part %s", part_number)
    mfr_result = get_mfr_seo(part_number)
    if not isinstance(mfr_result, tuple) or len(mfr_result) != 3:
        LOGGER.info("Part %s NOT FOUND: invalid autocomplete return shape %r", part_number, mfr_result)
        return {
            "input_part_number": part_number,
            "status": "NOT FOUND",
            "url": "",
            "error": "Invalid autocomplete return format",
        }
    mfr_seo, resolved_part, part_seo = mfr_result
    if not mfr_seo or not resolved_part:
        LOGGER.info("Part %s NOT FOUND: autocomplete could not resolve exact part", part_number)
        return {
            "input_part_number": part_number,
            "status": "NOT FOUND",
            "url": "",
            "error": "Not found in autocomplete",
        }

    url_candidates = []
    if part_seo:
        url_candidates.append(f"https://www.arrow.com/en/products/{part_seo}/{mfr_seo}.html")
    url_candidates.append(f"https://www.arrow.com/en/products/{resolved_part}/{mfr_seo}.html")

    page_headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-GB,en-US;q=0.9,en;q=0.8,hi;q=0.7,nl;q=0.6',
        'priority': 'u=0, i',
        'referer': 'https://www.arrow.com/',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
    }
    response = None
    url = ""
    state = None
    productdetail_node = None
    ssr_props = None
    for candidate_url in url_candidates:
        local_headers = dict(page_headers)
        local_headers["referer"] = candidate_url
        try:
            candidate_response = request_with_retry(
                candidate_url,
                method="GET",
                headers=local_headers,
                timeout=25,
            )
            if candidate_response.status_code >= 400:
                LOGGER.info("Candidate URL failed HTTP %s: %s", candidate_response.status_code, candidate_url)
                continue
            candidate_state = get_initial_state(candidate_response.text)
            candidate_productdetail_node = get_productdetail_node(candidate_state)
            candidate_ssr = get_ssr_props(candidate_state)
            if candidate_ssr:
                response = candidate_response
                url = candidate_url
                state = candidate_state
                productdetail_node = candidate_productdetail_node
                ssr_props = candidate_ssr
                break
            LOGGER.info("Candidate URL had no ssrProps: %s", candidate_url)
        except Exception as exc:
            LOGGER.info("Candidate URL error for %s: %s", candidate_url, exc)

    if response is None or not url:
        LOGGER.info("Part %s NOT FOUND: no valid product page from candidates", part_number)
        return {
            "input_part_number": part_number,
            "status": "NOT FOUND",
            "url": url_candidates[0] if url_candidates else "",
            "error": "Could not resolve valid product page from candidate URLs",
        }

    technical_specs = extract_technical_specs(ssr_props)
    stock_status = extract_stock_status(productdetail_node, ssr_props, response.text)
    minimum_quantity = extract_minimum_quantity(productdetail_node, ssr_props, state, response.text)
    stock_quantity = extract_total_stock_quantity(productdetail_node, ssr_props)
    product_image = extract_product_image(productdetail_node, ssr_props)
    product_category = extract_product_category(productdetail_node, ssr_props)
    price_bands = extract_price_bands_enhanced(productdetail_node, ssr_props, state)

    return {
        "input_part_number": part_number,
        "Arrow_partnumber": ssr_props.get("fullPart", resolved_part),
        "product_url": url,
        "manufacturer": ssr_props.get("mfrName", ""),
        "product_name": ssr_props.get("storedPartDescription", "")
        or ssr_props.get("externalPartDescription", ""),
        "Product_category": product_category,
        "stock_status": stock_status,
        "Stock_Quantity": stock_quantity if stock_quantity is not None else "",
        "minimum_quantity": minimum_quantity if minimum_quantity is not None else "",
        "product_image": product_image,
        "technical_specifications": json.dumps(technical_specs, ensure_ascii=False),
        "price_bands": json.dumps(price_bands, ensure_ascii=False),
        "status": "FOUND",
        "error": "",
    }


def process_part(index, total, part_number):
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            log_part(index, total, part_number, f"Attempt {attempt + 1} started")
            # part_number = 35068218
            row = scrape_product(part_number)
            if row.get("status") == "FOUND":
                log_part(index, total, part_number, "FOUND")
            else:
                log_part(index, total, part_number, f"NOT FOUND ({row.get('error', '')})")
            tprint(f"[{index}/{total}] {part_number} -> {row.get('status')}")
            time.sleep(PER_THREAD_DELAY_SEC)
            return row
        except Exception as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                log_part(index, total, part_number, f"ERROR on attempt {attempt + 1}: {last_error}. Retrying.")
                time.sleep(1.5)
                continue
            log_part(index, total, part_number, f"ERROR final: {last_error}")
            tprint(f"[{index}/{total}] {part_number} -> ERROR: {last_error}")

    return {
        "input_part_number": part_number,
        "status": "NOT FOUND",
        "url": "",
        "error": last_error or "Unknown error",
    }


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(clean_row_for_csv(row))


def main():
    requests.packages.urllib3.disable_warnings()
    setup_logging()
    LOGGER.info("Arrow scraper started")
    df = read_excel_rows(EXCEL_FILE)
    part_numbers = df["Cleaned Part Number"].dropna().astype(str).str.strip().tolist()
    part_numbers = [pn for pn in part_numbers if pn]
    total = len(part_numbers)

    print(f"Loaded {total} cleaned part numbers from Excel.")
    print(f"Using {MAX_WORKERS} threads.")
    LOGGER.info("Loaded %s cleaned part numbers from Excel. Using %s threads.", total, MAX_WORKERS)

    found_rows = []
    not_found_rows = []
    results_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {
            executor.submit(process_part, idx, total, pn): pn
            for idx, pn in enumerate(part_numbers, start=1)
        }
        for future in as_completed(future_map):
            row = future.result()
            with results_lock:
                if row.get("status") == "FOUND":
                    found_rows.append(row)
                else:
                    not_found_rows.append(row)

    found_rows.sort(key=lambda r: r.get("input_part_number", ""))
    not_found_rows.sort(key=lambda r: r.get("input_part_number", ""))

    found_fields = [
        "input_part_number",
        "Arrow_partnumber",
        "manufacturer",
        "product_name",
        "Product_category",
        "product_url",
        "stock_status",
        "Stock_Quantity",
        "minimum_quantity",
        "product_image",
        "technical_specifications",
        "price_bands",
        "status",
        "error",

    ]
    not_found_fields = ["input_part_number", "status", "url", "error"]

    write_csv(FOUND_CSV, found_rows, found_fields)
    write_csv(NOT_FOUND_CSV, not_found_rows, not_found_fields)

    print(f"Done. Found: {len(found_rows)} | Not found/errors: {len(not_found_rows)}")
    print(f"Found CSV: {FOUND_CSV}")
    print(f"Not Found CSV: {NOT_FOUND_CSV}")
    LOGGER.info("Run complete. Found=%s NotFoundOrErrors=%s", len(found_rows), len(not_found_rows))
    LOGGER.info("Found CSV: %s", FOUND_CSV)
    LOGGER.info("Not Found CSV: %s", NOT_FOUND_CSV)
    LOGGER.info("Log file: %s", LOG_FILE)


if __name__ == "__main__":
    main()
