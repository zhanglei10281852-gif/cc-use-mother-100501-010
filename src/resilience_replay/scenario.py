"""冻结场景：演练开始前锁定的设施依赖、可用替代、服务目标与单位职责。

场景一旦进入 FROZEN 状态即不可修改；如需修订，必须以新的 revision 发布，
历史演练记录永远引用其冻结时的场景版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .clock import parse_ts
from .jsonio import to_plain


class ScenarioState(str, Enum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class Facility:
    """设施/系统节点，如：变电站、核心交换机、算力集群、通信链路。"""

    facility_id: str
    name: str
    kind: str  # power | network | compute | service | logistics ...
    owner_unit: str
    criticality: str = "normal"  # critical | important | normal
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Dependency:
    """有向依赖：facility_id 依赖 on_facility_id 才能正常工作。"""

    facility_id: str
    on_facility_id: str
    kind: str = "hard"  # hard: 上游失效必传播；soft: 降级但不直接判定失效
    note: str = ""


@dataclass(frozen=True, slots=True)
class Alternative:
    """可用替代能力。

    启用后下游可继续运行（视为绕行/降级），但服务目标 capacity 受 capacity_ratio 限制；
    requires 中的设施必须同时可用该替代才成立。
    """

    alternative_id: str
    for_facility_id: str  # 被替代的主设施
    backup_facility_id: str  # 实际承载的替代设施
    switch_kind: str = "manual"  # auto | manual
    capacity_ratio: float = 1.0
    activation_minutes: int = 0  # 预计切换耗时（分钟），用于计划顺序对比
    requires: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class ServiceObjective:
    """关键服务目标：旅客与物资保障的判定口径。"""

    service_id: str
    name: str
    facility_ids: tuple[str, ...]  # 提供该服务的入口设施（任一可用即服务可用）
    target: str  # 服务目标描述，如“旅客疏导信息推送 P95 < 2 分钟”
    required_capacity_ratio: float = 1.0  # 维持目标所需的最低容量比例


@dataclass(frozen=True, slots=True)
class UnitResponsibility:
    """参与单位职责，事件上报与结论签收都以此为准。"""

    unit_id: str
    name: str
    contacts: tuple[str, ...] = ()
    scope: str = ""


@dataclass(frozen=True, slots=True)
class PlanStep:
    """冻结的计划恢复步骤，planned_offset_minutes 相对演练开始 T0。"""

    step_id: str
    target_facility_id: str
    action: str  # restore | activate_alternative
    alternative_id: str | None = None
    planned_offset_minutes: int = 0
    responsible_unit: str = ""
    restores_service_id: str | None = None  # 该步骤预计解除哪个服务目标风险
    note: str = ""


@dataclass(frozen=True, slots=True)
class Scenario:
    exercise_code: str
    revision: str
    coordinator_unit: str
    state: str  # ScenarioState 的值，以字符串存储保持冻结可序列化
    frozen_at: str | None
    facilities: tuple[Facility, ...]
    dependencies: tuple[Dependency, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    services: tuple[ServiceObjective, ...] = ()
    units: tuple[UnitResponsibility, ...] = ()
    plan: tuple[PlanStep, ...] = ()

    # -- 便捷查询 -----------------------------------------------------------

    def facility_map(self) -> dict[str, Facility]:
        return {f.facility_id: f for f in self.facilities}

    def dependents_of(self, upstream_id: str) -> list[str]:
        """直接依赖 upstream 的设施。"""
        return [d.facility_id for d in self.dependencies if d.on_facility_id == upstream_id]

    def upstream_of(self, facility_id: str) -> list[str]:
        return [d.on_facility_id for d in self.dependencies if d.facility_id == facility_id]

    def outgoing_map(self, *, hard_only: bool = False) -> dict[str, list[str]]:
        """upstream -> 下游列表。"""
        out: dict[str, list[str]] = {}
        for dep in self.dependencies:
            if hard_only and dep.kind != "hard":
                continue
            out.setdefault(dep.on_facility_id, []).append(dep.facility_id)
        return out

    def alternatives_for(self, facility_id: str) -> list[Alternative]:
        return [a for a in self.alternatives if a.for_facility_id == facility_id]

    def to_dict(self) -> dict[str, Any]:
        return to_plain(self)


def build_scenario(
    *,
    exercise_code: str,
    revision: str,
    coordinator_unit: str,
    facilities: list[Facility],
    dependencies: list[Dependency] | None = None,
    alternatives: list[Alternative] | None = None,
    services: list[ServiceObjective] | None = None,
    units: list[UnitResponsibility] | None = None,
    plan: list[PlanStep] | None = None,
) -> Scenario:
    """构造处于 DRAFT 的场景并做完整性校验。"""
    if not exercise_code.strip() or not revision.strip() or not coordinator_unit.strip():
        raise ValueError("exercise_code/revision/coordinator_unit 不能为空")
    scenario = Scenario(
        exercise_code=exercise_code,
        revision=revision,
        coordinator_unit=coordinator_unit,
        state=ScenarioState.DRAFT.value,
        frozen_at=None,
        facilities=tuple(facilities),
        dependencies=tuple(dependencies or ()),
        alternatives=tuple(alternatives or ()),
        services=tuple(services or ()),
        units=tuple(units or ()),
        plan=tuple(plan or ()),
    )
    validate_scenario(scenario)
    return scenario


def validate_scenario(scenario: Scenario) -> None:
    fids = {f.facility_id for f in scenario.facilities}
    if len(fids) != len(scenario.facilities):
        raise ValueError("设施标识重复")

    for dep in scenario.dependencies:
        if dep.facility_id not in fids:
            raise ValueError(f"依赖引用了未知设施: {dep.facility_id}")
        if dep.on_facility_id not in fids:
            raise ValueError(f"依赖引用了未知设施: {dep.on_facility_id}")
        if dep.facility_id == dep.on_facility_id:
            raise ValueError(f"设施不能依赖自身: {dep.facility_id}")
        if dep.kind not in ("hard", "soft"):
            raise ValueError(f"未知依赖类型: {dep.kind}")

    # 硬依赖不允许成环，否则“根因传播”没有良定义。
    _ensure_acyclic(scenario.outgoing_map(hard_only=True))

    for alt in scenario.alternatives:
        if alt.for_facility_id not in fids:
            raise ValueError(f"替代目标设施不存在: {alt.for_facility_id}")
        if alt.backup_facility_id not in fids:
            raise ValueError(f"替代承载设施不存在: {alt.backup_facility_id}")
        if not 0.0 < alt.capacity_ratio <= 1.0:
            raise ValueError(f"替代容量比例必须在 (0,1]: {alt.alternative_id}")
        for req in alt.requires:
            if req not in fids:
                raise ValueError(f"替代前置设施不存在: {req}")

    for svc in scenario.services:
        if not svc.facility_ids:
            raise ValueError(f"服务目标至少需要一个入口设施: {svc.service_id}")
        for fid in svc.facility_ids:
            if fid not in fids:
                raise ValueError(f"服务 {svc.service_id} 引用未知设施: {fid}")
        if not 0.0 < svc.required_capacity_ratio <= 1.0:
            raise ValueError(f"服务容量阈值必须在 (0,1]: {svc.service_id}")

    unit_ids = {u.unit_id for u in scenario.units}
    if len(unit_ids) != len(scenario.units):
        raise ValueError("单位标识重复")
    if scenario.coordinator_unit not in unit_ids:
        raise ValueError("指挥单位必须出现在单位职责清单中")
    for f in scenario.facilities:
        if f.owner_unit not in unit_ids:
            raise ValueError(f"设施 {f.facility_id} 的责任单位未登记: {f.owner_unit}")

    seen_steps: set[str] = set()
    alt_ids = {a.alternative_id for a in scenario.alternatives}
    svc_ids = {s.service_id for s in scenario.services}
    for step in scenario.plan:
        if step.step_id in seen_steps:
            raise ValueError(f"计划步骤标识重复: {step.step_id}")
        seen_steps.add(step.step_id)
        if step.target_facility_id not in fids:
            raise ValueError(f"计划步骤 {step.step_id} 引用未知设施: {step.target_facility_id}")
        if step.action not in ("restore", "activate_alternative"):
            raise ValueError(f"计划步骤 {step.step_id} 动作非法: {step.action}")
        if step.action == "activate_alternative":
            if step.alternative_id not in alt_ids:
                raise ValueError(f"计划步骤 {step.step_id} 引用未知替代: {step.alternative_id}")
        elif step.alternative_id is not None:
            raise ValueError(f"恢复步骤 {step.step_id} 不应携带替代标识")
        if step.responsible_unit and step.responsible_unit not in unit_ids:
            raise ValueError(f"计划步骤 {step.step_id} 责任单位未登记: {step.responsible_unit}")
        if step.restores_service_id is not None and step.restores_service_id not in svc_ids:
            raise ValueError(f"计划步骤 {step.step_id} 引用未知服务目标: {step.restores_service_id}")


def _ensure_acyclic(outgoing: dict[str, list[str]]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in set(outgoing) | {d for ds in outgoing.values() for d in ds}}
    stack: list[tuple[str, int]] = []
    for start in list(color):
        if color[start] != WHITE:
            continue
        stack.append((start, 0))
        while stack:
            node, idx = stack[-1]
            if idx == 0:
                color[node] = GRAY
            children = outgoing.get(node, [])
            if idx < len(children):
                nxt = children[idx]
                stack[-1] = (node, idx + 1)
                if color[nxt] == GRAY:
                    raise ValueError(f"硬依赖图存在环: {nxt}")
                if color[nxt] == WHITE:
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                stack.pop()


def freeze_scenario(scenario: Scenario, frozen_at: str) -> Scenario:
    """把场景冻结。冻结后任何修改都必须产生新 revision（由应用层保证另存）。"""
    if scenario.state == ScenarioState.FROZEN.value:
        raise ValueError("场景已冻结，不能重复冻结")
    if scenario.state == ScenarioState.CLOSED.value:
        raise ValueError("场景已关闭")
    parse_ts(frozen_at)  # 仅校验
    validate_scenario(scenario)
    from dataclasses import replace

    return replace(scenario, state=ScenarioState.FROZEN.value, frozen_at=frozen_at)


def scenario_from_dict(data: dict[str, Any]) -> Scenario:
    """从字典（如 JSON 导入）重建场景，保持不可变与校验。"""
    required = {
        "exercise_code",
        "revision",
        "coordinator_unit",
        "facilities",
    }
    missing = required - data.keys()
    if missing:
        raise ValueError(f"场景缺少字段: {sorted(missing)}")
    scenario = Scenario(
        exercise_code=data["exercise_code"],
        revision=data["revision"],
        coordinator_unit=data["coordinator_unit"],
        state=data.get("state", ScenarioState.DRAFT.value),
        frozen_at=data.get("frozen_at"),
        facilities=tuple(Facility(**f) for f in data["facilities"]),
        dependencies=tuple(Dependency(**d) for d in data.get("dependencies", ())),
        alternatives=tuple(
            Alternative(
                alternative_id=a["alternative_id"],
                for_facility_id=a["for_facility_id"],
                backup_facility_id=a["backup_facility_id"],
                switch_kind=a.get("switch_kind", "manual"),
                capacity_ratio=a.get("capacity_ratio", 1.0),
                activation_minutes=a.get("activation_minutes", 0),
                requires=tuple(a.get("requires", ())),
                note=a.get("note", ""),
            )
            for a in data.get("alternatives", ())
        ),
        services=tuple(
            ServiceObjective(
                service_id=s["service_id"],
                name=s["name"],
                facility_ids=tuple(s["facility_ids"]),
                target=s["target"],
                required_capacity_ratio=s.get("required_capacity_ratio", 1.0),
            )
            for s in data.get("services", ())
        ),
        units=tuple(
            UnitResponsibility(
                unit_id=u["unit_id"],
                name=u["name"],
                contacts=tuple(u.get("contacts", ())),
                scope=u.get("scope", ""),
            )
            for u in data.get("units", ())
        ),
        plan=tuple(
            PlanStep(
                step_id=p["step_id"],
                target_facility_id=p["target_facility_id"],
                action=p["action"],
                alternative_id=p.get("alternative_id"),
                planned_offset_minutes=p.get("planned_offset_minutes", 0),
                responsible_unit=p.get("responsible_unit", ""),
                restores_service_id=p.get("restores_service_id"),
                note=p.get("note", ""),
            )
            for p in data.get("plan", ())
        ),
    )
    validate_scenario(scenario)
    return scenario
