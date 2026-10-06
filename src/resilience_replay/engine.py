"""确定性重放引擎。

给定冻结场景与只追加事件日志，纯函数式地重放整场演练，输出：
- 设施风险传播状态与关键服务失效区间；
- 临时绕行与真正恢复的区分及各自生效时刻；
- 有期限会商及其超期/裁决状态；
- 证据版本的提交、佐证、驳回、取代、撤回链路；
- 发现 -> 整改（接受/验证关闭）的完整追踪；
- 计划恢复顺序与实际恢复顺序的偏差；
- 引用确认证据版本的最终结论。

实现采用两遍扫描：
  1) 事实状态扫描：按发生时间处理证据确认图、会商、发现、整改与阶段；
     纠正事件使误报/错误证据在最终状态中失效，原始记录仍保留在日志中。
  2) 时间区间扫描：只对“生效故障”和“已确认证据”产生的状态突变求传播快照，
     因此重复上报（日志层幂等）、乱序事件（按发生时间排序）、
     演练中断重启（重放同一日志）都不会改变结果。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from .clock import format_ts, now_utc, parse_ts
from .events import (
    CONSULTATION_OPEN,
    CONSULTATION_RESOLVE,
    CORRECTION,
    DECISION,
    EXERCISE_PHASE,
    FAULT,
    FINDING,
    RECOVERY_EVIDENCE,
    REMEDIATION,
    RESOURCE_ALLOCATION,
    EventLog,
    EventRecord,
)
from .jsonio import digest, short_digest
from .scenario import Scenario

DEFAULT_CONSULTATION_MINUTES = 30

FACILITY_HEALTHY = "HEALTHY"
FACILITY_FAILED = "FAILED"
FACILITY_BYPASSED = "BYPASSED"
FACILITY_DEGRADED = "DEGRADED"  # 硬链可用但软依赖受损

SERVICE_OK = "OK"
SERVICE_OUTAGE = "OUTAGE"

MODE_RESTORE = "restore"
MODE_BYPASS = "bypass"


class IngestValidationError(ValueError):
    """事件语义与冻结场景或既有事件矛盾。"""


@dataclass(slots=True)
class EvidenceView:
    record: EventRecord
    facility_id: str
    mode: str
    alternative_id: str | None
    status: str = "submitted"  # submitted | confirmed | rejected | superseded | retracted | orphan
    submitted_at: str = ""
    confirmed_at: str | None = None
    confirmed_by: tuple[str, ...] = ()
    superseded_by: str | None = None
    deactivated_at: str | None = None  # 绕行证据失效时刻（撤回或被新版本取代）
    retraction: EventRecord | None = None
    ineffective_reason: str | None = None


@dataclass(slots=True)
class Consultation:
    consultation_id: str
    opened_at: str
    deadline_at: str
    facility_id: str | None
    subject: str
    parties: tuple[str, ...]
    claim_event_ids: tuple[str, ...]
    status: str = "open"  # open | confirmed | rejected | inconclusive，可能追加 _overdue
    resolved_at: str | None = None
    resolution_evidence_id: str | None = None
    agreed_facts: str = ""
    auto: bool = False

    def is_overdue_at(self, ts: str) -> bool:
        return parse_ts(ts) > parse_ts(self.deadline_at)


@dataclass(slots=True)
class Interval:
    start_at: str
    end_at: str | None
    state: str
    reason: str = ""

    def duration_seconds(self) -> float | None:
        if self.end_at is None:
            return None
        return (parse_ts(self.end_at) - parse_ts(self.start_at)).total_seconds()


@dataclass(slots=True)
class RemediationView:
    remediation_id: str
    finding_id: str
    action: str
    owner_unit: str
    created_event: EventRecord
    created_at: str
    due_at: str | None = None
    status: str = "registered"  # registered | accepted | closed
    accepted_by: str | None = None
    accepted_at: str | None = None
    accepted_event: EventRecord | None = None
    verified_by: str | None = None
    verified_at: str | None = None
    closed_event: EventRecord | None = None


@dataclass(slots=True)
class FindingView:
    finding_id: str
    title: str
    severity: str
    description: str
    source: str  # explicit | auto
    triggering_event_ids: tuple[str, ...]
    created_at: str


@dataclass(slots=True)
class LiftEvent:
    """服务由失效转可用的关键时刻：区分绕行与真正恢复。"""

    service_id: str
    lifted_at: str
    mode: str  # recovery | workaround
    evidence_ids: tuple[str, ...]
    detail: str


# ===========================================================================
# 写入前语义校验（应用层调用；重放本身对异常数据保持宽容并产出告警）
# ===========================================================================


def validate_ingest(
    scenario: Scenario, log_records: list[EventRecord], event_type: str, payload: dict[str, Any], unit_id: str
) -> None:
    """在追加事件前做语义校验。场景必须已冻结。"""
    from .scenario import ScenarioState

    if scenario.state != ScenarioState.FROZEN.value:
        raise IngestValidationError("场景未冻结，不能接收演练事件")
    unit_ids = {u.unit_id for u in scenario.units}
    if unit_id not in unit_ids:
        raise IngestValidationError(f"上报单位未在职责清单中登记: {unit_id}")
    fmap = scenario.facility_map()
    retractions: dict[str, EventRecord] = {}
    for r in log_records:
        if r.event_type == CORRECTION:
            retractions[str(r.payload.get("target_event_id"))] = r

    def alive(rec: EventRecord) -> bool:
        return rec.event_id not in retractions

    def require_str(key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise IngestValidationError(f"{event_type} 事件缺少字段: {key}")
        return value

    if event_type == FAULT:
        fid = require_str("facility_id")
        if fid not in fmap:
            raise IngestValidationError(f"故障设施不存在: {fid}")
        # 同设施故障期间的重复/并发故障不在接入层拒绝：完全相同者由日志幂等拦截，
        # 其余由确定性重放合并去重（重复上报不改变结果）。
    elif event_type == DECISION:
        require_str("decision_id")
        kind = payload.get("kind", "other")
        if kind not in ("activate_alternative", "restore", "other"):
            raise IngestValidationError(f"未知决策类型: {kind}")
        if "facility_id" in payload and payload["facility_id"] not in fmap:
            raise IngestValidationError(f"决策引用未知设施: {payload['facility_id']}")
        if kind == "activate_alternative":
            alt_id = require_str("alternative_id")
            if not any(a.alternative_id == alt_id for a in scenario.alternatives):
                raise IngestValidationError(f"决策引用未知替代: {alt_id}")
    elif event_type == RESOURCE_ALLOCATION:
        require_str("allocation_id")
        require_str("resource")
    elif event_type == RECOVERY_EVIDENCE:
        fid = require_str("facility_id")
        if fid not in fmap:
            raise IngestValidationError(f"证据引用未知设施: {fid}")
        require_str("evidence_id")
        mode = require_str("mode")
        if mode not in (MODE_RESTORE, MODE_BYPASS):
            raise IngestValidationError(f"证据模式非法: {mode}")
        if mode == MODE_BYPASS:
            alt_id = require_str("alternative_id")
            alt = next((a for a in scenario.alternatives if a.alternative_id == alt_id), None)
            if alt is None or alt.for_facility_id != fid:
                raise IngestValidationError(f"替代 {alt_id} 不适用于设施 {fid}")
        # supersedes 必须引用既存证据；confirms/disputes 允许引用稍后才到达的事件
        # （乱序接收的常态），重放时统一解析，缺失只产生告警而非拒收。
        for key in ("supersedes",):
            ref = payload.get(key)
            if ref is not None:
                target = next((r for r in log_records if r.event_id == ref), None)
                if target is None or not alive(target):
                    raise IngestValidationError(f"{key} 引用的证据不存在或已撤回: {ref}")
                if target.event_type != RECOVERY_EVIDENCE:
                    raise IngestValidationError(f"{key} 必须引用恢复证据: {ref}")
        for key in ("confirms_evidence", "disputes_evidence"):
            ref = payload.get(key)
            if ref is not None and not isinstance(ref, str):
                raise IngestValidationError(f"{key} 必须是事件标识字符串")
    elif event_type == CORRECTION:
        target_id = require_str("target_event_id")
        target = next((r for r in log_records if r.event_id == target_id), None)
        if target is None:
            raise IngestValidationError(f"纠正目标不存在: {target_id}")
        if target.event_type == CORRECTION:
            raise IngestValidationError("不能对纠正事件再纠正")
        if target_id in retractions:
            raise IngestValidationError(f"事件已被纠正: {target_id}")
        require_str("reason")
    elif event_type == CONSULTATION_OPEN:
        require_str("consultation_id")
        require_str("deadline_at")
        parse_ts(payload["deadline_at"])
    elif event_type == CONSULTATION_RESOLVE:
        cid = require_str("consultation_id")
        opened = next(
            (
                r
                for r in log_records
                if r.event_type == CONSULTATION_OPEN
                and r.payload.get("consultation_id") == cid
                and alive(r)
            ),
            None,
        )
        # 显式会商必须已开启；自动会商（cons- 前缀）在重放时由分歧主张派生，
        # 接入时尚不存在记录，故这里放行、由重放校验裁决是否能对应到会商。
        if opened is None and not cid.startswith("cons-"):
            raise IngestValidationError(f"会商不存在或已撤回: {cid}")
        resolution = require_str("resolution")
        if resolution not in ("confirmed", "rejected", "inconclusive"):
            raise IngestValidationError(f"会商结论非法: {resolution}")
        if resolution == "confirmed":
            ev_ref = require_str("evidence_id")
            ev_rec = next((r for r in log_records if r.event_id == ev_ref), None)
            if ev_rec is not None and ev_rec.event_type != RECOVERY_EVIDENCE:
                raise IngestValidationError(f"裁决确认的不是恢复证据: {ev_ref}")
    elif event_type == FINDING:
        require_str("finding_id")
        require_str("title")
    elif event_type == REMEDIATION:
        rid = require_str("remediation_id")
        require_str("finding_id")
        require_str("action")
        status = payload.get("status", "registered")
        if status not in ("registered", "accepted", "closed"):
            raise IngestValidationError(f"整改状态非法: {status}")
        prior = [
            r
            for r in log_records
            if r.event_type == REMEDIATION and r.payload.get("remediation_id") == rid and alive(r)
        ]
        order = {"registered": 0, "accepted": 1, "closed": 2}
        if prior:
            last_status = max(
                (str(r.payload.get("status", "registered")) for r in prior),
                key=lambda s: order.get(s, 0),
            )
            if order[status] < order.get(last_status, 0):
                raise IngestValidationError(f"整改 {rid} 状态不能回退: {last_status} -> {status}")
        elif status != "registered":
            raise IngestValidationError(f"整改 {rid} 必须先登记")
        if status == "accepted":
            require_str("accepted_by")
        if status == "closed":
            require_str("verified_by")
    elif event_type == EXERCISE_PHASE:
        phase = require_str("phase")
        if phase not in ("started", "interrupted", "resumed", "ended"):
            raise IngestValidationError(f"演练阶段非法: {phase}")
        seq = [
            str(r.payload.get("phase"))
            for r in log_records
            if r.event_type == EXERCISE_PHASE and alive(r)
        ]
        allowed_next = {
            None: {"started"},
            "started": {"interrupted", "ended"},
            "interrupted": {"resumed"},
            "resumed": {"interrupted", "ended"},
            "ended": set(),
        }[seq[-1] if seq else None]
        if phase not in allowed_next:
            raise IngestValidationError(f"演练阶段不能从 {seq[-1] if seq else None} 转为 {phase}")
    else:  # pragma: no cover - 类型表已在事件层约束
        raise IngestValidationError(f"未知事件类型: {event_type}")


# ===========================================================================
# 传播快照
# ===========================================================================


@dataclass(slots=True)
class _Snapshot:
    """某一时刻由已确认状态推导出的全网可用性。"""

    direct_faults: set[str]
    bypasses: dict[str, list[EvidenceView]]
    ratio: dict[str, float]
    bypass_trace: dict[str, frozenset[str]]
    degraded_trace: dict[str, frozenset[str]]


def _build_snapshot(scenario: Scenario, direct_faults: set[str], bypasses: dict[str, list[EvidenceView]]) -> _Snapshot:
    snap = _Snapshot(
        direct_faults=set(direct_faults),
        bypasses={k: list(v) for k, v in bypasses.items()},
        ratio={},
        bypass_trace={},
        degraded_trace={},
    )
    hard_up: dict[str, list[str]] = {}
    soft_up: dict[str, list[str]] = {}
    for dep in scenario.dependencies:
        (hard_up if dep.kind == "hard" else soft_up).setdefault(dep.facility_id, []).append(dep.on_facility_id)
    alt_by_id = {a.alternative_id: a for a in scenario.alternatives}

    def compute(fid: str, visiting: frozenset[str]) -> tuple[float, frozenset[str], frozenset[str]]:
        if fid in snap.ratio:
            return snap.ratio[fid], snap.bypass_trace[fid], snap.degraded_trace[fid]
        if fid in visiting:
            return 0.0, frozenset(), frozenset()
        visiting = visiting | {fid}

        # 1) 主用路径：直接故障或硬依赖上游不可用/降级
        primary = 0.0 if fid in snap.direct_faults else 1.0
        trace: set[str] = set()
        for up in hard_up.get(fid, ()):
            r, t, _ = compute(up, visiting)
            primary = min(primary, r)
            trace |= t

        # 2) 仅当主路径不可用（<1）时评估本设施的替代能力；
        #    真正恢复后 primary=1，绕行不再计入，不影响“真恢复”判定。
        own = primary
        if primary < 1.0:
            active_bypass: list[tuple[float, set[str]]] = []
            for ev in snap.bypasses.get(fid, ()):
                alt = alt_by_id.get(ev.alternative_id or "")
                if alt is None:
                    continue
                ratios = []
                support_trace: set[str] = set()
                for sup in (alt.backup_facility_id, *alt.requires):
                    r, t, _ = compute(sup, visiting)
                    ratios.append(r)
                    support_trace |= t
                if ratios and min(ratios) > 0:
                    active_bypass.append((alt.capacity_ratio * min(ratios), support_trace))
            if active_bypass:
                best_ratio, support_trace = max(active_bypass, key=lambda x: x[0])
                if best_ratio >= primary:
                    own = best_ratio
                    trace = {fid} | support_trace

        soft_degraded: set[str] = set()
        for up in soft_up.get(fid, ()):
            r, t, d = compute(up, visiting)
            if r < 1.0:
                soft_degraded |= {up} | t | d

        snap.ratio[fid] = own
        snap.bypass_trace[fid] = frozenset(trace)
        snap.degraded_trace[fid] = frozenset(soft_degraded)
        return own, snap.bypass_trace[fid], snap.degraded_trace[fid]

    for f in scenario.facilities:
        compute(f.facility_id, frozenset())
    return snap


def _facility_state(snap: _Snapshot, scenario: Scenario, fid: str) -> tuple[str, float, str]:
    ratio = snap.ratio.get(fid, 1.0)
    if ratio <= 0:
        if fid in snap.direct_faults:
            return FACILITY_FAILED, ratio, "直接故障"
        ups = sorted(u for u in scenario.upstream_of(fid) if snap.ratio.get(u, 1.0) <= 0)
        return FACILITY_FAILED, ratio, f"上游失效传播: {','.join(ups) or '依赖链'}"
    if snap.bypass_trace.get(fid) or ratio < 1.0:
        return FACILITY_BYPASSED, ratio, f"绕行承载，容量比例 {ratio:.2f}"
    if snap.degraded_trace.get(fid):
        return FACILITY_DEGRADED, ratio, "软依赖受损降级"
    return FACILITY_HEALTHY, ratio, ""


# ===========================================================================
# 重放
# ===========================================================================


def _confirm_evidence(
    evidences: dict[str, EvidenceView], claim: EvidenceView, at: str, parties: tuple[str, ...]
) -> None:
    """确认一份证据；新版本确认时，才把它所取代的旧版本置为 superseded。"""
    claim.status = "confirmed"
    claim.confirmed_at = claim.confirmed_at or at
    claim.confirmed_by = tuple(sorted(set(claim.confirmed_by) | set(parties)))
    ref_old = claim.record.payload.get("supersedes")
    if isinstance(ref_old, str) and ref_old in evidences:
        old = evidences[ref_old]
        if old.facility_id == claim.facility_id and old.status in ("confirmed", "superseded"):
            old.status = "superseded"
            old.superseded_by = claim.record.event_id
            old.deactivated_at = at


def _later(a: str, b: str) -> str:
    return a if parse_ts(a) >= parse_ts(b) else b


def _resolve_evidence_graph(
    evidences: dict[str, EvidenceView],
    ordered: list[EventRecord],
    retractions: dict[str, EventRecord],
    warnings: list[str],
) -> tuple[dict[str, Consultation], dict[str, str]]:
    """整体解析证据间的佐证、异议与取代关系（乱序免疫）。

    返回 (自动会商映射, 证据确认生效时刻映射)。确认生效时刻取主张与佐证
    *上报时间* 的较晚者——必须在双方都上报后确认才成立。
    """
    consultations: dict[str, Consultation] = {}
    confirm_at: dict[str, str] = {}

    # 1) 佐证关系（双向标记为 confirmed，除非撤回/同单位/设施不一致）
    for rec in evidence_list(ordered):
        view = evidences[rec.event_id]
        if view.status == "retracted":
            continue
        ref = rec.payload.get("confirms_evidence")
        if not isinstance(ref, str):
            continue
        target = evidences.get(ref)
        if target is None or target.status == "retracted":
            warnings.append(f"佐证引用了不存在或已撤回的证据: {ref}")
            continue
        if target.facility_id != view.facility_id:
            warnings.append(f"佐证 {rec.event_id} 与原证据设施不一致，已忽略佐证关系")
            continue
        if rec.unit_id == target.record.unit_id:
            warnings.append(f"证据 {ref} 仅得到上报单位自身确认，仍需异单位佐证")
            continue
        parties = tuple(
            sorted(
                set(target.confirmed_by)
                | {target.record.unit_id, rec.unit_id}
            )
        )
        # 双方都上报后确认才成立 -> 取上报时间较晚者
        at = _later(rec.reported_at, target.record.reported_at)
        for v in (target, view):
            v.status = "confirmed"
            v.confirmed_by = tuple(sorted(set(v.confirmed_by) | set(parties)))
            prev = v.confirmed_at
            v.confirmed_at = at if prev is None else _later(prev, at)
        confirm_at[target.record.event_id] = at
        confirm_at[view.record.event_id] = at

    # 2) 异议关系 -> 自动会商（同一对主张只立一次案）
    pairs: set[frozenset[str]] = set()
    for rec in evidence_list(ordered):
        view = evidences[rec.event_id]
        if view.status == "retracted":
            continue
        ref = rec.payload.get("disputes_evidence")
        if not isinstance(ref, str):
            continue
        target = evidences.get(ref)
        if target is None or target.status == "retracted":
            warnings.append(f"异议引用了不存在或已撤回的证据: {ref}")
            continue
        pair = frozenset({rec.event_id, ref})
        if pair in pairs:
            continue
        pairs.add(pair)
        opener = rec if parse_ts(rec.reported_at) >= parse_ts(target.record.reported_at) else target.record
        cid = f"cons-{short_digest(sorted(pair))}"
        consultations[cid] = Consultation(
            consultation_id=cid,
            opened_at=opener.occurred_at,
            deadline_at=format_ts(parse_ts(opener.occurred_at) + timedelta(minutes=DEFAULT_CONSULTATION_MINUTES)),
            facility_id=view.facility_id,
            subject=f"对设施 {view.facility_id} 恢复事实的分歧",
            parties=tuple(sorted({rec.unit_id, target.record.unit_id})),
            claim_event_ids=tuple(sorted(pair)),
            auto=True,
        )

    # 3) 独立主张冲突（同设施、异单位、不同模式、无佐证/异议链接）-> 自动会商
    active = [
        v
        for v in evidences.values()
        if v.status != "retracted"
        and not v.record.payload.get("confirms_evidence")
        and not v.record.payload.get("disputes_evidence")
    ]
    for i, a in enumerate(active):
        for b in active[i + 1 :]:
            if (
                a.facility_id == b.facility_id
                and a.record.unit_id != b.record.unit_id
                and a.mode != b.mode
            ):
                pair = frozenset({a.record.event_id, b.record.event_id})
                if pair in pairs:
                    continue
                pairs.add(pair)
                opener = (
                    a.record
                    if parse_ts(a.record.reported_at) >= parse_ts(b.record.reported_at)
                    else b.record
                )
                cid = f"cons-{short_digest(sorted(pair))}"
                consultations[cid] = Consultation(
                    consultation_id=cid,
                    opened_at=opener.occurred_at,
                    deadline_at=format_ts(parse_ts(opener.occurred_at) + timedelta(minutes=DEFAULT_CONSULTATION_MINUTES)),
                    facility_id=a.facility_id,
                    subject=f"对设施 {a.facility_id} 恢复事实的分歧",
                    parties=tuple(sorted({a.record.unit_id, b.record.unit_id})),
                    claim_event_ids=tuple(sorted(pair)),
                    auto=True,
                )

    # 4) 取代关系（仅登记；旧版本在新版本经佐证或会商确认后停用）
    for rec in evidence_list(ordered):
        ref = rec.payload.get("supersedes")
        if isinstance(ref, str) and ref in evidences:
            old = evidences[ref]
            new = evidences[rec.event_id]
            if old.facility_id == new.facility_id:
                old.superseded_by = rec.event_id
                if new.status == "confirmed" and old.status == "confirmed":
                    old.status = "superseded"
                    old.deactivated_at = new.confirmed_at

    return consultations, confirm_at


def evidence_list(ordered: list[EventRecord]) -> list[EventRecord]:
    return [r for r in ordered if r.event_type == RECOVERY_EVIDENCE]


@dataclass(slots=True)
class ReplayResult:
    scenario: Scenario
    t0: str | None
    ended_at: str | None
    interrupted_segments: list[tuple[str, str]]
    evidences: dict[str, EvidenceView]
    consultations: dict[str, Consultation]
    findings: list[FindingView]
    remediations: dict[str, RemediationView]
    facility_intervals: dict[str, list[Interval]]
    service_intervals: dict[str, list[Interval]]
    lift_events: list[LiftEvent]
    plan_comparison: dict[str, Any]
    conclusion: dict[str, Any]
    warnings: list[str]
    timeline: list[dict[str, Any]]
    log_head: str

    def to_dict(self) -> dict[str, Any]:
        from .jsonio import to_plain

        return to_plain(
            {
                "exercise": {
                    "exercise_code": self.scenario.exercise_code,
                    "scenario_revision": self.scenario.revision,
                    "t0": self.t0,
                    "ended_at": self.ended_at,
                    "interrupted_segments": self.interrupted_segments,
                },
                "evidences": {
                    k: {
                        "event_id": v.record.event_id,
                        "facility_id": v.facility_id,
                        "mode": v.mode,
                        "alternative_id": v.alternative_id,
                        "status": v.status,
                        "submitted_at": v.submitted_at,
                        "confirmed_at": v.confirmed_at,
                        "confirmed_by": list(v.confirmed_by),
                        "superseded_by": v.superseded_by,
                        "deactivated_at": v.deactivated_at,
                        "ineffective_reason": v.ineffective_reason,
                        "retracted": v.retraction is not None,
                    }
                    for k, v in self.evidences.items()
                },
                "consultations": {
                    k: {
                        "consultation_id": v.consultation_id,
                        "opened_at": v.opened_at,
                        "deadline_at": v.deadline_at,
                        "facility_id": v.facility_id,
                        "subject": v.subject,
                        "parties": list(v.parties),
                        "claim_event_ids": list(v.claim_event_ids),
                        "status": v.status,
                        "resolved_at": v.resolved_at,
                        "resolution_evidence_id": v.resolution_evidence_id,
                        "agreed_facts": v.agreed_facts,
                        "auto": v.auto,
                    }
                    for k, v in self.consultations.items()
                },
                "findings": [
                    {
                        "finding_id": f.finding_id,
                        "title": f.title,
                        "severity": f.severity,
                        "description": f.description,
                        "source": f.source,
                        "triggering_event_ids": list(f.triggering_event_ids),
                        "created_at": f.created_at,
                    }
                    for f in self.findings
                ],
                "remediations": {
                    k: {
                        "remediation_id": v.remediation_id,
                        "finding_id": v.finding_id,
                        "action": v.action,
                        "owner_unit": v.owner_unit,
                        "status": v.status,
                        "created_at": v.created_at,
                        "created_event": v.created_event.event_id,
                        "due_at": v.due_at,
                        "accepted_by": v.accepted_by,
                        "accepted_at": v.accepted_at,
                        "accepted_event": v.accepted_event.event_id if v.accepted_event else None,
                        "verified_by": v.verified_by,
                        "verified_at": v.verified_at,
                        "closed_event": v.closed_event.event_id if v.closed_event else None,
                    }
                    for k, v in self.remediations.items()
                },
                "facility_intervals": {k: [asdict(i) for i in v] for k, v in self.facility_intervals.items()},
                "service_intervals": {k: [asdict(i) for i in v] for k, v in self.service_intervals.items()},
                "lift_events": [asdict(e) for e in self.lift_events],
                "plan_comparison": self.plan_comparison,
                "conclusion": self.conclusion,
                "warnings": self.warnings,
                "timeline": self.timeline,
                "log_head": self.log_head,
            }
        )


def replay(scenario: Scenario, log: EventLog, *, as_of: str | None = None) -> ReplayResult:
    """对日志做确定性重放。as_of 可截断到某发生时刻（用于逐步重放）。"""
    # 注意：必须包含已纠正记录——CORRECTION 需要引用其目标；
    # 被纠正的普通事件在各处理分支中跳过，原始记录仍出现在时间线供审计。
    all_ordered = log.ordered_by_occurrence(include_retracted=True)
    if as_of is not None:
        cutoff = parse_ts(as_of)
        all_ordered = [r for r in all_ordered if parse_ts(r.occurred_at) <= cutoff]
    ordered = all_ordered
    # 窗口内的纠正映射（重放截断时，窗口外纠正不生效）
    retractions = {
        str(r.payload.get("target_event_id")): r
        for r in ordered
        if r.event_type == CORRECTION
    }

    warnings: list[str] = []
    t0: str | None = None
    ended_at: str | None = None
    interrupted_segments: list[tuple[str, str]] = []
    pending_interrupt: str | None = None

    evidences: dict[str, EvidenceView] = {}
    consultations: dict[str, Consultation] = {}
    findings: dict[str, FindingView] = {}
    remediations: dict[str, RemediationView] = {}
    timeline: list[dict[str, Any]] = []
    fault_events: dict[str, EventRecord] = {}  # facility -> 当前未闭合的有效故障事件

    # ---- 第一遍：阶段、证据图、会商、发现、整改 ----------------------------
    # 证据图先整体解析：佐证/异议/取代关系不依赖事件到达顺序，
    # 因而乱序（佐证晚于主张接入）也能得到一致结果。
    evidence_order = [r for r in ordered if r.event_type == RECOVERY_EVIDENCE]
    for r in evidence_order:
        p = r.payload
        view = EvidenceView(
            record=r,
            facility_id=str(p["facility_id"]),
            mode=str(p["mode"]),
            alternative_id=p.get("alternative_id"),
            submitted_at=r.occurred_at,
        )
        if r.event_id in retractions:
            view.status = "retracted"
            view.retraction = retractions[r.event_id]
            view.deactivated_at = retractions[r.event_id].occurred_at
        evidences[r.event_id] = view

    auto_consultations, confirm_effect_at = _resolve_evidence_graph(
        evidences, ordered, retractions, warnings
    )
    consultations.update(auto_consultations)

    for rec in ordered:
        p = rec.payload
        etype = rec.event_type
        effect = ""

        if etype == EXERCISE_PHASE:
            phase = str(p.get("phase"))
            if phase == "started":
                if t0 is None:
                    t0 = rec.occurred_at
                    effect = f"演练开始 T0={t0}"
                else:
                    warnings.append(f"重复的 started 事件已忽略: {rec.event_id}")
            elif phase == "interrupted":
                pending_interrupt = rec.occurred_at
                effect = "演练中断（重放不受影响）"
            elif phase == "resumed":
                if pending_interrupt:
                    interrupted_segments.append((pending_interrupt, rec.occurred_at))
                    pending_interrupt = None
                effect = "演练恢复"
            elif phase == "ended":
                if pending_interrupt:
                    interrupted_segments.append((pending_interrupt, rec.occurred_at))
                    pending_interrupt = None
                ended_at = rec.occurred_at
                effect = "演练结束"

        elif etype == FAULT:
            fid = str(p.get("facility_id"))
            if fid not in scenario.facility_map():
                warnings.append(f"故障事件引用未知设施，已忽略: {fid}")
            elif rec.event_id in retractions:
                effect = f"误报故障（已纠正，不计入风险）: {fid}"
            elif fid in fault_events:
                warnings.append(f"设施 {fid} 已在故障中，重复故障事件无效: {rec.event_id}")
            else:
                fault_events[fid] = rec
                effect = f"设施故障: {fid}"

        elif etype == DECISION:
            effect = f"决策: {p.get('decision_id')} ({p.get('kind')})"

        elif etype == RESOURCE_ALLOCATION:
            effect = f"资源调拨: {p.get('allocation_id')} -> {p.get('facility_id', p.get('target', '?'))}"

        elif etype == RECOVERY_EVIDENCE:
            view = evidences[rec.event_id]
            if view.status == "retracted":
                effect = f"证据撤回（已纠正）: {p.get('evidence_id')}"
            elif p.get("confirms_evidence"):
                effect = (
                    f"异单位佐证确认证据 {p.get('confirms_evidence')}，"
                    f"确认生效时刻 {confirm_effect_at.get(rec.event_id, rec.occurred_at)}"
                )
            elif p.get("disputes_evidence"):
                effect = f"对恢复证据 {p.get('disputes_evidence')} 提出异议，进入会商"
            elif view.status == "confirmed":
                at = confirm_effect_at.get(rec.event_id)
                if at:
                    tail = f"，后于 {at} 经异单位佐证确认" if at != rec.occurred_at else "，经异单位佐证确认"
                else:
                    tail = "，后经会商裁决确认"
                effect = f"恢复主张提交: {p.get('evidence_id')}（{view.mode}{tail}）"
            else:
                effect = f"恢复主张提交: {p.get('evidence_id')}（{view.mode}，待异单位确认）"

        elif etype == CORRECTION:
            target_id = str(p.get("target_event_id"))
            target = next((r for r in ordered if r.event_id == target_id), None)
            if target is None:
                warnings.append(f"纠正事件引用了重放范围外的事件 {target_id}")
                effect = f"纠正事件 {target_id}（范围外）"
            elif target.event_type == RECOVERY_EVIDENCE and target_id in evidences:
                ev = evidences[target_id]
                ev.status = "retracted"
                ev.retraction = rec
                ev.deactivated_at = rec.occurred_at
                effect = f"恢复证据撤回: {target_id}（{p.get('reason', '')}）"
            elif target.event_type == FAULT:
                fid = str(target.payload.get("facility_id"))
                if fault_events.get(fid) is not None and fault_events[fid].event_id == target_id:
                    del fault_events[fid]
                effect = f"纠正误报: 设施 {fid} 故障撤回"
            else:
                effect = f"纠正事件 {target_id}: {p.get('reason', '')}"

        elif etype == CONSULTATION_OPEN:
            cid = str(p["consultation_id"])
            claims = frozenset(p.get("claim_event_ids", ()))
            # 人工开启的会商若覆盖了同一对分歧主张，则并入正式会商，不重复计时
            twin = next(
                (
                    c
                    for c in consultations.values()
                    if c.status == "open" and claims and frozenset(c.claim_event_ids) == claims
                ),
                None,
            )
            if twin is not None:
                old_key = twin.consultation_id
                twin.consultation_id = cid
                twin.auto = False
                if p.get("subject"):
                    twin.subject = str(p["subject"])
                consultations.pop(old_key, None)
                consultations[cid] = twin
                effect = f"正式会商 {cid} 已建立（合并自动立案，期限 {twin.deadline_at}）"
            elif cid in consultations:
                warnings.append(f"会商 {cid} 已存在，重复开启忽略")
            else:
                consultations[cid] = Consultation(
                    consultation_id=cid,
                    opened_at=rec.occurred_at,
                    deadline_at=format_ts(parse_ts(str(p["deadline_at"]))),
                    facility_id=p.get("facility_id"),
                    subject=str(p.get("subject", "")),
                    parties=tuple(sorted(p.get("parties", [rec.unit_id]))),
                    claim_event_ids=tuple(p.get("claim_event_ids", [])),
                    auto=False,
                )
                effect = f"会商开启 {cid}（期限至 {consultations[cid].deadline_at}）"

        elif etype == CONSULTATION_RESOLVE:
            cid = str(p["consultation_id"])
            cons = consultations.get(cid)
            resolution = str(p.get("resolution"))
            if cons is None:
                warnings.append(f"裁决引用了不存在的会商 {cid}，已忽略")
            elif cons.status != "open":
                warnings.append(f"会商 {cid} 已裁决，重复裁决忽略")
            else:
                base = "confirmed" if resolution == "confirmed" else resolution
                if cons.is_overdue_at(rec.occurred_at):
                    base = f"{base}_overdue"
                cons.status = base
                cons.resolved_at = rec.occurred_at
                cons.agreed_facts = str(p.get("agreed_facts", ""))
                ev_id = p.get("evidence_id")
                if resolution == "confirmed" and isinstance(ev_id, str) and ev_id in evidences:
                    _confirm_evidence(
                        evidences,
                        evidences[ev_id],
                        rec.occurred_at,
                        tuple(sorted(set(cons.parties) | {rec.unit_id})),
                    )
                    cons.resolution_evidence_id = ev_id
                elif resolution == "rejected":
                    for claim_id in cons.claim_event_ids:
                        claim = evidences.get(claim_id)
                        if claim is not None and claim.status == "submitted":
                            claim.status = "rejected"
                effect = f"会商 {cid} 裁决: {cons.status}"

        elif etype == FINDING:
            fid_ = str(p["finding_id"])
            findings[fid_] = FindingView(
                finding_id=fid_,
                title=str(p.get("title")),
                severity=str(p.get("severity", "major")),
                description=str(p.get("description", "")),
                source="explicit",
                triggering_event_ids=tuple(p.get("triggering_event_ids", [])),
                created_at=rec.occurred_at,
            )
            effect = f"登记发现 {fid_}"

        elif etype == REMEDIATION:
            rid = str(p["remediation_id"])
            status = str(p.get("status", "registered"))
            view = remediations.get(rid)
            if view is None:
                view = RemediationView(
                    remediation_id=rid,
                    finding_id=str(p["finding_id"]),
                    action=str(p["action"]),
                    owner_unit=str(p.get("owner_unit", rec.unit_id)),
                    created_event=rec,
                    created_at=rec.occurred_at,
                    due_at=p.get("due_at"),
                )
                remediations[rid] = view
                effect = f"整改登记 {rid}（发现 {view.finding_id} -> {view.owner_unit}）"
            if status == "accepted" and view.status == "registered":
                view.status = "accepted"
                view.accepted_by = str(p.get("accepted_by"))
                view.accepted_at = rec.occurred_at
                view.accepted_event = rec
                effect = f"整改 {rid} 由 {view.accepted_by} 接受"
            elif status == "closed":
                if view.status == "registered":
                    warnings.append(f"整改 {rid} 未经接受直接关闭，标记异常")
                view.status = "closed"
                view.verified_by = str(p.get("verified_by"))
                view.verified_at = rec.occurred_at
                view.closed_event = rec
                effect = f"整改 {rid} 验证关闭（验证人 {view.verified_by}）"

        timeline.append(
            {
                "at": rec.occurred_at,
                "reported_at": rec.reported_at,
                "event_id": rec.event_id,
                "type": rec.event_type,
                "unit_id": rec.unit_id,
                "effect": effect,
                "payload": rec.payload,
            }
        )

    # 截止时刻
    final_ts = ended_at or (ordered[-1].occurred_at if ordered else None)
    for cons in consultations.values():
        if cons.status == "open" and final_ts and cons.is_overdue_at(final_ts):
            cons.status = "overdue"

    # ---- 第二遍：由“生效突变”求传播与失效区间 ------------------------------
    # 动作只有：故障打开、真正恢复、已确认绕行上线/下线（撤回或被取代）。
    # 绕行是否有效，取决于确认时刻该设施的*主路径*是否不可用
    # （直接故障或上游传播皆可）；真正恢复后主路径回到 1，绕行自动不再承载。
    raw_actions: list[tuple[str, int, str, Any]] = []
    for r in ordered:
        if r.event_type == FAULT and r.event_id not in retractions:
            fid = str(r.payload.get("facility_id", ""))
            if fid in scenario.facility_map():
                raw_actions.append((r.occurred_at, r.sequence, "fault_on", r))
    for ev in evidences.values():
        if ev.status not in ("confirmed", "superseded") or ev.confirmed_at is None:
            continue
        # 纯佐证（confirms_evidence）确认的是他人主张，本身不再产生一次独立动作
        if ev.record.payload.get("confirms_evidence"):
            continue
        kind = "restore" if ev.mode == MODE_RESTORE else "bypass_on"
        raw_actions.append((ev.confirmed_at, ev.record.sequence, kind, ev))
        if ev.mode == MODE_BYPASS and ev.deactivated_at:
            raw_actions.append((ev.deactivated_at, ev.record.sequence + 1_000_000, "bypass_off", ev))

    # 同刻分组：先以分组前快照判定孤儿，再统一应用，最后只 sweep 一次。
    groups: dict[str, list[tuple[int, str, Any]]] = {}
    for ts, seq, kind, obj in raw_actions:
        groups.setdefault(ts, []).append((seq, kind, obj))

    direct_faults: set[str] = set()
    bypasses: dict[str, list[EvidenceView]] = {}
    fac_prev: dict[str, tuple[str, str]] = {f.facility_id: (FACILITY_HEALTHY, "") for f in scenario.facilities}
    fac_open: dict[str, Interval] = {}
    fac_intervals: dict[str, list[Interval]] = {f.facility_id: [] for f in scenario.facilities}
    svc_prev: dict[str, str] = {s.service_id: SERVICE_OK for s in scenario.services}
    svc_mode: dict[str, str] = {s.service_id: "recovery" for s in scenario.services}
    svc_open: dict[str, Interval] = {}
    svc_intervals: dict[str, list[Interval]] = {s.service_id: [] for s in scenario.services}
    lift_events: list[LiftEvent] = []
    svc_fault_since: dict[str, str] = {}

    def sweep(ts: str) -> None:
        snap = _build_snapshot(scenario, direct_faults, bypasses)
        for fid in fac_intervals:
            state, _ratio, reason = _facility_state(snap, scenario, fid)
            prev_state, _prev_reason = fac_prev[fid]
            if state != prev_state:
                if prev_state != FACILITY_HEALTHY and fid in fac_open:
                    iv = fac_open.pop(fid)
                    iv.end_at = ts
                    fac_intervals[fid].append(iv)
                if state != FACILITY_HEALTHY:
                    fac_open[fid] = Interval(start_at=ts, end_at=None, state=state, reason=reason)
                fac_prev[fid] = (state, reason)
        for svc in scenario.services:
            ratio = max((snap.ratio.get(fid, 1.0) for fid in svc.facility_ids), default=0.0)
            state = SERVICE_OK if ratio >= svc.required_capacity_ratio else SERVICE_OUTAGE
            supporting = set()
            for fid in svc.facility_ids:
                if snap.ratio.get(fid, 1.0) >= svc.required_capacity_ratio:
                    supporting |= snap.bypass_trace.get(fid, frozenset())
            mode = "workaround" if state == SERVICE_OK and supporting else "recovery"
            prev_state = svc_prev[svc.service_id]
            prev_mode = svc_mode[svc.service_id]

            if state == SERVICE_OUTAGE and prev_state == SERVICE_OK:
                svc_open[svc.service_id] = Interval(start_at=ts, end_at=None, state=SERVICE_OUTAGE, reason=svc.name)
                svc_fault_since[svc.service_id] = ts
                svc_prev[svc.service_id] = state
            elif state == SERVICE_OK and prev_state == SERVICE_OUTAGE:
                if svc.service_id in svc_open:
                    iv = svc_open.pop(svc.service_id)
                    iv.end_at = ts
                    svc_intervals[svc.service_id].append(iv)
                since = svc_fault_since.pop(svc.service_id, None)
                lift_events.append(_make_lift(scenario, snap, svc, ts, mode, since, evidences))
                svc_prev[svc.service_id] = state
                svc_mode[svc.service_id] = mode
            elif state == SERVICE_OK and prev_mode == "workaround" and mode == "recovery":
                # 状态仍达标，但承载方式由绕行转为真正恢复——风险性质发生变化，需单独记录
                lift_events.append(_make_lift(scenario, snap, svc, ts, mode, None, evidences))
                svc_mode[svc.service_id] = mode

    for ts in sorted(groups, key=lambda t: parse_ts(t)):
        items = sorted(groups[ts], key=lambda x: x[0])
        pre_snap = _build_snapshot(scenario, direct_faults, bypasses)
        for _seq, kind, obj in items:
            if kind == "fault_on":
                fid = str(obj.payload["facility_id"])
                if fid in direct_faults:
                    continue  # 故障期间重复故障，不产生突变
                direct_faults.add(fid)
            elif kind == "restore":
                if obj.facility_id in direct_faults:
                    direct_faults.discard(obj.facility_id)
                    bypasses.pop(obj.facility_id, None)
                elif pre_snap.ratio.get(obj.facility_id, 1.0) >= 1.0:
                    obj.status = "orphan"
                    obj.ineffective_reason = "确认时设施主路径本就可用（故障可能为误报或已恢复）"
            elif kind == "bypass_on":
                if pre_snap.ratio.get(obj.facility_id, 1.0) < 1.0:
                    bypasses.setdefault(obj.facility_id, [])
                    if obj not in bypasses[obj.facility_id]:
                        bypasses[obj.facility_id].append(obj)
                else:
                    obj.status = "orphan"
                    obj.ineffective_reason = "确认时主路径可用，绕行无承载对象"
            else:  # bypass_off
                lst = bypasses.get(obj.facility_id)
                if lst and obj in lst:
                    lst.remove(obj)
        sweep(ts)

    if final_ts:
        snap = _build_snapshot(scenario, direct_faults, bypasses)
        for fid, iv in list(fac_open.items()):
            iv.end_at = final_ts
            fac_intervals[fid].append(iv)
        for sid, iv in list(svc_open.items()):
            iv.end_at = final_ts
            svc_intervals[sid].append(iv)
    else:
        snap = _build_snapshot(scenario, direct_faults, bypasses)

    # ---- 派生发现 ----------------------------------------------------------
    auto_findings: list[FindingView] = []
    for cons in consultations.values():
        if cons.status in ("overdue", "inconclusive_overdue", "rejected_overdue", "confirmed_overdue"):
            auto_findings.append(
                FindingView(
                    finding_id=f"find-auto-cons-{cons.consultation_id}",
                    title=f"会商超期: {cons.subject}",
                    severity="critical" if cons.status == "overdue" else "major",
                    description=f"会商 {cons.consultation_id} 期限 {cons.deadline_at} 前未形成有效裁决",
                    source="auto",
                    triggering_event_ids=cons.claim_event_ids,
                    created_at=final_ts or "",
                )
            )
    # 已被会商（无论裁决结果）覆盖的主张不再另派发现，避免重复计数
    consulted_claims = {cid_ for c in consultations.values() for cid_ in c.claim_event_ids}
    for ev in evidences.values():
        # 恢复声明最终仍停留在“待确认/被驳回”：不能作为风险解除依据，必须形成发现
        if ev.status in ("submitted", "rejected") and ev.record.event_id not in consulted_claims:
            still_down = snap.ratio.get(ev.facility_id, 1.0) < 1.0
            auto_findings.append(
                FindingView(
                    finding_id=f"find-auto-unconfirmed-{ev.record.event_id}",
                    title=(
                        f"恢复声明未获确认，设施 {ev.facility_id} 风险未解除"
                        if still_down
                        else f"恢复声明未获采信: 设施 {ev.facility_id}（{ev.status}）"
                    ),
                    severity="critical" if still_down else "major",
                    description=(
                        "仅有上报单位自身标记恢复，缺少异单位佐证或会商裁决，不能据此判定风险解除"
                        if ev.status == "submitted"
                        else "该恢复主张在会商中被驳回，不得作为结论证据"
                    ),
                    source="auto",
                    triggering_event_ids=(ev.record.event_id,),
                    created_at=final_ts or "",
                )
            )
    all_findings = list(findings.values()) + auto_findings

    finding_ids = {f.finding_id for f in all_findings}
    for v in remediations.values():
        if v.finding_id not in finding_ids:
            warnings.append(f"整改 {v.remediation_id} 引用了未知发现 {v.finding_id}")

    # ---- 计划 vs 实际 ------------------------------------------------------
    plan_comparison = _compare_plan(scenario, t0, fac_intervals, lift_events)

    # ---- 最终结论 ----------------------------------------------------------
    confirmed_evidence_ids = tuple(
        sorted(ev.record.event_id for ev in evidences.values() if ev.status in ("confirmed", "superseded"))
    )
    # 残余风险：截止时仍未达到服务目标容量（区间末尾是否收口并不代表风险是否还在）
    unresolved_services = [
        svc.service_id
        for svc in scenario.services
        if max((snap.ratio.get(fid, 1.0) for fid in svc.facility_ids), default=0.0)
        < svc.required_capacity_ratio
    ]
    workaround_services = [
        svc.service_id
        for svc in scenario.services
        if svc.service_id not in unresolved_services
        and any(
            snap.bypass_trace.get(fid)
            for fid in svc.facility_ids
            if snap.ratio.get(fid, 1.0) >= svc.required_capacity_ratio
        )
    ]
    conclusion = {
        "title": "跨网络韧性演练复盘结论",
        "exercise_code": scenario.exercise_code,
        "scenario_revision": scenario.revision,
        "scenario_fingerprint": digest(scenario.to_dict()),
        "generated_at": format_ts(now_utc()),
        "evidence_basis": {
            "log_head": log.head_hash,
            "confirmed_evidence_versions": list(confirmed_evidence_ids),
        },
        "key_service_outcomes": [
            {
                "service_id": sid,
                "outage_intervals": [
                    {"start": i.start_at, "end": i.end_at, "seconds": i.duration_seconds()}
                    for i in ivs
                    if i.state == SERVICE_OUTAGE
                ],
                "lifts": [asdict(e) for e in lift_events if e.service_id == sid],
                "residual_risk": sid in unresolved_services,
            }
            for sid, ivs in svc_intervals.items()
        ],
        "consultation_summary": {
            cid: {
                "status": c.status,
                "resolved_at": c.resolved_at,
                "evidence_id": c.resolution_evidence_id,
            }
            for cid, c in consultations.items()
        },
        "remediation_summary": {
            "registered": sum(1 for v in remediations.values() if v.status == "registered"),
            "accepted_open": sum(1 for v in remediations.values() if v.status == "accepted"),
            "verified_closed": sum(1 for v in remediations.values() if v.status == "closed"),
        },
        "finding_count": len(all_findings),
        "final": not unresolved_services and ended_at is not None,
    }

    return ReplayResult(
        scenario=scenario,
        t0=t0,
        ended_at=ended_at,
        interrupted_segments=interrupted_segments,
        evidences=evidences,
        consultations=consultations,
        findings=all_findings,
        remediations=remediations,
        facility_intervals=fac_intervals,
        service_intervals=svc_intervals,
        lift_events=lift_events,
        plan_comparison=plan_comparison,
        conclusion=conclusion,
        warnings=warnings,
        timeline=timeline,
        log_head=log.head_hash,
    )


def _make_lift(
    scenario: Scenario,
    snap: _Snapshot,
    svc: Any,
    ts: str,
    mode: str,
    since: str | None,
    evidences: dict[str, EvidenceView],
) -> LiftEvent:
    supporting = set()
    for fid in svc.facility_ids:
        if snap.ratio.get(fid, 1.0) >= svc.required_capacity_ratio:
            supporting |= snap.bypass_trace.get(fid, frozenset())
    ev_ids = tuple(
        sorted(
            e.record.event_id
            for e in evidences.values()
            if e.status in ("confirmed", "superseded")
            and e.confirmed_at
            and (since is None or since <= e.confirmed_at <= ts)
            and (
                (mode == "workaround" and e.facility_id in supporting)
                or (mode == "recovery" and e.mode == MODE_RESTORE and _caused_lift(scenario, snap, svc.facility_ids, e))
            )
        )
    )
    return LiftEvent(
        service_id=svc.service_id,
        lifted_at=ts,
        mode=mode,
        evidence_ids=ev_ids,
        detail="经替代链路绕行，容量受限，尚未真正恢复"
        if mode == "workaround"
        else "主用设施恢复，风险真正解除",
    )


def _caused_lift(scenario: Scenario, snap: _Snapshot, entry_ids: tuple[str, ...], ev: EvidenceView) -> bool:
    """恢复证据所恢复的设施是否在服务入口的硬依赖闭包内。"""
    seen: set[str] = set()
    stack = list(entry_ids)
    up = {dep.facility_id: dep.on_facility_id for dep in scenario.dependencies if dep.kind == "hard"}
    # 一个上游可能被多个下游引用，需要完整遍历
    up_map: dict[str, list[str]] = {}
    for dep in scenario.dependencies:
        if dep.kind == "hard":
            up_map.setdefault(dep.facility_id, []).append(dep.on_facility_id)
    del up
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(up_map.get(cur, ()))
    return ev.facility_id in seen


def _compare_plan(
    scenario: Scenario,
    t0: str | None,
    fac_intervals: dict[str, list[Interval]],
    lift_events: list[LiftEvent],
) -> dict[str, Any]:
    steps = sorted(scenario.plan, key=lambda s: (s.planned_offset_minutes, s.step_id))
    actual_facility: dict[str, str] = {}
    for fid, ivs in fac_intervals.items():
        if ivs and ivs[0].state in (FACILITY_FAILED, FACILITY_BYPASSED, FACILITY_DEGRADED) and ivs[0].end_at:
            actual_facility[fid] = ivs[0].end_at
    actual_service: dict[str, str] = {}
    for lift in sorted(lift_events, key=lambda e: e.lifted_at):
        actual_service.setdefault(lift.service_id, lift.lifted_at)

    rows: list[dict[str, Any]] = []
    for step in steps:
        # 步骤的实际完成时刻优先取其目标设施离开失效状态的时刻
        # （绕行激活即设施离开 FAILED）；服务级解除仅作兜底。
        actual_at = actual_facility.get(step.target_facility_id) or actual_service.get(
            step.restores_service_id or ""
        )
        planned_at = None
        delta = None
        if t0 and actual_at:
            planned_at = format_ts(parse_ts(t0) + timedelta(minutes=step.planned_offset_minutes))
            delta = round((parse_ts(actual_at) - parse_ts(planned_at)).total_seconds() / 60.0, 1)
        rows.append(
            {
                "step_id": step.step_id,
                "target_facility_id": step.target_facility_id,
                "action": step.action,
                "planned_offset_minutes": step.planned_offset_minutes,
                "planned_at": planned_at,
                "actual_at": actual_at,
                "delta_minutes": delta,
                "restores_service_id": step.restores_service_id,
            }
        )

    executed = [r for r in rows if r["actual_at"] is not None]
    planned_rank = {r["step_id"]: i for i, r in enumerate(rows)}
    actual_sorted = sorted(executed, key=lambda r: r["actual_at"] or "")
    inversions: list[dict[str, str]] = []
    for i, a in enumerate(actual_sorted):
        for b in actual_sorted[i + 1 :]:
            if planned_rank[a["step_id"]] > planned_rank[b["step_id"]]:
                inversions.append({"later_than_planned": a["step_id"], "earlier_than_planned": b["step_id"]})

    return {
        "planned_order": [s.step_id for s in steps],
        "actual_order": [r["step_id"] for r in actual_sorted],
        "steps": rows,
        "order_inversions": inversions,
        "steps_not_executed": [r["step_id"] for r in rows if r["actual_at"] is None],
    }
