"""
Value-Bet Telegram Agent
=========================
Τρέχει μια φορά την ημέρα (μέσω GitHub Actions cron).

Ανάλυση ανά ματς (ΟΧΙ μόνο τιμή/αποδόσεις):
1. Fixtures της ημέρας (football-data.org).
2. Recent form ΚΑΘΕ ομάδας, χωρισμένο σε "στο γήπεδό της" / "εκτός έδρας"
   (όχι blended) — goals for/against.
3. Θέση στη βαθμολογία (points-per-game) ως πρόσθετο δείκτη δύναμης.
4. Head-to-head ιστορικό μεταξύ των δύο ομάδων.
5. Poisson μοντέλο goals από τα παραπάνω → πιθανότητες 1X2 + Over/Under 2.5.
6. Αποδόσεις από ΠΟΛΛΑΠΛΑ bookmakers (the-odds-api.com) → "δίκαιη" τιμή
   αγοράς (vig-free) ΚΑΙ έλεγχος συμφωνίας μεταξύ bookmakers.
7. Confidence score: δεν κοιτάει μόνο edge/πιθανότητα — κοιτάει ΚΑΙ αν
   έχουμε αρκετά δεδομένα, αν το H2H συμφωνεί, αν η αγορά συμφωνεί με
   ποιος είναι φαβορί.
8. Στέλνει 1 πρόταση/μέρα στο Telegram, πάντα, με ετικέτα σιγουριάς.

ΣΗΜΑΝΤΙΚΟ: Στατιστικό εργαλείο, ΟΧΙ εγγύηση κέρδους. Το στοίχημα έχει
πάντα ρίσκο. Στοιχηματίζεις πάντα με δικά σου κριτήρια και μόνο ό,τι
μπορείς να χάσεις.
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from itertools import product
from typing import Any

import requests

try:
    from rapidfuzz import fuzz

    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FD_BASE = "https://api.football-data.org/v4"
ODDS_BASE = "https://api.the-odds-api.com/v4"
AF_BASE = "https://v3.football.api-sports.io"

COMPETITIONS = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "CL": "soccer_uefa_champs_league",
    "ELC": "soccer_efl_champ",
    "PPL": "soccer_portugal_primeira_liga",
    "DED": "soccer_netherlands_eredivisie",
    "BSA": "soccer_brazil_campeonato",
    "WC": "soccer_fifa_world_cup",
    "EC": "soccer_uefa_european_championship",
    "CLI": "soccer_conmebol_copa_libertadores",
}


def current_season_year() -> int:
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


AF_LEAGUES = {
    "Greece Super League": {"league_id": 197, "odds_key": "soccer_greece_super_league"},
    "Austria Bundesliga": {"league_id": 218, "odds_key": "soccer_austria_bundesliga"},
    "Switzerland Super League": {"league_id": 207, "odds_key": "soccer_switzerland_superleague"},
    "Poland Ekstraklasa": {"league_id": 106, "odds_key": "soccer_poland_ekstraklasa"},
    "Turkey Super Lig": {"league_id": 203, "odds_key": "soccer_turkey_super_league"},
    "Scotland Premiership": {"league_id": 179, "odds_key": "soccer_scotland_premiership"},
    "Belgium Pro League": {"league_id": 144, "odds_key": "soccer_belgium_first_div"},
    "UEFA Europa League": {"league_id": 3, "odds_key": "soccer_uefa_europa_league"},
    "UEFA Conference League": {"league_id": 848, "odds_key": "soccer_uefa_europa_conference_league"},
}

MIN_EDGE = 0.03
MIN_MODEL_PROB = 0.60
MAX_GOALS = 6
RECENT_MATCHES = 10
MIN_VENUE_SAMPLE = 3
REQUEST_PAUSE = 6.5
MIN_BOOKMAKERS = 2
MIN_TOTAL_SAMPLE = 3
STATE_FILE = "state/last_run_date.txt"

FUZZY_MATCH_THRESHOLD = 85
PICK_RESOLVE_TIMEOUT_DAYS = 14

FD_VOID_STATUSES = frozenset({"POSTPONED", "CANCELLED", "SUSPENDED", "AWARDED"})
AF_VOID_STATUSES = frozenset({"PST", "CANC", "ABD", "INT", "SUSP", "AWD", "WO"})

HISTORY_FILE = "state/pick_history.json"
MIN_RESOLVED_FOR_CALIBRATION = 15
CALIBRATION_MIN = 0.80
CALIBRATION_MAX = 1.20
CALIBRATION_STEP = 0.02

logger = logging.getLogger(__name__)

# In-run caches (cleared at start of main())
_team_matches_cache: dict[int, list[dict[str, Any]]] = {}
_af_team_stats_cache: dict[tuple[int, int, int], dict[str, Any] | None] = {}
_standings_cache: dict[str, dict[int, float]] = {}
_af_standings_cache: dict[tuple[int, int], dict[int, float]] = {}


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def clear_run_caches() -> None:
    _team_matches_cache.clear()
    _af_team_stats_cache.clear()
    _standings_cache.clear()
    _af_standings_cache.clear()


# ---------------------------------------------------------------------------
# football-data.org helpers
# ---------------------------------------------------------------------------


def fd_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    r = requests.get(f"{FD_BASE}{path}", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    time.sleep(REQUEST_PAUSE)
    return r.json()


def get_todays_fixtures(comp_code: str) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    data = fd_get(
        f"/competitions/{comp_code}/matches",
        params={"dateFrom": str(today), "dateTo": str(today), "status": "SCHEDULED"},
    )
    return data.get("matches", [])


def get_standings(comp_code: str) -> dict[int, float]:
    if comp_code in _standings_cache:
        return _standings_cache[comp_code]
    try:
        data = fd_get(f"/competitions/{comp_code}/standings")
    except Exception as e:
        logger.warning("[%s] standings error: %s", comp_code, e)
        _standings_cache[comp_code] = {}
        return {}

    ppg: dict[int, float] = {}
    for table_group in data.get("standings", []):
        if table_group.get("type") != "TOTAL":
            continue
        for row in table_group.get("table", []):
            played = row.get("playedGames") or 0
            if played > 0:
                ppg[row["team"]["id"]] = row["points"] / played
    _standings_cache[comp_code] = ppg
    return ppg


def get_team_matches(team_id: int) -> list[dict[str, Any]]:
    if team_id in _team_matches_cache:
        return _team_matches_cache[team_id]
    data = fd_get(
        f"/teams/{team_id}/matches",
        params={"status": "FINISHED", "limit": RECENT_MATCHES},
    )
    matches = data.get("matches", [])
    _team_matches_cache[team_id] = matches
    return matches


def split_home_away_stats(matches: list[dict[str, Any]], team_id: int) -> dict[str, Any]:
    overall_gf, overall_ga = [], []
    home_gf, home_ga = [], []
    away_gf, away_ga = [], []

    for m in matches:
        is_home = m["homeTeam"]["id"] == team_id
        score = m.get("score", {}).get("fullTime", {})
        h, a = score.get("home"), score.get("away")
        if h is None or a is None:
            continue
        gf = h if is_home else a
        ga = a if is_home else h

        overall_gf.append(gf)
        overall_ga.append(ga)
        if is_home:
            home_gf.append(gf)
            home_ga.append(ga)
        else:
            away_gf.append(gf)
            away_ga.append(ga)

    def avg(lst: list[float | int]) -> float | None:
        return sum(lst) / len(lst) if lst else None

    return {
        "overall_gf": avg(overall_gf),
        "overall_ga": avg(overall_ga),
        "home_gf": avg(home_gf),
        "home_ga": avg(home_ga),
        "home_n": len(home_gf),
        "away_gf": avg(away_gf),
        "away_ga": avg(away_ga),
        "away_n": len(away_gf),
    }


def get_h2h(fixture_id: int, limit: int = 5) -> dict[str, Any] | None:
    try:
        data = fd_get(f"/matches/{fixture_id}/head2head", params={"limit": limit})
    except Exception as e:
        logger.warning("h2h error for fixture %s: %s", fixture_id, e)
        return None
    return data.get("aggregates")


# ---------------------------------------------------------------------------
# API-Football helpers
# ---------------------------------------------------------------------------


def af_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    r = requests.get(f"{AF_BASE}{path}", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_af_todays_fixtures(league_id: int, season: int) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    data = af_get("/fixtures", params={"league": league_id, "season": season, "date": str(today)})
    return data.get("response", [])


def get_af_team_stats(league_id: int, season: int, team_id: int) -> dict[str, Any] | None:
    cache_key = (league_id, season, team_id)
    if cache_key in _af_team_stats_cache:
        return _af_team_stats_cache[cache_key]

    data = af_get("/teams/statistics", params={"league": league_id, "season": season, "team": team_id})
    resp = data.get("response")
    if not resp:
        _af_team_stats_cache[cache_key] = None
        return None

    goals_for = resp.get("goals", {}).get("for", {}).get("average", {})
    goals_against = resp.get("goals", {}).get("against", {}).get("average", {})
    played = resp.get("fixtures", {}).get("played", {})

    def to_float(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    stats = {
        "overall_gf": to_float(goals_for.get("total")),
        "overall_ga": to_float(goals_against.get("total")),
        "home_gf": to_float(goals_for.get("home")),
        "home_ga": to_float(goals_against.get("home")),
        "home_n": played.get("home", 0) or 0,
        "away_gf": to_float(goals_for.get("away")),
        "away_ga": to_float(goals_against.get("away")),
        "away_n": played.get("away", 0) or 0,
    }
    _af_team_stats_cache[cache_key] = stats
    return stats


def get_af_standings_ppg(league_id: int, season: int) -> dict[int, float]:
    cache_key = (league_id, season)
    if cache_key in _af_standings_cache:
        return _af_standings_cache[cache_key]

    try:
        data = af_get("/standings", params={"league": league_id, "season": season})
    except Exception as e:
        logger.warning("[AF %s] standings error: %s", league_id, e)
        _af_standings_cache[cache_key] = {}
        return {}

    ppg: dict[int, float] = {}
    try:
        table = data["response"][0]["league"]["standings"][0]
        for row in table:
            played = row.get("all", {}).get("played") or 0
            if played > 0:
                ppg[row["team"]["id"]] = row["points"] / played
    except (IndexError, KeyError, TypeError):
        pass
    _af_standings_cache[cache_key] = ppg
    return ppg


def get_af_h2h(home_af_id: int, away_af_id: int, limit: int = 5) -> dict[str, Any] | None:
    try:
        data = af_get("/fixtures/headtohead", params={"h2h": f"{home_af_id}-{away_af_id}", "last": limit})
    except Exception as e:
        logger.warning("AF h2h error: %s", e)
        return None
    matches = data.get("response", [])
    if not matches:
        return None
    home_wins = away_wins = 0
    for m in matches:
        winner_home = m["teams"]["home"]["winner"]
        winner_away = m["teams"]["away"]["winner"]
        if winner_home:
            if m["teams"]["home"]["id"] == home_af_id:
                home_wins += 1
            else:
                away_wins += 1
        elif winner_away:
            if m["teams"]["away"]["id"] == home_af_id:
                home_wins += 1
            else:
                away_wins += 1
    return {"numberOfMatches": len(matches), "homeTeam": {"wins": home_wins}, "awayTeam": {"wins": away_wins}}


# ---------------------------------------------------------------------------
# Poisson model
# ---------------------------------------------------------------------------


def poisson_pmf(k: int, lam: float) -> float:
    return (lam**k) * math.exp(-lam) / math.factorial(k)


def blended_rate(
    venue_val: float | None,
    venue_n: int,
    overall_val: float | None,
    min_n: int = MIN_VENUE_SAMPLE,
) -> float | None:
    if venue_val is None:
        return overall_val
    if venue_n >= min_n:
        weight_venue = 0.7
    else:
        weight_venue = 0.3 * (venue_n / min_n)
    return weight_venue * venue_val + (1 - weight_venue) * overall_val


def expected_goals(
    home_split: dict[str, Any],
    away_split: dict[str, Any],
    home_ppg: float | None,
    away_ppg: float | None,
    league_avg_ppg: float,
    league_avg_goals: float = 1.35,
) -> tuple[float, float]:
    h_gf = blended_rate(home_split["home_gf"], home_split["home_n"], home_split["overall_gf"])
    h_ga = blended_rate(home_split["home_ga"], home_split["home_n"], home_split["overall_ga"])
    a_gf = blended_rate(away_split["away_gf"], away_split["away_n"], away_split["overall_gf"])
    a_ga = blended_rate(away_split["away_ga"], away_split["away_n"], away_split["overall_ga"])

    home_attack = h_gf / league_avg_goals
    home_defense = h_ga / league_avg_goals
    away_attack = a_gf / league_avg_goals
    away_defense = a_ga / league_avg_goals

    home_lambda = home_attack * away_defense * league_avg_goals * 1.10
    away_lambda = away_attack * home_defense * league_avg_goals * 0.95

    if league_avg_ppg > 0 and home_ppg is not None and away_ppg is not None:
        strength_diff = (home_ppg - away_ppg) / league_avg_ppg
        adj = max(-0.10, min(0.10, strength_diff * 0.08))
        home_lambda *= 1 + adj
        away_lambda *= 1 - adj

    return max(min(home_lambda, 4.5), 0.3), max(min(away_lambda, 4.5), 0.3)


def match_probabilities(home_lambda: float, away_lambda: float) -> dict[str, float]:
    grid: dict[tuple[int, int], float] = {}
    for h, a in product(range(MAX_GOALS + 1), repeat=2):
        grid[(h, a)] = poisson_pmf(h, home_lambda) * poisson_pmf(a, away_lambda)

    p_home = sum(p for (h, a), p in grid.items() if h > a)
    p_draw = sum(p for (h, a), p in grid.items() if h == a)
    p_away = sum(p for (h, a), p in grid.items() if h < a)
    p_over = sum(p for (h, a), p in grid.items() if h + a > 2)
    p_under = 1 - p_over

    return {
        "home": p_home,
        "draw": p_draw,
        "away": p_away,
        "over25": p_over,
        "under25": p_under,
        "1x": p_home + p_draw,
        "x2": p_away + p_draw,
        "12": p_home + p_away,
    }


# ---------------------------------------------------------------------------
# the-odds-api.com helpers
# ---------------------------------------------------------------------------


def get_odds_for_sport(sport_key: str) -> list[dict[str, Any]]:
    base_params = {"apiKey": ODDS_API_KEY, "regions": "eu", "oddsFormat": "decimal"}
    try:
        r = requests.get(
            f"{ODDS_BASE}/sports/{sport_key}/odds",
            params={**base_params, "markets": "h2h,totals,double_chance"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            logger.info("double_chance μη διαθέσιμο για %s, fallback σε h2h+totals", sport_key)
            r = requests.get(
                f"{ODDS_BASE}/sports/{sport_key}/odds",
                params={**base_params, "markets": "h2h,totals"},
                timeout=20,
            )
            r.raise_for_status()
            return r.json()
        raise


def market_summary(event: dict[str, Any]) -> dict[str, Any]:
    prices: dict[str, list[float]] = {
        "home": [],
        "draw": [],
        "away": [],
        "over25": [],
        "under25": [],
        "1x": [],
        "x2": [],
        "12": [],
    }
    home_team = event["home_team"]
    away_team = event["away_team"]
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] == "h2h":
                for outcome in market["outcomes"]:
                    if outcome["name"] == home_team:
                        prices["home"].append(outcome["price"])
                    elif outcome["name"] == away_team:
                        prices["away"].append(outcome["price"])
                    else:
                        prices["draw"].append(outcome["price"])
            elif market["key"] == "totals":
                for outcome in market["outcomes"]:
                    if outcome.get("point") != 2.5:
                        continue
                    if outcome["name"] == "Over":
                        prices["over25"].append(outcome["price"])
                    elif outcome["name"] == "Under":
                        prices["under25"].append(outcome["price"])
            elif market["key"] == "double_chance":
                for outcome in market["outcomes"]:
                    name = outcome["name"]
                    has_home = home_team in name
                    has_away = away_team in name
                    if has_home and has_away:
                        prices["12"].append(outcome["price"])
                    elif has_home:
                        prices["1x"].append(outcome["price"])
                    elif has_away:
                        prices["x2"].append(outcome["price"])

    n_bookmakers = len(event.get("bookmakers", []))
    best = {k: (max(v) if v else 0) for k, v in prices.items()}
    median = {k: (statistics.median(v) if v else 0) for k, v in prices.items()}
    return {"best": best, "median": median, "n_bookmakers": n_bookmakers}


def implied_probs_no_vig(price_dict: dict[str, float], keys: list[str]) -> dict[str, float]:
    raw = {k: (1 / price_dict[k]) for k in keys if price_dict.get(k)}
    total = sum(raw.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def implied_probs_double_chance(price_dict: dict[str, float]) -> dict[str, float]:
    keys = ["1x", "x2", "12"]
    raw = {k: (1 / price_dict[k]) for k in keys if price_dict.get(k)}
    if len(raw) < 2:
        return {}
    total = sum(raw.values())
    if total == 0:
        return {}
    return {k: v * (2 / total) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Team-name matching
# ---------------------------------------------------------------------------


def normalize(name: str) -> str:
    return name.lower().replace("fc", "").replace("cf", "").replace(".", "").replace("-", " ").strip()


def _substring_name_match(a: str, b: str) -> bool:
    return a in b or b in a


def _fuzzy_name_match(a: str, b: str) -> bool:
    if _substring_name_match(a, b):
        return True
    if HAS_RAPIDFUZZ:
        return fuzz.token_sort_ratio(a, b) >= FUZZY_MATCH_THRESHOLD
    return False


def find_matching_event(
    fixture: dict[str, Any],
    odds_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    home_n = normalize(fixture["homeTeam"]["name"])
    away_n = normalize(fixture["awayTeam"]["name"])
    for ev in odds_events:
        ev_home = normalize(ev["home_team"])
        ev_away = normalize(ev["away_team"])
        if _fuzzy_name_match(home_n, ev_home) and _fuzzy_name_match(away_n, ev_away):
            return ev
    return None


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def _build_candidates(
    home_name: str,
    away_name: str,
    model_probs: dict[str, float],
    market_1x2: dict[str, float],
    market_ou: dict[str, float],
    market_dc: dict[str, float],
    odds: dict[str, float],
) -> list[tuple[float, str, str, float, float]]:
    candidates: list[tuple[float, str, str, float, float]] = []
    for key, label in [
        ("home", f"{home_name} νίκη"),
        ("draw", "Ισοπαλία"),
        ("away", f"{away_name} νίκη"),
    ]:
        if key in market_1x2 and odds.get(key):
            edge = model_probs[key] - market_1x2[key]
            candidates.append((edge, label, key, odds[key], model_probs[key]))
    for key, label in [("over25", "Over 2.5 goals"), ("under25", "Under 2.5 goals")]:
        if key in market_ou and odds.get(key):
            edge = model_probs[key] - market_ou[key]
            candidates.append((edge, label, key, odds[key], model_probs[key]))
    for key, label in [
        ("1x", f"{home_name} ή Ισοπαλία (Double Chance)"),
        ("x2", f"{away_name} ή Ισοπαλία (Double Chance)"),
        ("12", f"{home_name} ή {away_name}, χωρίς ισοπαλία (Double Chance)"),
    ]:
        if key in market_dc and odds.get(key):
            edge = model_probs[key] - market_dc[key]
            candidates.append((edge, label, key, odds[key], model_probs[key]))
    return candidates


def analyze_competition(
    comp_code: str,
    sport_key: str,
    results: list[dict[str, Any]],
    stats: dict[str, int],
    calibration_factor: float = 1.0,
) -> None:
    try:
        fixtures = get_todays_fixtures(comp_code)
    except Exception as e:
        logger.warning("[%s] fixtures error: %s", comp_code, e)
        stats["errors"] += 1
        return
    if not fixtures:
        return
    stats["fixtures_found"] += len(fixtures)

    try:
        odds_events = get_odds_for_sport(sport_key)
    except Exception as e:
        logger.warning("[%s] odds error: %s", comp_code, e)
        stats["errors"] += 1
        return

    ppg_table = get_standings(comp_code)
    league_avg_ppg = (sum(ppg_table.values()) / len(ppg_table)) if ppg_table else 1.35

    for fx in fixtures:
        home_id, away_id = fx["homeTeam"]["id"], fx["awayTeam"]["id"]
        home_name, away_name = fx["homeTeam"]["name"], fx["awayTeam"]["name"]

        try:
            home_matches = get_team_matches(home_id)
            away_matches = get_team_matches(away_id)
        except Exception as e:
            logger.warning("[%s] team matches error: %s", comp_code, e)
            continue

        home_split = split_home_away_stats(home_matches, home_id)
        away_split = split_home_away_stats(away_matches, away_id)
        if home_split["overall_gf"] is None or away_split["overall_gf"] is None:
            continue
        home_total_n = home_split["home_n"] + home_split["away_n"]
        away_total_n = away_split["home_n"] + away_split["away_n"]
        if home_total_n < MIN_TOTAL_SAMPLE or away_total_n < MIN_TOTAL_SAMPLE:
            continue

        event = find_matching_event(fx, odds_events)
        if not event:
            continue

        summary = market_summary(event)
        odds = summary["best"]
        fair_odds = summary["median"]

        home_ppg = ppg_table.get(home_id)
        away_ppg = ppg_table.get(away_id)
        h_lam, a_lam = expected_goals(home_split, away_split, home_ppg, away_ppg, league_avg_ppg)
        model_probs = match_probabilities(h_lam, a_lam)
        model_probs = apply_calibration(model_probs, calibration_factor)

        market_1x2 = implied_probs_no_vig(fair_odds, ["home", "draw", "away"])
        market_ou = implied_probs_no_vig(fair_odds, ["over25", "under25"])
        market_dc = implied_probs_double_chance(fair_odds)

        h2h = get_h2h(fx["id"])
        candidates = _build_candidates(home_name, away_name, model_probs, market_1x2, market_ou, market_dc, odds)
        debug_log_match(home_name, away_name, model_probs, market_1x2, market_ou, market_dc, candidates)

        positive_edge = [c for c in candidates if c[0] > 0]
        if not positive_edge:
            continue

        edge, label, key, price, model_p = max(positive_edge, key=lambda c: c[0])

        data_quality = 0
        if home_split["home_n"] >= MIN_VENUE_SAMPLE:
            data_quality += 1
        if away_split["away_n"] >= MIN_VENUE_SAMPLE:
            data_quality += 1
        if home_ppg is not None and away_ppg is not None:
            data_quality += 1

        h2h_agrees = None
        if h2h and h2h.get("numberOfMatches", 0) >= 2 and key in ("home", "away", "draw"):
            hw = h2h.get("homeTeam", {}).get("wins", 0)
            aw = h2h.get("awayTeam", {}).get("wins", 0)
            if key == "home":
                h2h_agrees = hw > aw
            elif key == "away":
                h2h_agrees = aw > hw
            else:
                h2h_agrees = abs(hw - aw) <= 1

        results.append({
            "match": f"{home_name} - {away_name}",
            "competition": comp_code,
            "pick": label,
            "odds": round(price, 2),
            "model_prob": round(model_p * 100, 1),
            "edge": round(edge * 100, 1),
            "data_quality": data_quality,
            "h2h_agrees": h2h_agrees,
            "bookmaker_consensus": summary["n_bookmakers"] >= MIN_BOOKMAKERS,
            "n_bookmakers": summary["n_bookmakers"],
            "fixture_id": fx["id"],
            "source": "fd",
            "market_key": key,
        })


def analyze_af_league(
    league_name: str,
    league_id: int,
    odds_sport_key: str,
    results: list[dict[str, Any]],
    stats: dict[str, int],
    calibration_factor: float = 1.0,
) -> None:
    if not API_FOOTBALL_KEY:
        return
    season = current_season_year()

    try:
        fixtures = get_af_todays_fixtures(league_id, season)
    except Exception as e:
        logger.warning("[AF %s] fixtures error: %s", league_name, e)
        stats["errors"] += 1
        return
    if not fixtures:
        return
    stats["fixtures_found"] += len(fixtures)

    try:
        odds_events = get_odds_for_sport(odds_sport_key)
    except Exception as e:
        logger.warning("[AF %s] odds error: %s", league_name, e)
        stats["errors"] += 1
        return

    ppg_table = get_af_standings_ppg(league_id, season)
    league_avg_ppg = (sum(ppg_table.values()) / len(ppg_table)) if ppg_table else 1.35

    for fx in fixtures:
        home_id = fx["teams"]["home"]["id"]
        away_id = fx["teams"]["away"]["id"]
        home_name = fx["teams"]["home"]["name"]
        away_name = fx["teams"]["away"]["name"]

        try:
            home_split = get_af_team_stats(league_id, season, home_id)
            away_split = get_af_team_stats(league_id, season, away_id)
        except Exception as e:
            logger.warning("[AF %s] team stats error: %s", league_name, e)
            continue
        if not home_split or not away_split or home_split["overall_gf"] is None or away_split["overall_gf"] is None:
            continue
        home_total_n = home_split["home_n"] + home_split["away_n"]
        away_total_n = away_split["home_n"] + away_split["away_n"]
        if home_total_n < MIN_TOTAL_SAMPLE or away_total_n < MIN_TOTAL_SAMPLE:
            continue

        event = find_matching_event(
            {"homeTeam": {"name": home_name}, "awayTeam": {"name": away_name}},
            odds_events,
        )
        if not event:
            continue

        summary = market_summary(event)
        odds = summary["best"]
        fair_odds = summary["median"]

        home_ppg = ppg_table.get(home_id)
        away_ppg = ppg_table.get(away_id)
        h_lam, a_lam = expected_goals(home_split, away_split, home_ppg, away_ppg, league_avg_ppg)
        model_probs = match_probabilities(h_lam, a_lam)
        model_probs = apply_calibration(model_probs, calibration_factor)

        market_1x2 = implied_probs_no_vig(fair_odds, ["home", "draw", "away"])
        market_ou = implied_probs_no_vig(fair_odds, ["over25", "under25"])
        market_dc = implied_probs_double_chance(fair_odds)

        h2h = get_af_h2h(home_id, away_id)
        candidates = _build_candidates(home_name, away_name, model_probs, market_1x2, market_ou, market_dc, odds)
        debug_log_match(home_name, away_name, model_probs, market_1x2, market_ou, market_dc, candidates)

        positive_edge = [c for c in candidates if c[0] > 0]
        if not positive_edge:
            continue

        edge, label, key, price, model_p = max(positive_edge, key=lambda c: c[0])

        data_quality = 0
        if home_split["home_n"] >= MIN_VENUE_SAMPLE:
            data_quality += 1
        if away_split["away_n"] >= MIN_VENUE_SAMPLE:
            data_quality += 1
        if home_ppg is not None and away_ppg is not None:
            data_quality += 1

        h2h_agrees = None
        if h2h and h2h.get("numberOfMatches", 0) >= 2 and key in ("home", "away", "draw"):
            hw = h2h.get("homeTeam", {}).get("wins", 0)
            aw = h2h.get("awayTeam", {}).get("wins", 0)
            if key == "home":
                h2h_agrees = hw > aw
            elif key == "away":
                h2h_agrees = aw > hw
            else:
                h2h_agrees = abs(hw - aw) <= 1

        results.append({
            "match": f"{home_name} - {away_name}",
            "competition": league_name,
            "pick": label,
            "odds": round(price, 2),
            "model_prob": round(model_p * 100, 1),
            "edge": round(edge * 100, 1),
            "data_quality": data_quality,
            "h2h_agrees": h2h_agrees,
            "bookmaker_consensus": summary["n_bookmakers"] >= MIN_BOOKMAKERS,
            "n_bookmakers": summary["n_bookmakers"],
            "fixture_id": fx["fixture"]["id"],
            "source": "af",
            "market_key": key,
        })


def debug_log_match(
    home_name: str,
    away_name: str,
    model_probs: dict[str, float],
    market_1x2: dict[str, float],
    market_ou: dict[str, float],
    market_dc: dict[str, float],
    candidates: list[tuple[float, str, str, float, float]],
) -> None:
    best_edge = max((c[0] for c in candidates), default=None)
    logger.info(
        "  [%s - %s] model(H/D/A)=%.3f/%.3f/%.3f market(H/D/A)=%.3f/%.3f/%.3f "
        "model(O/U 2.5)=%.3f/%.3f market(O/U 2.5)=%.3f/%.3f best_edge=%s",
        home_name,
        away_name,
        model_probs["home"],
        model_probs["draw"],
        model_probs["away"],
        market_1x2.get("home", 0),
        market_1x2.get("draw", 0),
        market_1x2.get("away", 0),
        model_probs["over25"],
        model_probs["under25"],
        market_ou.get("over25", 0),
        market_ou.get("under25", 0),
        best_edge if best_edge is None else round(best_edge, 4),
    )


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not r.ok:
        logger.error("Telegram error response: %s %s", r.status_code, r.text)
    r.raise_for_status()


def confidence_tier(r: dict[str, Any]) -> str:
    model_prob = r["model_prob"] / 100
    edge = r["edge"] / 100
    signals_ok = (r["data_quality"] >= 2) and r["bookmaker_consensus"] and (r["h2h_agrees"] is not False)

    if model_prob >= MIN_MODEL_PROB and edge >= MIN_EDGE and signals_ok:
        return "🟢 ΥΨΗΛΗΣ ΣΙΓΟΥΡΙΑΣ"
    if model_prob >= 0.50 and edge >= 0 and r["data_quality"] >= 1:
        return "🟡 ΜΕΤΡΙΑΣ ΣΙΓΟΥΡΙΑΣ"
    return "🔴 ΧΑΜΗΛΗΣ ΣΙΓΟΥΡΙΑΣ — καλύτερο διαθέσιμο σήμερα, όχι κάτι που θα έπαιζα κανονικά"


def select_top_picks(results: list[dict[str, Any]], n: int = 4) -> list[dict[str, Any]]:
    ranked = sorted(
        results,
        key=lambda r: (r["model_prob"] / 100, r["edge"] / 100, r["data_quality"]),
        reverse=True,
    )
    return ranked[:n]


def format_message(top: list[dict[str, Any]], stats: dict[str, int]) -> str:
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    if not top:
        if stats["errors"] > 0 and stats["fixtures_found"] == 0:
            return (
                f"⚽ <b>Προτάσεις — {today}</b>\n\n"
                f"⚠️ {stats['errors']} λίγκες απέτυχαν λόγω σφάλματος API (δες logs "
                "στο GitHub Actions) — δεν μπόρεσα να ελέγξω κανένα ματς σήμερα."
            )
        if stats["fixtures_found"] == 0:
            return (
                f"⚽ <b>Προτάσεις — {today}</b>\n\n"
                "Δεν παίζει καμία διοργάνωση σήμερα σε καμία από τις λίγκες που "
                "καλύπτω (φυσιολογικό π.χ. Δευτέρες, ή σε διεθνείς διακοπές) — "
                "όχι πρόβλημα σύνδεσης. Ξαναδοκίμασε αύριο."
            )
        return (
            f"⚽ <b>Προτάσεις — {today}</b>\n\n"
            f"Βρέθηκαν {stats['fixtures_found']} ματς σήμερα, αλλά κανένα δεν "
            "είχε θετικό edge έναντι της αγοράς — δηλαδή η αγορά "
            "'συμφωνούσε' με το μοντέλο παντού. Στατιστικά φυσιολογικό, όχι "
            "σφάλμα. Καλύτερα καμία πρόταση παρά κάτι χωρίς πλεονέκτημα."
        )

    lines = [f"⚽ <b>Προτάσεις — {today}</b>\n"]
    for i, r in enumerate(top, 1):
        tier = confidence_tier(r)
        h2h_note = {
            True: "✅ H2H συμφωνεί",
            False: "⚠️ H2H διαφωνεί",
            None: "ℹ️ Ανεπαρκές H2H",
        }[r["h2h_agrees"]]
        competition = html.escape(str(r["competition"]))
        match = html.escape(str(r["match"]))
        pick = html.escape(str(r["pick"]))
        lines.append(
            f"{i}. {tier}\n"
            f"🏆 {competition} — <b>{match}</b>\n"
            f"Pick: <b>{pick}</b>  @ {r['odds']}  "
            f"({r['model_prob']}% μοντέλο, +{r['edge']}% edge, {h2h_note}, "
            f"{r['n_bookmakers']} bookmakers)\n"
        )

    lines.append("🔎 Βρες τα στο stoiximan.cy αναζητώντας τα ματς παραπάνω.\n")
    if len(top) == 1:
        lines.append(
            "⚠️ Στατιστική εκτίμηση, ΟΧΙ εγγύηση — μη ποντάρεις κάτι που "
            "δεν αντέχεις να χάσεις."
        )
    else:
        lines.append(
            f"⚠️ Αυτά είναι {len(top)} ΑΝΕΞΑΡΤΗΤΑ picks, το καθένα με τη δική "
            "του σιγουριά — ΔΕΝ είναι προτεινόμενο combo. Αν τα παίξεις όλα "
            "μαζί σε ένα combo, ο συνδυασμένος κίνδυνος πολλαπλασιάζεται "
            "(ακόμα κι αν το καθένα ξεχωριστά είναι 🟢, μαζί μπορεί να έχουν "
            "λιγότερο από 50% να βγουν όλα). Στατιστική εκτίμηση, ΟΧΙ "
            "εγγύηση — μη ποντάρεις κάτι που δεν αντέχεις να χάσεις."
        )

    return "\n".join(lines)


def already_ran_today() -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            if f.read().strip() == today:
                return True
    return False


def mark_ran_today() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(today)


# ---------------------------------------------------------------------------
# Self-calibration
# ---------------------------------------------------------------------------


def load_json_state(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json_state(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_history() -> dict[str, Any]:
    return load_json_state(HISTORY_FILE, {"picks": [], "calibration_factor": 1.0})


def apply_calibration(probs: dict[str, float], factor: float) -> dict[str, float]:
    return {k: max(0.01, min(0.99, 0.5 + (p - 0.5) * factor)) for k, p in probs.items()}


def pick_won(market_key: str, home_goals: int, away_goals: int) -> bool | None:
    if market_key == "home":
        return home_goals > away_goals
    if market_key == "draw":
        return home_goals == away_goals
    if market_key == "away":
        return home_goals < away_goals
    if market_key == "over25":
        return (home_goals + away_goals) > 2
    if market_key == "under25":
        return (home_goals + away_goals) <= 2
    if market_key == "1x":
        return home_goals >= away_goals
    if market_key == "x2":
        return away_goals >= home_goals
    if market_key == "12":
        return home_goals != away_goals
    return None


def get_fixture_outcome(source: str, fixture_id: int) -> dict[str, Any]:
    """Επιστρέφει dict με status: finished|void|pending και optional score."""
    try:
        if source == "fd":
            data = fd_get(f"/matches/{fixture_id}")
            status = data.get("status", "")
            if status in FD_VOID_STATUSES:
                return {"status": "void", "reason": status}
            if status != "FINISHED":
                return {"status": "pending"}
            score = data.get("score", {}).get("fullTime", {})
            h, a = score.get("home"), score.get("away")
            if h is None or a is None:
                return {"status": "pending"}
            return {"status": "finished", "home_goals": h, "away_goals": a}

        data = af_get("/fixtures", params={"id": fixture_id})
        resp = data.get("response", [])
        if not resp:
            return {"status": "pending"}
        fx = resp[0]
        short_status = fx.get("fixture", {}).get("status", {}).get("short", "")
        if short_status in AF_VOID_STATUSES:
            return {"status": "void", "reason": short_status}
        if short_status != "FT":
            return {"status": "pending"}
        goals = fx.get("goals", {})
        h, a = goals.get("home"), goals.get("away")
        if h is None or a is None:
            return {"status": "pending"}
        return {"status": "finished", "home_goals": h, "away_goals": a}
    except Exception as e:
        logger.warning("αποτυχία ελέγχου αποτελέσματος fixture %s: %s", fixture_id, e)
        return {"status": "pending"}


def resolve_pending_picks(history: dict[str, Any]) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    resolved_count = 0
    void_count = 0
    for pick in history["picks"]:
        if pick.get("result") is not None:
            continue
        pick_date = datetime.strptime(pick["date"], "%Y-%m-%d").date()
        days_old = (today - pick_date).days
        if days_old < 1:
            continue

        outcome = get_fixture_outcome(pick["source"], pick["fixture_id"])

        if outcome["status"] == "void":
            pick["result"] = "void"
            pick["final_score"] = outcome.get("reason", "void")
            void_count += 1
            continue

        if outcome["status"] == "finished":
            home_goals = outcome["home_goals"]
            away_goals = outcome["away_goals"]
            won = pick_won(pick["market_key"], home_goals, away_goals)
            if won is None:
                continue
            pick["result"] = "win" if won else "loss"
            pick["final_score"] = f"{home_goals}-{away_goals}"
            resolved_count += 1
            continue

        if days_old >= PICK_RESOLVE_TIMEOUT_DAYS:
            pick["result"] = "void"
            pick["final_score"] = "timeout"
            void_count += 1
            logger.info(
                "Pick timeout (%s days): %s — %s",
                days_old,
                pick.get("match", pick["fixture_id"]),
                pick.get("pick"),
            )

    if resolved_count:
        logger.info("Επιλύθηκαν %s παλιά picks (νίκη/ήττα καταγράφηκε).", resolved_count)
    if void_count:
        logger.info("Χαρακτηρίστηκαν %s picks ως void (ακυρώθηκαν/timeout).", void_count)
    return history


def update_calibration(history: dict[str, Any]) -> float:
    resolved = [p for p in history["picks"] if p.get("result") in ("win", "loss")]
    current_factor = history.get("calibration_factor", 1.0)

    if len(resolved) < MIN_RESOLVED_FOR_CALIBRATION:
        logger.info(
            "Calibration: μόνο %s resolved picks (χρειάζονται %s+) — καμία αλλαγή.",
            len(resolved),
            MIN_RESOLVED_FOR_CALIBRATION,
        )
        return current_factor

    recent = resolved[-100:]
    avg_predicted = sum(p["model_prob_raw"] for p in recent) / len(recent)
    actual_hit_rate = sum(1 for p in recent if p["result"] == "win") / len(recent)

    logger.info(
        "Calibration check: %s resolved picks, μέση δηλωμένη πιθανότητα=%.3f, "
        "πραγματικό ποσοστό επιτυχίας=%.3f",
        len(recent),
        avg_predicted,
        actual_hit_rate,
    )

    diff = actual_hit_rate - avg_predicted
    if diff < -0.03:
        new_factor = current_factor - CALIBRATION_STEP
    elif diff > 0.03:
        new_factor = current_factor + CALIBRATION_STEP
    else:
        new_factor = current_factor

    new_factor = max(CALIBRATION_MIN, min(CALIBRATION_MAX, new_factor))
    if new_factor != current_factor:
        logger.info("Calibration factor: %.3f -> %.3f", current_factor, new_factor)
    return new_factor


def record_new_picks(history: dict[str, Any], top_picks: list[dict[str, Any]], today_str: str) -> None:
    for p in top_picks:
        history["picks"].append({
            "date": today_str,
            "source": p["source"],
            "fixture_id": p["fixture_id"],
            "market_key": p["market_key"],
            "match": p["match"],
            "pick": p["pick"],
            "odds": p["odds"],
            "model_prob_raw": p["model_prob"] / 100,
            "result": None,
        })
    history["picks"] = history["picks"][-500:]


def main() -> None:
    configure_logging()
    clear_run_caches()

    if not HAS_RAPIDFUZZ:
        logger.warning("rapidfuzz not installed — using substring-only team matching")

    missing = [
        n
        for n, v in [
            ("FOOTBALL_DATA_API_KEY", FOOTBALL_DATA_API_KEY),
            ("ODDS_API_KEY", ODDS_API_KEY),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ]
        if not v
    ]
    if missing:
        logger.error("Λείπουν env vars: %s", missing)
        sys.exit(1)

    history = load_history()
    history = resolve_pending_picks(history)
    calibration_factor = update_calibration(history)
    history["calibration_factor"] = calibration_factor
    save_json_state(HISTORY_FILE, history)

    if already_ran_today():
        logger.info("Ήδη στάλθηκε μήνυμα σήμερα — παραλείπεται αυτό το run.")
        return

    if not API_FOOTBALL_KEY:
        logger.warning(
            "API_FOOTBALL_KEY δεν έχει οριστεί — παραλείπονται "
            "Ελλάδα/Αυστρία/Ελβετία/Πολωνία/Τουρκία/Σκωτία/Βέλγιο."
        )

    results: list[dict[str, Any]] = []
    stats = {"fixtures_found": 0, "errors": 0}
    for comp_code, sport_key in COMPETITIONS.items():
        analyze_competition(comp_code, sport_key, results, stats, calibration_factor)

    for league_name, cfg in AF_LEAGUES.items():
        analyze_af_league(league_name, cfg["league_id"], cfg["odds_key"], results, stats, calibration_factor)

    logger.info("Σύνολο ματς σήμερα: %s, σφάλματα λιγκών: %s", stats["fixtures_found"], stats["errors"])
    top = select_top_picks(results, n=4)
    message = format_message(top, stats)
    logger.info(message)
    send_telegram(message)
    mark_ran_today()

    if top:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        history = load_history()
        record_new_picks(history, top, today_str)
        save_json_state(HISTORY_FILE, history)


if __name__ == "__main__":
    main()
