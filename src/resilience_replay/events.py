"""只追加事件日志。

核心纪律：
- 每条事件同时记录 *发生时间* occurred_at 与 *上报时间* reported_at；
- 乱序上报照常接收，重放时按发生时间排序；
- 重复上报以幂等键去重，同一事实不会产生两条记录；
- 误报通过追加 CORRECTION 事件纠正，被纠正事件原样保留在日志中，
  任何接口都不得物理删除或改写原始记录；
- 记录按接入顺序形成哈希链，落盘为 JSONL，演练中断重启后重新加载、
  校验链完整性并重放，结果与中断前一致。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable
import json
import uuid

from .clock import format_ts, now_utc, parse_ts
from .jsonio import canonical_dumps, digest


# 事件类型常量
FAULT = "fault"
DECISION = "decision"
RESOURCE_ALLOCATION = "resource_allocation"
RECOVERY_EVIDENCE = "recovery_evidence"
CORRECTION = "correction"
CONSULTATION_OPEN = "consultation_open"
CONSULTATION_RESOLVE = "consultation_resolve"
FINDING = "finding"
REMEDIATION = "remediation"
EXERCISE_PHASE = "exercise_phase"  # started | interrupted | resumed | ended

ALL_EVENT_TYPES = frozenset(
    {
        FAULT,
        DECISION,
        RESOURCE_ALLOCATION,
        RECOVERY_EVIDENCE,
        CORRECTION,
        CONSULTATION_OPEN,
        CONSULTATION_RESOLVE,
        FINDING,
        REMEDIATION,
        EXERCISE_PHASE,
    }
)


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    event_type: str
    occurred_at: str  # ISO-8601, UTC
    reported_at: str
    ingested_at: str
    unit_id: str
    sequence: int
    payload: dict[str, Any]
    idempotency_key: str
    predecessor_hash: str
    record_hash: str

    def is_effective(self, retracted_ids: frozenset[str] = frozenset()) -> bool:
        """重放只采用未被纠正的事件；被纠正事件仍可查询、引用。"""
        return self.event_id not in retracted_ids


def _payload_digest(event_type: str, unit_id: str, occurred_at: str, payload: dict[str, Any]) -> str:
    return digest([event_type, unit_id, occurred_at, payload])


class DuplicateEventError(ValueError):
    """重复上报：同一幂等键已存在。"""

    def __init__(self, existing: EventRecord) -> None:
        super().__init__(f"重复上报，已存在事件 {existing.event_id}")
        self.existing = existing


class EventLog:
    """内存中的只追加日志，可持久化到 JSONL。"""

    def __init__(self, records: Iterable[EventRecord] = ()) -> None:
        self._records: list[EventRecord] = list(records)
        self._by_id: dict[str, EventRecord] = {r.event_id: r for r in self._records}
        self._by_idem: dict[str, EventRecord] = {r.idempotency_key: r for r in self._records}

    # -- 写入 ---------------------------------------------------------------

    def append(
        self,
        event_type: str,
        *,
        occurred_at: str,
        unit_id: str,
        payload: dict[str, Any],
        reported_at: str | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
        ingested_at: str | None = None,
    ) -> EventRecord:
        if event_type not in ALL_EVENT_TYPES:
            raise ValueError(f"未知事件类型: {event_type}")
        if not isinstance(payload, dict):
            raise ValueError("事件 payload 必须是对象")
        occurred = format_ts(parse_ts(occurred_at))
        reported = format_ts(parse_ts(reported_at)) if reported_at else occurred
        if parse_ts(reported) < parse_ts(occurred):
            raise ValueError("上报时间不能早于发生时间")
        if not unit_id.strip():
            raise ValueError("unit_id 不能为空")

        canonical_payload = json.loads(canonical_dumps(payload))  # 规范化键序，稳定去重
        key = idempotency_key or _payload_digest(event_type, unit_id, occurred, canonical_payload)
        if key in self._by_idem:
            raise DuplicateEventError(self._by_idem[key])

        eid = event_id or f"evt-{uuid.uuid4().hex[:12]}"
        if eid in self._by_id:
            raise ValueError(f"事件标识重复: {eid}")

        sequence = len(self._records)
        predecessor = self._records[-1].record_hash if self._records else "GENESIS"
        ingested = format_ts(parse_ts(ingested_at)) if ingested_at else format_ts(now_utc())
        record = EventRecord(
            event_id=eid,
            event_type=event_type,
            occurred_at=occurred,
            reported_at=reported,
            ingested_at=ingested,
            unit_id=unit_id,
            sequence=sequence,
            payload=canonical_payload,
            idempotency_key=key,
            predecessor_hash=predecessor,
            record_hash="",
        )
        h = digest(
            [
                eid,
                event_type,
                occurred,
                reported,
                ingested,
                unit_id,
                sequence,
                canonical_payload,
                key,
                predecessor,
            ]
        )
        record = replace(record, record_hash=h)
        self._records.append(record)
        self._by_id[eid] = record
        self._by_idem[key] = record
        return record

    # -- 读取 ---------------------------------------------------------------

    def get(self, event_id: str) -> EventRecord:
        return self._by_id[event_id]

    def contains_id(self, event_id: str) -> bool:
        return event_id in self._by_id

    def find_duplicate(
        self,
        event_type: str,
        unit_id: str,
        occurred_at: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> EventRecord | None:
        """在语义校验前判定是否为同一事实的重复上报。"""
        canonical = json.loads(canonical_dumps(payload))
        key = idempotency_key or _payload_digest(event_type, unit_id, format_ts(parse_ts(occurred_at)), canonical)
        return self._by_idem.get(key)

    def all(self) -> list[EventRecord]:
        return list(self._records)

    def retraction_map(self) -> dict[str, EventRecord]:
        """被纠正事件 -> CORRECTION 事件。纠正事件本身不可被纠正（由引擎校验）。"""
        result: dict[str, EventRecord] = {}
        for r in self._records:
            if r.event_type == CORRECTION:
                target = r.payload.get("target_event_id")
                if isinstance(target, str):
                    result[target] = r
        return result

    def effective(self) -> list[EventRecord]:
        retracted = frozenset(self.retraction_map())
        return [r for r in self._records if r.is_effective(retracted)]

    def ordered_by_occurrence(self, *, include_retracted: bool = False) -> list[EventRecord]:
        """重放顺序：先按发生时间，再按接入序号消解同时刻歧义。"""
        records = self._records if include_retracted else self.effective()
        return sorted(records, key=lambda r: (parse_ts(r.occurred_at), r.sequence))

    @property
    def head_hash(self) -> str:
        return self._records[-1].record_hash if self._records else "GENESIS"

    def __len__(self) -> int:
        return len(self._records)

    # -- 持久化 -------------------------------------------------------------

    _FIELDS = (
        "event_id",
        "event_type",
        "occurred_at",
        "reported_at",
        "ingested_at",
        "unit_id",
        "sequence",
        "payload",
        "idempotency_key",
        "predecessor_hash",
        "record_hash",
    )

    def save_jsonl(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for r in self._records:
                row = {k: getattr(r, k) for k in self._FIELDS}
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    @classmethod
    def load_jsonl(cls, path: str | Path, *, verify_chain: bool = True) -> "EventLog":
        path = Path(path)
        records: list[EventRecord] = []
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                missing = set(cls._FIELDS) - data.keys()
                if missing:
                    raise ValueError(f"{path}:{lineno} 缺少字段: {sorted(missing)}")
                record = EventRecord(**{k: data[k] for k in cls._FIELDS})
                if verify_chain:
                    expected_pred = records[-1].record_hash if records else "GENESIS"
                    if record.predecessor_hash != expected_pred:
                        raise ValueError(
                            f"{path}:{lineno} 哈希链断裂（事件 {record.event_id}），日志可能被篡改"
                        )
                    if record.sequence != len(records):
                        raise ValueError(
                            f"{path}:{lineno} 序号不连续：期望 {len(records)}，实际 {record.sequence}"
                        )
                    rebuilt = digest(
                        [
                            record.event_id,
                            record.event_type,
                            record.occurred_at,
                            record.reported_at,
                            record.ingested_at,
                            record.unit_id,
                            record.sequence,
                            record.payload,
                            record.idempotency_key,
                            record.predecessor_hash,
                        ]
                    )
                    if rebuilt != record.record_hash:
                        raise ValueError(f"事件 {record.event_id} 摘要校验失败，内容被改写")
                records.append(record)
        return cls(records)
