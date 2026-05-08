from __future__ import annotations

import csv
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import scrapy
from scrapy.http import Request, Response

from scorestream_scrapy.items import ForageEventItem, ScrapeErrorItem

TARGET_METROS = (
    ("atlanta", "GA"),
    ("austin", "TX"),
    ("orlando", "FL"),
    ("nashville", "TN"),
)
ZIP_PATTERN = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uuid5_str(*parts: str) -> str:
    u = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(p.strip() for p in parts if p is not None))
    return str(u)


def parse_scorestream_local_datetime_to_utc_z(value: str | None, tz_name: str | None) -> str | None:
    """
    games.get: naive `startDateTime` / `endDateTime` is wall time in `localGameTimezone`;
    convert to UTC and emit `YYYY-MM-DDTHH:MM:SSZ`.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None

    parse_formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
    parsed = None
    for fmt in parse_formats:
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None

    timezone_aliases = {
        "UTC": "UTC",
        "US/Eastern": "America/New_York",
        "US/Central": "America/Chicago",
        "US/Mountain": "America/Denver",
        "US/Pacific": "America/Los_Angeles",
        "EST": "America/New_York",
        "CST": "America/Chicago",
        "MST": "America/Denver",
        "PST": "America/Los_Angeles",
    }
    tz_raw = str(tz_name or "").strip()
    tz_key = timezone_aliases.get(tz_raw, tz_raw or "UTC")
    try:
        source_tz = ZoneInfo(tz_key)
    except ZoneInfoNotFoundError:
        source_tz = timezone.utc

    localized = parsed.replace(tzinfo=source_tz)
    return localized.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_sport_name(sport_name: str | None) -> tuple[str, str]:
    if not sport_name:
        return "", ""
    raw = str(sport_name).strip()
    if not raw:
        return "", ""
    sport_id = raw.lower().replace(" ", "-")
    sport_title = raw[:1].upper() + raw[1:]
    return sport_id, sport_title


def title_from_teams(home: str, away: str) -> str:
    home = (home or "").strip()
    away = (away or "").strip()
    if home and away:
        return f"{away} at {home}"
    return home or away or ""


def _index_collection_by_id(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        v = it.get(key)
        if v is None:
            continue
        out[str(v)] = it
    return out


def infer_datetime_type(value: str | None) -> str:
    s = str(value or "").strip()
    if not s:
        return "dateTime"
    if "T" in s or " " in s:
        return "dateTime"
    return "date"


def resolve_site_id(default_site_id: int | None, city: str | None, state: str | None) -> int | None:
    city_norm = str(city or "").strip().lower()
    state_norm = str(state or "").strip().lower()
    if city_norm == "atlanta" and state_norm == "ga":
        return 85
    if city_norm == "austin" and state_norm == "tx":
        return 269
    if city_norm == "orlando" and state_norm == "fl":
        return None
    if city_norm == "nashville" and state_norm == "tn":
        return None
    return None


def _extract_zip_from_address(address: str | None) -> str | None:
    s = str(address or "").strip()
    if not s:
        return None
    matches = ZIP_PATTERN.findall(s)
    if not matches:
        return None
    return matches[-1]


def _metro_matches_dma_state(dma_name: str | None, st_abv: str | None) -> bool:
    dma = str(dma_name or "").strip().lower()
    st = str(st_abv or "").strip().upper()
    if len(st) > 2:
        st = st[:2]
    if len(st) != 2:
        return False
    for city_key, st_req in TARGET_METROS:
        if st != st_req:
            continue
        if dma == city_key:
            return True
        if city_key in dma or dma.startswith(city_key + " ") or dma.startswith(city_key + "-"):
            return True
    return False


def _address_contains_target_location(address: str | None) -> bool:
    s = str(address or "").lower()
    if not s:
        return False
    return any(f"{city}, {state.lower()}" in s for city, state in TARGET_METROS)


def _in_scope_by_dma(address: str | None, zip_dma_map: dict[str, list[tuple[str, str]]]) -> bool:
    if not address or not str(address).strip():
        return False
    z = _extract_zip_from_address(address)
    if not z:
        return _address_contains_target_location(address)
    rows = zip_dma_map.get(z) or []
    if not rows:
        return _address_contains_target_location(address)
    return any(_metro_matches_dma_state(dma, st) for dma, st in rows)


def _load_zip_dma_map(path: Path, logger) -> dict[str, list[tuple[str, str]]]:
    if not path.exists():
        logger.warning("DMA CSV not found at %s", path)
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            zip_raw = str(row.get("Zip") or "").strip()
            market = str(row.get("Market") or "").strip()
            st = str(row.get("State") or "").strip()
            m = re.search(r"(\d{5})", zip_raw)
            if not m or not market or not st:
                continue
            z5 = m.group(1)
            out.setdefault(z5, []).append((market, st))
    logger.info("Loaded DMA ZIP map rows: %d (unique zips: %d)", sum(len(v) for v in out.values()), len(out))
    return out


def scorestream_game_to_forage_record(
    *,
    game: dict[str, Any],
    collections: dict[str, Any],
    now_iso: str,
    provider: str,
    module: str,
    site_id: int | None,
    source_id: str,
    source_name: str,
) -> dict[str, Any]:
    team_list = ((collections.get("teamCollection") or {}).get("list")) or []
    venue_list = ((collections.get("venueCollection") or {}).get("list")) or []
    location_list = ((collections.get("locationCollection") or {}).get("list")) or []
    squad_list = ((collections.get("squadCollection") or {}).get("list")) or []
    teams_by_id = _index_collection_by_id(team_list, "teamId")
    venues_by_id = _index_collection_by_id(venue_list, "venueId")
    locations_by_id = _index_collection_by_id(location_list, "locationId")
    squads_by_id = _index_collection_by_id(squad_list, "squadId")

    home_team = teams_by_id.get(str(game.get("homeTeamId") or ""), {}) if teams_by_id else {}
    away_team = teams_by_id.get(str(game.get("awayTeamId") or ""), {}) if teams_by_id else {}
    home_squad = squads_by_id.get(str(game.get("homeSquadId") or ""), {}) if squads_by_id else {}
    away_squad = squads_by_id.get(str(game.get("awaySquadId") or ""), {}) if squads_by_id else {}
    venue = venues_by_id.get(str(game.get("venueId") or ""), {}) if venues_by_id else {}
    location = {}
    if isinstance(venue, dict) and venue.get("locationId") is not None:
        location = locations_by_id.get(str(venue.get("locationId")), {}) if locations_by_id else {}

    home_name = str(home_team.get("apTeamName") or home_team.get("teamName") or "")
    away_name = str(away_team.get("apTeamName") or away_team.get("teamName") or "")
    home_mascot = str(home_team.get("mascot1") or "")
    away_mascot = str(away_team.get("mascot1") or "")

    sport_id, sport_title = normalize_sport_name(str(game.get("sportName") or ""))
    raw_timezone = str(game.get("localGameTimezone") or "")
    raw_start = str(game.get("startDateTime") or "")
    raw_end = str(game.get("endDateTime") or "")
    start_utc = parse_scorestream_local_datetime_to_utc_z(raw_start, raw_timezone)
    end_utc = parse_scorestream_local_datetime_to_utc_z(raw_end, raw_timezone)

    location_name = ""
    if isinstance(venue, dict):
        location_name = str(venue.get("name") or venue.get("officialName") or "")
    address = ""
    city = ""
    state = ""
    if isinstance(location, dict) and location:
        city = str(location.get("city") or "").strip()
        state = str(location.get("state") or "").strip()
        parts = [city, state, location.get("country")]
        address = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    # Prefer source coordinates from game; fallback to location/venue payload.
    lat = game.get("latitude")
    lon = game.get("longitude")
    if lat is None and isinstance(location, dict):
        lat = (
            location.get("latitude")
            or location.get("lat")
            or location.get("y")
        )
    if lon is None and isinstance(location, dict):
        lon = (
            location.get("longitude")
            or location.get("lon")
            or location.get("lng")
            or location.get("x")
        )
    if lat is None and isinstance(venue, dict):
        lat = venue.get("latitude") or venue.get("lat")
    if lon is None and isinstance(venue, dict):
        lon = venue.get("longitude") or venue.get("lon") or venue.get("lng")
    # Final source fallback: team coordinates from teamCollection.
    if lat is None:
        lat = home_team.get("latitude") or away_team.get("latitude")
    if lon is None:
        lon = home_team.get("longitude") or away_team.get("longitude")
    try:
        lat = float(lat) if lat is not None else None
    except (TypeError, ValueError):
        lat = None
    try:
        lon = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        lon = None
    game_url = str(game.get("url") or game.get("minUrl") or "")
    record_id = uuid5_str(source_id, str(game.get("gameId") or ""), game_url)
    record_source_id = uuid5_str("recordSource", game_url)
    title = title_from_teams(home_name, away_name)

    last = game.get("lastScore") if isinstance(game.get("lastScore"), dict) else {}
    home_score = last.get("homeTeamScore")
    away_score = last.get("awayTeamScore")
    totals: list[dict[str, Any]] = []
    if isinstance(home_score, int) or (isinstance(home_score, str) and str(home_score).isdigit()):
        totals.append({"competitorId": "home", "value": int(home_score)})
    if isinstance(away_score, int) or (isinstance(away_score, str) and str(away_score).isdigit()):
        totals.append({"competitorId": "away", "value": int(away_score)})

    status = "scheduled"
    raw_status_parts = [
        game.get("gameStatus"),
        game.get("status"),
        game.get("statusName"),
        game.get("gameStatusName"),
        game.get("displayStatus"),
        game.get("eventStatus"),
        game.get("gameState"),
        game.get("state"),
    ]
    raw_game_status = " ".join(str(v).strip().lower() for v in raw_status_parts if v)
    cancel_tokens = ("cancel", "canceled", "cancelled", "withdraw")
    postpone_tokens = ("postpon", "ppd", "postponed", "resched")

    is_cancelled_flag = any(t in raw_game_status for t in cancel_tokens)
    stoppage_status_id = last.get("stoppageStatusId")
    stoppage_message = str(last.get("stoppageMessage") or "").strip().lower()
    if stoppage_status_id in {30} or any(t in stoppage_message for t in cancel_tokens):
        is_cancelled_flag = True

    is_postponed_flag = bool(
        not is_cancelled_flag
        and (
            any(t in raw_game_status for t in postpone_tokens)
            or any(t in stoppage_message for t in postpone_tokens)
        )
    )
    start_dt = None
    if start_utc:
        try:
            start_dt = datetime.strptime(start_utc, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            start_dt = None
    now_dt = None
    try:
        now_dt = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        now_dt = None
    is_final_flag = bool(
        game.get("gameComplete")
        or game.get("gameDone")
        or game.get("isFinal")
        or ("final" in raw_game_status)
    )
    if is_cancelled_flag:
        status = "cancelled"
    elif is_postponed_flag:
        status = "postponed"
    elif is_final_flag:
        status = "completed"
    elif start_dt and now_dt:
        if now_dt < start_dt:
            status = "scheduled"
        elif totals:
            status = "completed"
        else:
            status = "inProgress"
    elif totals:
        status = "inProgress"

    def _school_id(team: dict[str, Any]) -> str | None:
        raw_team_id = str(team.get("teamId") or "").strip()
        base_id = raw_team_id.split("-")[0] if raw_team_id else ""
        team_state = str(team.get("state") or "").strip()
        if team_state and base_id:
            return f"{team_state}-{base_id}"
        if base_id:
            return base_id
        return None

    def _team_record(team: dict[str, Any], squad: dict[str, Any], side: str, game_data: dict[str, Any]) -> str:
        for key in (
            "record",
            "overallRecord",
            "overallRecordStr",
            "recordStr",
            "teamRecord",
            "wonLost",
            "wonLostTied",
        ):
            v = team.get(key)
            if v:
                return str(v).strip()
        for key in (
            "record",
            "overallRecord",
            "overallRecordStr",
            "recordStr",
            "teamRecord",
            "wonLost",
            "wonLostTied",
        ):
            v = squad.get(key)
            if v:
                return str(v).strip()
        for key in (
            f"{side}TeamRecord",
            f"{side}Record",
            f"{side}OverallRecord",
            f"{side}OverallRecordStr",
            f"{side}WonLostTied",
        ):
            v = game_data.get(key)
            if v:
                return str(v).strip()
        return ""

    def _full_team_name(name: str, mascot: str) -> str:
        name = (name or "").strip()
        mascot = (mascot or "").strip()
        if name and mascot:
            return f"{name} {mascot}"
        return name or mascot

    competitors = [
        {
            "competitorId": "home",
            "entityType": "team",
            "schoolId": _school_id(home_team),
            "schoolName": home_name,
            "teamId": f"{home_team.get('teamId')}-{sport_id}" if home_team.get("teamId") else "",
            "teamName": _full_team_name(home_name, home_mascot),
            "teamRecord": _team_record(home_team, home_squad, "home", game),
        },
        {
            "competitorId": "away",
            "entityType": "team",
            "schoolId": _school_id(away_team),
            "schoolName": away_name,
            "teamId": f"{away_team.get('teamId')}-{sport_id}" if away_team.get("teamId") else "",
            "teamName": _full_team_name(away_name, away_mascot),
            "teamRecord": _team_record(away_team, away_squad, "away", game),
        },
    ]
    for c in competitors:
        if not c.get("schoolId"):
            c.pop("schoolId", None)

    winner_competitor_id = None
    is_draw = False
    if len(totals) == 2:
        home_total = next((t["value"] for t in totals if t["competitorId"] == "home"), None)
        away_total = next((t["value"] for t in totals if t["competitorId"] == "away"), None)
        if isinstance(home_total, int) and isinstance(away_total, int):
            if home_total > away_total:
                winner_competitor_id = "home"
            elif away_total > home_total:
                winner_competitor_id = "away"
            else:
                is_draw = True

    raw_gender = str(
        game.get("genderName")
        or game.get("gender")
        or home_squad.get("gender")
        or away_squad.get("gender")
        or home_team.get("gender")
        or away_team.get("gender")
        or ""
    ).strip().lower()
    gender = "Unknown"
    if "boy" in raw_gender or "men" in raw_gender or raw_gender in {"m", "male"}:
        gender = "Boys"
    elif "girl" in raw_gender or "women" in raw_gender or raw_gender in {"f", "female"}:
        gender = "Girls"
    elif "coed" in raw_gender or "mixed" in raw_gender:
        gender = "Coed"

    level = str(home_squad.get("shortLevel") or home_squad.get("level") or "").strip() or "High School"

    event_kind = "span" if start_utc and end_utc else "point"
    schedule_precision = (
        "date"
        if infer_datetime_type(raw_start) == "date"
        and (not end_utc or infer_datetime_type(raw_end) == "date")
        else "dateTime"
    )
    event_schedule: dict[str, Any] = {
        "kind": event_kind,
        "precision": schedule_precision,
        "allDay": schedule_precision == "date",
        "start": {
            "type": infer_datetime_type(raw_start),
            "value": start_utc,
            # `value` = UTC instant; naive `sourceStartDateTime` was interpreted in `sourceLocalGameTimezone`.
            "sourceStartDateTime": raw_start or None,
            "sourceLocalGameTimezone": raw_timezone or None,
        },
    }
    if end_utc:
        event_schedule["end"] = {
            "type": infer_datetime_type(raw_end),
            "value": end_utc,
            "sourceEndDateTime": raw_end or None,
            "sourceLocalGameTimezone": raw_timezone or None,
        }

    resolved_site_id = resolve_site_id(default_site_id=site_id, city=city, state=state)
    result_kind = (
        "tie" if (status not in ("cancelled", "postponed") and is_draw) else "winner"
    )
    record: dict[str, Any] = {
        "provider": provider,
        "module": module,
        "groupId": f"{source_id}-{sport_id}-{game.get('gameId')}",
        "id": record_id,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "title": title,
        "source": {"name": source_name, "id": source_id},
        "recordSource": {"id": record_source_id, "url": game_url},
        "location": {
            "name": location_name,
            "address": address,
            "latitude": lat,
            "longitude": lon,
        },
        "siteId": resolved_site_id,
        "metadata": {
            "sport": {
                "sportId": sport_id,
                "sportName": sport_title or str(game.get("sportName") or ""),
                "level": level,
                "gender": gender,
                "competitionType": "regularSeason",
                "status": status,
                "eventSchedule": event_schedule,
                "participants": {
                    "kind": "teamGame",
                    "competitors": competitors,
                    "venueAssignment": {"kind": "hosted", "hostCompetitorId": "home"},
                },
                "score": {"kind": "simple", "totals": totals},
                "result": {
                    "kind": result_kind,
                    "winnerCompetitorId": winner_competitor_id,
                },
            }
        },
    }
    return record


def _read_game_ids_from_csv(path: Path) -> list[int]:
    ids: set[int] = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = (row.get("game_id") or "").strip()
            if not raw:
                continue
            try:
                ids.add(int(raw))
            except ValueError:
                continue
    return sorted(ids)


def _chunks(xs: list[int], n: int) -> Iterable[list[int]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


class ScoreStreamScrapeSpider(scrapy.Spider):
    """
    Read game ids from the listing CSV and fetch full details via ScoreStream JSON-RPC.

    Run from `scorestream/`:
      scrapy crawl scorestream_scrape
      scrapy crawl scorestream_scrape -a csv_path=scorestream_game_links.csv
    """

    name = "scorestream_scrape"
    allowed_domains: list[str] = []

    custom_settings = {
        "DOWNLOAD_DELAY": 0.15,
        "SCORESTREAM_JSON_PATH": "scorestream_games_14_04_2026.json",
        "SCORESTREAM_ERRORS_JSON_PATH": "scorestream_games.errors.json",
        "SCORESTREAM_LISTING_CSV_PATH": "scorestream_game_links.csv",
        "ITEM_PIPELINES": {
            "scorestream_scrapy.pipelines.ForageJsonPipeline": 400,
        },
    }

    def __init__(self, csv_path: str | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.csv_path_arg = csv_path
        self._api_url = ""
        self._provider = ""
        self._module = ""
        self._site_id = 0
        self._source_id = ""
        self._source_name = ""
        self._now = utc_now_iso()
        self._csv_path: Path | None = None
        project_root = Path(__file__).resolve().parents[3]
        dma_path = project_root / "mainz_sportstaetten_scraper" / "Tegna - Pilot Scope Locations.csv"
        self._zip_dma_map: dict[str, list[tuple[str, str]]] = _load_zip_dma_map(dma_path, self.logger)

    @classmethod
    def from_crawler(cls, crawler, *args: Any, **kwargs: Any):
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings
        spider.allowed_domains = list(settings.getlist("SCORESTREAM_ALLOWED_DOMAINS"))
        spider._api_url = str(settings.get("SCORESTREAM_API_URL"))
        spider._provider = str(settings.get("SCORESTREAM_PROVIDER"))
        spider._module = str(settings.get("SCORESTREAM_MODULE"))
        spider._site_id = int(settings.getint("SCORESTREAM_SITE_ID"))
        spider._source_id = str(settings.get("SCORESTREAM_SOURCE_ID"))
        spider._source_name = str(settings.get("SCORESTREAM_SOURCE_NAME"))
        csv_path_raw = spider.csv_path_arg or str(settings.get("SCORESTREAM_LISTING_CSV_PATH"))
        spider._csv_path = Path(csv_path_raw)
        return spider

    def start_requests(self) -> Iterable[Request]:
        assert self._csv_path is not None
        if not self._csv_path.exists():
            yield ScrapeErrorItem(
                game_url=str(self._csv_path),
                error="csv_not_found (run scorestream_listing first or pass -a csv_path=...)",
            )
            return

        ids = _read_game_ids_from_csv(self._csv_path)
        if not ids:
            yield ScrapeErrorItem(game_url=str(self._csv_path), error="csv_no_game_ids")
            return

        for batch in _chunks(ids, 50):
            body = {"api": "scoreStream", "method": "games.get", "params": {"gameIds": batch}}
            yield scrapy.Request(
                url=self._api_url,
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps(body).encode("utf-8"),
                callback=self.parse_games_get,
                meta={"batch": batch},
                dont_filter=True,
            )

    def parse_games_get(self, response: Response) -> Iterable[Any]:
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            yield ScrapeErrorItem(game_url=response.url, error="games_get_bad_json")
            return

        result = data.get("result") or {}
        if not isinstance(result, dict):
            yield ScrapeErrorItem(game_url=response.url, error="games_get_no_result")
            return

        collections = result.get("collections") or {}
        games = ((collections.get("gameCollection") or {}).get("list")) or []
        if not isinstance(games, list) or not games:
            yield ScrapeErrorItem(game_url=response.url, error="games_get_empty")
            return

        venue_ids: set[int] = set()
        for g in games:
            if isinstance(g, dict) and g.get("venueId") is not None:
                try:
                    venue_ids.add(int(g["venueId"]))
                except (TypeError, ValueError):
                    continue

        if not venue_ids:
            yield from self._emit_records(games, collections)
            return

        body = {
            "api": "scoreStream",
            "method": "venues.get",
            "params": {"venueIds": sorted(venue_ids)},
        }
        yield scrapy.Request(
            url=self._api_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(body).encode("utf-8"),
            callback=self.parse_venues_get,
            meta={"games": games, "collections": collections},
            dont_filter=True,
        )

    def parse_venues_get(self, response: Response) -> Iterable[Any]:
        games = response.meta.get("games") or []
        collections = response.meta.get("collections") or {}
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            yield ScrapeErrorItem(game_url=response.url, error="venues_get_bad_json")
            return

        result = data.get("result") or {}
        vcols = (result.get("collections") or {}).get("venueCollection") or {}
        vlist = vcols.get("list") or []
        if not isinstance(vlist, list):
            vlist = []

        collections = dict(collections)
        collections["venueCollection"] = {"list": vlist}

        location_ids: set[int] = set()
        for v in vlist:
            if isinstance(v, dict) and v.get("locationId") is not None:
                try:
                    location_ids.add(int(v["locationId"]))
                except (TypeError, ValueError):
                    continue

        if not location_ids:
            yield from self._emit_records(games, collections)
            return

        body = {
            "api": "scoreStream",
            "method": "locations.get",
            "params": {"locationIds": sorted(location_ids)},
        }
        yield scrapy.Request(
            url=self._api_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(body).encode("utf-8"),
            callback=self.parse_locations_get,
            meta={"games": games, "collections": collections},
            dont_filter=True,
        )

    def parse_locations_get(self, response: Response) -> Iterable[Any]:
        games = response.meta.get("games") or []
        collections = response.meta.get("collections") or {}
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            yield ScrapeErrorItem(game_url=response.url, error="locations_get_bad_json")
            return

        result = data.get("result") or {}
        lcols = (result.get("collections") or {}).get("locationCollection") or {}
        llist = lcols.get("list") or []
        if not isinstance(llist, list):
            llist = []

        collections = dict(collections)
        collections["locationCollection"] = {"list": llist}
        yield from self._emit_records(games, collections)

    def _emit_records(self, games: list[Any], collections: dict[str, Any]) -> Iterable[Any]:
        for g in games:
            if not isinstance(g, dict):
                continue
            try:
                record = scorestream_game_to_forage_record(
                    game=g,
                    collections=collections,
                    now_iso=self._now,
                    provider=self._provider,
                    module=self._module,
                    site_id=self._site_id,
                    source_id=self._source_id,
                    source_name=self._source_name,
                )
            except Exception as ex:  # noqa: BLE001
                yield ScrapeErrorItem(
                    game_url=str(g.get("url") or g.get("minUrl") or ""),
                    error=f"map_error: {ex}",
                )
                continue
            address = str((record.get("location") or {}).get("address") or "")
            if not _in_scope_by_dma(address=address, zip_dma_map=self._zip_dma_map):
                continue
            yield ForageEventItem(record=record, game_url=str(record["recordSource"]["url"]))

