# AllEvents Scrapy

Scrapy project for collecting AllEvents listings and event details, then emitting Forage-shaped JSON records.

## What This Scraper Does

- Calls AllEvents listing API (`POST /api/events/list`) for configured markets.
- Stores listing rows in `event_links.csv`.
- Visits event detail pages and builds final records in `forage_events.json`.
- Logs parse/runtime issues in `forage_events.errors.json`.

## Project Layout

- `allevents_scrapy/spiders/allevents_events.py`  
  Combined listing + detail spider.
- `allevents_scrapy/spiders/allevents_from_csv.py`  
  Detail-only spider using existing CSV.
- `allevents_scrapy/event_utils.py`  
  Core transformation/parsing logic.
- `allevents_scrapy/pipelines.py`  
  CSV + JSON output pipelines.
- `allevents_scrapy/allevents_config.py`  
  Market definitions and constants.
- `Tegna - Pilot Scope Locations.csv`  
  ZIP scope sheet used for `location.nearBy` evaluation.

## Requirements

- Python 3.11+ recommended.
- Scrapy installed in your environment.
- For Windows environments, install `tzdata` if timezone data is missing.

## Run Commands

From `allevents/allevents_scrapy` (where `scrapy.cfg` exists):

- Full run (listing + details):
  - `scrapy crawl allevents_events`
- Limit pages/details:
  - `scrapy crawl allevents_events -a max_pages=2 -a detail_limit=50`
- Listing only:
  - `scrapy crawl allevents_events -a list_only=1`
- Details from existing CSV:
  - `scrapy crawl allevents_from_csv -a csv_path=event_links.csv`

## Output Files

- `event_links.csv`  
  Listing-stage rows used for detail crawl.
- `forage_events.json`  
  Final Forage-shaped records.
- `forage_events.errors.json`  
  Per-event errors (missing JSON-LD, parse exceptions, etc.).

## Key Data Rules Implemented

- **Schedule extraction**
  - Prefers visible UI date/time labels from event page.
  - Supports both bullet format (`Fri, 29 May • 06:00 PM`) and `at ... to ...` ranges.
  - Falls back to listing epoch fields and then share dates when needed.

- **Pricing**
  - `kind: "free"` when effective single price is exactly `0`.
  - `kind: "point"`, `kind: "span"`, or `kind: "tbd"` otherwise.

- **Location normalization**
  - Cleans whitespace and removes placeholder/TBA values.
  - Invalid venue fallback names like `Austin, TX` are nulled.
  - If `location.name` is missing, address/lat/long are dropped.

- **nearBy + siteId logic**
  - ZIP-first check using `Tegna - Pilot Scope Locations.csv`.
  - If ZIP present and found, market + state must match sheet row(s).
  - If ZIP missing, fallback requires valid market/state combo and both present in address text.
  - `siteId` mapping:
    - Atlanta -> `85`
    - Austin -> `269`
    - Orlando/Nashville -> `null`
  - If address is missing market/state text, `location.nearBy` is `null`.

- **Media**
  - Media IDs use `media-001` style.
  - `source.id` uses image query param `v=` value when available.

## Logging

- Global Scrapy logging configured in `settings.py`:
  - `LOG_LEVEL = INFO`
  - timestamped log format
- Pipelines and utility loaders emit structured info/warn/error logs.

## Configuration Notes

Main settings live in `allevents_scrapy/settings.py`:

- `ALLEVENTS_ALLOWED_DOMAINS`
- `ALLEVENTS_COUNTRY`
- `ALLEVENTS_LIST_PAGE_SIZE_DEFAULT`
- `ALLEVENTS_DETAIL_LIMIT_DEFAULT`
- XPath selectors for title/description extraction
- Output file paths

Location market definitions live in `allevents_scrapy/allevents_config.py`.

