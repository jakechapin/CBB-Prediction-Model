# scripts/run_model.py
# Generates output/today_predictions.json for the dashboard.
# Uses optional:
# - ODDS_API_KEY (TheOddsAPI) to pull today's NCAAB slate + spreads
# - data/efficiency.csv for KenPom-style efficiency ratings

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError, HTTPError
from urllib.request import urlopen, Request


# ----------------------------
# Config
# ----------------------------
MODEL_VERSION = "v0.5-eff"
HOME_COURT_ADV = 3.0  # points (simple default)
DEFAULT_ADJ_O = 100.0
DEFAULT_ADJ_D = 100.0
DEFAULT_TEMPO = 69.0

# Odds API (TheOddsAPI) - free tier works
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_API_URL = (
    "https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds/"
    "?regions=us&markets=spreads&oddsFormat=american&bookmakers="
    "draftkings,fanduel,betmgm,pointsbetus,caesars"
    "&apiKey={api_key}"
)

# Files
EFFICIENCY_CSV = Path("data/efficiency.csv")
OUT_DIR = Path("output")
OUT_FILE = OUT_DIR / "today_predictions.json"
DASHBOARD_OUT_FILE = Path("dashboard/output/today_predictions.json")  # optional mirror


# ----------------------------
# Data structures
# ----------------------------
@dataclass(frozen=True)
class TeamRatings:
    team: str
    adj_o: float  # adjusted offensive efficiency (pts/100)
    adj_d: float  # adjusted defensive efficiency (pts/100)
    tempo: float  # possessions per 40


@dataclass(frozen=True)
class Game:
    home_team: str
    away_team: str
    start_utc: datetime
    market_home_spread: Optional[float]  # home team's spread line (e.g., -3.5)
    book: str = ""


# ----------------------------
# Helpers
# ----------------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def normalize_team_name(name: str) -> str:
    # Light normalization to improve joins across sources
    return " ".join(name.replace("\u00a0", " ").split()).strip()


def load_efficiency_ratings(path: Path) -> Dict[str, TeamRatings]:
    """
    Expected columns (case-insensitive, flexible):
      team, adj_o, adj_d, tempo
    You can also use: offense, defense, adj_off, adj_def, pace, poss40, etc.
    """
    ratings: Dict[str, TeamRatings] = {}
    if not path.exists():
        return ratings

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return ratings

        # map headers
        headers = {h.lower().strip(): h for h in reader.fieldnames}

        def pick(*candidates: str) -> Optional[str]:
            for c in candidates:
                if c in headers:
                    return headers[c]
            return None

        team_col = pick("team", "school", "name")
        o_col = pick("adj_o", "adjo", "offense", "adj_off", "adjoff", "o")
        d_col = pick("adj_d", "adjd", "defense", "adj_def", "adjdef", "d")
        t_col = pick("tempo", "pace", "poss40", "possessions", "t")

        if not team_col or not o_col or not d_col:
            # Not enough info to use
            return ratings

        for row in reader:
            team_raw = row.get(team_col, "") or ""
            team = normalize_team_name(team_raw)
            if not team:
                continue

            adj_o = safe_float(row.get(o_col, ""), DEFAULT_ADJ_O)
            adj_d = safe_float(row.get(d_col, ""), DEFAULT_ADJ_D)
            tempo = safe_float(row.get(t_col, ""), DEFAULT_TEMPO) if t_col else DEFAULT_TEMPO

            ratings[team.lower()] = TeamRatings(team=team, adj_o=adj_o, adj_d=adj_d, tempo=tempo)

    return ratings


def find_team(ratings: Dict[str, TeamRatings], team_name: str) -> Optional[TeamRatings]:
    key = normalize_team_name(team_name).lower()
    if key in ratings:
        return ratings[key]

    # fallback: try looser match (contains)
    for k, v in ratings.items():
        if k == key:
            return v
    return None


def project_home_margin(home: TeamRatings, away: TeamRatings) -> float:
    """
    KenPom-style margin estimate (very simplified):
      AdjEM = AdjO - AdjD
      margin ≈ (AdjEM_home - AdjEM_away) + HCA
    """
    home_em = home.adj_o - home.adj_d
    away_em = away.adj_o - away.adj_d
    return (home_em - away_em) + HOME_COURT_ADV


def margin_to_win_prob(margin_home: float) -> float:
    """
    Convert point margin to win probability using logistic curve.
    Scale tuned so ~7 pts ≈ strong favorite.
    """
    scale = 7.0
    p = 1.0 / (1.0 + math.exp(-margin_home / scale))
    return max(0.01, min(0.99, p))


def confidence_from_edge(edge_pts: Optional[float], win_prob: float) -> int:
    """
    0-100 style confidence
    """
    base = abs((win_prob - 0.5) * 200)  # 0..100
    if edge_pts is None:
        conf = base * 0.7
    else:
        conf = min(100.0, base + min(35.0, abs(edge_pts) * 6.0))
    return int(round(max(1.0, min(99.0, conf))))


def format_time_et(dt_utc: datetime) -> str:
    # Rough ET display without external deps (DST accuracy not critical for MVP).
    # If you want perfect ET, we can switch to zoneinfo (standard lib) next.
    dt = dt_utc.astimezone(timezone(timedelta(hours=-5)))  # EST baseline
    return dt.strftime("%-I:%M %p") if hasattr(dt, "strftime") else dt.isoformat()


def fetch_odds_games() -> List[Game]:
    if not ODDS_API_KEY:
        return []

    url = ODDS_API_URL.format(api_key=ODDS_API_KEY)
    req = Request(url, headers={"User-Agent": "cbb-model/1.0"})
    try:
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[warn] Odds API fetch failed: {e}")
        return []

    games: List[Game] = []
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=36)

    for ev in data:
        try:
            home_team = normalize_team_name(ev.get("home_team", ""))
            away_team = normalize_team_name(ev.get("away_team", ""))
            if not home_team or not away_team:
                continue

            commence = ev.get("commence_time")
            if not commence:
                continue

            # ISO timestamps from API (Z)
            start_utc = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if start_utc < now - timedelta(hours=3) or start_utc > horizon:
                # keep it "today-ish"
                continue

            market_home_spread = None
            book_name = ""

            # Prefer a bookmaker with spreads
            for book in ev.get("bookmakers", []) or []:
                btitle = book.get("title", "") or ""
                for m in book.get("markets", []) or []:
                    if (m.get("key") or "") != "spreads":
                        continue
                    outcomes = m.get("outcomes", []) or []
                    # Each outcome has: name (team), point (spread), price (odds)
                    for o in outcomes:
                        if normalize_team_name(o.get("name", "")) == home_team:
                            market_home_spread = safe_float(o.get("point", None), None)
                            book_name = btitle
                            break
                    if market_home_spread is not None:
                        break
                if market_home_spread is not None:
                    break

            games.append(
                Game(
                    home_team=home_team,
                    away_team=away_team,
                    start_utc=start_utc,
                    market_home_spread=market_home_spread,
                    book=book_name,
                )
            )
        except Exception:
            continue

    # sort by start time
    games.sort(key=lambda g: g.start_utc)
    return games


def fallback_sample_games() -> List[Game]:
    # If ODDS_API_KEY isn't set yet, keep dashboard alive with sample entries
    now = datetime.now(timezone.utc)
    return [
        Game("Team A", "Team B", now + timedelta(hours=6), -3.5, "sample"),
        Game("Team C", "Team D", now + timedelta(hours=8), +6.0, "sample"),
    ]


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    ratings = load_efficiency_ratings(EFFICIENCY_CSV)
    if ratings:
        print(f"Loaded efficiency ratings: {len(ratings)} teams")
    else:
        print("No efficiency ratings found (data/efficiency.csv). Using defaults.")

    games = fetch_odds_games()
    if not games:
        print("No games fetched from Odds API. Using fallback sample games.")
        games = fallback_sample_games()

    todays: List[dict] = []

    for g in games:
        home = find_team(ratings, g.home_team)
        away = find_team(ratings, g.away_team)

        # fallback ratings if team missing
        if not home:
            home = TeamRatings(g.home_team, DEFAULT_ADJ_O, DEFAULT_ADJ_D, DEFAULT_TEMPO)
        if not away:
            away = TeamRatings(g.away_team, DEFAULT_ADJ_O, DEFAULT_ADJ_D, DEFAULT_TEMPO)

        margin_home = project_home_margin(home, away)

        # Spread is "home line": negative means home favored.
        model_home_spread = -margin_home

        market = g.market_home_spread
        edge = None
        note = ""
        if market is not None:
            # Positive edge means model favors home more than market (i.e., model line more negative)
            edge = market - model_home_spread
            if abs(edge) >= 2.0:
                note = "Model edge"
            elif abs(edge) >= 1.0:
                note = "Lean"
            else:
                note = ""

        win_prob = margin_to_win_prob(margin_home)
        conf = confidence_from_edge(edge, win_prob)

        matchup = f"{away.team} @ {home.team}"

        todays.append(
            {
                "time_et": format_time_et(g.start_utc),
                "matchup": matchup,
                "market_spread": market,
                "model_spread": round(model_home_spread, 1),
                "edge_pts": round(edge, 1) if edge is not None else None,
                "win_prob": round(win_prob, 2),
                "confidence": conf,
                "note": note if note else (g.book + " spread" if g.book else ""),
                "book": g.book,
            }
        )

    out = {
        "generated_at": now_utc_iso(),
        "model_version": MODEL_VERSION,
        "summary": {"games_count": len(todays)},
        "todays_games": todays,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_FILE}")

    # Optional mirror into dashboard folder if you ever fetch locally there
    try:
        DASHBOARD_OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        DASHBOARD_OUT_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"Wrote {DASHBOARD_OUT_FILE}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
