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

import os
import sys
import math
import time
import html
import requests
from datetime import datetime, timezone
from itertools import product

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")   # δωρεάν, api-football.com — 100 req/μέρα
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FD_BASE = "https://api.football-data.org/v4"
ODDS_BASE = "https://api.the-odds-api.com/v4"
AF_BASE = "https://v3.football.api-sports.io"

# football-data.org competition code -> the-odds-api sport key
# Αυτό είναι η ΜΕΓΙΣΤΗ κάλυψη στο δωρεάν tier του football-data.org (13
# διοργανώσεις συνολικά). Οι WC/EC είναι τουρνουά — θα έχουν fixtures μόνο
# στα διαστήματα που παίζονται, τις υπόλοιπες μέρες απλά προσπερνιούνται.
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

# Επιπλέον λίγκες μέσω API-Football (δωρεάν, 100 req/μέρα — γι' αυτό ΔΕΝ
# αντικαθιστά το football-data.org, απλά προσθέτει ό,τι λείπει από εκεί).
# league_id: επίσημο ID API-Football. season: έτος έναρξης σεζόν (π.χ. η
# σεζόν 2026-27 είναι season=2026 στο API).
# ΠΡΟΣΟΧΗ: επιβεβαίωσε τα league_id όταν πάρεις το key σου, καλώντας
# GET https://v3.football.api-sports.io/leagues?name=<όνομα> — αν κάποιο
# ID έχει αλλάξει, ενημέρωσέ το εδώ.
def current_season_year():
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1

AF_LEAGUES = {
    "Greece Super League":       {"league_id": 197, "odds_key": "soccer_greece_super_league"},
    "Austria Bundesliga":        {"league_id": 218, "odds_key": "soccer_austria_bundesliga"},
    "Switzerland Super League":  {"league_id": 207, "odds_key": "soccer_switzerland_superleague"},
    "Poland Ekstraklasa":        {"league_id": 106, "odds_key": "soccer_poland_ekstraklasa"},
    "Turkey Super Lig":          {"league_id": 203, "odds_key": "soccer_turkey_super_league"},
    "Scotland Premiership":      {"league_id": 179, "odds_key": "soccer_scotland_premiership"},
    "Belgium Pro League":        {"league_id": 144, "odds_key": "soccer_belgium_first_div"},
}

MIN_EDGE = 0.03              # edge για "Υψηλής σιγουρίας" tier
MIN_MODEL_PROB = 0.60        # πιθανότητα μοντέλου για "Υψηλής σιγουρίας" tier
MAX_GOALS = 6                # όριο goals στο Poisson grid
RECENT_MATCHES = 10          # πόσα πρόσφατα ματς τραβάμε ανά ομάδα (any venue)
MIN_VENUE_SAMPLE = 3         # ελάχιστα ματς-στο-ίδιο-venue για να τα εμπιστευτούμε
REQUEST_PAUSE = 6.5          # football-data.org free tier = 10 req/min
MIN_BOOKMAKERS = 2           # πόσα bookmakers min για να θεωρηθεί η τιμή αξιόπιστη


# ---------------------------------------------------------------------------
# football-data.org helpers
# ---------------------------------------------------------------------------

def fd_get(path, params=None):
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    r = requests.get(f"{FD_BASE}{path}", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    time.sleep(REQUEST_PAUSE)  # μένουμε μέσα στο free-tier rate limit
    return r.json()


def get_todays_fixtures(comp_code):
    today = datetime.now(timezone.utc).date()
    data = fd_get(
        f"/competitions/{comp_code}/matches",
        params={"dateFrom": str(today), "dateTo": str(today), "status": "SCHEDULED"},
    )
    return data.get("matches", [])


def get_standings(comp_code):
    """points-per-game ανά team_id, από τον τρέχοντα πίνακα βαθμολογίας."""
    try:
        data = fd_get(f"/competitions/{comp_code}/standings")
    except Exception as e:
        print(f"[{comp_code}] standings error: {e}")
        return {}

    ppg = {}
    for table_group in data.get("standings", []):
        if table_group.get("type") != "TOTAL":
            continue
        for row in table_group.get("table", []):
            played = row.get("playedGames") or 0
            if played > 0:
                ppg[row["team"]["id"]] = row["points"] / played
    return ppg


def get_team_matches(team_id):
    """Τελευταία N ΤΕΛΕΙΩΜΕΝΑ ματς μιας ομάδας, οποιαδήποτε διοργάνωση."""
    data = fd_get(f"/teams/{team_id}/matches", params={"status": "FINISHED", "limit": RECENT_MATCHES})
    return data.get("matches", [])


def split_home_away_stats(matches, team_id):
    """Από τη λίστα ματς μιας ομάδας, βγάζει overall / home-only / away-only
    goals-for / goals-against averages."""
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

    def avg(lst):
        return sum(lst) / len(lst) if lst else None

    return {
        "overall_gf": avg(overall_gf), "overall_ga": avg(overall_ga),
        "home_gf": avg(home_gf), "home_ga": avg(home_ga), "home_n": len(home_gf),
        "away_gf": avg(away_gf), "away_ga": avg(away_ga), "away_n": len(away_gf),
    }


def get_h2h(fixture_id, limit=5):
    """Ιστορικές αναμετρήσεις μεταξύ των δύο ομάδων αυτού του fixture."""
    try:
        data = fd_get(f"/matches/{fixture_id}/head2head", params={"limit": limit})
    except Exception as e:
        print(f"h2h error for fixture {fixture_id}: {e}")
        return None
    return data.get("aggregates")


# ---------------------------------------------------------------------------
# API-Football helpers (επιπλέον λίγκες: Ελλάδα, Αυστρία, Ελβετία, Πολωνία,
# Τουρκία, Σκωτία — δωρεάν αλλά με όριο 100 requests/μέρα, γι' αυτό
# χρησιμοποιείται μόνο ΣΥΜΠΛΗΡΩΜΑΤΙΚΑ, όχι σαν κύρια πηγή).
# ---------------------------------------------------------------------------

def af_get(path, params=None):
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    r = requests.get(f"{AF_BASE}{path}", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_af_todays_fixtures(league_id, season):
    today = datetime.now(timezone.utc).date()
    data = af_get("/fixtures", params={"league": league_id, "season": season, "date": str(today)})
    return data.get("response", [])


def get_af_team_stats(league_id, season, team_id):
    """API-Football δίνει ΚΑΤΕΥΘΕΙΑΝ season averages home/away — δε
    χρειάζεται να μαζέψουμε ματς ένα-ένα όπως στο football-data.org."""
    data = af_get("/teams/statistics", params={"league": league_id, "season": season, "team": team_id})
    resp = data.get("response")
    if not resp:
        return None
    goals_for = resp.get("goals", {}).get("for", {}).get("average", {})
    goals_against = resp.get("goals", {}).get("against", {}).get("average", {})
    played = resp.get("fixtures", {}).get("played", {})

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "overall_gf": to_float(goals_for.get("total")), "overall_ga": to_float(goals_against.get("total")),
        "home_gf": to_float(goals_for.get("home")), "home_ga": to_float(goals_against.get("home")),
        "home_n": played.get("home", 0) or 0,
        "away_gf": to_float(goals_for.get("away")), "away_ga": to_float(goals_against.get("away")),
        "away_n": played.get("away", 0) or 0,
    }


def get_af_standings_ppg(league_id, season):
    try:
        data = af_get("/standings", params={"league": league_id, "season": season})
    except Exception as e:
        print(f"[AF {league_id}] standings error: {e}")
        return {}
    ppg = {}
    try:
        table = data["response"][0]["league"]["standings"][0]
        for row in table:
            played = row.get("all", {}).get("played") or 0
            if played > 0:
                ppg[row["team"]["id"]] = row["points"] / played
    except (IndexError, KeyError, TypeError):
        pass
    return ppg


def get_af_h2h(home_af_id, away_af_id, limit=5):
    try:
        data = af_get("/fixtures/headtohead", params={"h2h": f"{home_af_id}-{away_af_id}", "last": limit})
    except Exception as e:
        print(f"AF h2h error: {e}")
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

def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def blended_rate(venue_val, venue_n, overall_val, min_n=MIN_VENUE_SAMPLE):
    """Αν έχουμε αρκετά venue-specific δεδομένα, τα εμπιστευόμαστε περισσότερο.
    Αλλιώς κάνουμε blend προς το overall."""
    if venue_val is None:
        return overall_val
    if venue_n >= min_n:
        weight_venue = 0.7
    else:
        weight_venue = 0.3 * (venue_n / min_n)
    return weight_venue * venue_val + (1 - weight_venue) * overall_val


def expected_goals(home_split, away_split, home_ppg, away_ppg, league_avg_ppg, league_avg_goals=1.35):
    """Συνδυάζει venue-specific form + standings strength."""
    h_gf = blended_rate(home_split["home_gf"], home_split["home_n"], home_split["overall_gf"])
    h_ga = blended_rate(home_split["home_ga"], home_split["home_n"], home_split["overall_ga"])
    a_gf = blended_rate(away_split["away_gf"], away_split["away_n"], away_split["overall_gf"])
    a_ga = blended_rate(away_split["away_ga"], away_split["away_n"], away_split["overall_ga"])

    home_attack = h_gf / league_avg_goals
    home_defense = h_ga / league_avg_goals
    away_attack = a_gf / league_avg_goals
    away_defense = a_ga / league_avg_goals

    home_lambda = home_attack * away_defense * league_avg_goals * 1.10  # home advantage
    away_lambda = away_attack * home_defense * league_avg_goals * 0.95

    # Standings adjustment: sanity-check πάνω στο goals-based μοντέλο (±10% max)
    if league_avg_ppg > 0 and home_ppg is not None and away_ppg is not None:
        strength_diff = (home_ppg - away_ppg) / league_avg_ppg
        adj = max(-0.10, min(0.10, strength_diff * 0.08))
        home_lambda *= (1 + adj)
        away_lambda *= (1 - adj)

    return max(home_lambda, 0.1), max(away_lambda, 0.1)


def match_probabilities(home_lambda, away_lambda):
    grid = {}
    for h, a in product(range(MAX_GOALS + 1), repeat=2):
        grid[(h, a)] = poisson_pmf(h, home_lambda) * poisson_pmf(a, away_lambda)

    p_home = sum(p for (h, a), p in grid.items() if h > a)
    p_draw = sum(p for (h, a), p in grid.items() if h == a)
    p_away = sum(p for (h, a), p in grid.items() if h < a)
    p_over = sum(p for (h, a), p in grid.items() if h + a > 2)
    p_under = 1 - p_over

    return {"home": p_home, "draw": p_draw, "away": p_away, "over25": p_over, "under25": p_under}


# ---------------------------------------------------------------------------
# the-odds-api.com helpers
# ---------------------------------------------------------------------------

def get_odds_for_sport(sport_key):
    r = requests.get(
        f"{ODDS_BASE}/sports/{sport_key}/odds",
        params={"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def market_summary(event):
    """Μαζεύει τιμές απ' ΟΛΑ τα bookmakers, ώστε να μπορούμε να δούμε πόσα
    συμφωνούν (consensus) και όχι μόνο 1 outlier τιμή."""
    prices = {"home": [], "draw": [], "away": [], "over25": [], "under25": []}
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] == "h2h":
                for outcome in market["outcomes"]:
                    if outcome["name"] == event["home_team"]:
                        prices["home"].append(outcome["price"])
                    elif outcome["name"] == event["away_team"]:
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

    n_bookmakers = len(event.get("bookmakers", []))
    best = {k: (max(v) if v else 0) for k, v in prices.items()}
    return {"best": best, "n_bookmakers": n_bookmakers}


def implied_probs_no_vig(price_dict, keys):
    raw = {k: (1 / price_dict[k]) for k in keys if price_dict.get(k)}
    total = sum(raw.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Team-name matching
# ---------------------------------------------------------------------------

def normalize(name):
    return name.lower().replace("fc", "").replace("cf", "").replace(".", "").replace("-", " ").strip()


def find_matching_event(fixture, odds_events):
    home_n = normalize(fixture["homeTeam"]["name"])
    away_n = normalize(fixture["awayTeam"]["name"])
    for ev in odds_events:
        ev_home = normalize(ev["home_team"])
        ev_away = normalize(ev["away_team"])
        if (home_n in ev_home or ev_home in home_n) and (away_n in ev_away or ev_away in away_n):
            return ev
    return None


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def analyze_competition(comp_code, sport_key, results, stats):
    try:
        fixtures = get_todays_fixtures(comp_code)
    except Exception as e:
        print(f"[{comp_code}] fixtures error: {e}")
        stats["errors"] += 1
        return
    if not fixtures:
        return
    stats["fixtures_found"] += len(fixtures)

    try:
        odds_events = get_odds_for_sport(sport_key)
    except Exception as e:
        print(f"[{comp_code}] odds error: {e}")
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
            print(f"[{comp_code}] team matches error: {e}")
            continue

        home_split = split_home_away_stats(home_matches, home_id)
        away_split = split_home_away_stats(away_matches, away_id)
        if home_split["overall_gf"] is None or away_split["overall_gf"] is None:
            continue

        event = find_matching_event(fx, odds_events)
        if not event:
            continue

        summary = market_summary(event)
        odds = summary["best"]

        home_ppg = ppg_table.get(home_id)
        away_ppg = ppg_table.get(away_id)
        h_lam, a_lam = expected_goals(home_split, away_split, home_ppg, away_ppg, league_avg_ppg)
        model_probs = match_probabilities(h_lam, a_lam)

        market_1x2 = implied_probs_no_vig(odds, ["home", "draw", "away"])
        market_ou = implied_probs_no_vig(odds, ["over25", "under25"])

        h2h = get_h2h(fx["id"])

        candidates = []
        for key, label in [("home", f"{home_name} νίκη"), ("draw", "Ισοπαλία"), ("away", f"{away_name} νίκη")]:
            if key in market_1x2 and odds.get(key):
                edge = model_probs[key] - market_1x2[key]
                candidates.append((edge, label, key, odds[key], model_probs[key]))
        for key, label in [("over25", "Over 2.5 goals"), ("under25", "Under 2.5 goals")]:
            if key in market_ou and odds.get(key):
                edge = model_probs[key] - market_ou[key]
                candidates.append((edge, label, key, odds[key], model_probs[key]))

        positive_edge = [c for c in candidates if c[0] > 0]
        if not positive_edge:
            continue

        edge, label, key, price, model_p = max(positive_edge, key=lambda c: c[0])

        # --- Ποιότητα δεδομένων / συμφωνία σημάτων, για το confidence score ---
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

        bookmaker_consensus = summary["n_bookmakers"] >= MIN_BOOKMAKERS

        results.append({
            "match": f"{home_name} - {away_name}",
            "competition": comp_code,
            "pick": label,
            "odds": round(price, 2),
            "model_prob": round(model_p * 100, 1),
            "edge": round(edge * 100, 1),
            "data_quality": data_quality,
            "h2h_agrees": h2h_agrees,
            "bookmaker_consensus": bookmaker_consensus,
            "n_bookmakers": summary["n_bookmakers"],
        })


def analyze_af_league(league_name, league_id, odds_sport_key, results, stats):
    """Ίδια λογική ανάλυσης με analyze_competition, αλλά πάνω σε
    API-Football δεδομένα (season stats αντί για ματς-ένα-ένα)."""
    if not API_FOOTBALL_KEY:
        return
    season = current_season_year()

    try:
        fixtures = get_af_todays_fixtures(league_id, season)
    except Exception as e:
        print(f"[AF {league_name}] fixtures error: {e}")
        stats["errors"] += 1
        return
    if not fixtures:
        return
    stats["fixtures_found"] += len(fixtures)

    try:
        odds_events = get_odds_for_sport(odds_sport_key)
    except Exception as e:
        print(f"[AF {league_name}] odds error: {e}")
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
            print(f"[AF {league_name}] team stats error: {e}")
            continue
        if not home_split or not away_split or home_split["overall_gf"] is None or away_split["overall_gf"] is None:
            continue

        event = find_matching_event(
            {"homeTeam": {"name": home_name}, "awayTeam": {"name": away_name}}, odds_events
        )
        if not event:
            continue

        summary = market_summary(event)
        odds = summary["best"]

        home_ppg = ppg_table.get(home_id)
        away_ppg = ppg_table.get(away_id)
        h_lam, a_lam = expected_goals(home_split, away_split, home_ppg, away_ppg, league_avg_ppg)
        model_probs = match_probabilities(h_lam, a_lam)

        market_1x2 = implied_probs_no_vig(odds, ["home", "draw", "away"])
        market_ou = implied_probs_no_vig(odds, ["over25", "under25"])

        h2h = get_af_h2h(home_id, away_id)

        candidates = []
        for key, label in [("home", f"{home_name} νίκη"), ("draw", "Ισοπαλία"), ("away", f"{away_name} νίκη")]:
            if key in market_1x2 and odds.get(key):
                edge = model_probs[key] - market_1x2[key]
                candidates.append((edge, label, key, odds[key], model_probs[key]))
        for key, label in [("over25", "Over 2.5 goals"), ("under25", "Under 2.5 goals")]:
            if key in market_ou and odds.get(key):
                edge = model_probs[key] - market_ou[key]
                candidates.append((edge, label, key, odds[key], model_probs[key]))

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
        })


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=20)
    if not r.ok:
        print(f"Telegram error response: {r.status_code} {r.text}")
    r.raise_for_status()


def confidence_tier(r):
    """Δεν κοιτάει ΜΟΝΟ πιθανότητα/edge — κοιτάει και πόσο 'πλήρης' ήταν
    η ανάλυση πίσω από το pick."""
    model_prob = r["model_prob"] / 100
    edge = r["edge"] / 100
    signals_ok = (r["data_quality"] >= 2) and r["bookmaker_consensus"] and (r["h2h_agrees"] is not False)

    if model_prob >= MIN_MODEL_PROB and edge >= MIN_EDGE and signals_ok:
        return "🟢 ΥΨΗΛΗΣ ΣΙΓΟΥΡΙΑΣ"
    if model_prob >= 0.50 and edge >= 0 and r["data_quality"] >= 1:
        return "🟡 ΜΕΤΡΙΑΣ ΣΙΓΟΥΡΙΑΣ"
    return "🔴 ΧΑΜΗΛΗΣ ΣΙΓΟΥΡΙΑΣ — καλύτερο διαθέσιμο σήμερα, όχι κάτι που θα έπαιζα κανονικά"


def format_message(results, stats):
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    if not results:
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

    # Μέχρι 4 ΑΝΕΞΑΡΤΗΤΑ picks/μέρα — το καθένα αξιολογείται στα δικά του
    # μέτρα (δεν επιλέγονται για να "χωράνε" σε κάποιο συνδυασμένο στόχο).
    # Ταξινόμηση: πρώτα όσα είναι πιο "σίγουρα" (μοντέλο + edge + data quality).
    ranked = sorted(
        results,
        key=lambda r: (r["model_prob"] / 100, r["edge"] / 100, r["data_quality"]),
        reverse=True,
    )
    top = ranked[:4]

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
    lines.append(
        "⚠️ Αυτά είναι 4 ΑΝΕΞΑΡΤΗΤΑ picks, το καθένα με τη δική του σιγουριά "
        "— ΔΕΝ είναι προτεινόμενο combo. Αν τα παίξεις όλα μαζί σε ένα "
        "combo, ο συνδυασμένος κίνδυνος πολλαπλασιάζεται (ακόμα κι αν το "
        "καθένα ξεχωριστά είναι 🟢, μαζί μπορεί να έχουν λιγότερο από 50% "
        "να βγουν όλα). Στατιστική εκτίμηση, ΟΧΙ εγγύηση — μη ποντάρεις "
        "κάτι που δεν αντέχεις να χάσεις."
    )

    return "\n".join(lines)


def main():
    missing = [n for n, v in [
        ("FOOTBALL_DATA_API_KEY", FOOTBALL_DATA_API_KEY),
        ("ODDS_API_KEY", ODDS_API_KEY),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ] if not v]
    if missing:
        print(f"Λείπουν env vars: {missing}")
        sys.exit(1)

    if not API_FOOTBALL_KEY:
        print("API_FOOTBALL_KEY δεν έχει οριστεί — παραλείπονται Ελλάδα/Αυστρία/Ελβετία/Πολωνία/Τουρκία/Σκωτία.")

    results = []
    stats = {"fixtures_found": 0, "errors": 0}
    for comp_code, sport_key in COMPETITIONS.items():
        analyze_competition(comp_code, sport_key, results, stats)

    for league_name, cfg in AF_LEAGUES.items():
        analyze_af_league(league_name, cfg["league_id"], cfg["odds_key"], results, stats)

    print(f"Σύνολο ματς σήμερα: {stats['fixtures_found']}, σφάλματα λιγκών: {stats['errors']}")
    message = format_message(results, stats)
    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
