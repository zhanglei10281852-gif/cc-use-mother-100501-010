"""事先冻结的演练场景：设施依赖、可用替代、服务目标与单位职责。

场景一旦创建即不可变（frozen dataclass），并通过 :meth:`Scenario.fingerprint`
得到内容指纹；演练全程只能引用冻结快照，任何修改都必须生成新的修订版本。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any, Mapping

from .errors import ScenarioValidationError
from .time_model import parse_ts


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioValidationError(f"{label} 不能为空")
    return value.strip()


@dataclass(frozen=True, slots=True)
class Facility:
    """设施/网络节点（变电站、光缆、基站、算力节点、链路等）。"""

    facility_id: str
    name: str
    kind: str
    owner_unit: str
    critical: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.facility_id, "facility_id")
        _nonempty(self.name, "名称")
        _nonempty(self.kind, "设施类型")
        _nonempty(self.owner_unit, "归属单位")


@dataclass(frozen=True, slots=True)
class Dependency:
    """``upstream`` 为 ``downstream`` 提供运行支撑。

    ``required`` 为真表示硬依赖（上游失效则下游失效）；
    为假表示降效依赖，本平台只跟踪硬依赖的失效传播。
    """

    upstream: str
    downstream: str
    label: str = ""
    required: bool = True


@dataclass(frozen=True, slots=True)
class Alternative:
    """``backup`` 可以在应急期间替代 ``primary`` 支撑同一服务。

    绕行是临时性的：需要激活动作（activate_detour）并给出
    ``valid_from/valid_to`` 计划窗口，且必须显式归还（detour_reverted）
    或由对备份设施自身的故障事件覆盖。

    ``transparent`` 描述替代的物理性质：
    * 真（如柴油发电替代市电）：下游设备获得的供给与原设施等价，
      下游节点随之恢复；
    * 假（如网络切换、算力迁移）：只有显式切到备份路径的节点受益，
      主设施的既有下游不会被“自动治愈”。
    """

    primary: str
    backup: str
    label: str = ""
    valid_from: str = ""
    valid_to: str = ""
    transparent: bool = False

    def window(self) -> tuple[Any, Any]:
        if not self.valid_from or not self.valid_to:
            return (None, None)
        start = parse_ts(self.valid_from)
        end = parse_ts(self.valid_to)
        if end <= start:
            raise ScenarioValidationError(
                f"替代 {self.primary}->{self.backup} 的窗口结束必须晚于开始"
            )
        return (start, end)


@dataclass(frozen=True, slots=True)
class ServiceObjective:
    """关键服务的保障目标：恢复时间目标（RTO，秒）与责任单位。"""

    service_id: str
    name: str
    depends_on: tuple[str, ...]
    rto_seconds: int
    owner_unit: str

    def __post_init__(self) -> None:
        _nonempty(self.service_id, "service_id")
        _nonempty(self.name, "服务名称")
        if not self.depends_on:
            raise ScenarioValidationError(f"服务 {self.service_id} 至少依赖一个设施")
        if self.rto_seconds <= 0:
            raise ScenarioValidationError(f"服务 {self.service_id} 的 RTO 必须为正数")
        _nonempty(self.owner_unit, "责任单位")


@dataclass(frozen=True, slots=True)
class UnitRole:
    """参与单位在演练中的职责边界。"""

    unit_id: str
    name: str
    responsibilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.unit_id, "unit_id")
        _nonempty(self.name, "单位名称")


@dataclass(frozen=True, slots=True)
class Scenario:
    """一次演练冻结下来的完整场景快照。"""

    exercise_code: str
    scenario_revision: str
    coordinator: str
    frozen_at: str
    facilities: tuple[Facility, ...]
    dependencies: tuple[Dependency, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    services: tuple[ServiceObjective, ...] = ()
    units: tuple[UnitRole, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.exercise_code, "演练编号")
        _nonempty(self.scenario_revision, "场景修订号")
        _nonempty(self.coordinator, "联合指挥部")
        parse_ts(self.frozen_at)
        if not self.facilities:
            raise ScenarioValidationError("场景至少需要一个设施")
        self._validate()

    # ---- 校验 -----------------------------------------------------------

    def _validate(self) -> None:
        facility_ids = {item.facility_id for item in self.facilities}
        if len(facility_ids) != len(self.facilities):
            raise ScenarioValidationError("设施标识重复")

        for dep in self.dependencies:
            if dep.upstream not in facility_ids:
                raise ScenarioValidationError(f"依赖引用了不存在的设施: {dep.upstream}")
            if dep.downstream not in facility_ids:
                raise ScenarioValidationError(f"依赖引用了不存在的设施: {dep.downstream}")
            if dep.upstream == dep.downstream:
                raise ScenarioValidationError(f"设施不能依赖自身: {dep.upstream}")
        self._assert_acyclic()

        for alt in self.alternatives:
            if alt.primary not in facility_ids:
                raise ScenarioValidationError(f"替代主设施不存在: {alt.primary}")
            if alt.backup not in facility_ids:
                raise ScenarioValidationError(f"替代备设施不存在: {alt.backup}")
            alt.window()

        unit_ids = {unit.unit_id for unit in self.units}
        if len(unit_ids) != len(self.units):
            raise ScenarioValidationError("单位标识重复")
        if self.coordinator not in unit_ids:
            raise ScenarioValidationError(f"指挥部 {self.coordinator} 不在参与单位列表中")
        for facility in self.facilities:
            if facility.owner_unit not in unit_ids:
                raise ScenarioValidationError(
                    f"设施 {facility.facility_id} 的归属单位 {facility.owner_unit} 未登记职责"
                )

        service_ids: set[str] = set()
        for svc in self.services:
            if svc.service_id in service_ids:
                raise ScenarioValidationError(f"服务标识重复: {svc.service_id}")
            service_ids.add(svc.service_id)
            for ref in svc.depends_on:
                if ref not in facility_ids:
                    raise ScenarioValidationError(
                        f"服务 {svc.service_id} 依赖了不存在的设施: {ref}"
                    )
            if svc.owner_unit not in unit_ids:
                raise ScenarioValidationError(
                    f"服务 {svc.service_id} 的责任单位 {svc.owner_unit} 未登记职责"
                )

    def _assert_acyclic(self) -> None:
        graph: dict[str, list[str]] = {f.facility_id: [] for f in self.facilities}
        for dep in self.dependencies:
            if dep.required:
                graph[dep.upstream].append(dep.downstream)
        WHITE, GREY, BLACK = 0, 1, 2
        color = dict.fromkeys(graph, WHITE)

        def visit(node: str) -> None:
            color[node] = GREY
            for nxt in graph[node]:
                if color[nxt] == GREY:
                    raise ScenarioValidationError(f"设施依赖图存在环路（涉及 {nxt}）")
                if color[nxt] == WHITE:
                    visit(nxt)
            color[node] = BLACK

        for node in graph:
            if color[node] == WHITE:
                visit(node)

    # ---- 查询视图 -------------------------------------------------------

    def facility(self, facility_id: str) -> Facility:
        for item in self.facilities:
            if item.facility_id == facility_id:
                return item
        raise ScenarioValidationError(f"设施不存在: {facility_id}")

    def hard_edges(self) -> list[tuple[str, str]]:
        return [(d.upstream, d.downstream) for d in self.dependencies if d.required]

    def propagation_closure(self, roots: set[str]) -> set[str]:
        """沿硬依赖方向（上游→下游）求风险传播闭包。"""
        graph: dict[str, list[str]] = {f.facility_id: [] for f in self.facilities}
        for upstream, downstream in self.hard_edges():
            graph[upstream].append(downstream)
        impacted: set[str] = set()
        stack = list(roots)
        while stack:
            node = stack.pop()
            if node in impacted:
                continue
            impacted.add(node)
            stack.extend(graph[node])
        return impacted

    def backups_for(self, primary: str) -> list[Alternative]:
        return [alt for alt in self.alternatives if alt.primary == primary]

    def primary_backed_by(self, backup: str) -> list[Alternative]:
        return [alt for alt in self.alternatives if alt.backup == backup]

    # ---- 序列化与指纹 ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["facilities"] = [
            {k: v for k, v in item.items()} for item in data["facilities"]
        ]
        return data

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Scenario":
        def require(key: str) -> Any:
            if key not in data:
                raise ScenarioValidationError(f"场景缺少字段: {key}")
            return data[key]

        return cls(
            exercise_code=require("exercise_code"),
            scenario_revision=require("scenario_revision"),
            coordinator=require("coordinator"),
            frozen_at=require("frozen_at"),
            facilities=tuple(Facility(**item) for item in require("facilities")),
            dependencies=tuple(Dependency(**item) for item in data.get("dependencies", ())),
            alternatives=tuple(
                Alternative(**item) for item in data.get("alternatives", ())
            ),
            services=tuple(
                ServiceObjective(
                    service_id=item["service_id"],
                    name=item["name"],
                    depends_on=tuple(item["depends_on"]),
                    rto_seconds=item["rto_seconds"],
                    owner_unit=item["owner_unit"],
                )
                for item in require("services")
            ),
            units=tuple(
                UnitRole(
                    unit_id=item["unit_id"],
                    name=item["name"],
                    responsibilities=tuple(item.get("responsibilities", ())),
                )
                for item in require("units")
            ),
        )
