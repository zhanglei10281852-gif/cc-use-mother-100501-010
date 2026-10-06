"""平台服务层：冻结场景、接收事件、确定性重放。

这是 API 与命令行共用的唯一入口，业务规则全部沉淀在领域模块中，
本层只负责编排与事务边界（加载→追加→落盘；加载→校验→重放）。
"""

from __future__ import annotations

from typing import Any, Mapping

from .engine import DEFAULT_DISPUTE_SLA_SECONDS, ReplayEngine
from .errors import EventValidationError
from .journal import EventJournal
from .scenario import Scenario
from .store import ExerciseStore


class ResiliencePlatform:
    """跨网络韧性演练与复盘平台。"""

    def __init__(self, data_dir: str = "./data") -> None:
        self.store = ExerciseStore(data_dir)

    # ---- 场景冻结 -------------------------------------------------------

    def freeze_scenario(self, data: Mapping[str, Any] | Scenario) -> dict[str, Any]:
        scenario = data if isinstance(data, Scenario) else Scenario.from_dict(data)
        self.store.save_scenario(scenario)
        return {
            "exercise_code": scenario.exercise_code,
            "scenario_revision": scenario.scenario_revision,
            "scenario_fingerprint": scenario.fingerprint(),
            "frozen": True,
        }

    def list_exercises(self) -> list[str]:
        return self.store.list_exercises()

    def get_scenario(self, exercise_code: str) -> Scenario:
        return self.store.load_scenario(exercise_code)

    # ---- 事件接收 -------------------------------------------------------

    def report_event(
        self,
        exercise_code: str,
        *,
        event_id: str,
        unit_id: str,
        occurred_at: str,
        reported_at: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """接收一条事件。

        返回 ``status=recorded`` 或 ``status=duplicate``（幂等忽略）。
        同一 ``event_id`` 内容冲突抛 :class:`EventConflictError`。
        """
        scenario = self.store.load_scenario(exercise_code)
        unit_ids = {unit.unit_id for unit in scenario.units}
        if unit_id not in unit_ids:
            raise EventValidationError(
                f"上报单位 {unit_id} 不是演练 {exercise_code} 的登记参与单位"
            )
        journal = self.store.load_journal(exercise_code)
        envelope = journal.append(
            event_id=event_id,
            unit_id=unit_id,
            occurred_at=occurred_at,
            reported_at=reported_at,
            kind=kind,
            payload=payload,
        )
        self.store.append_journal(exercise_code, journal)
        if envelope is None:
            return {"status": "duplicate", "event_id": event_id}
        return {
            "status": "recorded",
            "event_id": envelope.event_id,
            "seq": envelope.seq,
            "record_hash": envelope.record_hash,
        }

    def ingest_many(
        self, exercise_code: str, events: list[Mapping[str, Any]]
    ) -> dict[str, int]:
        """批量接收（乱序也没关系）；返回计数。"""
        scenario = self.store.load_scenario(exercise_code)
        unit_ids = {unit.unit_id for unit in scenario.units}
        journal = self.store.load_journal(exercise_code)
        recorded = duplicated = 0
        for item in events:
            missing = [
                key for key in ("event_id", "unit_id", "occurred_at", "reported_at", "kind")
                if key not in item
            ]
            if missing:
                raise EventValidationError(f"事件缺少字段: {', '.join(missing)}")
            if item["unit_id"] not in unit_ids:
                raise EventValidationError(
                    f"上报单位 {item['unit_id']} 不是登记参与单位"
                )
            envelope = journal.append(
                event_id=item["event_id"],
                unit_id=item["unit_id"],
                occurred_at=item["occurred_at"],
                reported_at=item["reported_at"],
                kind=item["kind"],
                payload=item.get("payload", {}),
            )
            if envelope is None:
                duplicated += 1
            else:
                recorded += 1
        # 引用完整性在重放加载时把关；乱序逐条/批量上报中途允许
        # 纠正记录先于目标到达，只要最终集合完整即可。
        self.store.append_journal(exercise_code, journal)
        return {"recorded": recorded, "duplicates_ignored": duplicated}

    def list_events(self, exercise_code: str) -> list[dict[str, Any]]:
        journal = self.store.load_journal(exercise_code)
        return [record.to_dict() for record in journal.all_records()]

    def verify_integrity(self, exercise_code: str) -> dict[str, Any]:
        scenario = self.store.load_scenario(exercise_code)
        journal = self.store.load_journal(exercise_code)  # 构造时已验链
        journal.validate_references()
        return {
            "exercise_code": exercise_code,
            "scenario_fingerprint": scenario.fingerprint(),
            "journal_tail_hash": journal.tail_hash(),
            "records": len(journal.records),
            "chain_valid": True,
            "references_valid": True,
        }

    # ---- 重放 -----------------------------------------------------------

    def replay(
        self,
        exercise_code: str,
        *,
        as_of: str | None = None,
        dispute_sla_seconds: int = DEFAULT_DISPUTE_SLA_SECONDS,
    ) -> dict[str, Any]:
        scenario = self.store.load_scenario(exercise_code)
        journal = self.store.load_journal(exercise_code)
        engine = ReplayEngine(
            scenario,
            journal,
            dispute_sla_seconds=dispute_sla_seconds,
            as_of=as_of,
        )
        return engine.replay()

    @staticmethod
    def replay_from_snapshot(
        scenario: Scenario,
        journal: EventJournal,
        *,
        as_of: str | None = None,
        dispute_sla_seconds: int = DEFAULT_DISPUTE_SLA_SECONDS,
    ) -> dict[str, Any]:
        """不经过存储直接重放（测试与导入分析使用）。"""
        return ReplayEngine(
            scenario, journal,
            dispute_sla_seconds=dispute_sla_seconds, as_of=as_of,
        ).replay()
