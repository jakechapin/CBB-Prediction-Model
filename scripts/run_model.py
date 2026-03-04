#!/usr/bin/env python3
"""
CBB Model Runner (v0.4)
- Fetches today's NCAAB games + spreads from The Odds API
- Writes predictions JSON to:
    1) output/today_predictions.json
    2) dashboard/output/today_predictions.json
So Vercel (Root Directory = dashboard) can serve:
    /output/today_predictions.json  -> dashboard/output/today_predictions.json
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None  # type: ignore


SPORT_KEY = "basketball_ncaab"  # The Odds API sport key for men's college basketball
MODEL_VERSION = "v0.4"

# You can restrict books (optional). Leaving None uses whatever the API returns.
# Example: "draftkings,fanduel,betmgm"
BOOKMAKERS: Optional[str] = None

REGIONS = "us"
MARKETS = "spreads"
ODDS_FORMAT = "american"


@dataclass
class SpreadQuote:
    bookmaker: str
    away: float
    home: float


def iso_to_et_string(iso_z: str) -> str:
    """
    Convert ISO8601 time (usually with Z) to ET display string like '7:00 PM'.
    If conversion fails, return original.
    """
    try:
        # Handle Z suffix
        if iso_z.endswith("Z"):
            iso_z = iso_z[:-1] + "+00:00"
        dt_utc = datetime.fromisoformat(iso_z)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)

        if ZoneInfo is None:
            # fallback: just show UTC
            return dt_utc.astimezone(timezone.utc).strftime("%-I:%M %p UTC")

        dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
        # Windows strftime doesn't like %-I; use lstrip trick
        s = dt_et.strftime("%I:%M %p").lstrip("0")
        return s
    except Exception:
        return iso_z


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def extract_spreads(event: Dict[str, Any]) -> List[SpreadQuote]:
    """
    Extract spread points for away and home from The Odds API response.

    The Odds API typically returns:
      event["bookmakers"][i]["markets"][j]["key"] == "spreads"
      market["outcomes"] contains two outcomes with:
         {"name": TEAM_NAME, "point": +/-X, "price": ...}
    """
    away_team = event.get("away_team")
    home_team = event.get("home_team")

    spreads: List[SpreadQuote] = []

    for bm in event.get("bookmakers", []) or []:
        bm_key = bm.get("key") or bm.get("title") or "book"
        for m in bm.get("markets", []) or []:
            if m.get("key") != "spreads":
                continue
            outcomes = m.get("outcomes", []) or []
            # map team -> point
            pts: Dict[str, Optional[float]] = {}
            for o in outcomes:
                name = o.get("name")
                pt = safe_float(o.get("point"))
                if name:
                    pts[name] = pt

            a = pts.get(away_team)
            h = pts.get(home_team)

            if a is None or h is None:
                continue

            spreads.append(SpreadQuote(bookmaker=str(bm_key), away=float(a), home=float(h)))

    return spreads


def consensus_spread_for_away(spreads: List[SpreadQuote]) -> Optional[float]:
    """
    Return consensus spread for the away team (negative means away favored).
    Uses median to reduce outliers.
    """
    if not spreads:
        return None
    vals = [q.away for q in spreads if isinstance(q.away, (int, float))]
    if not vals:
        return None
    return float(statistics.median(vals))


def placeholder_model_spread(away: str, home: str, market_away_spread: float) -> Tuple[float, float, float, int, str]:
    """
    TEMP model:
    - Adds a tiny deterministic adjustment based on team-name hashes.
    - This is ONLY to make the pipeline useful today.
    Next phase: replace with real features (tempo, ratings, travel/rest/injuries, weekly recalibration).

    Returns:
      model_spread_away, edge_pts, win_prob, confidence, note
    """
    # deterministic pseudo-adjustment in [-1.5, +1.5]
    h = (hash(away) - hash(home)) % 300
    adj = (h / 100.0) - 1.5

    model_spread = market_away_spread + adj
    edge = abs(model_spread - market_away_spread)

    # crude win prob proxy from spread (just for display)
    # win_prob ~ sigmoid-ish: clamp between 0.25 and 0.75
    win_prob = 0.5 + max(min((-model_spread) / 20.0, 0.25), -0.25)

    # confidence: combine edge + win_prob distance from 0.5
    conf = int(round(min(95, max(50, 55 + edge * 10 + abs(win_prob - 0.5) * 60))))

    note = "Placeholder model (upgrade next: real features + weekly recalibration)"
    return float(model_spread), float(model_spread - market_away_spread), float(win_prob), conf, note


def fetch_odds(api_key: str) -> List[Dict[str, Any]]:
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds"
    params = {
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
        "apiKey": api_key,
    }
    if BOOKMAKERS:
        params["bookmakers"] = BOOKMAKERS

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Unexpected API response (expected a list).")
    return data


def build_output(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    games_out: List[Dict[str, Any]] = []

    for e in events:
        away = e.get("away_team")
        home = e.get("home_team")
        if not away or not home:
            continue

        time_iso = e.get("commence_time") or ""
        time_et = iso_to_et_string(str(time_iso))

        spreads = extract_spreads(e)
        market_away = consensus_spread_for_away(spreads)

        # If no spread is available, skip (or include with nulls)
        if market_away is None:
            continue

        model_spread, edge_pts, win_prob, conf, note = placeholder_model_spread(
            away=str(away),
            home=str(home),
            market_away_spread=float(market_away),
        )

        games_out.append(
            {
                "time_et": time_et,
                "matchup": f"{away} @ {home}",
                # Convention: spread is for AWAY team; negative => away favored
                "market_spread": round(float(market_away), 1),
                "model_spread": round(float(model_spread), 1),
                # edge_pts = (model - market) in spread points (away perspective)
                "edge_pts": round(float(edge_pts), 1),
                "win_prob": round(float(win_prob), 3),
                "confidence": int(conf),
                "note": note,
            }
        )

    # Sort by time string is imperfect; keep stable, and dashboard can sort later
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "model_version": MODEL_VERSION,
        "summary": {"games_count": len(games_out)},
        "todays_games": games_out,
    }
    return out


def write_json(payload: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    api_key = os.getenv("ODDS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "Missing ODDS_API_KEY environment variable.\n"
            "Add it locally (PowerShell):  $env:ODDS_API_KEY='YOUR_KEY'\n"
            "Add it to GitHub Actions Secrets: Settings → Secrets and variables → Actions → New repository secret"
        )

    events = fetch_odds(api_key)
    payload = build_output(events)

    # Write to BOTH locations
    write_json(payload, Path("output/today_predictions.json"))
    write_json(payload, Path("dashboard/output/today_predictions.json"))

    print(f"Wrote output/today_predictions.json ({payload['summary']['games_count']} games)")
    print(f"Wrote dashboard/output/today_predictions.json ({payload['summary']['games_count']} games)")


if __name__ == "__main__":
    main()
