"""国庆客流高峰联合演练演示数据。

故事线：物流枢纽变电站断电 -> 通信交换机与算力集群级联失效 ->
5G 备用链路绕行 + 算力任务向边缘节点迁移（容量受限）->
物流单位自行标记“已恢复”但无佐证 -> 分歧进入有期限会商 ->
变电站真正修复后经异单位确认，服务目标全部恢复。
事件以乱序接入（晚到的决策、补报），并包含重复上报与误报纠正，
另含一次演练中断/恢复；重放结果不受接入顺序、重复与中断影响。
"""

from __future__ import annotations

from typing import Any

from .engine import IngestValidationError
from .events import DuplicateEventError
from .service import ExerciseService

DEMO_CODE = "EX-2026-NATDAY-HUB"
DEMO_REVISION = "2026-09-30-r1"

SCENARIO: dict[str, Any] = {
    "exercise_code": DEMO_CODE,
    "revision": DEMO_REVISION,
    "coordinator_unit": "U-JHQ",
    "facilities": [
        {"facility_id": "F-POWER", "name": "枢纽变电站", "kind": "power", "owner_unit": "U-POWER", "criticality": "critical"},
        {"facility_id": "F-SWITCH", "name": "核心通信交换机", "kind": "network", "owner_unit": "U-NET", "criticality": "critical"},
        {"facility_id": "F-LINK5G", "name": "5G 备用链路", "kind": "network", "owner_unit": "U-NET", "criticality": "important"},
        {"facility_id": "F-COMPUTE", "name": "算力主集群", "kind": "compute", "owner_unit": "U-COMPUTE", "criticality": "critical"},
        {"facility_id": "F-EDGE", "name": "边缘备用算力节点", "kind": "compute", "owner_unit": "U-COMPUTE", "criticality": "important"},
        {"facility_id": "F-PINFO", "name": "旅客信息推送服务入口", "kind": "service", "owner_unit": "U-LOG", "criticality": "critical"},
        {"facility_id": "F-DISP", "name": "物资调度系统入口", "kind": "service", "owner_unit": "U-LOG", "criticality": "critical"},
    ],
    "dependencies": [
        {"facility_id": "F-SWITCH", "on_facility_id": "F-POWER", "kind": "hard"},
        {"facility_id": "F-COMPUTE", "on_facility_id": "F-SWITCH", "kind": "hard"},
        {"facility_id": "F-COMPUTE", "on_facility_id": "F-POWER", "kind": "hard"},
        {"facility_id": "F-PINFO", "on_facility_id": "F-COMPUTE", "kind": "hard"},
        {"facility_id": "F-DISP", "on_facility_id": "F-COMPUTE", "kind": "hard"},
    ],
    "alternatives": [
        {
            "alternative_id": "ALT-NET",
            "for_facility_id": "F-SWITCH",
            "backup_facility_id": "F-LINK5G",
            "switch_kind": "manual",
            "capacity_ratio": 0.6,
            "activation_minutes": 10,
        },
        {
            "alternative_id": "ALT-COMPUTE",
            "for_facility_id": "F-COMPUTE",
            "backup_facility_id": "F-EDGE",
            "switch_kind": "manual",
            "capacity_ratio": 0.85,
            "activation_minutes": 15,
            "requires": ["F-SWITCH"],  # 迁移流量须经已切至 5G 的通信链路（0.6），故综合 0.85×0.6
        },
    ],
    "services": [
        {
            "service_id": "S-PINFO",
            "name": "旅客疏导信息推送",
            "facility_ids": ["F-PINFO"],
            "target": "P95 推送时延 < 2 分钟，容量保持率 >= 50%",
            "required_capacity_ratio": 0.5,
        },
        {
            "service_id": "S-DISP",
            "name": "应急物资调度",
            "required_capacity_ratio": 1.0,
            "facility_ids": ["F-DISP"],
            "target": "调度指令容量 100%（边缘绕行综合容量 51%，不达标，必须真正恢复）",
        },
    ],
    "units": [
        {"unit_id": "U-JHQ", "name": "联合指挥部", "contacts": ["指挥值班席"], "scope": "统一会商与结论签发"},
        {"unit_id": "U-POWER", "name": "电力保障组", "contacts": ["变电运维班"], "scope": "枢纽供电"},
        {"unit_id": "U-NET", "name": "通信保障组", "contacts": ["网络运维中心"], "scope": "有线/5G 通信切换"},
        {"unit_id": "U-COMPUTE", "name": "算力保障组", "contacts": ["平台调度岗"], "scope": "算力任务迁移与恢复"},
        {"unit_id": "U-LOG", "name": "物流保障组", "contacts": ["枢纽调度室"], "scope": "旅客与物资保障服务确认"},
    ],
    "plan": [
        {"step_id": "PS-NET", "target_facility_id": "F-SWITCH", "action": "activate_alternative", "alternative_id": "ALT-NET", "planned_offset_minutes": 10, "responsible_unit": "U-NET", "restores_service_id": "S-PINFO"},
        {"step_id": "PS-MIG", "target_facility_id": "F-COMPUTE", "action": "activate_alternative", "alternative_id": "ALT-COMPUTE", "planned_offset_minutes": 25, "responsible_unit": "U-COMPUTE", "restores_service_id": "S-PINFO"},
        {"step_id": "PS-POWER", "target_facility_id": "F-POWER", "action": "restore", "planned_offset_minutes": 60, "responsible_unit": "U-POWER", "restores_service_id": "S-DISP"},
    ],
}

# (event_type, unit_id, occurred_at, reported_at, payload)
# 接入顺序故意与发生时间不同（决策迟报、佐证晚到），并夹一条重复上报。
# @n 表示“本列表第 n 条事件实际落库后的 event_id”，接入时解析。
EVENTS: list[tuple[str, str, str, str | None, dict[str, Any]]] = [
    ("exercise_phase", "U-JHQ", "2026-09-30T08:00:00+08:00", None, {"phase": "started"}),
    ("fault", "U-POWER", "2026-09-30T08:03:00+08:00", "2026-09-30T08:05:00+08:00",
     {"facility_id": "F-POWER", "description": "枢纽变电站全站失电"}),
    ("fault", "U-NET", "2026-09-30T08:08:00+08:00", None,
     {"facility_id": "F-LINK5G", "description": "误报：5G 链路告警（事后判定为仪表误报）"}),
    ("correction", "U-NET", "2026-09-30T08:12:00+08:00", None,
     {"target_event_id": "@3", "reason": "仪表误报，5G 链路实际可用"}),
    ("recovery_evidence", "U-NET", "2026-09-30T08:15:00+08:00", None,
     {"evidence_id": "EV-NET", "facility_id": "F-SWITCH", "mode": "bypass", "alternative_id": "ALT-NET",
      "note": "通信切至 5G 备用链路，带宽 60%"}),
    ("recovery_evidence", "U-COMPUTE", "2026-09-30T08:18:00+08:00", None,
     {"evidence_id": "EV-NET-CONF", "facility_id": "F-SWITCH", "mode": "bypass", "alternative_id": "ALT-NET",
      "confirms_evidence": "@5", "note": "算力组侧观测到链路切换成功，异单位佐证"}),
    ("exercise_phase", "U-JHQ", "2026-09-30T08:33:00+08:00", None, {"phase": "interrupted", "reason": "演练系统临时重启"}),
    ("exercise_phase", "U-JHQ", "2026-09-30T08:37:00+08:00", None, {"phase": "resumed"}),
    ("recovery_evidence", "U-LOG", "2026-09-30T08:27:00+08:00", "2026-09-30T08:40:00+08:00",
     {"evidence_id": "EV-LOGP", "facility_id": "F-PINFO", "mode": "restore",
      "note": "物流组在本系统内标记旅客信息服务已恢复（无对侧佐证）"}),
    ("decision", "U-NET", "2026-09-30T08:06:00+08:00", "2026-09-30T08:41:00+08:00",
     {"decision_id": "DEC-NET", "kind": "activate_alternative", "facility_id": "F-SWITCH", "alternative_id": "ALT-NET"}),
    ("decision", "U-COMPUTE", "2026-09-30T08:20:00+08:00", "2026-09-30T08:42:00+08:00",
     {"decision_id": "DEC-MIG", "kind": "activate_alternative", "facility_id": "F-COMPUTE", "alternative_id": "ALT-COMPUTE"}),
    ("recovery_evidence", "U-COMPUTE", "2026-09-30T08:26:00+08:00", "2026-09-30T08:43:00+08:00",
     {"evidence_id": "EV-EDGE", "facility_id": "F-COMPUTE", "mode": "bypass", "alternative_id": "ALT-COMPUTE",
      "note": "算力任务迁移至边缘节点，综合容量约 51%（边缘 85% × 5G 链路 60%），依赖 5G 链路"}),
    ("recovery_evidence", "U-LOG", "2026-09-30T08:31:00+08:00", "2026-09-30T08:44:00+08:00",
     {"evidence_id": "EV-EDGE-DISP", "facility_id": "F-COMPUTE", "mode": "bypass", "alternative_id": "ALT-COMPUTE",
      "disputes_evidence": "@12", "note": "物流组异议：调度口径下容量不足，不能算恢复，需会商"}),
    ("consultation_open", "U-JHQ", "2026-09-30T08:46:00+08:00", None,
     {"consultation_id": "CONS-EDGE", "facility_id": "F-COMPUTE",
      "subject": "边缘算力迁移是否构成物资调度能力恢复",
      "deadline_at": "2026-09-30T09:16:00+08:00",
      "parties": ["U-COMPUTE", "U-LOG"],
      "claim_event_ids": ["@12", "@13"]}),
    ("consultation_resolve", "U-JHQ", "2026-09-30T08:50:00+08:00", None,
     {"consultation_id": "CONS-EDGE", "resolution": "confirmed", "evidence_id": "@12",
      "agreed_facts": "边缘迁移确已生效但仅为绕行，综合容量约 51%（边缘 85% × 5G 链路 60%）；旅客信息目标达成，物资调度目标未达成"}),
    ("finding", "U-JHQ", "2026-09-30T09:00:00+08:00", None,
     {"finding_id": "F-UNIFY", "title": "各单位自行标记恢复，缺少统一证据口径",
      "severity": "major", "description": "物流组在无对侧佐证情况下标记服务恢复，导致风险是否解除无法判定",
      "triggering_event_ids": ["@9"]}),
    ("remediation", "U-JHQ", "2026-09-30T09:05:00+08:00", None,
     {"remediation_id": "R-UNIFY", "finding_id": "F-UNIFY", "action": "建立恢复证据异单位佐证与会商联动机制",
      "owner_unit": "U-JHQ", "due_at": "2026-10-07T18:00:00+08:00"}),
    ("recovery_evidence", "U-POWER", "2026-09-30T09:10:00+08:00", None,
     {"evidence_id": "EV-POWER", "facility_id": "F-POWER", "mode": "restore",
      "note": "变电站故障排除，市电恢复"}),
    ("recovery_evidence", "U-NET", "2026-09-30T09:14:00+08:00", None,
     {"evidence_id": "EV-POWER-CONF", "facility_id": "F-POWER", "mode": "restore",
      "confirms_evidence": "@18", "note": "通信组确认交换机供电与主链路恢复，异单位佐证"}),
    ("remediation", "U-JHQ", "2026-09-30T09:30:00+08:00", None,
     {"remediation_id": "R-UNIFY", "finding_id": "F-UNIFY", "action": "建立恢复证据异单位佐证与会商联动机制",
      "status": "accepted", "accepted_by": "U-JHQ-值班总值班长"}),
    ("exercise_phase", "U-JHQ", "2026-09-30T09:40:00+08:00", None, {"phase": "ended"}),
    ("fault", "U-POWER", "2026-09-30T08:03:00+08:00", "2026-09-30T10:00:00+08:00",
     {"facility_id": "F-POWER", "description": "枢纽变电站全站失电"}),
    ("remediation", "U-JHQ", "2026-10-05T10:00:00+08:00", None,
     {"remediation_id": "R-UNIFY", "finding_id": "F-UNIFY", "action": "建立恢复证据异单位佐证与会商联动机制",
      "status": "closed", "verified_by": "U-JHQ-审计组"}),
]


def _resolve_refs(value: Any, created: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("@"):
        if value not in created:
            raise ValueError(f"演示数据引用了尚未接入的事件: {value}")
        return created[value]
    if isinstance(value, dict):
        return {k: _resolve_refs(v, created) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, created) for v in value]
    return value


def build_demo(service: ExerciseService) -> dict[str, Any]:
    """写入演示场景与事件；所有 @n 引用在接入时解析为真实 event_id。"""
    service.save_draft(SCENARIO)
    service.freeze(DEMO_CODE, DEMO_REVISION, frozen_at="2026-09-30T07:30:00+08:00")

    created: dict[str, str] = {}
    ingested = rejected = 0
    for index, (etype, unit, occurred, reported, payload) in enumerate(EVENTS, 1):
        resolved = _resolve_refs(payload, created)
        try:
            rec = service.ingest(
                DEMO_CODE,
                DEMO_REVISION,
                etype,
                occurred_at=occurred,
                reported_at=reported,
                unit_id=unit,
                payload=resolved,
            )
        except (DuplicateEventError, IngestValidationError):
            rejected += 1
            continue
        created[f"@{index}"] = rec.event_id
        ingested += 1

    return {"ingested": ingested, "rejected": rejected, "exercise": DEMO_CODE, "revision": DEMO_REVISION}
