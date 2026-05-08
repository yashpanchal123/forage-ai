### ScoreStream Scrapy

Two spiders:

- **`scorestream_listing`**: walks High School Explore for **target states only** (default: GA, TX, FL, TN — mapped from Atlanta, Austin, Orlando, Nashville) and writes `scorestream_game_links.csv`
- **`scorestream_scrape`**: reads that CSV and writes `scorestream_games.json` in the `sport_foramt.json` shape

**Regions:** ScoreStream’s public Explore URLs are **state-level only** (e.g. `https://scorestream.com/explore/r/georgia/high-school`). There is no city/metro path in this flow, so each “market” is implemented as the **whole state**; the CSV includes a **`market_label`** column for operator context (e.g. “Atlanta GA (Georgia)”).

**Sports (high school only):** Baseball, Basketball, Football, Soccer, Softball, Volleyball — including **boys-** / **girls-** variants when the site lists them separately.

**Volume:** Spring/summer breaks and off-season can mean **few or zero games** in a short date window. Increase `days_back` (default **120**) to pull older games.

### Run

From `d:\Forage-ai\scorestream`:

```bash
scrapy crawl scorestream_listing
scrapy crawl scorestream_scrape
```

Optional:

```bash
scrapy crawl scorestream_listing -a days_back=180
scrapy crawl scorestream_listing -a state_slug=georgia
scrapy crawl scorestream_listing -a state_slugs=georgia,texas,florida,tennessee
scrapy crawl scorestream_listing -a all_states=1
scrapy crawl scorestream_scrape -a csv_path=scorestream_game_links.csv
```

Config defaults live in `scorestream_scrapy/settings.py` and per-spider `custom_settings`.
