from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


# ----------------------------
# Config
# ----------------------------
SPORT_KEY = "basketball_ncaab"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Output locations (keep both so Vercel/GitHub Pages setups work either way)
OUT_ROOT = Path("output") / "today_predictions.json"
OUT_DASH = Path("dashboard") / "output" / "today_predictions.json"

RATINGS_PATH = Path("data") / "efficiency.csv"

# Model knobs
BASE_EFF = 100.0          # baseline points/100
HOME_ADV_PTS = 2.2        # approx home advantage in points
MARGIN_STD = 11.0         # std dev for margin -> win prob


# ----------------------------
# Data structures
# ----------------------------
@dataclass(frozen=True)
class TeamRatings:
    team: str
    adj_o: float
    adj_d: float
    tempo: float


@dataclass
class Game:
    commence_time: str
    home_team: str
    away_team: str
    market_home_spread: Optional[float]  # spread from API (home line)
    sportsbook: Optional[str]


# ----------------------------
# Helpers
# ----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def norm_cdf(x: float) -> float:
    # Standard normal CDF via erf
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def win_prob_from_margin(margin_pts_for_home: float, std: float = MARGIN_STD) -> float:
    # P(home wins) assuming margin ~ Normal(mean=margin, std=std)
    return norm_cdf(margin_pts_for_home / std)


def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


def load_ratings(path: Path) -> Dict[str, TeamRatings]:
    if not path.exists():
        raise FileNotFoundError(f"Missing ratings file: {path.as_posix()}")

    ratings: Dict[str, TeamRatings] = {}
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"team", "adj_o", "adj_d", "tempo"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"{path.as_posix()} must have columns: team, adj_o, adj_d, tempo")

        for row in reader:
            team = (row["team"] or "").strip()
            if not team:
                continue
            ratings[team.lower()] = TeamRatings(
                team=team,
                adj_o=float(row["adj_o"]),
                adj_d=float(row["adj_d"]),
                tempo=float(row["tempo"]),
            )
    return ratings


def pick_best_spread(bookmakers: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[str]]:
    """
    Returns (home_spread, sportsbook).
    Uses the first available spread market; prefers FanDuel/DK/BetMGM if present.
    """
    preferred = ["fanduel", "draftkings", "betmgm", "pointsbetus", "caesars", "betrivers"]

    def market_home_spread_from_bm(bm: Dict[str, Any]) -> Optional[float]:
        markets = bm.get("markets") or []
        for m in markets:
            if m.get("key") != "spreads":
                continue
            outcomes = m.get("outcomes") or []
            # For spreads market, outcomes include home & away with "point"
            # We want the HOME team's point.
            for o in outcomes:
                if "name" in o and "point" in o:
                    # We'll match later after we know home team name; so return dict-like? nope.
                    pass
        return None

    # We'll just scan and return first BM that contains spreads;
    # later, we compute actual home spread by matching outcome "name" to home team.
    for key in preferred:
        for bm in bookmakers:
            if bm.get("key") == key:
                return None, key  # signal preferred found; spread extracted later
    # fallback: any bookmaker
    if bookmakers:
        return None, bookmakers[0].get("key")
    return None, None


def extract_home_spread(bookmakers: List[Dict[str, Any]], home_team: str) -> Tuple[Optional[float], Optional[str]]:
    # Try preferred order, then any
    preferred = ["fanduel", "draftkings", "betmgm", "pointsbetus", "caesars", "betrivers"]

    def try_bm(bm: Dict[str, Any]) -> Optional[float]:
        markets = bm.get("markets") or []
        for m in markets:
            if m.get("key") != "spreads":
                continue
            outcomes = m.get("outcomes") or []
            for o in outcomes:
                if (o.get("name") or "").strip().lower() == home_team.strip().lower():
                    pt = o.get("point", None)
                    if pt is None:
                        return None
                    return float(pt)
        return None

    # preferred pass
    for key in preferred:
        for bm in bookmakers:
            if bm.get("key") == key:
                sp = try_bm(bm)
                if sp is not None:
                    return sp, key

    # any pass
    for bm in bookmakers:
        sp = try_bm(bm)
        if sp is not None:
            return sp, bm.get("key")

    return None, None


def fetch_odds_games() -> List[Game]:
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing ODDS_API_KEY env var (add it as a GitHub Actions secret).")

    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "spreads",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }

    res = requests.get(url, params=params, timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"Odds API error {res.status_code}: {res.text[:300]}")

    data = res.json()
    games: List[Game] = []

    for g in data:
        home = g.get("home_team")
        away = g.get("away_team")
        commence = g.get("commence_time") or ""
        bookmakers = g.get("bookmakers") or []

        market_spread, book = extract_home_spread(bookmakers, home)

        games.append(
            Game(
                commence_time=commence,
                home_team=home,
                away_team=away,
                market_home_spread=market_spread,
                sportsbook=book,
            )
        )

    return games


def project_home_margin(home: TeamRatings, away: TeamRatings) -> float:
    """
    KenPom-style: expected points/100 for each side based on offense vs opponent defense.
    Using multiplicative interaction around BASE_EFF:
      home_pp100 = home_adjO * (away_adjD / BASE_EFF)
      away_pp100 = away_adjO * (home_adjD / BASE_EFF)

    Then scale by possessions (tempo average / 100).
    """
    poss = (home.tempo + away.tempo) / 2.0

    home_pp100 = home.adj_o * (away.adj_d / BASE_EFF)
    away_pp100 = away.adj_o * (home.adj_d / BASE_EFF)

    margin = (home_pp100 - away_pp100) * (poss / 100.0)
    margin += HOME_ADV_PTS
    return margin


def time_et_label(iso_time: str) -> str:
    # Keep it simple: show ISO time if parsing fails.
    # (If you want perfect ET conversion, we can add zoneinfo next.)
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        # Display as local string; GitHub runner is UTC, but dashboard is fine with a label.
        # We'll just show HH:MM (ET) is handled later if you want.
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return "—"


def build_predictions(games: List[Game], ratings: Dict[str, TeamRatings]) -> Dict[str, Any]:
    rows = []

    home = ratings.get(g.home_team.lower())
away = ratings.get(g.away_team.lower())

# fallback ratings if team missing
if not home:
    home = TeamRatings(g.home_team, 100, 100, 69)

if not away:
    away = TeamRatings(g.away_team, 100, 100, 69)

        margin_home = project_home_margin(home, away)
        # Spread is "home line": negative means home favored.
        model_home_spread = -margin_home

        market = g.market_home_spread
        edge = None
        note = ""

        if market is not None:
            # Positive edge means our model makes the home spread "more negative" than market
            # Example: market -2.5, model -5.0 => edge = (+2.5) (value on home -2.5)
            edge = market - model_home_spread

            if edge >= 1.5:
                note = "Home value"
            elif edge <= -1.5:
                note = "Away value"
            else:
                note = "Small edge"
        else:
            note = "No market line"

        wp_home = win_prob_from_margin(margin_home)
        # Confidence: scale with edge magnitude if market exists; else neutral-ish
        conf = 60
        if edge is not None:
            conf = int(clamp(55 + abs(edge) * 6, 55, 90))

        rows.append(
            {
                "time_et": time_et_label(g.commence_time),
                "matchup": f"{g.away_team} @ {g.home_team}",
                "market_spread": market,
                "model_spread": round(model_home_spread, 1),
                "edge_pts": round(edge, 1) if edge is not None else None,
                "win_prob": round(wp_home, 3),
                "confidence": conf,
                "note": note,
                "market_source": (g.sportsbook or "").lower() + " spread" if g.sportsbook else "",
            }
        )

    # Sort: biggest absolute edge first, then confidence
    rows.sort(key=lambda r: (abs(r["edge_pts"] or 0), r["confidence"]), reverse=True)

    payload = {
        "generated_at": utc_now_iso(),
        "model_version": "v0.5-eff",
        "summary": {"games_count": len(rows)},
        "todays_games": rows,
    }
    return payload


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def main() -> None:
    ratings = load_ratings(RATINGS_PATH)
    games = fetch_odds_games()
    payload = build_predictions(games, ratings)

    write_json(OUT_ROOT, payload)
    write_json(OUT_DASH, payload)

    print(f"Wrote {OUT_ROOT.as_posix()} and {OUT_DASH.as_posix()}")
    print(f"Games output: {payload['summary']['games_count']}")


if __name__ == "__main__":
    main()
