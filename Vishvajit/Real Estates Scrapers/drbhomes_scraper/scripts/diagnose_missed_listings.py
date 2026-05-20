"""Compare drbhomes_listings.csv vs drbhomes_details.csv; classify missed URLs via public API."""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

LIST = Path("drbhomes_listings.csv")
DET = Path("drbhomes_details.csv")
OUT = Path("drbhomes_missed_report.csv")

BY_NAME = "https://api.drbhomes.com/api/v1/public/inventory/state/region/by-name"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://www.drbhomes.com",
    "referer": "https://www.drbhomes.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def parse_quick_move_in_url(url: str) -> dict | None:
    if not url:
        return None
    path = [p for p in urlparse(url).path.split("/") if p]
    try:
        idx = path.index("communities")
    except ValueError:
        return None
    if idx + 5 >= len(path) or path[idx + 4] != "quick-move-in-homes":
        return None
    return {
        "state": path[idx + 1],
        "region": path[idx + 2],
        "community": path[idx + 3],
        "address": path[idx + 5],
    }


def main():
    cwd = Path.cwd()
    list_path = cwd / LIST if not LIST.is_absolute() else LIST
    det_path = cwd / DET if not DET.is_absolute() else DET
    out_path = cwd / OUT if not OUT.is_absolute() else OUT

    if not list_path.exists():
        print(f"Missing {list_path}", file=sys.stderr)
        sys.exit(1)

    listing_urls: list[str] = []
    with list_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            u = (row.get("house_url") or "").strip().rstrip("/")
            if u:
                listing_urls.append(u)

    detail_urls: set[str] = set()
    if det_path.exists():
        with det_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                u = (row.get("url") or "").strip().rstrip("/")
                if u:
                    detail_urls.add(u)

    missed = [u for u in listing_urls if u not in detail_urls]
    print(f"listings={len(listing_urls)} details_unique={len(detail_urls)} missed={len(missed)}")

    rows_out = []
    delay = 0.12
    for i, url in enumerate(missed):
        parsed = parse_quick_move_in_url(url)
        if not parsed:
            rows_out.append(
                {
                    "house_url": url,
                    "reason": "parse_failed",
                    "api_status": "",
                    "api_message": "",
                    "state": "",
                    "region": "",
                    "community": "",
                    "address": "",
                }
            )
            continue
        resp = requests.get(BY_NAME, params=parsed, headers=HEADERS, timeout=30)
        msg = ""
        try:
            body = resp.json()
            msg = str(body.get("message") or "")[:500]
        except Exception:
            msg = (resp.text or "")[:500]

        if resp.status_code != 200:
            rows_out.append(
                {
                    "house_url": url,
                    "reason": f"by_name_http_{resp.status_code}",
                    "api_status": str(resp.status_code),
                    "api_message": msg,
                    **parsed,
                }
            )
        else:
            inv_id = body.get("id") if isinstance(body, dict) else None
            if inv_id is None:
                rows_out.append(
                    {
                        "house_url": url,
                        "reason": "by_name_200_missing_id",
                        "api_status": "200",
                        "api_message": json.dumps(body)[:500],
                        **parsed,
                    }
                )
            else:
                inv = requests.get(
                    f"https://api.drbhomes.com/api/v1/public/inventory/{inv_id}",
                    headers=HEADERS,
                    timeout=30,
                )
                if inv.status_code != 200:
                    rows_out.append(
                        {
                            "house_url": url,
                            "reason": f"inventory_http_{inv.status_code}",
                            "api_status": str(inv.status_code),
                            "api_message": (inv.text or "")[:500],
                            **parsed,
                        }
                    )
                else:
                    rows_out.append(
                        {
                            "house_url": url,
                            "reason": "api_ok_but_missing_from_details_csv",
                            "api_status": "200",
                            "api_message": f"id={inv_id}",
                            **parsed,
                        }
                    )
        if (i + 1) % 25 == 0:
            print(f"  processed {i + 1}/{len(missed)}")
        time.sleep(delay)

    with out_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "reason",
            "api_status",
            "api_message",
            "state",
            "region",
            "community",
            "address",
            "house_url",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_out)

    from collections import Counter

    c = Counter(r["reason"] for r in rows_out)
    print("missed breakdown:", dict(c))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
