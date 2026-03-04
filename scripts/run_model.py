from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

# --- Odds API config ---
SPORT_KEY = "basketball_ncaab"
REGIONS = "us"
MARKETS = "spreads"
ODDS_FORMAT = "american"
DATE_FORMAT = "iso"
BOOKMAKER_PREFERENCE = [
    "draftkings",
    "fanduel",
    "betmgm",
    "pointsbetus",
    "caesars",
]


@dataclass
class GamePick:
    time_et: str
    matchup: str
    market_spread: Optional[float]
    model_spread: Optional[float]
    edge_pts: Optional[float]
    win_prob: Optional[float]
    confidence: Optional[int]
    note: str


def ensure_dirs() -> None:
    Path("output").mkdir(parents=True, exist_ok=True)
    Path("dashboard/output").mkdir(parents=True, exist_ok=True)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_odds_games() -> List[Dict[str, Any]]:
    """
    Pull today's NCAAB odds (spreads) from The Odds API.
    Docs: https://the-odds-api.com/
    """
    if not ODDS_API_KEY:
        raise RuntimeError("Missing ODDS_API_KEY secret/env var")

    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": DATE_FORMAT,
    }
    resp = requests.get(url, params=params, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("Unexpected Odds API response format")
    return data


def pick_bookmaker(bookmakers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not bookmakers:
        return None
    by_key = {b.get("key"): b for b in bookmakers if b.get("key")}
    for k in BOOKMAKER_PREFERENCE:
        if k in by_key:
            return by_key[k]
    return bookmakers[0]


def extract_spread(game: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """
    Returns (market_spread_for_away_team, source_note).
    Convention: market_spread is spread applied to AWAY team.
    Example: Away -3.5 means away favored by 3.5.
    """
    bookmakers = game.get("bookmakers") or []
    book = pick_bookmaker(bookmakers)
    if not book:
        return None, "No market spread"

    markets = book.get("markets") or []
    spreads = next((m for m in markets if m.get("key") == "spreads"), None)
    if not spreads:
        return None, f"No spreads market ({book.get('key','book')})"

    outcomes = spreads.get("outcomes") or []
    home = game.get("home_team")
    away = game.get("away_team")

    # Outcomes usually have "name" and "point"
    home_out = next((o for o in outcomes if o.get("name") == home), None)
    away_out = next((o for o in outcomes if o.get("name") == away), None)

    # If away outcome exists, use its point directly
    if away_out and isinstance(away_out.get("point"), (int, float)):
        return float(away_out["point"]), f"{book.get('key','book')} spread"

    # Otherwise infer from home outcome (away = -home)
    if home_out and isinstance(home_out.get("point"), (int, float)):
        return float(-home_out["point"]), f"{book.get('key','book')} spread"

    return None, f"Bad spread data ({book.get('key','book')})"


def simple_model_spread(market_spread: Optional[float]) -> Optional[float]:
    """
    Placeholder for now: "model" tweaks market a bit.
    We'll replace this with real efficiency/tempo model next.
    """
    if market_spread is None:
        return None
    # Tiny tilt so we can see edges
    return float(market_spread) - 1.0


def spread_to_win_prob(model_spread: Optional[float]) -> Optional[float]:
    """
    Very rough mapping: convert spread to win probability.
    We'll replace with calibrated curve later.
    """
    if model_spread is None:
        return None
    # logistic-ish approximation
    import math
    return 1.0 / (1.0 + math.exp(model_spread / 6.0))


def confidence_from_edge(edge: Optional[float]) -> Optional[int]:
    if edge is None:
        return None
    e = abs(edge)
    # 0-100 scale
    c = int(min(95, max(50, 50 + e * 10)))
    return c


def format_time_et(commence_time_iso: str) -> str:
    # We will keep it simple: show local-ish time string based on ISO (UTC) without tz conversion.
    # (We can add real ET conversion next if you want.)
    try:
        dt = datetime.fromisoformat(commence_time_iso.replace("Z", "+00:00"))
        # display as HH:MM AM/PM
        return dt.strftime("%-I:%M %p")
    except Exception:
        return "TBD"


def build_predictions(games: List[Dict[str, Any]]) -> Dict[str, Any]:
    picks: List[GamePick] = []

    for g in games:
        home = g.get("home_team", "Home")
        away = g.get("away_team", "Away")
        matchup = f"{away} @ {home}"

        market_spread, market_note = extract_spread(g)
        model_spread = simple_model_spread(market_spread)
        edge = None if (market_spread is None or model_spread is None) else (market_spread - model_spread)

        win_prob = spread_to_win_prob(model_spread)
        conf = confidence_from_edge(edge)

        note = "Model edge" if (edge is not None and edge >= 2.0) else ("Lean dog" if (market_spread is not None and market_spread > 0) else "")
        if market_spread is None:
            note = "No market spread"

        time_et = format_time_et(g.get("commence_time", ""))

        picks.append(
            GamePick(
                time_et=time_et,
                matchup=matchup,
                market_spread=market_spread,
                model_spread=model_spread,
                edge_pts=edge,
                win_prob=win_prob,
                confidence=conf,
                note=note if note else market_note,
            )
        )

    out = {
        "generated_at": utcnow_iso(),
        "model_version": "v0.4",
        "summary": {"games_count": len(picks)},
        "todays_games": [asdict(p) for p in picks],
    }
    return out


def fallback_predictions() -> Dict[str, Any]:
    return {
        "generated_at": utcnow_iso(),
        "model_version": "v0.4",
        "summary": {"games_count": 2},
        "todays_games": [
            {
                "time_et": "7:00 PM",
                "matchup": "Team A @ Team B",
                "market_spread": -3.5,
                "model_spread": -5.8,
                "edge_pts": 2.3,
                "win_prob": 0.66,
                "confidence": 73,
                "note": "Model edge (fallback)",
            },
            {
                "time_et": "9:00 PM",
                "matchup": "Team C @ Team D",
                "market_spread": 6.0,
                "model_spread": 4.2,
                "edge_pts": 1.8,
                "win_prob": 0.41,
                "confidence": 60,
                "note": "Lean dog (fallback)",
            },
        ],
    }


def write_outputs(payload: Dict[str, Any]) -> None:
    ensure_dirs()
    text = json.dumps(payload, indent=2)

    Path("output/today_predictions.json").write_text(text, encoding="utf-8")
    Path("dashboard/output/today_predictions.json").write_text(text, encoding="utf-8")
    print("Wrote output/today_predictions.json and dashboard/output/today_predictions.json")


def main() -> None:
    try:
        games = fetch_odds_games()
        payload = build_predictions(games)
    except Exception as e:
        print(f"[WARN] Using fallback predictions due to error: {e}")
        payload = fallback_predictions()

    write_outputs(payload)


if __name__ == "__main__":
    main()
