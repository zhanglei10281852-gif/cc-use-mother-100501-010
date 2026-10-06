"""基于文件的持久化：冻结场景存 JSON，追加日志存 JSONL。

目录布局::

    <base_dir>/<exercise_code>/scenario.json
    <base_dir>/<exercise_code>/events.jsonl

每个演练目录自包含，复制目录即可归档；重放只依赖这两个文件，
因此演练中断、进程重启甚至换一台机器，结果都不变。
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ExerciseNotFoundError, JournalIntegrityError
from .journal import EventJournal
from .scenario import Scenario


class ExerciseStore:
    """演练目录的读写仓储（无外部数据库依赖）。"""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    # ---- 路径 -----------------------------------------------------------

    def _dir(self, exercise_code: str) -> Path:
        return self.base_dir / exercise_code

    def _scenario_path(self, exercise_code: str) -> Path:
        return self._dir(exercise_code) / "scenario.json"

    def _journal_path(self, exercise_code: str) -> Path:
        return self._dir(exercise_code) / "events.jsonl"

    # ---- 场景 -----------------------------------------------------------

    def save_scenario(self, scenario: Scenario) -> Path:
        directory = self._dir(scenario.exercise_code)
        directory.mkdir(parents=True, exist_ok=True)
        path = self._scenario_path(scenario.exercise_code)
        if path.exists():
            raise FileExistsError(
                f"演练 {scenario.exercise_code} 的场景已冻结，"
                f"修订请使用新的 scenario_revision 新建演练"
            )
        document = {
            "scenario": scenario.to_dict(),
            "scenario_fingerprint": scenario.fingerprint(),
        }
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._journal_path(scenario.exercise_code).touch()
        return path

    def load_scenario(self, exercise_code: str) -> Scenario:
        path = self._scenario_path(exercise_code)
        if not path.exists():
            raise ExerciseNotFoundError(exercise_code)
        document = json.loads(path.read_text(encoding="utf-8"))
        scenario = Scenario.from_dict(document["scenario"])
        stored = document.get("scenario_fingerprint")
        if stored and stored != scenario.fingerprint():
            raise JournalIntegrityError(
                f"演练 {exercise_code} 场景指纹不匹配，冻结快照被改动"
            )
        return scenario

    def list_exercises(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        return sorted(
            path.name
            for path in self.base_dir.iterdir()
            if path.is_dir() and (path / "scenario.json").exists()
        )

    # ---- 日志 -----------------------------------------------------------

    def load_journal(self, exercise_code: str) -> EventJournal:
        if not self._scenario_path(exercise_code).exists():
            raise ExerciseNotFoundError(exercise_code)
        path = self._journal_path(exercise_code)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return EventJournal.from_jsonl(exercise_code, text)

    def append_journal(self, exercise_code: str, journal: EventJournal) -> None:
        """整文件原子重写：哈希链始终落盘为一致状态。"""
        path = self._journal_path(exercise_code)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(journal.to_jsonl() + ("\n" if journal.records else ""), encoding="utf-8")
        tmp.replace(path)
