"""命令行入口：等价于 python -m resilience_replay.cli。

无参数时输出基础契约冒烟信息；带参数时执行演练复盘 CLI，例如：
    python run_cli.py demo
    python run_cli.py conclusion EX-2026-NATDAY-HUB 2026-09-30-r1
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

if len(sys.argv) == 1:
    import json
    from dataclasses import asdict

    from resilience_replay import ExerciseScenario

    item = ExerciseScenario(
        exercise_code="exercise-code-001",
        scenario_revision="scenario-revision-001",
        coordinator="coordinator-001",
        state="state-001",
    )
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))
    print("\n完整平台命令：python run_cli.py --help")
else:
    from resilience_replay.cli import main

    sys.exit(main(sys.argv[1:]))
