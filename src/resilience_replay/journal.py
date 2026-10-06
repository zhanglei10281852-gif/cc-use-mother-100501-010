"""追加式事件日志：哈希链、幂等上报与纠错留痕。

设计要点
--------
* 每条事件都有业务 ``event_id``（由上报单位给出）；重复上报同一
  ``event_id`` 且内容一致时直接忽略，内容冲突则拒绝（防伪造）。
* 事件以 :data:`EventEnvelope.seq` 追加，每条记录前一条哈希形成哈希链；
  日志可整体重算校验，演练中断重启后从磁盘恢复，结果不变。
* 误报只能用 ``retract_event`` 纠正：原记录保留，新增一条纠正记录。
* 计算一律按**发生时间** ``occurred_at`` 排序，与上报/到达顺序无关；
  同一时刻按 ``(event_id, seq)`` 打破平局，保证确定性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any, Mapping

from .errors import EventConflictError, EventValidationError, JournalIntegrityError
from .time_model import iso, parse_ts

# 载荷类型注册表：校验函数与必填字段。
PAYLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "facility_fault": ("facility_id",),
    "facility_restored": ("facility_id", "restoration_kind"),  # restoration_kind: real|detour
    "decision": ("summary",),
    "resource_dispatch": ("resource", "to_unit"),
    "evidence_reported": ("facility_id", "summary", "version"),
    "evidence_confirmed": ("evidence_id", "version"),
    "activate_detour": ("primary", "backup"),
    "detour_reverted": ("primary", "backup"),
    "retract_event": ("target_event_id", "reason"),
    "dispute_opened": ("fact", "positions"),
    "dispute_position": ("dispute_id", "position"),
    "dispute_resolved": ("dispute_id", "resolution", "winning"),
    "action_registered": ("finding_id", "title", "owner_unit"),
    "action_accepted": ("action_id", "accepted_by"),
    "action_verified": ("action_id", "evidence_id"),
    "exercise_paused": (),
    "exercise_resumed": (),
}

GENESIS = "0" * 64


def _require_fields(payload: Mapping[str, Any], fields: tuple[str, ...], kind: str) -> None:
    for name in fields:
        if name not in payload:
            raise EventValidationError(f"{kind} 载荷缺少字段: {name}")
        value = payload[name]
        if value is None or (isinstance(value, str) and not value.strip()):
            raise EventValidationError(f"{kind} 载荷字段 {name} 不能为空")
    if kind == "facility_restored" and payload.get("restoration_kind") not in (
        "real",
        "detour",
    ):
        raise EventValidationError("restoration_kind 必须是 real 或 detour")
    if kind == "evidence_reported":
        version = payload.get("version")
        if not isinstance(version, int) or version < 1:
            raise EventValidationError("证据版本必须是 >=1 的整数")


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """日志中一条不可变记录。"""

    seq: int
    event_id: str
    exercise_code: str
    unit_id: str
    occurred_at: str          # 事件实际发生时间（业务时间）
    reported_at: str          # 单位上报时间
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    record_hash: str = ""

    def body_for_hash(self) -> str:
        data = {
            "seq": self.seq,
            "event_id": self.event_id,
            "exercise_code": self.exercise_code,
            "unit_id": self.unit_id,
            "occurred_at": self.occurred_at,
            "reported_at": self.reported_at,
            "kind": self.kind,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _hash(body: str) -> str:
        return sha256(body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "exercise_code": self.exercise_code,
            "unit_id": self.unit_id,
            "occurred_at": self.occurred_at,
            "reported_at": self.reported_at,
            "kind": self.kind,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EventEnvelope":
        return cls(**dict(data))


@dataclass(slots=True)
class EventJournal:
    """某场演练的追加日志，可持久化为 JSONL。"""

    exercise_code: str
    records: list[EventEnvelope] = field(default_factory=list)
    _by_id: dict[str, EventEnvelope] = field(default_factory=dict)
    _retracted: set[str] = field(default_factory=set)

    # ---- 追加 -----------------------------------------------------------

    def append(
        self,
        *,
        event_id: str,
        unit_id: str,
        occurred_at: str,
        reported_at: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> EventEnvelope | None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise EventValidationError("event_id 不能为空")
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise EventValidationError("unit_id 不能为空")
        if kind not in PAYLOAD_TYPES:
            raise EventValidationError(f"未知事件类型: {kind}")
        payload = dict(payload)
        _require_fields(payload, PAYLOAD_TYPES[kind], kind)
        # 时间可解析且发生时间不晚于上报时间（演练数据的基本常识约束）。
        occurred = parse_ts(occurred_at)
        reported = parse_ts(reported_at)
        if occurred > reported:
            raise EventValidationError(
                f"事件 {event_id} 的发生时间晚于上报时间"
            )

        duplicate = self._by_id.get(event_id.strip())
        if duplicate is not None:
            # 幂等：内容一致即忽略；不一致即冲突。
            candidate_fingerprint = self._fingerprint_payload(
                unit_id, occurred_at, reported_at, kind, payload
            )
            existing_fingerprint = self._fingerprint_payload(
                duplicate.unit_id,
                duplicate.occurred_at,
                duplicate.reported_at,
                duplicate.kind,
                duplicate.payload,
            )
            if candidate_fingerprint == existing_fingerprint:
                return None
            raise EventConflictError(f"重复事件标识 {event_id} 的内容与原记录冲突")

        seq = len(self.records) + 1
        prev_hash = self.records[-1].record_hash if self.records else GENESIS
        envelope = EventEnvelope(
            seq=seq,
            event_id=event_id.strip(),
            exercise_code=self.exercise_code,
            unit_id=unit_id.strip(),
            occurred_at=iso(occurred),
            reported_at=iso(reported),
            kind=kind,
            payload=payload,
            prev_hash=prev_hash,
        )
        object.__setattr__(envelope, "record_hash", EventEnvelope._hash(envelope.body_for_hash()))
        self.records.append(envelope)
        self._by_id[envelope.event_id] = envelope
        if kind == "retract_event":
            # 目标可能因乱序上报尚未到达，此处不强制校验；
            # 由 validate_references() 在加载/重放前统一把关。
            self._retracted.add(payload["target_event_id"])
        return envelope

    def validate_references(self) -> None:
        """检查纠正记录：目标存在，且纠正不能早于被纠正事件的发生时间。"""
        for record in self.records:
            if record.kind != "retract_event":
                continue
            target_id = record.payload["target_event_id"]
            target = self._by_id.get(target_id)
            if target is None:
                raise EventValidationError(
                    f"纠正记录 {record.event_id} 指向不存在的事件: {target_id}"
                )
            if parse_ts(record.occurred_at) < parse_ts(target.occurred_at):
                raise EventValidationError(
                    f"纠正记录 {record.event_id} 的发生时间早于被纠正事件 {target_id}"
                )

    @staticmethod
    def _fingerprint_payload(
        unit_id: str,
        occurred_at: str,
        reported_at: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> str:
        body = json.dumps(
            {
                "unit_id": unit_id,
                "occurred_at": occurred_at,
                "reported_at": reported_at,
                "kind": kind,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(body.encode("utf-8")).hexdigest()

    # ---- 读取视图 -------------------------------------------------------

    def is_retracted(self, event_id: str) -> bool:
        return event_id in self._retracted

    def active_records(self) -> list[EventEnvelope]:
        """按发生时间确定性排序后的有效（未被纠正）记录。

        乱序上报在此被消除：平局顺序为 ``(occurred_at, event_id, seq)``。
        """
        active = [r for r in self.records if r.event_id not in self._retracted]
        return sorted(
            active,
            key=lambda r: (parse_ts(r.occurred_at), r.event_id, r.seq),
        )

    def all_records(self) -> list[EventEnvelope]:
        """含被纠正记录的追加顺序视图，供审计；纠正记录不抹除原始记录。"""
        return list(self.records)

    def tail_hash(self) -> str:
        return self.records[-1].record_hash if self.records else GENESIS

    # ---- 完整性与持久化 -------------------------------------------------

    def verify_chain(self) -> None:
        previous = GENESIS
        for record in self.records:
            if record.prev_hash != previous:
                raise JournalIntegrityError(
                    f"seq={record.seq} 前向哈希不匹配，日志可能被截断"
                )
            expected = EventEnvelope._hash(record.body_for_hash())
            if record.record_hash != expected:
                raise JournalIntegrityError(
                    f"seq={record.seq} 记录哈希不匹配，内容被篡改"
                )
            previous = record.record_hash

    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
            for record in self.records
        )

    @classmethod
    def from_jsonl(cls, exercise_code: str, text: str) -> "EventJournal":
        journal = cls(exercise_code=exercise_code)
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                envelope = EventEnvelope.from_dict(data)
            except (TypeError, json.JSONDecodeError) as exc:
                raise JournalIntegrityError(f"第 {line_no} 行无法解析") from exc
            if envelope.exercise_code != exercise_code:
                raise JournalIntegrityError(
                    f"第 {line_no} 行属于另一场演练 {envelope.exercise_code}"
                )
            journal.records.append(envelope)
            journal._by_id[envelope.event_id] = envelope
            if envelope.kind == "retract_event":
                journal._retracted.add(envelope.payload["target_event_id"])
        journal.verify_chain()
        return journal

    def dangling_retractions(self) -> list[str]:
        """返回指向缺失事件的纠正记录 event_id（最终集合仍不完整时才有值）。"""
        return [
            record.event_id
            for record in self.records
            if record.kind == "retract_event"
            and record.payload["target_event_id"] not in self._by_id
        ]
