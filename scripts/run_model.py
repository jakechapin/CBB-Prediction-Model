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
    market_spread: Optional[float]  # DK / consensus; negative = away favored if you format "Away @ Home"
    model_spread: Optional[float]
    edge_pts: Optional[float]
    win_prob: Optional[float]       # 0..1
    confidence: Optional[int]       # 0..100
    note: str = ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    # TODO: Replace this stub list with real data ingestion later.
    todays_games: List[GamePrediction] = [
        GamePrediction(
            time_et="7:00 PM",
            matchup="Duke @ NC State",
            market_spread=-3.5,
            model_spread=-6.0,
            edge_pts=2.5,
            win_prob=0.67,
            confidence=72,
            note="Value: model > market"
        ),
        GamePrediction(
            time_et="8:30 PM",
            matchup="Providence @ Creighton",
            market_spread=+7.0,
            model_spread=+5.0,
            edge_pts=2.0,
            win_prob=0.39,
            confidence=61,
            note="Lean: dog"
        ),
    ]

    out = {
        "generated_at": utc_now_iso(),
        "date_et": "2026-03-04",  # TODO: compute from timezone later
        "model_version": "v0.1-stub",
        "summary": {
            "games_count": len(todays_games),
            "notes": "Stub predictions. Next step: auto-fetch schedule + lines + team ratings."
        },
        "todays_games": [asdict(g) for g in todays_games],
    }

    Path("output").mkdir(exist_ok=True)
    Path("output/today_predictions.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("Wrote output/today_predictions.json")


if __name__ == "__main__":
    main()
