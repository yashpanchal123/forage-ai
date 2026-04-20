import requests
from bs4 import BeautifulSoup
import pandas as pd
import csv
import json
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────

INPUT_EXCEL = r"C:\Users\Dell\OneDrive\Desktop\Crawl\TTI_family\20260409_Trial PN list_vShare.xlsx"

COL_PART_NUMBER  = "Cleaned Part Number"
COL_SUPPLIER     = "Supplier"

OUTPUT_FOUND     = r"C:\Users\Dell\OneDrive\Desktop\Crawl\TTI_family\tti_parts_found.csv"
OUTPUT_NOT_FOUND = r"C:\Users\Dell\OneDrive\Desktop\Crawl\TTI_family\tti_parts_not_found.csv"

MAX_THREADS = 10
RETRY_COUNT = 3

BASE_URL = "https://www.tti.com/content/ttiinc/en/apps/part-detail.html"

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "upgrade-insecure-requests": "1",
}

proxy_host = "proxy.zyte.com"
proxy_port = "8011"
proxy_user = "7916eb9714394ae9a160c862c3e3da93"
proxy_pass = ""

proxies = {
    "http":  f"http://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}",
    "https": f"http://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}",
}

FOUND_FIELDS = [
    "Product ID",
    "Product URL",
    "Description",
    "Manufacturer",
    "Mfr Part",
    "TTI Part",
    "Specifications",
    "Export and Environmental Classification",
    "Unit Price",
    "Pricing Table",
    "Minimum Quantity",
    "Stock",
    "Stock Quantity",   # ← NEW
    "Sales Phone",
    "Sales Email",
]

NOT_FOUND_FIELDS = [
    "Cleaned Part Number",
    "Supplier",
    "Reason",
]


# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────

def load_parts_from_excel(path: str) -> list[dict]:
    df = pd.read_excel(path, dtype=str)
    df.columns = df.columns.str.strip()
    df[COL_PART_NUMBER] = df[COL_PART_NUMBER].str.strip()
    df[COL_SUPPLIER]    = df[COL_SUPPLIER].str.strip()

    parts = []
    for _, row in df.iterrows():
        part_number = row.get(COL_PART_NUMBER, "")
        supplier    = row.get(COL_SUPPLIER, "")
        if pd.isna(part_number) or str(part_number).strip() == "":
            continue
        parts.append({
            "partsNumber":  str(part_number).strip(),
            "mfgShortname": str(supplier).strip() if pd.notna(supplier) else "",
        })
    return parts


def build_url(parts_number: str, mfg_shortname: str) -> str:
    return (
        f"{BASE_URL}"
        f"?partsNumber={parts_number}"
        f"&mfgShortname={mfg_shortname}"
        f"&autoRedirect=true"
    )


def fetch_page(url: str) -> str | None:
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            response = requests.get(
                url, headers=HEADERS, proxies=proxies, verify=False, timeout=30
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            print(f"  [ERROR] Fetch failed (attempt {attempt}/{RETRY_COUNT}): {e}")
    return None


def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    output_dir = os.path.dirname(path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def extract_initial_state(html: str) -> dict:
    match = re.search(r"var initial_state\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def extract_specifications(soup: BeautifulSoup) -> dict:
    specs = {}
    spec_section = soup.find("div", class_=re.compile(r"specifications", re.I))
    if not spec_section:
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            if "Description" in headers and "Product Attribute" in headers:
                spec_section = table
                break

    if spec_section:
        for row in spec_section.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) >= 2:
                key   = cols[0].get_text(strip=True)
                value = cols[1].get_text(strip=True)
                if key:
                    specs[key] = value
    return specs


def extract_export_env(soup: BeautifulSoup) -> dict:
    """
    Extract Export & Environmental Classification table.
    Also cleans up concatenated Yes/No + label strings, e.g.:
      'YesRoHS Compliant'  -> 'Yes (RoHS Compliant)'
      'NoLead Free'        -> 'No (Lead Free)'
    """
    export_data = {}

    h2_tag = soup.find("h2", string=lambda text: text and "Classification" in text)
    if not h2_tag:
        h2_tag = next(
            (tag for tag in soup.find_all("h2") if "Classification" in tag.get_text()),
            None,
        )
    if not h2_tag:
        return export_data

    target_table = None
    for sibling in h2_tag.find_next_siblings():
        if sibling.name == "table":
            target_table = sibling
            break

    if not target_table:
        return export_data

    tbody = target_table.find("tbody") or target_table
    for row in tbody.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) >= 2:
            key   = cols[0].get_text(strip=True)
            value = cols[1].get_text(strip=True)
            if key:
                export_data[key] = _format_yes_no_value(value)

    return export_data


def _format_yes_no_value(raw: str) -> str:
    """
    Cleans up values where Yes/No is directly concatenated with a label.

    Examples:
        'YesRoHS Compliant'  -> 'Yes (RoHS Compliant)'
        'NoLead Free'        -> 'No (Lead Free)'
        'Yes'                -> 'Yes'
        'No'                 -> 'No'
        'ECCN: EAR99'        -> 'ECCN: EAR99'   (unchanged)
    """
    # Match "Yes" or "No" at the start, followed immediately by an uppercase letter
    # (i.e. no space between the boolean and the label)
    match = re.match(r'^(Yes|No)([A-Z].*)$', raw)
    if match:
        boolean_part = match.group(1)   # "Yes" or "No"
        label_part   = match.group(2)   # "RoHS Compliant", "Lead Free", etc.
        return f"{boolean_part} ({label_part})"
    return raw


def extract_sales_contact(soup: BeautifulSoup) -> tuple[str, str]:
    """
    Extracts sales phone number and email from contact divs.
    XPath equivalent: //div[contains(@class,'contact')]/a

    Returns:
        (phone, email) — each defaults to "" if not found.
    """
    phone = ""
    email = ""

    contact_divs = soup.find_all("div", class_=re.compile(r"contact", re.I))

    for div in contact_divs:
        anchors = div.find_all("a")
        for a in anchors:
            href = a.get("href", "").strip()
            text = a.get_text(strip=True)

            if href.lower().startswith("tel:") and not phone:
                phone = href[4:].strip() or text
            elif href.lower().startswith("mailto:") and not email:
                email = href[7:].strip() or text
            else:
                if not phone and re.match(r"[\+\d][\d\s\-\(\)\.]{6,}", text):
                    phone = text
                if not email and re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                    email = text

    return phone, email


def extract_min_quantity(soup: BeautifulSoup) -> str:
    def normalize_min(raw: str) -> str:
        m = re.search(r"([0-9][0-9,]*)", raw or "")
        return m.group(1).strip() if m else "N/A"

    pattern = re.compile(r"Minimum\s*[:\-]\s*([0-9][0-9,]*)", re.I)

    for text_node in soup.find_all(string=pattern):
        m = pattern.search(text_node)
        if m:
            return normalize_min(m.group(1).strip())

    for cell in soup.find_all(["th", "td"]):
        if re.match(r"^\s*Minimum\s*$", cell.get_text(), re.I):
            sibling = cell.find_next_sibling(["th", "td"])
            if sibling:
                raw = sibling.get_text(strip=True)
                if raw:
                    return normalize_min(raw)

    return "N/A"


def parse_numeric_quantity(raw: str) -> int | None:
    match = re.search(r"([0-9][0-9,]*)", raw or "")
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def parse_numeric_price(raw: str) -> float | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw.replace(",", "") if raw else "")
    if not match:
        return None
    return float(match.group(1))


def extract_pricing_table(soup: BeautifulSoup) -> list[dict]:
    """
    Returns pricing as a list of dicts in the format:
        [{"quantity": 2000, "price_usd": 0.6844}, ...]

    This replaces the old dict[int, float] return type.
    """
    pricing: list[dict] = []

    table = soup.find("table", class_=re.compile(r"quantity-price-table", re.I))

    if not table:
        for tbl in soup.find_all("table"):
            ths = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
            has_qty   = any("quantity" in h for h in ths)
            has_price = any(("unit" in h and "price" in h) or h == "unit price" for h in ths)
            if has_qty and has_price:
                table = tbl
                break

    if not table:
        return pricing

    qty_idx   = 0
    price_idx = 1

    header_row = table.find("tr")
    if header_row:
        ths = header_row.find_all(["th", "td"])
        for i, th in enumerate(ths):
            text = th.get_text(strip=True).lower()
            if "quantity" in text:
                qty_idx = i
            elif "unit price" in text or (
                "price" in text and "ext" not in text and "extended" not in text
            ):
                price_idx = i

    tbody = table.find("tbody") or table
    for row in tbody.find_all("tr"):
        cols = row.find_all(["td", "th"])
        if len(cols) <= max(qty_idx, price_idx):
            continue

        qty_raw   = cols[qty_idx].get_text(strip=True)
        price_raw = cols[price_idx].get_text(strip=True)

        if re.search(r"[a-zA-Z]{3,}", qty_raw):
            continue

        qty_num   = parse_numeric_quantity(qty_raw)
        price_num = parse_numeric_price(price_raw)
        if qty_num is not None and price_num is not None:
            pricing.append({"quantity": qty_num, "price_usd": price_num})

    return pricing


def _to_int_quantity(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        qty = parse_numeric_quantity(value)
        return int(qty) if qty is not None else None
    return None


def _extract_stock_quantity_from_detail(detail: dict) -> int | None:
    """
    Extract stock quantity from structured JSON fields.
    Uses explicit stock/inventory keys and ignores minimum/pricing fields.
    """
    if not isinstance(detail, dict):
        return None

    candidate_keys = [
        "quantityAvailable",
        "availableQuantity",
        "qtyAvailable",
        "stockQuantity",
        "inStockQuantity",
        "inventoryQuantity",
        "quantityOnHand",
        "availableToSell",
        "nettableInventory",
    ]

    candidates: list[int] = []

    # Explicit keys first
    for key in candidate_keys:
        if key in detail:
            qty = _to_int_quantity(detail.get(key))
            if qty is not None:
                candidates.append(qty)

    # Generic scan for stock-like keys in nested dicts
    def collect_from_dict(data: dict):
        for k, v in data.items():
            key = str(k).lower()

            # Skip pricing/minimum style fields to avoid values like 29
            if any(skip in key for skip in ["min", "minimum", "price", "break", "tier"]):
                continue

            looks_stock_key = any(token in key for token in ["stock", "invent", "available", "qty", "quantity"])
            if looks_stock_key:
                qty = _to_int_quantity(v)
                if qty is not None:
                    candidates.append(qty)

            if isinstance(v, dict):
                collect_from_dict(v)

    collect_from_dict(detail)

    positives = [q for q in candidates if q > 0]
    if positives:
        return max(positives)
    if candidates:
        return 0
    return None


def extract_stock_status(soup: BeautifulSoup, detail: dict) -> tuple[str, str]:
    """
    Returns (stock_status_string, stock_quantity_string).

    stock_quantity_string is the numeric quantity (e.g. "41080") when
    the item is in stock, or "" otherwise.
    """
    lead_time = str(detail.get("additionalStockLeadTime", "") or "").strip()
    no_parts_found_message = str(detail.get("noPartsFoundMessage", "") or "").strip().lower()

    # ── Primary path: structured data has stock quantity ───────────────────
    qty_from_detail = _extract_stock_quantity_from_detail(detail)
    if qty_from_detail is not None:
        qty_int = int(qty_from_detail)
        if qty_int > 0:
            return "In Stock", str(qty_int)
        if lead_time:
            return f"Out of Stock (Lead Time: {lead_time})", ""
        return "Out of Stock", ""

    # ── Not Available Online ──────────────────────────────────────────────
    if "not available online" in no_parts_found_message:
        return "Not Available Online", ""

    # ── Fallback: scrape page text ────────────────────────────────────────
    page_text = " ".join(soup.stripped_strings).lower()

    # Try to find a quantity figure near "in stock" text in the HTML
    stock_qty_str = _scrape_stock_quantity(soup)

    if "out of stock" in page_text:
        status = f"Out of Stock (Lead Time: {lead_time})" if lead_time else "Out of Stock"
        return status, ""
    if "in stock" in page_text:
        return "In Stock", stock_qty_str
    if lead_time:
        return f"Out of Stock (Lead Time: {lead_time})", ""

    return "Unknown", ""


def _scrape_stock_quantity(soup: BeautifulSoup) -> str:
    """
    Attempts to scrape the displayed stock quantity from the page HTML.

    Looks for patterns like:
      - An element whose text is a number near an 'in stock' sibling
      - A data attribute containing the quantity
      - Common class names used by TTI for stock display

    Returns the quantity as a plain string (e.g. "41080"), or "" if not found.
    """
    # Strategy 1: look for a tag that contains only digits (possibly with commas)
    # near an element that says "in stock"
    stock_containers = soup.find_all(
        class_=re.compile(r"(stock|inventory|available|qty|quantity)", re.I)
    )
    for container in stock_containers:
        text = container.get_text(strip=True)
        # Pure number possibly with commas/spaces: "41,080" or "41080"
        m = re.fullmatch(r"[\d,\s]+", text)
        if m:
            digits = re.sub(r"[,\s]", "", text)
            if digits.isdigit():
                return digits

    # Strategy 2: scan all visible text nodes for "X in stock" or "Stock: X" patterns
    full_text = " ".join(soup.stripped_strings)
    patterns = [
        r"([\d,]+)\s+in\s+stock",          # "41,080 in stock"
        r"stock(?:ed)?\s*[:\-]\s*([\d,]+)", # "Stock: 41080"
        r"available\s*[:\-]\s*([\d,]+)",    # "Available: 41080"
        r"qty\s*available\s*[:\-]?\s*([\d,]+)",  # "Qty Available: 41080"
    ]
    for pat in patterns:
        m = re.search(pat, full_text, re.I)
        if m:
            return m.group(1).replace(",", "")

    return ""


def is_product_not_found(state: dict) -> tuple[bool, str]:
    error = state.get("errorResponse", {})
    if error:
        reason = error.get("message") or error.get("errorMessage") or "Product not found"
        return True, reason

    detail = state.get("detail", {})
    if not detail:
        return True, "No product detail in page response"

    if not detail.get("ttiPartNumber") and not detail.get("mfrPartNumber"):
        return True, "Part number not matched on TTI"

    return False, ""


def _format_pricing_table(pricing_list: list[dict]) -> str:
    """
    Serialises the pricing list into the required JSON shape:

        {"price_bands": [{"quantity": 2000, "price_usd": 0.6844}, ...]}
    """
    return json.dumps({"price_bands": pricing_list})


def parse_part(html: str, parts_number: str, mfg_shortname: str) -> dict:
    soup   = BeautifulSoup(html, "html.parser")
    state  = extract_initial_state(html)
    detail = state.get("detail", {})

    product_id  = parts_number
    product_url = build_url(parts_number, mfg_shortname)
    description = detail.get("description", "")
    manufacturer = detail.get("manufacturer", "")
    mfr_part    = detail.get("mfrPartNumber", "")
    tti_part    = detail.get("ttiPartNumber", "")

    if not description:
        desc_tag = soup.find("p", class_=re.compile(r"description", re.I))
        if not desc_tag:
            h1 = soup.find("h1")
            if h1:
                sib = h1.find_next_sibling()
                description = sib.get_text(strip=True) if sib else ""
        else:
            description = desc_tag.get_text(strip=True)

    if not manufacturer:
        mfr_tag = soup.find(string=re.compile(r"Manufacturer:", re.I))
        if mfr_tag and mfr_tag.find_next("a"):
            manufacturer = mfr_tag.find_next("a").get_text(strip=True)

    # ── Minimum Quantity ──────────────────────────────────────────────────
    min_quantity = extract_min_quantity(soup)

    if min_quantity == "N/A":
        formatted_min = (
            detail.get("formattedMin")
            or detail.get("min")
            or detail.get("minimumOrderQuantity")
            or detail.get("minimumOrderQty")
        )
        if formatted_min:
            min_quantity = re.sub(r"^\s*minimum\s*[:\-]?\s*", "", str(formatted_min), flags=re.I).strip()
            m = re.search(r"([0-9][0-9,]*)", min_quantity)
            min_quantity = m.group(1) if m else "N/A"

    if min_quantity == "N/A":
        pricing_breaks = detail.get("pricingBreaks", [])
        if pricing_breaks:
            first_qty = pricing_breaks[0].get("quantity", "N/A")
            min_quantity = str(first_qty)

    if min_quantity != "N/A":
        min_quantity = str(parse_numeric_quantity(min_quantity) or min_quantity)

    # ── Pricing Table ─────────────────────────────────────────────────────
    pricing_list = extract_pricing_table(soup)

    if pricing_list:
        unit_price = pricing_list[0]["price_usd"]
    else:
        pricing_breaks = detail.get("pricingBreaks", [])
        if pricing_breaks:
            pricing_list = []
            for b in pricing_breaks:
                qty_num   = parse_numeric_quantity(str(b.get("quantity", "")))
                price_num = parse_numeric_price(str(b.get("unitPrice", "")))
                if qty_num is not None and price_num is not None:
                    pricing_list.append({"quantity": qty_num, "price_usd": price_num})
            unit_price = pricing_list[0]["price_usd"] if pricing_list else "Not Available - Contact Sales Rep"
        else:
            unit_price   = "Not Available - Contact Sales Rep"
            pricing_list = []

    # ── Stock Status + Quantity ───────────────────────────────────────────
    stock, stock_quantity = extract_stock_status(soup, detail)

    specs = extract_specifications(soup)
    if not specs:
        packaging = detail.get("packaging", "")
        category  = detail.get("productCategory", "")
        if manufacturer or packaging or category:
            specs = {
                "Manufacturer":     manufacturer or mfg_shortname,
                "Product Category": category or "",
                "Packaging":        packaging or "Each",
            }

    export_env = extract_export_env(soup)

    # ── Sales Contact ─────────────────────────────────────────────────────
    sales_phone, sales_email = extract_sales_contact(soup)

    return {
        "Product ID":                              product_id,
        "Product URL":                             product_url,
        "Description":                             description,
        "Manufacturer":                            manufacturer,
        "Mfr Part":                                mfr_part or parts_number,
        "TTI Part":                                tti_part or parts_number,
        "Specifications":                          json.dumps(specs),
        "Export and Environmental Classification": json.dumps(export_env),
        "Unit Price":                              unit_price,
        "Pricing Table":                           _format_pricing_table(pricing_list),
        "Minimum Quantity":                        min_quantity,
        "Stock":                                   stock,
        "Stock Quantity":                          stock_quantity,  # ← NEW
        "Sales Phone":                             sales_phone,
        "Sales Email":                             sales_email,
    }


# ─────────────────────────────────────────
#  THREAD WORKER
# ─────────────────────────────────────────

def process_part(
    part: dict,
    index: int,
    total: int,
    print_lock: Lock,
) -> tuple[str, dict | None, dict | None]:
    parts_number  = part["partsNumber"]
    mfg_shortname = part["mfgShortname"]

    with print_lock:
        print(f"[{index}/{total}] Fetching {parts_number} ({mfg_shortname}) ...")

    html = fetch_page(build_url(parts_number, mfg_shortname))

    if html is None:
        with print_lock:
            print(f"  [{parts_number}] -> NOT FOUND (fetch error)")
        return parts_number, None, {
            "Cleaned Part Number": parts_number,
            "Supplier":            mfg_shortname,
            "Reason":              "Fetch error / network failure",
        }

    state = extract_initial_state(html)
    not_found, reason = is_product_not_found(state)

    if not_found:
        with print_lock:
            print(f"  [{parts_number}] -> NOT FOUND: {reason}")
        return parts_number, None, {
            "Cleaned Part Number": parts_number,
            "Supplier":            mfg_shortname,
            "Reason":              reason,
        }

    row = parse_part(html, parts_number, mfg_shortname)
    with print_lock:
        print(f"  [{parts_number}] -> FOUND: {row['Description'] or '(no description)'}")
    return parts_number, row, None


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

def main():
    print(f"[INFO] Reading parts from '{INPUT_EXCEL}' ...")
    parts = load_parts_from_excel(INPUT_EXCEL)
    total = len(parts)
    print(f"[INFO] Loaded {total} part(s). Running with {MAX_THREADS} threads.\n")

    found_map     : dict[str, dict] = {}
    not_found_map : dict[str, dict] = {}
    print_lock = Lock()

    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        futures = {
            executor.submit(process_part, part, i, total, print_lock): part["partsNumber"]
            for i, part in enumerate(parts, start=1)
        }

        for future in as_completed(futures):
            pn = futures[future]
            try:
                parts_number, found_row, not_found_row = future.result()
                if found_row:
                    found_map[parts_number] = found_row
                if not_found_row:
                    not_found_map[parts_number] = not_found_row
            except Exception as exc:
                with print_lock:
                    print(f"  [{pn}] -> EXCEPTION: {exc}")
                not_found_map[pn] = {
                    "Cleaned Part Number": pn,
                    "Supplier":            "",
                    "Reason":              f"Unexpected exception: {exc}",
                }

    found_rows     = []
    not_found_rows = []
    for part in parts:
        pn = part["partsNumber"]
        if pn in found_map:
            found_rows.append(found_map[pn])
        elif pn in not_found_map:
            not_found_rows.append(not_found_map[pn])

    write_csv(OUTPUT_FOUND, FOUND_FIELDS, found_rows)
    write_csv(OUTPUT_NOT_FOUND, NOT_FOUND_FIELDS, not_found_rows)

    print(f"\nDone.")
    print(f"    Found     : {len(found_rows):>4} part(s)  -> '{OUTPUT_FOUND}'")
    print(f"    Not found : {len(not_found_rows):>4} part(s)  -> '{OUTPUT_NOT_FOUND}'")
    print(f"    Total     : {total:>4} part(s)")


if __name__ == "__main__":
    main()