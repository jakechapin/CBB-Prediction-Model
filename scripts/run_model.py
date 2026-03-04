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
    market_spread: Optional[float]
    model_spread: Optional[float]
    edge_pts: Optional[float]
    win_prob: Optional[float]
    confidence: Optional[int]
    note: str = ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_stub_predictions() -> List[GamePrediction]:

    games: List[GamePrediction] = [
        GamePrediction(
            time_et="7:00 PM",
            matchup="Team A @ Team B",
            market_spread=-3.5,
            model_spread=-5.8,
            edge_pts=2.3,
            win_prob=0.66,
            confidence=73,
            note="Model edge"
        ),
        GamePrediction(
            time_et="9:00 PM",
            matchup="Team C @ Team D",
            market_spread=+6.0,
            model_spread=+4.2,
            edge_pts=1.8,
            win_prob=0.41,
            confidence=60,
            note="Lean dog"
        ),
    ]

    return games


def write_json_outputs(out: dict) -> None:

    payload = json.dumps(out, indent=2)

    # repo output
    Path("output").mkdir(exist_ok=True)
    Path("output/today_predictions.json").write_text(payload)

    # dashboard output for Vercel
    Path("dashboard/output").mkdir(parents=True, exist_ok=True)
    Path("dashboard/output/today_predictions.json").write_text(payload)


def main():

    todays_games = build_stub_predictions()

    out = {
        "generated_at": utc_now_iso(),
        "model_version": "v0.3",
        "summary": {
            "games_count": len(todays_games)
        },
        "todays_games": [asdict(g) for g in todays_games],
    }

    write_json_outputs(out)

    print("Predictions written to output/ and dashboard/output/")


if __name__ == "__main__":
    main()
