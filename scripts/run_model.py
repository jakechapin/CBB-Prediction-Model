from datetime import datetime
import json
from pathlib import Path

out = {
  "generated_at": datetime.utcnow().isoformat() + "Z",
  "message": "Model pipeline is running ✅"
}

Path("output").mkdir(exist_ok=True)
Path("output/today_predictions.json").write_text(json.dumps(out, indent=2))
print("Wrote output/today_predictions.json")
