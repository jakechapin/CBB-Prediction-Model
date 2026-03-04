from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


@dataclass
class GamePrediction:
    time_et: str
    matchup: str
    market_spread: Optional[float]   # + = home dog (if matchup is Away @ Home), - = away favored
    model_spread: Optional[float]    # same sign convention as market_spread
    edge_pts: Optional[float]        # model_spread - market_spread (positive = model likes away more)
    win_prob: Optional[float]        # 0..1
    confidence: Optional[int]        # 0..100
    note: str = ""


def utc_now_iso() -> str:
    # Timezone-aware UTC timestamp
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_stub_predictions() -> List[GamePrediction]:
    """
    STUB DATA ONLY:
    Replace this later with:
      - today's schedule ingestion
      - odds/spreads ingestion
      - team strength model output
    """
    games: List[GamePrediction] = [
        GamePrediction(
            time_et="7:00 PM",
            matchup="Team A @ Team B",
            market_spread=-3.5,
            model_spread=-5.8,
            edge_pts=(-5.8) - (-3.5),
            win_prob=0.66,
            confidence=73,
            note="Value: model > market"
        ),
        GamePrediction(
            time_et="9:00 PM",
            matchup="Team C @ Team D",
            market_spread=+6.0,
            model_spread=+4.2,
            edge_pts=(+4.2) - (+6.0),
            win_prob=0.41,
            confidence=60,
            note="Lean: dog"
        ),
    ]
    return games


def write_json_outputs(out: dict) -> None:
    payload = json.dumps(out, indent=2)

    # Repo history / raw output
    Path("output").mkdir(exist_ok=True)
    Path("output/today_predictions.json").write_text(payload, encoding="utf-8")

    # Vercel-served output (because Root Directory is dashboard)
    Path("dashboard/output").mkdir(parents=True, exist_ok=True)
    Path("dashboard/output/today_predictions.json").write_text(payload, encoding="utf-8")


def main() -> None:
    todays_games = build_stub_predictions()

    out = {
        "generated_at": utc_now_iso(),
        "model_version": "v0.3-stub-to-vercel",
        "summary": {
            "games_count": len(todays_games),
            "notes": "Stub predictions to validate dashboard + deployment. Replace with real data feed next."
        },
        "todays_games": [asdict(g) for g in todays_games],
    }

    write_json_outputs(out)
    print("Wrote output/today_predictions.json AND dashboard/output/today_predictions.json")


if __name__ == "__main__":
    main()
