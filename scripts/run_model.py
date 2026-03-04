from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List


@dataclass
class GamePrediction:
    time_et: str
    matchup: str
    market_spread: Optional[float]
    model_spread: Optional[float]
    edge_pts: Optional[float]
    win_prob: Optional[float]       # 0..1
    confidence: Optional[int]       # 0..100
    note: str = ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    # STUB DATA (for testing the dashboard). We'll replace this with real schedule + odds next.
    todays_games: List[GamePrediction] = [
        GamePrediction(
            time_et="7:00 PM",
            matchup="Team A @ Team B",
            market_spread=-3.5,
            model_spread=-5.8,
            edge_pts=2.3,
            win_prob=0.66,
            confidence=73,
            note="Value: model > market"
        ),
        GamePrediction(
            time_et="9:00 PM",
            matchup="Team C @ Team D",
            market_spread=+6.0,
            model_spread=+4.2,
            edge_pts=1.8,
            win_prob=0.41,
            confidence=60,
            note="Lean: dog"
        ),
    ]

    out = {
        "generated_at": utc_now_iso(),
        "model_version": "v0.2-stub-table",
        "summary": {
            "games_count": len(todays_games),
            "notes": "Stub predictions to validate dashboard rendering."
        },
        "todays_games": [asdict(g) for g in todays_games],
    }

# ...
Path("output").mkdir(exist_ok=True)
Path("dashboard/output").mkdir(parents=True, exist_ok=True)

payload = json.dumps(out, indent=2)

Path("output/today_predictions.json").write_text(payload, encoding="utf-8")
Path("dashboard/output/today_predictions.json").write_text(payload, encoding="utf-8")

print("Wrote output/today_predictions.json and dashboard/output/today_predictions.json")


if __name__ == "__main__":
    main()
