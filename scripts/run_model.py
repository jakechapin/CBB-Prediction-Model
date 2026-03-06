from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import URLError, HTTPError
from urllib.request import urlopen, Request


MODEL_VERSION = "v0.6-eff-safe"
HOME_COURT_ADV = 3.0

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_API_URL = (
    "https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds/"
    "?regions=us&markets=spreads&oddsFormat=american&bookmakers="
    "draftkings,fanduel,betmgm,pointsbetus,caesars"
    "&apiKey={api_key}"
)

EFFICIENCY_CSV = Path("data/efficiency.csv")
OUT_FILE = Path("output/today_predictions.json")
DASHBOARD_OUT_FILE = Path("dashboard/output/today_predictions.json")


@dataclass(frozen=True)
class TeamRatings:
    team: str
    adj_o: float
    adj_d: float
    tempo: float


@dataclass(frozen=True)
class Game:
    home_team: str
    away_team: str
    start_utc: datetime
    market_home_spread: Optional[float]
    book: str = ""


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def normalize_team_name(name: str) -> str:
    return " ".join((name or "").replace("\u00a0", " ").split()).strip()


def load_efficiency_ratings(path: Path) -> Dict[str, TeamRatings]:
    ratings: Dict[str, TeamRatings] = {}
    if not path.exists():
        return ratings

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return ratings

        for row in reader:
            team = normalize_team_name(row.get("team", ""))
            if not team:
                continue

            ratings[team.lower()] = TeamRatings(
                team=team,
                adj_o=safe_float(row.get("adj_o"), 100.0),
                adj_d=safe_float(row.get("adj_d"), 100.0),
                tempo=safe_float(row.get("tempo"), 69.0),
            )

    return ratings


def find_team(ratings: Dict[str, TeamRatings], team_name: str) -> Optional[TeamRatings]:
    return ratings.get(normalize_team_name(team_name).lower())


def project_home_margin(home: TeamRatings, away: TeamRatings) -> float:
    home_em = home.adj_o - home.adj_d
    away_em = away.adj_o - away.adj_d
    return (home_em - away_em) + HOME_COURT_ADV


def margin_to_win_prob(margin_home: float) -> float:
    scale = 7.0
    p = 1.0 / (1.0 + math.exp(-margin_home / scale))
    return max(0.01, min(0.99, p))


def confidence_from_edge(edge_pts: float) -> int:
    return int(round(max(1.0, min(99.0, 30 + abs(edge_pts) * 8.0))))


def format_time_et(dt_utc: datetime) -> str:
    dt = dt_utc.astimezone(timezone(timedelta(hours=-5)))
    return dt.strftime("%-I:%M %p")


def fetch_odds_games() -> List[Game]:
    if not ODDS_API_KEY:
        return []

    url = ODDS_API_URL.format(api_key=ODDS_API_KEY)
    req = Request(url, headers={"User-Agent": "cbb-model/1.0"})

    try:
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
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

            start_utc = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if start_utc < now - timedelta(hours=3) or start_utc > horizon:
                continue

            market_home_spread = None
            book_name = ""

            for book in ev.get("bookmakers", []) or []:
                btitle = book.get("title", "") or ""
                for m in book.get("markets", []) or []:
                    if (m.get("key") or "") != "spreads":
                        continue
                    for o in m.get("outcomes", []) or []:
                        if normalize_team_name(o.get("name", "")) == home_team:
                            market_home_spread = safe_float(o.get("point"), None)
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

    games.sort(key=lambda g: g.start_utc)
    return games


def fallback_sample_games() -> List[Game]:
    now = datetime.now(timezone.utc)
    return [
        Game("Team A", "Team B", now + timedelta(hours=6), -3.5, "sample"),
        Game("Team C", "Team D", now + timedelta(hours=8), +6.0, "sample"),
    ]


def main() -> None:
    ratings = load_efficiency_ratings(EFFICIENCY_CSV)
    games = fetch_odds_games()
    if not games:
        games = fallback_sample_games()

    todays: List[dict] = []

    for g in games:
        home = find_team(ratings, g.home_team)
        away = find_team(ratings, g.away_team)

        market = g.market_home_spread

        if home and away:
            margin_home = project_home_margin(home, away)
            model_home_spread = -margin_home
            edge = None if market is None else market - model_home_spread
            win_prob = margin_to_win_prob(margin_home)
            conf = confidence_from_edge(edge or 0)
            note = "Model edge" if edge is not None and abs(edge) >= 2 else ("Lean" if edge is not None and abs(edge) >= 1 else "")
        else:
            model_home_spread = market
            edge = 0.0 if market is not None else None
            win_prob = 0.50
            conf = 0
            note = "Missing ratings"

        todays.append(
            {
                "time_et": format_time_et(g.start_utc),
                "matchup": f"{g.away_team} @ {g.home_team}",
                "market_spread": market,
                "model_spread": round(model_home_spread, 1) if model_home_spread is not None else None,
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

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")

    DASHBOARD_OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_OUT_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
