"""内置示例：国庆客流高峰前物流枢纽联合演练。

场景：枢纽变电站失电 → 主用光缆中断 → 通信切换 / 算力任务迁移。
事件清单故意包含：乱序上报、重复上报、误报纠正、多单位主张冲突
触发的有期限会商、临时绕行与真正恢复并存、演练中断重启、
RTO 超标发现及其整改措施全生命周期。
"""

from __future__ import annotations

from typing import Any

# ---- 参与单位 -----------------------------------------------------------

UNITS = [
    {"unit_id": "HQ", "name": "联合指挥部", "responsibilities": ("统一会商、结论裁定",)},
    {"unit_id": "POWER", "name": "供电公司", "responsibilities": ("枢纽供电与应急发电",)},
    {"unit_id": "TELCO-A", "name": "主用运营商", "responsibilities": ("主用光缆与基站集群",)},
    {"unit_id": "TELCO-B", "name": "应急运营商", "responsibilities": ("备用链路与应急基站",)},
    {"unit_id": "COMPUTE", "name": "算力中心", "responsibilities": ("调度算力与任务迁移",)},
    {"unit_id": "HUB", "name": "物流枢纽", "responsibilities": ("现场调度、旅客与物资保障",)},
]

# ---- 设施 ---------------------------------------------------------------

FACILITIES = [
    {"facility_id": "F-SUB", "name": "枢纽35kV变电站", "kind": "power", "owner_unit": "POWER", "critical": True},
    {"facility_id": "F-GEN", "name": "应急柴油发电机", "kind": "generator", "owner_unit": "POWER"},
    {"facility_id": "F-FIBER-A", "name": "主用光缆环网", "kind": "link", "owner_unit": "TELCO-A", "critical": True},
    {"facility_id": "F-FIBER-B", "name": "备用微波链路", "kind": "link", "owner_unit": "TELCO-B"},
    {"facility_id": "F-BS-A", "name": "枢纽基站集群A", "kind": "basestation", "owner_unit": "TELCO-A", "critical": True},
    {"facility_id": "F-BS-B", "name": "应急通信基站B", "kind": "basestation", "owner_unit": "TELCO-B"},
    {"facility_id": "F-COMPUTE", "name": "枢纽调度算力节点", "kind": "compute", "owner_unit": "COMPUTE", "critical": True},
    {"facility_id": "F-COMPUTE-BACKUP", "name": "云端备用算力集群", "kind": "compute", "owner_unit": "COMPUTE"},
]

# ---- 硬依赖（上游 → 下游） ----------------------------------------------

DEPENDENCIES = [
    {"upstream": "F-SUB", "downstream": "F-BS-A", "label": "供电"},
    {"upstream": "F-FIBER-A", "downstream": "F-BS-A", "label": "回传链路"},
    {"upstream": "F-SUB", "downstream": "F-COMPUTE", "label": "双路供电之一"},
    {"upstream": "F-BS-A", "downstream": "F-COMPUTE", "label": "调度网接入", "required": True},
    {"upstream": "F-FIBER-B", "downstream": "F-BS-B", "label": "回传链路"},
]

# ---- 冻结的可用替代（绕行必须在预案窗口内） ------------------------------

WINDOW = {"valid_from": "2026-09-30T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"}

ALTERNATIVES = [
    {"primary": "F-SUB", "backup": "F-GEN", "label": "柴油发电替代市电", "transparent": True, **WINDOW},
    {"primary": "F-FIBER-A", "backup": "F-FIBER-B", "label": "切至应急运营商微波", **WINDOW},
    {"primary": "F-BS-A", "backup": "F-BS-B", "label": "应急基站承接话务与广播", **WINDOW},
    {"primary": "F-COMPUTE", "backup": "F-COMPUTE-BACKUP", "label": "调度任务迁移上云", **WINDOW},
]

# ---- 关键服务目标 -------------------------------------------------------

SERVICES = [
    {
        "service_id": "S-COMMS",
        "name": "旅客通信与到离港广播",
        "depends_on": ["F-BS-A"],
        "rto_seconds": 240,
        "owner_unit": "TELCO-A",
    },
    {
        "service_id": "S-COMPUTE",
        "name": "物资调度算力服务",
        "depends_on": ["F-COMPUTE"],
        "rto_seconds": 600,
        "owner_unit": "COMPUTE",
    },
    {
        "service_id": "S-DISPATCH",
        "name": "联合现场调度指挥",
        "depends_on": ["F-COMPUTE", "F-BS-A"],
        "rto_seconds": 900,
        "owner_unit": "HUB",
    },
]


def scenario_dict() -> dict[str, Any]:
    return {
        "exercise_code": "EX-2026-ND-HUB-01",
        "scenario_revision": "2026-09-25-frozen-r1",
        "coordinator": "HQ",
        "frozen_at": "2026-09-25T08:00:00Z",
        "facilities": FACILITIES,
        "dependencies": DEPENDENCIES,
        "alternatives": ALTERNATIVES,
        "services": SERVICES,
        "units": UNITS,
    }


def _ev(
    event_id: str,
    unit_id: str,
    occurred_at: str,
    reported_at: str,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "unit_id": unit_id,
        "occurred_at": occurred_at,
        "reported_at": reported_at,
        "kind": kind,
        "payload": payload,
    }


# ---- 事件流（此处故意按“到达顺序”乱序书写，重放按发生时间排序） ----------

def events() -> list[dict[str, Any]]:
    T = "2026-09-30T{}Z"
    items: list[dict[str, Any]] = [
        # 08:00:10 变电站失电，供电公司 08:00:40 才上报
        _ev("EV-PWR-FAULT-01", "POWER", T.format("08:00:10"), T.format("08:00:40"),
            "facility_fault", {"facility_id": "F-SUB", "cause": "外线跳闸"}),
        # 08:01:00 主用光缆因失电区域倒杆中断（08:01:30 上报）
        _ev("EV-FIBER-FAULT-01", "TELCO-A", T.format("08:01:00"), T.format("08:01:30"),
            "facility_fault", {"facility_id": "F-FIBER-A", "cause": "倒杆断缆"}),
        # 算力中心 08:01:20 观测到本节点不可用（被断电+断网波及）
        _ev("EV-CMP-FAULT-01", "COMPUTE", T.format("08:01:20"), T.format("08:02:00"),
            "facility_fault", {"facility_id": "F-COMPUTE", "cause": "断电断网波及"}),
        # 08:02:30 启动柴油发电机（对变电站的临时绕行）
        _ev("EV-GEN-ON-01", "POWER", T.format("08:02:30"), T.format("08:02:50"),
            "activate_detour", {"primary": "F-SUB", "backup": "F-GEN"}),
        # 08:03:00 供电公司据此标记“绕行恢复”——但光缆仍断，基站仍不可用
        _ev("EV-SUB-RST-DETOUR-01", "POWER", T.format("08:03:00"), T.format("08:03:10"),
            "facility_restored", {"facility_id": "F-SUB", "restoration_kind": "detour"}),
        # 08:04:30 主用运营商切至应急微波 + 应急基站承接（08:05 才上报，乱序）
        _ev("EV-LINK-SWITCH-01", "TELCO-A", T.format("08:04:30"), T.format("08:05:20"),
            "activate_detour", {"primary": "F-FIBER-A", "backup": "F-FIBER-B"}),
        _ev("EV-BS-SWITCH-01", "TELCO-A", T.format("08:04:40"), T.format("08:05:30"),
            "activate_detour", {"primary": "F-BS-A", "backup": "F-BS-B"}),
        # 08:05:40 枢纽现场恢复通信（绕行），TELCO-B 确认
        _ev("EV-COMMS-RST-DETOUR-01", "TELCO-B", T.format("08:05:40"), T.format("08:05:50"),
            "facility_restored", {"facility_id": "F-BS-A", "restoration_kind": "detour"}),
        # 08:06:00 调度任务迁移上云（算力绕行）
        _ev("EV-CMP-MIGRATE-01", "COMPUTE", T.format("08:06:00"), T.format("08:06:20"),
            "activate_detour", {"primary": "F-COMPUTE", "backup": "F-COMPUTE-BACKUP"}),
        _ev("EV-CMP-RST-DETOUR-01", "COMPUTE", T.format("08:06:10"), T.format("08:06:30"),
            "facility_restored", {"facility_id": "F-COMPUTE", "restoration_kind": "detour"}),
        # 08:10 枢纽在自有看板上标记算力“真正恢复”，算力中心坚持仍失效 → 会商
        _ev("EV-CMP-RST-REAL-HUB-01", "HUB", T.format("08:10:00"), T.format("08:10:15"),
            "facility_restored",
            {"facility_id": "F-COMPUTE", "restoration_kind": "real", "basis": "看板变绿"}),
        # 会商中算力中心补充立场与证据
        _ev("EV-EVID-CMP-V1", "COMPUTE", T.format("08:12:00"), T.format("08:12:20"),
            "evidence_reported",
            {"facility_id": "F-COMPUTE", "version": 1,
             "summary": "本机心跳缺失，云端仅为迁移实例，非原节点恢复"}),
        _ev("EV-DISPUTE-POS-CMP", "COMPUTE", T.format("08:12:30"), T.format("08:12:40"),
            "dispute_position",
            {"dispute_id": "D/F-COMPUTE/20260930T081000Z", "position": "fault"}),
        # 08:14 指挥部裁决：仍失效，引用已确认证据 v1 → 风险不解除（但绕行有效）
        _ev("EV-EVID-CMP-V1-CONFIRM", "HQ", T.format("08:13:30"), T.format("08:13:50"),
            "evidence_confirmed", {"evidence_id": "EV-EVID-CMP-V1", "version": 1}),
        _ev("EV-DISPUTE-RESOLVE-01", "HQ", T.format("08:14:00"), T.format("08:14:10"),
            "dispute_resolved",
            {"dispute_id": "D/F-COMPUTE/20260930T081000Z",
             "resolution": "看板变绿来自云端迁移实例，原节点未恢复，按仍失效定论",
             "winning": "fault", "evidence_id": "EV-EVID-CMP-V1"}),
        # 08:30 供电公司一度误报发电机故障，08:31 自查发现是表计误报并纠正
        _ev("EV-GEN-FALSE-FAULT", "POWER", T.format("08:30:00"), T.format("08:30:20"),
            "facility_fault", {"facility_id": "F-GEN", "cause": "表计告警"}),
        _ev("EV-GEN-RETRACT", "POWER", T.format("08:31:00"), T.format("08:31:30"),
            "retract_event", {"target_event_id": "EV-GEN-FALSE-FAULT", "reason": "表计误报，发电机运行正常"}),
        # 08:38 市电真正恢复：证据 v1 上报，08:39 指挥部确认，08:40 关断
        _ev("EV-EVID-SUB-V1", "POWER", T.format("08:38:00"), T.format("08:38:20"),
            "evidence_reported",
            {"facility_id": "F-SUB", "version": 1,
             "summary": "外线复电，受电开关带电，核相正确"}),
        _ev("EV-EVID-SUB-V1-CONFIRM", "HQ", T.format("08:39:00"), T.format("08:39:10"),
            "evidence_confirmed", {"evidence_id": "EV-EVID-SUB-V1", "version": 1}),
        _ev("EV-SUB-RST-REAL-01", "POWER", T.format("08:40:00"), T.format("08:40:10"),
            "facility_restored", {"facility_id": "F-SUB", "restoration_kind": "real"}),
        _ev("EV-GEN-OFF-01", "POWER", T.format("08:42:00"), T.format("08:42:10"),
            "detour_reverted", {"primary": "F-SUB", "backup": "F-GEN"}),
        # 08:48 光缆真正修复并取证确认
        _ev("EV-EVID-FIBER-V1", "TELCO-A", T.format("08:47:00"), T.format("08:47:20"),
            "evidence_reported",
            {"facility_id": "F-FIBER-A", "version": 1,
             "summary": "倒杆复位，熔纤完成，OTDR 测试合格"}),
        _ev("EV-EVID-FIBER-V1-CONFIRM", "HQ", T.format("08:48:00"), T.format("08:48:10"),
            "evidence_confirmed", {"evidence_id": "EV-EVID-FIBER-V1", "version": 1}),
        _ev("EV-FIBER-RST-REAL-01", "TELCO-A", T.format("08:48:30"), T.format("08:48:40"),
            "facility_restored", {"facility_id": "F-FIBER-A", "restoration_kind": "real"}),
        # 08:50 基站集群 A 恢复真正服务，运营商链路/基站绕行归还
        _ev("EV-EVID-BS-V1", "TELCO-A", T.format("08:49:30"), T.format("08:49:40"),
            "evidence_reported",
            {"facility_id": "F-BS-A", "version": 1,
             "summary": "集群A全部小区载波在线，话务与广播回切成功"}),
        _ev("EV-EVID-BS-V1-CONFIRM", "HQ", T.format("08:50:00"), T.format("08:50:10"),
            "evidence_confirmed", {"evidence_id": "EV-EVID-BS-V1", "version": 1}),
        _ev("EV-BS-RST-REAL-01", "TELCO-A", T.format("08:50:20"), T.format("08:50:30"),
            "facility_restored", {"facility_id": "F-BS-A", "restoration_kind": "real"}),
        _ev("EV-LINK-SWITCH-OFF-01", "TELCO-A", T.format("08:51:00"), T.format("08:51:10"),
            "detour_reverted", {"primary": "F-FIBER-A", "backup": "F-FIBER-B"}),
        _ev("EV-BS-SWITCH-OFF-01", "TELCO-A", T.format("08:51:30"), T.format("08:51:40"),
            "detour_reverted", {"primary": "F-BS-A", "backup": "F-BS-B"}),
        # 08:55 原算力节点重新上线，任务回切，绕行归还
        _ev("EV-EVID-CMP-V2", "COMPUTE", T.format("08:54:00"), T.format("08:54:20"),
            "evidence_reported",
            {"facility_id": "F-COMPUTE", "version": 2,
             "summary": "原节点供电与网络恢复，心跳恢复，任务回切校验一致"}),
        _ev("EV-EVID-CMP-V2-CONFIRM", "HQ", T.format("08:54:40"), T.format("08:54:50"),
            "evidence_confirmed", {"evidence_id": "EV-EVID-CMP-V2", "version": 2}),
        _ev("EV-CMP-RST-REAL-01", "COMPUTE", T.format("08:55:00"), T.format("08:55:10"),
            "facility_restored", {"facility_id": "F-COMPUTE", "restoration_kind": "real"}),
        _ev("EV-CMP-MIGRATE-OFF-01", "COMPUTE", T.format("08:56:00"), T.format("08:56:10"),
            "detour_reverted", {"primary": "F-COMPUTE", "backup": "F-COMPUTE-BACKUP"}),
        # 决策与资源调拨记录
        _ev("EV-DEC-01", "HQ", T.format("08:05:00"), T.format("08:05:10"),
            "decision", {"summary": "批准双网切换与算力迁移，旅客广播优先恢复"}),
        _ev("EV-RES-01", "HUB", T.format("08:07:00"), T.format("08:07:20"),
            "resource_dispatch",
            {"resource": "应急广播车", "to_unit": "HUB", "from_unit": "HQ", "qty": 1}),
        # 09:30-09:40 演练中断（讲评暂停），随后重启继续
        _ev("EV-PAUSE-01", "HQ", T.format("09:30:00"), T.format("09:30:00"),
            "exercise_paused", {"reason": "阶段讲评"}),
        _ev("EV-RESUME-01", "HQ", T.format("09:40:00"), T.format("09:40:00"),
            "exercise_resumed", {}),
        # 复盘整改：RTO 超标发现触发 → 责任单位接受 → 凭确认证据验证关闭
        _ev("EV-ACTION-RTO-COMMS", "HQ", T.format("10:00:00"), T.format("10:05:00"),
            "action_registered",
            {"finding_id": "F/RTO/S-COMMS/20260930T080010Z",
             "title": "旅客通信恢复超 RTO：优化基站切换脚本，目标 ≤4 分钟",
             "owner_unit": "TELCO-A"}),
        _ev("EV-ACTION-RTO-COMMS-ACC", "TELCO-A", T.format("10:20:00"), T.format("10:22:00"),
            "action_accepted",
            {"action_id": "EV-ACTION-RTO-COMMS", "accepted_by": "TELCO-A"}),
        _ev("EV-ACTION-RTO-COMMS-VER", "HQ", T.format("11:30:00"), T.format("11:35:00"),
            "action_verified",
            {"action_id": "EV-ACTION-RTO-COMMS", "evidence_id": "EV-EVID-BS-V1"}),
    ]

    # 重复上报：同 event_id 同内容再来一次（平台必须幂等忽略）。
    items.append(dict(items[0]))
    return items
