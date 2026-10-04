"""跨网络韧性演练复盘命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from resilience_replay import ExerciseScenario


def main() -> None:
    item = ExerciseScenario(exercise_code='exercise-code-001', scenario_revision='scenario-revision-001', coordinator='coordinator-001', state='state-001')
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
