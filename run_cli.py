"""跨网络韧性演练复盘命令行入口（兼容仓库根目录直接运行）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from resilience_replay.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
