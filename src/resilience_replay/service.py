"""应用服务：场景冻结、事件接入、持久化与重放编排。

存储布局（默认 ./.rr-data）：
    <data_dir>/<exercise_code>/scenario__<revision>.json
    <data_dir>/<exercise_code>/events__<revision>.jsonl

事件日志只追加；场景冻结后以 JSON 快照固定，二者共同构成重放输入。
进程崩溃/演练中断后重启，只需重新加载这两个文件并重放，结果一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import tempfile

from .clock import format_ts, now_utc
from .engine import IngestValidationError, ReplayResult, replay, validate_ingest
from .events import (
    CORRECTION,
    DuplicateEventError,
    EventLog,
    EventRecord,
)
from .jsonio import canonical_dumps
from .scenario import (
    Scenario,
    ScenarioState,
    freeze_scenario,
    scenario_from_dict,
)


class ScenarioConflictError(ValueError):
    """同 code+revision 的冻结场景内容冲突。"""


class ExerciseService:
    def __init__(self, data_dir: str | Path = ".rr-data") -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- 路径 ---------------------------------------------------------------

    def _exercise_dir(self, code: str) -> Path:
        d = self.data_dir / code
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _scenario_path(self, code: str, revision: str) -> Path:
        return self._exercise_dir(code) / f"scenario__{revision}.json"

    def _events_path(self, code: str, revision: str) -> Path:
        return self._exercise_dir(code) / f"events__{revision}.jsonl"

    # -- 场景 ---------------------------------------------------------------

    def save_draft(self, data: dict[str, Any]) -> Scenario:
        """从字典建立 DRAFT 场景并落盘。已冻结版本拒绝覆盖。"""
        scenario = scenario_from_dict(
            {
                **data,
                "state": ScenarioState.DRAFT.value,
                "frozen_at": None,
            }
        )
        path = self._scenario_path(scenario.exercise_code, scenario.revision)
        if path.exists():
            existing = self.load_scenario(scenario.exercise_code, scenario.revision)
            if existing.state == ScenarioState.FROZEN.value:
                raise ScenarioConflictError(
                    f"场景 {existing.exercise_code}@{existing.revision} 已冻结，不能修改；请发布新 revision"
                )
        self._atomic_write_json(path, scenario.to_dict())
        return scenario

    def freeze(self, code: str, revision: str, *, frozen_at: str | None = None) -> Scenario:
        scenario = self.load_scenario(code, revision)
        frozen = freeze_scenario(scenario, frozen_at or format_ts(now_utc()))
        self._atomic_write_json(self._scenario_path(code, revision), frozen.to_dict())
        # 冻结同时建立空事件日志占位，明确“此后只追加”
        events_path = self._events_path(code, revision)
        if not events_path.exists():
            events_path.touch()
        return frozen

    def load_scenario(self, code: str, revision: str) -> Scenario:
        path = self._scenario_path(code, revision)
        if not path.exists():
            raise FileNotFoundError(f"场景不存在: {code}@{revision}")
        with path.open("r", encoding="utf-8") as fh:
            return scenario_from_dict(json.load(fh))

    def require_frozen(self, code: str, revision: str) -> Scenario:
        scenario = self.load_scenario(code, revision)
        if scenario.state != ScenarioState.FROZEN.value:
            raise IngestValidationError(f"场景 {code}@{revision} 尚未冻结")
        return scenario

    def list_exercises(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        if not self.data_dir.exists():
            return result
        for ex_dir in sorted(p for p in self.data_dir.iterdir() if p.is_dir()):
            for sp in sorted(ex_dir.glob("scenario__*.json")):
                with sp.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                result.append(
                    {
                        "exercise_code": data["exercise_code"],
                        "revision": data["revision"],
                        "state": data["state"],
                        "frozen_at": data.get("frozen_at"),
                    }
                )
        return result

    # -- 事件 ---------------------------------------------------------------

    def _load_log(self, code: str, revision: str) -> EventLog:
        return EventLog.load_jsonl(self._events_path(code, revision))

    def ingest(
        self,
        code: str,
        revision: str,
        event_type: str,
        *,
        occurred_at: str,
        unit_id: str,
        payload: dict[str, Any],
        reported_at: str | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> EventRecord:
        """接入一条事件：先做场景/语义校验，再追加并持久化。"""
        scenario = self.require_frozen(code, revision)
        events_path = self._events_path(code, revision)
        log = EventLog.load_jsonl(events_path)
        # 幂等判定先于语义校验：完全相同的重复上报一律返回 DuplicateEventError，
        # 不因“故障进行中”之类的语义规则而改变响应码。
        existing = log.find_duplicate(
            event_type, unit_id, occurred_at, payload, idempotency_key=idempotency_key
        )
        if existing is not None:
            raise DuplicateEventError(existing)
        validate_ingest(scenario, log.all(), event_type, payload, unit_id)
        record = log.append(
            event_type,
            occurred_at=occurred_at,
            reported_at=reported_at,
            unit_id=unit_id,
            payload=payload,
            event_id=event_id,
            idempotency_key=idempotency_key,
        )
        self._atomic_append_jsonl(events_path, record)
        return record

    def correct(
        self,
        code: str,
        revision: str,
        *,
        target_event_id: str,
        reason: str,
        unit_id: str,
        occurred_at: str,
        reported_at: str | None = None,
    ) -> EventRecord:
        """纠正误报的便捷入口：原始记录保留，只追加 CORRECTION。"""
        return self.ingest(
            code,
            revision,
            CORRECTION,
            occurred_at=occurred_at,
            reported_at=reported_at,
            unit_id=unit_id,
            payload={"target_event_id": target_event_id, "reason": reason},
        )

    def events(self, code: str, revision: str) -> list[EventRecord]:
        return self._load_log(code, revision).all()

    # -- 重放 ---------------------------------------------------------------

    def replay(self, code: str, revision: str, *, as_of: str | None = None) -> ReplayResult:
        scenario = self.require_frozen(code, revision)
        log = self._load_log(code, revision)
        return replay(scenario, log, as_of=as_of)

    @staticmethod
    def replay_files(scenario_path: str | Path, events_path: str | Path, *, as_of: str | None = None) -> ReplayResult:
        """供 CLI/外部使用：直接从场景 JSON 与事件 JSONL 重放。"""
        with open(scenario_path, "r", encoding="utf-8") as fh:
            scenario = scenario_from_dict(json.load(fh))
        if scenario.state != ScenarioState.FROZEN.value:
            raise IngestValidationError("场景文件不是冻结版本，不能作为复盘依据")
        log = EventLog.load_jsonl(events_path)
        return replay(scenario, log, as_of=as_of)

    # -- 原子写 -------------------------------------------------------------

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(canonical_dumps(data))
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _atomic_append_jsonl(path: Path, record: EventRecord) -> None:
        # 单进程服务下直接追加即可；以 O_APPEND 保证整行落盘。
        row = {
            "event_id": record.event_id,
            "event_type": record.event_type,
            "occurred_at": record.occurred_at,
            "reported_at": record.reported_at,
            "ingested_at": record.ingested_at,
            "unit_id": record.unit_id,
            "sequence": record.sequence,
            "payload": record.payload,
            "idempotency_key": record.idempotency_key,
            "predecessor_hash": record.predecessor_hash,
            "record_hash": record.record_hash,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
