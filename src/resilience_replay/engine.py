"""确定性推演引擎：从冻结场景与追加日志重算整场演练。

引擎是纯函数式的：输出只取决于「冻结场景 + 有效事件集合 + 评估时刻」，
不依赖上报顺序、进程状态或时钟。重复上报在日志层幂等忽略；乱序事件
统一按发生时间排序；中断重启只是重新执行一遍 :meth:`ReplayEngine.replay`。

失效区间全部使用半开区间 ``[start, end)``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Any

from .journal import EventEnvelope, EventJournal
from .scenario import Scenario
from .time_model import (
    Interval,
    intersect_intervals,
    iso,
    merge_intervals,
    parse_ts,
    subtract_intervals,
)

DEFAULT_DISPUTE_SLA_SECONDS = 900  # 会商期限：15 分钟


# ---------------------------------------------------------------- 内部状态


@dataclass(slots=True)
class _Outage:
    start: datetime
    start_event: str
    end: datetime | None = None
    end_event: str | None = None
    close_kind: str = ""  # consensus | dispute


@dataclass(slots=True)
class _FacilityRuntime:
    down: bool = False
    opened_at: datetime | None = None
    open_event: str = ""
    outages: list[_Outage] = field(default_factory=list)
    # 尚未被本方恢复事件或纠正记录调和的故障主张 unit -> (event_id, t)
    outstanding_claims: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    dispute_id: str | None = None


@dataclass(slots=True)
class _Dispute:
    dispute_id: str
    facility_id: str
    fact: str
    opened_at: datetime
    deadline: datetime
    positions: dict[str, str] = field(default_factory=dict)
    resolved_at: datetime | None = None
    resolution: str = ""
    winning: str = ""  # restored | fault
    evidence_id: str = ""
    evidence_version: int | None = None
    manual: bool = False


@dataclass(slots=True)
class _DetourSession:
    primary: str
    backup: str
    activate_at: datetime
    activate_event: str
    revert_at: datetime | None = None
    revert_event: str | None = None


@dataclass(slots=True)
class _Evidence:
    evidence_id: str
    facility_id: str
    version: int
    summary: str
    reported_by: str
    reported_at: datetime
    report_event: str
    confirmed_by: str = ""
    confirmed_at: datetime | None = None
    confirm_event: str = ""


# ---------------------------------------------------------------- 引擎


class ReplayEngine:
    """对单场演练执行一次确定性重放。"""

    def __init__(
        self,
        scenario: Scenario,
        journal: EventJournal,
        *,
        dispute_sla_seconds: int = DEFAULT_DISPUTE_SLA_SECONDS,
        as_of: str | datetime | None = None,
    ) -> None:
        if journal.exercise_code != scenario.exercise_code:
            raise ValueError("日志与场景不属于同一场演练")
        self.scenario = scenario
        self.journal = journal
        self.sla = timedelta(seconds=dispute_sla_seconds)
        self.warnings: list[str] = []
        self._fac = {f.facility_id: _FacilityRuntime() for f in scenario.facilities}
        self._disputes: dict[str, _Dispute] = {}
        self._detours: list[_DetourSession] = []
        self._evidence: dict[str, _Evidence] = {}
        self._evidence_by_facility: dict[str, list[str]] = {}
        self._pause_intervals: list[Interval] = []
        self._detour_markers: list[dict[str, Any]] = []
        # 待区间解析后回算的“激活时未失效”与“无自身故障却报真正恢复”。
        self._detour_activations: list[tuple[str, str, datetime, str]] = []
        self._redundant_real_restores: list[tuple[str, str, datetime, str]] = []
        self._actions: dict[str, dict[str, Any]] = {}
        self._as_of: datetime | None = (
            parse_ts(as_of) if isinstance(as_of, str) else as_of
        )

    # ---- 公开入口 -------------------------------------------------------

    def replay(self) -> dict[str, Any]:
        chronological = sorted(
            self.journal.all_records(),
            key=lambda r: (parse_ts(r.occurred_at), r.event_id, r.seq),
        )
        if self._as_of is None:
            candidates = [parse_ts(r.occurred_at) for r in chronological]
            self._as_of = max(candidates) if candidates else parse_ts(self.scenario.frozen_at)

        # 历史重放：只纳入发生时间 <= as_of 的记录，且纠正只在
        # “纠正动作本身已发生”时生效——不能用未来的纠正改变当时结论。
        in_window = [r for r in chronological if parse_ts(r.occurred_at) <= self._as_of]
        retracted_in_window = {
            r.payload["target_event_id"]
            for r in in_window if r.kind == "retract_event"
        }
        retracted_beyond_window = (
            {r.payload["target_event_id"]
             for r in chronological if r.kind == "retract_event"}
            - retracted_in_window
        )
        records = [r for r in in_window if r.event_id not in retracted_in_window]

        all_records = self.journal.all_records()
        known_ids = {r.event_id for r in all_records}
        for record in in_window:
            if record.kind == "retract_event" \
                    and record.payload["target_event_id"] not in known_ids:
                self.warnings.append(
                    f"纠正记录 {record.event_id} 指向不存在的事件，"
                    "该纠正不生效（原始日志保留）"
                )
        for target in retracted_beyond_window:
            self.warnings.append(
                f"事件 {target} 在评估时刻之后才被纠正，本次历史重放仍按有效处理"
            )

        for record in records:
            self._dispatch(record)

        # 超时未裁决的会商：按保守原则处理（风险不解除）。
        for dispute in self._disputes.values():
            if dispute.resolved_at is None and self._as_of > dispute.deadline:
                self._apply_timeout(dispute)

        own_effective, business, relief, physical_propagated = self._resolve_outages()
        self._relief = relief
        self._own_effective = own_effective
        self._business = business
        self._physical_propagated = physical_propagated
        self._annotate_detour_markers()
        self._annotate_lifecycle_warnings()
        service_view = self._build_services(business)
        detour_view = self._build_detour_view(business)
        findings = self._build_findings(service_view, detour_view)
        actions = self._link_actions(findings)
        conclusions = self._build_conclusions(service_view)
        plan_vs_actual = self._build_plan_vs_actual(business, service_view)

        report: dict[str, Any] = {
            "exercise_code": self.scenario.exercise_code,
            "scenario_revision": self.scenario.scenario_revision,
            "scenario_fingerprint": self.scenario.fingerprint(),
            "journal_tail_hash": self.journal.tail_hash(),
            "records_total": len(all_records),
            "records_active": len(records),
            "records_retracted": sum(
                1 for r in in_window if r.event_id in retracted_in_window
            ),
            "records_beyond_as_of": len(chronological) - len(in_window),
            "as_of": iso(self._as_of),
            "active_periods": [
                {"start": iso(i.start), "end": iso(i.end) if i.end else None}
                for i in self._active_periods()
            ],
            "propagation": self._build_propagation(records),
            "facilities": self._build_facility_view(own_effective, business),
            "detours": detour_view,
            "detour_restoration_markers": self._detour_markers,
            "services": service_view,
            "disputes": [self._dispute_dict(d) for d in sorted(
                self._disputes.values(), key=lambda d: (d.opened_at, d.dispute_id)
            )],
            "evidence": self._build_evidence_view(),
            "timeline": self._build_timeline(records),
            "plan_vs_actual": plan_vs_actual,
            "findings": findings,
            "corrective_actions": actions,
            "conclusions": conclusions,
            "warnings": sorted(set(self.warnings)),
        }
        report["replay_fingerprint"] = self._fingerprint(report)
        return report

    # ---- 事件分发 -------------------------------------------------------

    def _is_coordinator(self, e: EventEnvelope, action: str) -> bool:
        """终局裁定动作（确认证据、裁决会商、验证关闭）只可由指挥部执行。"""
        if e.unit_id == self.scenario.coordinator:
            return True
        self.warnings.append(
            f"{e.occurred_at} {action} 只能由联合指挥部 {self.scenario.coordinator} "
            f"执行，{e.unit_id} 的动作 {e.event_id} 不生效"
        )
        return False

    def _dispatch(self, record: EventEnvelope) -> None:
        kind = record.kind
        t = parse_ts(record.occurred_at)
        handler = getattr(self, f"_on_{kind}", None)
        if handler is not None:
            handler(record, t)

    def _close_outage(
        self, state: _FacilityRuntime, t: datetime, event_id: str, close_kind: str
    ) -> None:
        """关闭当前开放的物理失效区间（协商一致或会商裁决）。"""
        if not state.down:
            return
        state.down = False
        state.opened_at = None
        state.open_event = ""
        state.outstanding_claims.clear()
        if state.outages and state.outages[-1].end is None:
            current = state.outages[-1]
            state.outages[-1] = _Outage(
                start=current.start,
                start_event=current.start_event,
                end=t,
                end_event=event_id,
                close_kind=close_kind,
            )
        state.dispute_id = None

    def _on_facility_fault(self, e: EventEnvelope, t: datetime) -> None:
        fid = e.payload["facility_id"]
        state = self._facility_state(fid, e.event_id)
        if state is None:
            return
        if state.down:
            if e.unit_id in state.outstanding_claims:
                self.warnings.append(
                    f"{iso(t)} {e.unit_id} 对已失效设施 {fid} 重复上报故障({e.event_id})"
                )
            else:
                # 旁证：另一个单位同样观测到失效。
                state.outstanding_claims[e.unit_id] = (e.event_id, t)
            return
        state.down = True
        state.opened_at = t
        state.open_event = e.event_id
        state.outages.append(_Outage(start=t, start_event=e.event_id))
        state.outstanding_claims[e.unit_id] = (e.event_id, t)

    def _on_facility_restored(self, e: EventEnvelope, t: datetime) -> None:
        fid = e.payload["facility_id"]
        kind = e.payload["restoration_kind"]
        state = self._facility_state(fid, e.event_id)
        if state is None:
            return
        if kind == "detour":
            # 标记本身是否有效要等全场区间解析后才能判断（备份可能自身
            # 被传播失效或被别的绕行缓解），先记录，后回算。
            self._detour_markers.append({
                "event_id": e.event_id,
                "facility_id": fid,
                "unit_id": e.unit_id,
                "at": iso(t),
                "effective": False,
                "note": "绕行恢复标记（非真正恢复）",
            })
            return

        # kind == "real"：主张设施真正恢复。
        state.outstanding_claims.pop(e.unit_id, None)
        if not state.down:
            # 设施无自身开放故障：可能是上游恢复后的冗余确认，也可能是
            # 无的放矢，待全场区间解析后回算，先记录不误判。
            self._redundant_real_restores.append((fid, e.unit_id, t, e.event_id))
            return
        contrary = {u: c for u, c in state.outstanding_claims.items()}
        if contrary:
            # 存在仍坚持失效的单位 → 自动进入有期限会商，风险暂不解除。
            dispute = self._open_dispute(
                facility_id=fid,
                fact=f"设施 {fid} 是否真正恢复",
                t=t,
                restored_unit=e.unit_id,
                contrary=contrary,
                manual=False,
            )
            dispute.positions.setdefault(e.unit_id, "restored")
            state.dispute_id = dispute.dispute_id
            self.warnings.append(
                f"{iso(t)} 设施 {fid} 状态主张冲突，会商 {dispute.dispute_id} 开启"
            )
            return
        if state.dispute_id:
            # 会商进行中，记为立场，不直接关闭。
            dispute = self._disputes[state.dispute_id]
            dispute.positions[e.unit_id] = "restored"
            return
        self._close_outage(state, t, e.event_id, "consensus")

    def _on_activate_detour(self, e: EventEnvelope, t: datetime) -> None:
        primary, backup = e.payload["primary"], e.payload["backup"]
        if not self._known_pair(primary, backup, e.event_id):
            return
        for session in self._detours:
            if session.primary == primary and session.backup == backup and session.revert_at is None:
                self.warnings.append(
                    f"{iso(t)} 绕行 {primary}->{backup} 已处于激活状态({e.event_id})"
                )
                return
        state = self._fac[primary]
        self._detour_activations.append((primary, backup, t, e.event_id))
        self._detours.append(_DetourSession(
            primary=primary, backup=backup,
            activate_at=t, activate_event=e.event_id,
        ))

    def _on_detour_reverted(self, e: EventEnvelope, t: datetime) -> None:
        primary, backup = e.payload["primary"], e.payload["backup"]
        if not self._known_pair(primary, backup, e.event_id):
            return
        for session in reversed(self._detours):
            if session.primary == primary and session.backup == backup and session.revert_at is None:
                session.revert_at = t
                session.revert_event = e.event_id
                if self._fac[primary].down:
                    self.warnings.append(
                        f"{iso(t)} 绕行 {primary}->{backup} 归还时主设施仍未真正恢复，"
                        f"风险重新暴露({e.event_id})"
                    )
                return
        self.warnings.append(
            f"{iso(t)} 绕行 {primary}->{backup} 无激活会话却收到归还({e.event_id})"
        )

    def _on_evidence_reported(self, e: EventEnvelope, t: datetime) -> None:
        fid = e.payload["facility_id"]
        if fid not in self._fac:
            self.warnings.append(f"证据 {e.event_id} 引用未知设施 {fid}，忽略")
            return
        evidence = _Evidence(
            evidence_id=e.event_id,
            facility_id=fid,
            version=int(e.payload["version"]),
            summary=str(e.payload["summary"]),
            reported_by=e.unit_id,
            reported_at=t,
            report_event=e.event_id,
        )
        if e.event_id in self._evidence:
            return
        prior = [self._evidence[i] for i in self._evidence_by_facility.get(fid, [])]
        if any(old.version == evidence.version for old in prior):
            self.warnings.append(
                f"{iso(t)} 设施 {fid} 证据版本 v{evidence.version} 重复登记({e.event_id})"
            )
        self._evidence[e.event_id] = evidence
        self._evidence_by_facility.setdefault(fid, []).append(e.event_id)

    def _on_evidence_confirmed(self, e: EventEnvelope, t: datetime) -> None:
        if not self._is_coordinator(e, "证据确认"):
            return
        evidence_id = e.payload["evidence_id"]
        version = int(e.payload["version"])
        evidence = self._evidence.get(evidence_id)
        if evidence is None:
            self.warnings.append(f"确认动作 {e.event_id} 指向不存在的证据 {evidence_id}")
            return
        if evidence.version != version:
            self.warnings.append(
                f"证据 {evidence_id} 确认版本 v{version} 与登记版本 v{evidence.version} 不符"
            )
            return
        if evidence.confirmed_at is not None:
            self.warnings.append(f"证据 {evidence_id} v{version} 已被确认，重复确认忽略")
            return
        evidence.confirmed_by = e.unit_id
        evidence.confirmed_at = t
        evidence.confirm_event = e.event_id
        latest = self._latest_version(evidence.facility_id, at=t)
        if latest is not None and latest.evidence_id != evidence_id:
            self.warnings.append(
                f"{iso(t)} 确认的 {evidence_id} v{version} 并非设施 "
                f"{evidence.facility_id} 的最新证据版本（最新 v{latest.version}）"
            )

    def _on_dispute_opened(self, e: EventEnvelope, t: datetime) -> None:
        fid = e.payload.get("facility_id")
        dispute_id = e.payload.get("dispute_id") or f"D/MANUAL/{e.event_id}"
        if dispute_id in self._disputes:
            self.warnings.append(f"会商 {dispute_id} 已存在，重复开启忽略({e.event_id})")
            return
        positions = dict(e.payload.get("positions") or {})
        dispute = _Dispute(
            dispute_id=dispute_id,
            facility_id=fid or "",
            fact=str(e.payload["fact"]),
            opened_at=t,
            deadline=t + self.sla,
            positions={str(k): str(v) for k, v in positions.items()},
            manual=True,
        )
        self._disputes[dispute_id] = dispute
        if fid and fid in self._fac and self._fac[fid].down:
            self._fac[fid].dispute_id = dispute_id

    def _on_dispute_position(self, e: EventEnvelope, t: datetime) -> None:
        dispute = self._disputes.get(e.payload["dispute_id"])
        if dispute is None:
            self.warnings.append(f"立场事件 {e.event_id} 指向不存在的会商 {e.payload['dispute_id']}")
            return
        if dispute.resolved_at is not None:
            self.warnings.append(f"会商 {dispute.dispute_id} 已裁决，立场不再接收({e.event_id})")
            return
        dispute.positions[e.unit_id] = str(e.payload["position"])

    def _on_dispute_resolved(self, e: EventEnvelope, t: datetime) -> None:
        if not self._is_coordinator(e, "会商裁决"):
            return
        dispute = self._disputes.get(e.payload["dispute_id"])
        if dispute is None:
            self.warnings.append(f"裁决事件 {e.event_id} 指向不存在的会商 {e.payload['dispute_id']}")
            return
        if dispute.resolved_at is not None:
            self.warnings.append(f"会商 {dispute.dispute_id} 已有终局裁决，重复裁决忽略")
            return
        winning = str(e.payload["winning"])
        if winning not in ("restored", "fault"):
            self.warnings.append(f"会商 {dispute.dispute_id} 裁决值非法: {winning}")
            return
        dispute.resolved_at = t
        dispute.resolution = str(e.payload.get("resolution", ""))
        evidence_id = e.payload.get("evidence_id", "")
        if evidence_id:
            evidence = self._evidence.get(evidence_id)
            if evidence is None:
                self.warnings.append(
                    f"会商 {dispute.dispute_id} 裁决引用了不存在的证据 {evidence_id}"
                )
                evidence_id = ""
            elif evidence.confirmed_at is None or evidence.confirmed_at > t:
                self.warnings.append(
                    f"会商 {dispute.dispute_id} 裁决引用的 {evidence_id} "
                    f"v{evidence.version} 未经及时确认，按保守原则处理"
                )
                winning = "fault"
            elif dispute.facility_id and evidence.facility_id != dispute.facility_id:
                self.warnings.append(
                    f"会商 {dispute.dispute_id} 证据 {evidence_id} 不属于争议设施"
                )
                evidence_id = ""
        dispute.winning = winning
        dispute.evidence_id = evidence_id
        dispute.evidence_version = self._evidence[evidence_id].version if evidence_id else None

        state = self._fac.get(dispute.facility_id) if dispute.facility_id else None
        if winning == "restored":
            if state is not None and state.down:
                self._close_outage(state, t, e.event_id, "dispute")
            if state is not None:
                state.outstanding_claims.clear()
                state.dispute_id = None
        else:
            # 裁决为仍失效：恢复方主张作废，失效区间延续。
            if state is not None:
                state.dispute_id = None

    def _on_decision(self, e: EventEnvelope, t: datetime) -> None:
        return  # 时间线直接展示，无状态副作用

    def _on_resource_dispatch(self, e: EventEnvelope, t: datetime) -> None:
        return

    def _on_action_registered(self, e: EventEnvelope, t: datetime) -> None:
        action_id = e.event_id
        self._actions[action_id] = {
            "action_id": action_id,
            "title": str(e.payload["title"]),
            "owner_unit": str(e.payload["owner_unit"]),
            "finding_id": str(e.payload["finding_id"]),
            "registered_at": iso(t),
            "registered_by": e.unit_id,
            "status": "registered",
            "accepted_by": None,
            "accepted_at": None,
            "closed_evidence": None,
            "verified_at": None,
            "finding_known": False,
        }

    def _on_action_accepted(self, e: EventEnvelope, t: datetime) -> None:
        action = self._actions.get(e.payload["action_id"])
        if action is None:
            self.warnings.append(f"接受事件 {e.event_id} 指向不存在的整改 {e.payload['action_id']}")
            return
        if action["status"] != "registered":
            self.warnings.append(f"整改 {action['action_id']} 已被接受，重复接受忽略")
            return
        accepted_by = str(e.payload["accepted_by"])
        if accepted_by != action["owner_unit"]:
            self.warnings.append(
                f"整改 {action['action_id']} 应由责任单位 {action['owner_unit']} "
                f"接受，实际由 {accepted_by} 接受"
            )
        action["status"] = "accepted"
        action["accepted_by"] = accepted_by
        action["accepted_at"] = iso(t)

    def _on_action_verified(self, e: EventEnvelope, t: datetime) -> None:
        if not self._is_coordinator(e, "整改验证关闭"):
            return
        action = self._actions.get(e.payload["action_id"])
        if action is None:
            self.warnings.append(f"验证事件 {e.event_id} 指向不存在的整改 {e.payload['action_id']}")
            return
        if action["status"] == "verified":
            self.warnings.append(f"整改 {action['action_id']} 已验证关闭，重复验证忽略")
            return
        evidence = self._evidence.get(e.payload["evidence_id"])
        if evidence is None:
            self.warnings.append(
                f"整改 {action['action_id']} 验证引用了不存在的证据 {e.payload['evidence_id']}"
            )
            return
        if evidence.confirmed_at is None:
            self.warnings.append(
                f"整改 {action['action_id']} 验证引用的 {evidence.evidence_id} "
                f"v{evidence.version} 尚未确认，不能关闭"
            )
            return
        action["status"] = "verified"
        action["verified_at"] = iso(t)
        action["closed_evidence"] = {
            "evidence_id": evidence.evidence_id,
            "version": evidence.version,
            "confirmed_by": evidence.confirmed_by,
            "confirmed_at": iso(evidence.confirmed_at) if evidence.confirmed_at else None,
        }
        if action["accepted_at"] is None:
            action["status"] = "verified"
            action["accepted_by"] = action["owner_unit"]
            action["accepted_at"] = action["registered_at"]
            self.warnings.append(
                f"整改 {action['action_id']} 缺少接受记录，按登记即接受追溯补记"
            )

    def _on_exercise_paused(self, e: EventEnvelope, t: datetime) -> None:
        if self._pause_intervals and self._pause_intervals[-1].end is None:
            self.warnings.append(f"重复暂停事件 {e.event_id}，忽略")
            return
        self._pause_intervals.append(Interval(t, None))

    def _on_exercise_resumed(self, e: EventEnvelope, t: datetime) -> None:
        if not self._pause_intervals or self._pause_intervals[-1].end is not None:
            self.warnings.append(f"无对应暂停的恢复事件 {e.event_id}，忽略")
            return
        open_interval = self._pause_intervals[-1]
        self._pause_intervals[-1] = Interval(open_interval.start, t)

    # ---- 会商辅助 -------------------------------------------------------

    def _open_dispute(
        self,
        *,
        facility_id: str,
        fact: str,
        t: datetime,
        restored_unit: str,
        contrary: dict[str, tuple[str, datetime]],
        manual: bool,
    ) -> _Dispute:
        dispute_id = f"D/{facility_id}/{t.strftime('%Y%m%dT%H%M%SZ')}"
        suffix = 1
        unique_id = dispute_id
        while unique_id in self._disputes:
            suffix += 1
            unique_id = f"{dispute_id}-{suffix}"
        positions = {restored_unit: "restored"}
        for unit in sorted(contrary):
            positions[unit] = "fault"
        dispute = _Dispute(
            dispute_id=unique_id,
            facility_id=facility_id,
            fact=fact,
            opened_at=t,
            deadline=t + self.sla,
            positions=positions,
            manual=manual,
        )
        self._disputes[unique_id] = dispute
        return dispute

    def _apply_timeout(self, dispute: _Dispute) -> None:
        dispute.resolved_at = self._as_of
        dispute.resolution = "会商期限内未达成一致，按保守原则判定风险未解除"
        dispute.winning = "fault"
        state = self._fac.get(dispute.facility_id) if dispute.facility_id else None
        if state is not None:
            state.dispute_id = None
        self.warnings.append(
            f"会商 {dispute.dispute_id} 超过期限 {iso(dispute.deadline)} 未裁决，保守判定失效延续"
        )

    # ---- 区间计算 -------------------------------------------------------

    def _detour_covers(self, fid: str, t: datetime) -> bool:
        """区间解析完成后回算：时刻 t 是否存在对 fid 的有效绕行缓解。"""
        return any(
            cover.start <= t and (cover.end is None or t < cover.end)
            for cover in self._relief.get(fid, [])
        )

    def _annotate_detour_markers(self) -> None:
        """为所有“绕行恢复”标记回算有效性并产出告警。"""
        for marker in self._detour_markers:
            t = parse_ts(marker["at"])
            effective = self._detour_covers(marker["facility_id"], t)
            marker["effective"] = effective
            if effective:
                marker["note"] = "绕行恢复标记（非真正恢复），此刻存在有效绕行覆盖"
            else:
                marker["note"] = "绕行恢复标记（非真正恢复），但此刻不存在有效绕行覆盖"
                self.warnings.append(
                    f"{marker['at']} {marker['unit_id']} 标记 {marker['facility_id']} "
                    f"绕行恢复，但无有效绕行覆盖({marker['event_id']})"
                )

    def _annotate_lifecycle_warnings(self) -> None:
        """区间解析后回算两类“时点状态”告警，避免对传播失效误判。"""
        def physically_down_at(fid: str, t: datetime) -> bool:
            return any(
                p.start <= t and (p.end is None or t < p.end)
                for p in self._physical_propagated.get(fid, [])
            )

        for primary, backup, t, event_id in self._detour_activations:
            if not physically_down_at(primary, t):
                self.warnings.append(
                    f"{iso(t)} 绕行 {primary}->{backup} 激活时该设施并未失效({event_id})"
                )

        for fid, unit_id, t, event_id in self._redundant_real_restores:
            ever_down = bool(self._fac[fid].outages) or bool(
                self._physical_propagated.get(fid)
            )
            if not ever_down:
                self.warnings.append(
                    f"{iso(t)} {unit_id} 对从未失效的设施 {fid} 上报真正恢复({event_id})"
                )
            # 曾被上游波及、此刻已健康的，视为恢复确认，不告警。


    def _resolve_outages(
        self,
    ) -> tuple[
        dict[str, list[Interval]],
        dict[str, list[Interval]],
        dict[str, list[Interval]],
        dict[str, list[Interval]],
    ]:
        """求解三层区间，全部使用半开区间。

        1. ``physical_propagated[f]``：纯物理闭包，不考虑任何绕行，
           用于判定“真正恢复”。
        2. ``substantive[f]``（设施实质失效）：

           ``g[f] = (phys[f] ∪ ⋃上游g) − 透明替代[f]``

           透明替代（发电）给下游的是等价供给，沿依赖图治愈下游。
        3. ``business[f]``（业务可用失效）：

           ``b[f] = g[f] − 排他替代[f]``

           排他替代（网络切换、算力迁移）只拯救显式切换的节点，
           不沿依赖图自动治愈其下游。

        透明替代成立还要求备份此刻实质可用（``g[backup]`` 为空）。
        从最保守状态（g=物理闭包）迭代，缓解单调增长，必然收敛。
        """
        parents: dict[str, list[str]] = {f.facility_id: [] for f in self.scenario.facilities}
        for upstream, downstream in self.scenario.hard_edges():
            parents[downstream].append(upstream)
        fids = list(parents)
        horizon_end = self._as_of

        physical = {
            fid: merge_intervals(
                [Interval(o.start, o.end or horizon_end) for o in state.outages]
            )
            for fid, state in self._fac.items()
        }
        raw_open = {
            fid: bool(self._fac[fid].outages and self._fac[fid].outages[-1].end is None)
            for fid in fids
        }

        # 纯物理传播基线（无缓解）。
        physical_propagated: dict[str, list[Interval]] = {}

        def phys_of(fid: str) -> list[Interval]:
            if fid in physical_propagated:
                return physical_propagated[fid]
            intervals = list(physical[fid])
            for parent in parents[fid]:
                intervals.extend(phys_of(parent))
            physical_propagated[fid] = merge_intervals(intervals)
            return physical_propagated[fid]

        for fid in fids:
            phys_of(fid)

        def detour_segments() -> list[tuple[str, str, bool, Interval]]:
            """返回 (primary, backup, transparent, 窗口裁剪后的激活段)。"""
            found: list[tuple[str, str, bool, Interval]] = []
            for session in self._detours:
                alt = next(
                    (a for a in self.scenario.alternatives
                     if a.primary == session.primary and a.backup == session.backup),
                    None,
                )
                if alt is None or session.backup == session.primary:
                    continue
                segment = Interval(session.activate_at, session.revert_at or horizon_end)
                wf, wt = alt.window()
                if wf is not None:
                    segment = Interval(max(segment.start, wf), min(segment.end, wt))
                if segment.end > segment.start:
                    found.append((session.primary, session.backup, alt.transparent, segment))
            return found

        segments = detour_segments()

        def transparent_relief(g_now: dict[str, list[Interval]]) -> dict[str, list[Interval]]:
            relief: dict[str, list[Interval]] = {fid: [] for fid in fids}
            for primary, backup, transparent, segment in segments:
                if not transparent:
                    continue
                # 备份自身实质失效的时段，替代不能承担供给。
                relief[primary].extend(subtract_intervals(segment, g_now[backup]))
            return {fid: merge_intervals(relief[fid]) for fid in fids}

        # 从最保守状态开始迭代求 g（透明替代不动点）。
        substantive = {fid: list(physical_propagated[fid]) for fid in fids}
        transparent_cov: dict[str, list[Interval]] = {fid: [] for fid in fids}
        for _ in range(len(fids) + 2):
            transparent_cov = transparent_relief(substantive)
            new_substantive: dict[str, list[Interval]] = {}

            def g_of(fid: str) -> list[Interval]:
                if fid in new_substantive:
                    return new_substantive[fid]
                intervals = list(physical[fid])
                for parent in parents[fid]:
                    intervals.extend(g_of(parent))
                relieved = merge_intervals([
                    part
                    for interval in merge_intervals(intervals)
                    for part in subtract_intervals(interval, transparent_cov[fid])
                ])
                new_substantive[fid] = relieved
                return relieved

            for fid in fids:
                g_of(fid)
            if new_substantive == substantive:
                break
            substantive = new_substantive

        # 排他替代：只在显式切换的节点上挖洞，备份实质失效时段要扣除。
        exclusive_cov: dict[str, list[Interval]] = {fid: [] for fid in fids}
        for primary, backup, transparent, segment in segments:
            if transparent:
                continue
            exclusive_cov[primary].extend(subtract_intervals(segment, substantive[backup]))
        exclusive_cov = {fid: merge_intervals(exclusive_cov[fid]) for fid in fids}

        business = {
            fid: merge_intervals(
                [part for interval in substantive[fid]
                 for part in subtract_intervals(interval, exclusive_cov[fid])]
            )
            for fid in fids
        }

        # 设施“自身有效失效”（设施视图用）：物理自身故障扣除全部替代。
        own_effective: dict[str, list[Interval]] = {}
        for fid in fids:
            all_relief = merge_intervals(transparent_cov[fid] + exclusive_cov[fid])
            own_effective[fid] = merge_intervals(
                [part for interval in physical[fid]
                 for part in subtract_intervals(interval, all_relief)]
            )

        physical_open: dict[str, bool] = {}

        def phys_open(fid: str) -> bool:
            if fid in physical_open:
                return physical_open[fid]
            opened = raw_open[fid] or any(phys_open(p) for p in parents[fid])
            physical_open[fid] = opened
            return opened

        for fid in fids:
            phys_open(fid)

        def not_covered_now(cover: dict[str, list[Interval]], fid: str) -> bool:
            return not any(
                c.start <= horizon_end and (c.end is None or horizon_end < c.end)
                for c in cover[fid]
            )

        # 与 g 的区间公式同构的布尔不动点：
        # g_open[f] = 未被透明覆盖(f) 且 (自身物理开放 或 任一上游 g_open)
        substantive_open: dict[str, bool] = {}

        def subst_open(fid: str) -> bool:
            if fid in substantive_open:
                return substantive_open[fid]
            substantive_open[fid] = (
                not_covered_now(transparent_cov, fid)
                and (
                    raw_open[fid]
                    or any(subst_open(parent) for parent in parents[fid])
                )
            )
            return substantive_open[fid]

        for fid in fids:
            subst_open(fid)

        business_open = {
            fid: substantive_open[fid] and not_covered_now(exclusive_cov, fid)
            for fid in fids
        }
        own_open = {
            fid: raw_open[fid]
            and bool(own_effective[fid])
            and own_effective[fid][-1].end == horizon_end
            for fid in fids
        }

        def restore_open_ends(intervals: list[Interval], opened: bool) -> list[Interval]:
            if not opened or not intervals or intervals[-1].end != horizon_end:
                return intervals
            return [*intervals[:-1], Interval(intervals[-1].start, None)]

        return (
            {fid: restore_open_ends(own_effective[fid], own_open[fid]) for fid in fids},
            {fid: restore_open_ends(business[fid], business_open[fid]) for fid in fids},
            {fid: merge_intervals(transparent_cov[fid] + exclusive_cov[fid]) for fid in fids},
            {fid: restore_open_ends(physical_propagated[fid], physical_open[fid]) for fid in fids},
        )


    # ---- 视图构建 -------------------------------------------------------

    def _active_periods(self) -> list[Interval]:
        horizon = Interval(parse_ts(self.scenario.frozen_at), self._as_of)
        return subtract_intervals(horizon, merge_intervals(self._pause_intervals))

    def _active_seconds(self, interval: Interval) -> float:
        """失效持续秒数，按场景时钟计算（半开区间）。

        演练暂停/重启是值守组织动作，不是场景内旅客与物资经历的
        客观时间；若把暂停时长扣掉，结果反而依赖暂停记录是否完整。
        为保证“中断重启不改变结果”，这里不做暂停扣减。
        """
        return (
            (interval.end or self._as_of) - interval.start
        ).total_seconds()


    def _latest_version(self, fid: str, *, at: datetime) -> _Evidence | None:
        ids = self._evidence_by_facility.get(fid, [])
        items = [self._evidence[i] for i in ids if self._evidence[i].reported_at <= at]
        return max(items, key=lambda x: (x.version, x.reported_at), default=None)

    def _confirmed_evidence(self, fid: str, *, at: datetime) -> _Evidence | None:
        ids = self._evidence_by_facility.get(fid, [])
        items = [
            self._evidence[i] for i in ids
            if self._evidence[i].confirmed_at is not None and self._evidence[i].confirmed_at <= at
        ]
        return max(items, key=lambda x: (x.version, x.confirmed_at), default=None)

    def _facility_state(self, fid: str, event_id: str) -> _FacilityRuntime | None:
        state = self._fac.get(fid)
        if state is None:
            self.warnings.append(f"事件 {event_id} 引用了场景中不存在的设施 {fid}，忽略")
            return None
        return state

    def _known_pair(self, primary: str, backup: str, event_id: str) -> bool:
        if primary not in self._fac or backup not in self._fac:
            self.warnings.append(f"事件 {event_id} 引用了不存在的设施: {primary}->{backup}")
            return False
        if not any(
            a.primary == primary and a.backup == backup
            for a in self.scenario.alternatives
        ):
            self.warnings.append(
                f"事件 {event_id} 使用的替代关系 {primary}->{backup} 未在冻结场景中登记"
            )
            return False
        return True

    def _build_propagation(self, records: list[EventEnvelope]) -> list[dict[str, Any]]:
        view = []
        for record in records:
            if record.kind != "facility_fault":
                continue
            fid = record.payload.get("facility_id", "")
            if fid not in self._fac:
                continue
            impacted = self.scenario.propagation_closure({fid})
            view.append({
                "event_id": record.event_id,
                "at": record.occurred_at,
                "root_facility": fid,
                "impacted_facilities": sorted(impacted),
                "impacted_services": sorted(
                    s.service_id for s in self.scenario.services
                    if impacted & set(s.depends_on)
                ),
            })
        return view

    def _build_facility_view(
        self,
        own_effective: dict[str, list[Interval]],
        propagated: dict[str, list[Interval]],
    ) -> list[dict[str, Any]]:
        view = []
        for facility in self.scenario.facilities:
            state = self._fac[facility.facility_id]
            outages = []
            for outage in state.outages:
                end = outage.end
                evidence_ref = None
                validated = False
                if end is not None:
                    evidence = self._confirmed_evidence(facility.facility_id, at=end)
                    validated = evidence is not None
                    if evidence is not None:
                        evidence_ref = {
                            "evidence_id": evidence.evidence_id,
                            "version": evidence.version,
                            "confirmed_by": evidence.confirmed_by,
                        }
                outages.append({
                    "start": iso(outage.start),
                    "end": iso(end) if end else None,
                    "start_event": outage.start_event,
                    "end_event": outage.end_event,
                    "close_kind": outage.close_kind,
                    "validated_by_confirmed_evidence": validated,
                    "evidence": evidence_ref,
                })
            detour_cover = [
                {"start": iso(i.start), "end": iso(i.end) if i.end else None}
                for i in self._relief.get(facility.facility_id, [])
            ]
            view.append({
                "facility_id": facility.facility_id,
                "name": facility.name,
                "owner_unit": facility.owner_unit,
                "currently_down": any(i.end is None for i in own_effective[facility.facility_id]),
                "physical_outages": outages,
                "detour_coverage": detour_cover,
                "effective_outage": [
                    {"start": iso(i.start), "end": iso(i.end) if i.end else None}
                    for i in own_effective[facility.facility_id]
                ],
                "propagated_outage": [
                    {"start": iso(i.start), "end": iso(i.end) if i.end else None}
                    for i in propagated[facility.facility_id]
                ],
            })
        return view

    def _build_detour_view(self, propagated: dict[str, list[Interval]]) -> list[dict[str, Any]]:
        view = []
        for session in sorted(self._detours, key=lambda s: (s.activate_at, s.primary)):
            alt = next(
                a for a in self.scenario.alternatives
                if a.primary == session.primary and a.backup == session.backup
            )
            raw = Interval(session.activate_at, session.revert_at or self._as_of)
            wf, wt = alt.window()
            notes: list[str] = []
            if wf is not None and (raw.start < wf or raw.end > wt):
                notes.append("绕行激活超出预案窗口的部分不计入有效覆盖")
            backup_holes = propagated.get(session.backup, [])
            effective_parts = subtract_intervals(raw, backup_holes)
            if wf is not None:
                effective_parts = [
                    clipped
                    for part in effective_parts
                    for clipped in (Interval(max(part.start, wf), min(part.end, wt)),)
                    if clipped.end > clipped.start
                ]
            if any(
                h.start <= raw.start < (h.end or self._as_of)
                or (h.start < raw.end and (h.end is None or h.end > raw.start))
                for h in backup_holes
            ):
                notes.append("备份设施自身失效期间绕行中断，风险重新暴露")
            view.append({
                "primary": session.primary,
                "backup": session.backup,
                "label": alt.label,
                "activated_at": iso(session.activate_at),
                "reverted_at": iso(session.revert_at) if session.revert_at else None,
                "still_active": session.revert_at is None,
                "effective_coverage": [
                    {"start": iso(i.start), "end": iso(i.end) if i.end else None}
                    for i in merge_intervals(effective_parts)
                ],
                "notes": notes,
            })
        return view

    def _detours_relieving(self, roots: set[str], segment: Interval) -> list[str]:
        """该缓解段内对根因设施生效的绕行标签。"""
        begin, finish = segment.start, segment.end or self._as_of
        labels = []
        for session in self._detours:
            if session.primary not in roots:
                continue
            active = Interval(session.activate_at, session.revert_at or self._as_of)
            if active.start < finish and (active.end or self._as_of) > begin:
                labels.append(f"{session.primary}→{session.backup}")
        return sorted(set(labels))

    def _build_services(self, business: dict[str, list[Interval]]) -> list[dict[str, Any]]:
        view = []
        for svc in self.scenario.services:
            deps = list(svc.depends_on)
            risk_windows = merge_intervals(
                [interval for fid in deps for interval in self._physical_propagated[fid]]
            )
            unavailable_all = merge_intervals(
                [interval for fid in deps for interval in business[fid]]
            )
            episodes = []
            for index, window in enumerate(risk_windows, start=1):
                end_dt = window.end
                unavailable = intersect_intervals([window], unavailable_all)
                mitigated = subtract_intervals(window, unavailable)
                roots = sorted({
                    fid for fid in self._fac
                    if self.scenario.propagation_closure({fid}) & set(deps)
                    and any(
                        o.start < (window.end or self._as_of)
                        and (o.end is None or o.end > window.start)
                        for o in self._fac[fid].outages
                    )
                })

                # 首次可用性：窗口起点若处于停机段，则取该段右端；
                # 右端恰为物理风险窗口结束即为真正恢复，否则是绕行缓解。
                first_available_at: datetime | None = window.start
                if unavailable and unavailable[0].start <= window.start:
                    first_available_at = unavailable[0].end
                if first_available_at is not None and end_dt is not None \
                        and first_available_at >= end_dt:
                    first_kind = "real"
                    first_available_at = end_dt
                elif first_available_at is None:
                    first_kind = ""
                else:
                    first_kind = "detour"

                t_avail = (
                    self._active_seconds(Interval(window.start, first_available_at))
                    if first_available_at is not None else None
                )
                t_real = self._active_seconds(window) if end_dt is not None else None

                evidence_refs = []
                if end_dt is not None:
                    for fid in roots:
                        evidence = self._confirmed_evidence(fid, at=end_dt)
                        if evidence is not None:
                            evidence_refs.append({
                                "facility_id": fid,
                                "evidence_id": evidence.evidence_id,
                                "version": evidence.version,
                                "confirmed_by": evidence.confirmed_by,
                            })

                episodes.append({
                    "index": index,
                    "risk_window": {
                        "start": iso(window.start),
                        "end": iso(end_dt) if end_dt else None,
                    },
                    "root_facilities": roots,
                    "unavailable_segments": [
                        {"start": iso(i.start), "end": iso(i.end) if i.end else None}
                        for i in unavailable
                    ],
                    "mitigated_segments": [
                        {
                            "start": iso(i.start),
                            "end": iso(i.end) if i.end else None,
                            "via": self._detours_relieving(set(roots), i),
                        }
                        for i in mitigated
                    ],
                    "first_available_at": iso(first_available_at) if first_available_at else None,
                    "first_available_kind": first_kind or None,
                    "time_to_first_availability_active_seconds": (
                        round(t_avail, 3) if t_avail is not None else None
                    ),
                    "time_to_real_restoration_active_seconds": (
                        round(t_real, 3) if t_real is not None else None
                    ),
                    "rto_seconds": svc.rto_seconds,
                    "rto_breached": t_avail is None or t_avail > svc.rto_seconds,
                    "risk_closed_at": iso(end_dt) if end_dt else None,
                    "validated_by_confirmed_evidence": bool(evidence_refs),
                    "evidence_refs": evidence_refs,
                })

            last_episode = episodes[-1] if episodes else None
            view.append({
                "service_id": svc.service_id,
                "name": svc.name,
                "owner_unit": svc.owner_unit,
                "depends_on": deps,
                "rto_seconds": svc.rto_seconds,
                "currently_available": last_episode is None or (
                    last_episode["risk_closed_at"] is not None
                    or bool(last_episode["mitigated_segments"])
                ),
                "on_detour_only": (
                    last_episode is not None
                    and last_episode["risk_closed_at"] is None
                    and bool(last_episode["mitigated_segments"])
                ),
                "truly_restored": (
                    last_episode is None or last_episode["risk_closed_at"] is not None
                ),
                "episodes": episodes,
            })
        return view

    def _build_evidence_view(self) -> list[dict[str, Any]]:
        items = []
        for evidence in sorted(self._evidence.values(), key=lambda x: (x.reported_at, x.evidence_id)):
            latest = self._latest_version(evidence.facility_id, at=self._as_of)
            items.append({
                "evidence_id": evidence.evidence_id,
                "facility_id": evidence.facility_id,
                "version": evidence.version,
                "summary": evidence.summary,
                "reported_by": evidence.reported_by,
                "reported_at": iso(evidence.reported_at),
                "confirmed_by": evidence.confirmed_by or None,
                "confirmed_at": iso(evidence.confirmed_at) if evidence.confirmed_at else None,
                "is_latest_version": latest is not None and latest.evidence_id == evidence.evidence_id,
            })
        return items

    def _build_timeline(self, records: list[EventEnvelope]) -> list[dict[str, Any]]:
        summaries = {
            "facility_fault": lambda e: f"{e.unit_id} 上报故障：{e.payload['facility_id']}",
            "facility_restored": lambda e: (
                f"{e.unit_id} 标记恢复：{e.payload['facility_id']}"
                + ("（真正恢复）" if e.payload["restoration_kind"] == "real" else "（临时绕行）")
            ),
            "decision": lambda e: f"{e.unit_id} 决策：{e.payload['summary']}",
            "resource_dispatch": lambda e: (
                f"{e.unit_id} 调拨资源 {e.payload['resource']} → {e.payload['to_unit']}"
            ),
            "evidence_reported": lambda e: (
                f"{e.unit_id} 提交证据 v{e.payload['version']}：{e.payload['summary']}"
            ),
            "evidence_confirmed": lambda e: (
                f"{e.unit_id} 确认证据 {e.payload['evidence_id']} v{e.payload['version']}"
            ),
            "activate_detour": lambda e: (
                f"{e.unit_id} 启用绕行 {e.payload['primary']} → {e.payload['backup']}"
            ),
            "detour_reverted": lambda e: (
                f"{e.unit_id} 归还绕行 {e.payload['primary']} → {e.payload['backup']}"
            ),
            "retract_event": lambda e: (
                f"{e.unit_id} 纠正误报：{e.payload['target_event_id']}（{e.payload['reason']}）"
            ),
            "dispute_opened": lambda e: f"{e.unit_id} 发起会商：{e.payload['fact']}",
            "dispute_position": lambda e: (
                f"{e.unit_id} 会商立场 {e.payload['dispute_id']}：{e.payload['position']}"
            ),
            "dispute_resolved": lambda e: (
                f"{e.unit_id} 会商裁决 {e.payload['dispute_id']}：{e.payload['winning']}"
            ),
            "action_registered": lambda e: (
                f"登记整改 {e.event_id}（发现 {e.payload['finding_id']}）：{e.payload['title']}"
            ),
            "action_accepted": lambda e: (
                f"{e.payload['accepted_by']} 接受整改 {e.payload['action_id']}"
            ),
            "action_verified": lambda e: (
                f"{e.unit_id} 验证关闭整改 {e.payload['action_id']}"
            ),
            "exercise_paused": lambda e: "演练中断",
            "exercise_resumed": lambda e: "演练重启",
        }
        timeline = []
        for record in records:
            renderer = summaries.get(record.kind)
            timeline.append({
                "occurred_at": record.occurred_at,
                "reported_at": record.reported_at,
                "event_id": record.event_id,
                "unit_id": record.unit_id,
                "kind": record.kind,
                "summary": renderer(record) if renderer else record.kind,
            })
        return timeline

    def _build_findings(
        self,
        services: list[dict[str, Any]],
        detours: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []

        def add(fid: str, severity: str, title: str, detail: dict[str, Any]) -> None:
            findings.append({
                "finding_id": fid,
                "severity": severity,
                "title": title,
                "detail": detail,
            })

        for svc in services:
            for episode in svc["episodes"]:
                start = episode["risk_window"]["start"]
                compact = start.replace("-", "").replace(":", "").rstrip("Z") + "Z"
                base_detail = {
                    "service_id": svc["service_id"],
                    "episode_index": episode["index"],
                    "risk_window": episode["risk_window"],
                }
                if episode["rto_breached"]:
                    add(
                        f"F/RTO/{svc['service_id']}/{compact}",
                        "high",
                        f"关键服务 {svc['name']} 首次可用时间超出 RTO",
                        {
                            **base_detail,
                            "time_to_first_availability_active_seconds":
                                episode["time_to_first_availability_active_seconds"],
                            "rto_seconds": episode["rto_seconds"],
                        },
                    )
                if episode["first_available_kind"] == "detour":
                    add(
                        f"F/DETOUR/{svc['service_id']}/{compact}",
                        "medium",
                        f"服务 {svc['name']} 先靠临时绕行/迁移恢复，真正恢复滞后",
                        {
                            **base_detail,
                            "first_available_at": episode["first_available_at"],
                            "risk_closed_at": episode["risk_closed_at"],
                            "root_facilities": episode["root_facilities"],
                        },
                    )
                if episode["risk_closed_at"] is None:
                    if episode["mitigated_segments"]:
                        add(
                            f"F/OPEN-DETOUR/{svc['service_id']}/{compact}",
                            "high",
                            f"服务 {svc['name']} 仅靠临时绕行维持，风险未真正解除",
                            base_detail,
                        )
                    else:
                        add(
                            f"F/OPEN/{svc['service_id']}/{compact}",
                            "high",
                            f"服务 {svc['name']} 至评估时刻仍未恢复",
                            base_detail,
                        )
                elif not episode["evidence_refs"]:
                    add(
                        f"F/UNVERIFIED/{svc['service_id']}/{compact}",
                        "medium",
                        f"服务 {svc['name']} 真正恢复但缺少已确认证据版本",
                        {**base_detail, "root_facilities": episode["root_facilities"]},
                    )

        for dispute in self._disputes.values():
            if dispute.winning == "fault":
                compact = dispute.opened_at.strftime("%Y%m%dT%H%M%SZ")
                add(
                    f"F/DISPUTE/{dispute.facility_id or 'MANUAL'}/{compact}",
                    "high" if not dispute.manual else "medium",
                    f"设施 {dispute.facility_id} 会商结论为仍失效或超期未决",
                    {
                        "dispute_id": dispute.dispute_id,
                        "resolution": dispute.resolution,
                        "deadline": iso(dispute.deadline),
                    },
                )

        for detour in detours:
            if detour["still_active"]:
                add(
                    f"F/DETOUR-OPEN/{detour['primary']}/{detour['backup']}",
                    "medium",
                    f"绕行 {detour['primary']}→{detour['backup']} 至评估时刻仍未归还",
                    {"primary": detour["primary"], "backup": detour["backup"]},
                )

        return sorted(findings, key=lambda f: f["finding_id"])

    def _link_actions(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        known = {f["finding_id"] for f in findings}
        result = []
        for action in sorted(self._actions.values(), key=lambda a: (a["registered_at"], a["action_id"])):
            action["finding_known"] = action["finding_id"] in known
            result.append(action)
            if not action["finding_known"]:
                self.warnings.append(
                    f"整改 {action['action_id']} 引用的发现 {action['finding_id']} "
                    f"不在本次重放生成的发现清单中（外部登记）"
                )
        return result

    def _build_conclusions(self, services: list[dict[str, Any]]) -> list[dict[str, Any]]:
        conclusions = []
        for svc in services:
            for episode in svc["episodes"]:
                common = {
                    "service_id": svc["service_id"],
                    "episode_index": episode["index"],
                    "risk_window": episode["risk_window"],
                    "first_available_at": episode["first_available_at"],
                    "first_available_kind": episode["first_available_kind"],
                    "risk_closed_at": episode["risk_closed_at"],
                    "cited_evidence": episode["evidence_refs"],
                }
                if episode["risk_closed_at"] is None:
                    if episode["mitigated_segments"]:
                        conclusions.append({
                            **common,
                            "type": "temporary_mitigation",
                            "statement": (
                                f"关键服务 {svc['name']} 目前仅由临时绕行/迁移支撑，"
                                "旅客与物资保障风险被缓解但未真正解除，"
                                "不得据此作出风险解除结论"
                            ),
                        })
                    else:
                        conclusions.append({
                            **common,
                            "type": "risk_open",
                            "statement": (
                                f"关键服务 {svc['name']} 的失效区间仍开放，"
                                "不能作出风险解除结论"
                            ),
                        })
                elif episode["evidence_refs"]:
                    conclusions.append({
                        **common,
                        "type": "risk_lifted",
                        "statement": (
                            f"关键服务 {svc['name']} 的风险于 "
                            f"{episode['risk_closed_at']} 真正解除"
                            + (
                                f"（期间 {episode['first_available_at']} 起曾由绕行缓解）"
                                if episode["first_available_kind"] == "detour" else ""
                            )
                            + "，结论引用已确认证据版本"
                        ),
                    })
                else:
                    conclusions.append({
                        **common,
                        "type": "risk_closed_unverified",
                        "statement": (
                            f"关键服务 {svc['name']} 虽于 {episode['risk_closed_at']} 恢复，"
                            "但缺少已确认证据版本，结论暂不生效"
                        ),
                    })
        return conclusions

    def _build_plan_vs_actual(
        self,
        business: dict[str, list[Interval]],
        services: list[dict[str, Any]],
    ) -> dict[str, Any]:
        plan_service_order = [
            s.service_id
            for s in sorted(
                self.scenario.services, key=lambda s: (s.rto_seconds, s.service_id)
            )
        ]

        def svc_episodes(service_id: str) -> list[dict[str, Any]]:
            return next(s["episodes"] for s in services if s["service_id"] == service_id)

        actual_rows = []
        for sid in plan_service_order:
            episodes = svc_episodes(sid)
            first_available = min(
                (e["first_available_at"] for e in episodes if e["first_available_at"]),
                default=None,
            )
            first_real = min(
                (e["risk_closed_at"] for e in episodes if e["risk_closed_at"]),
                default=None,
            )
            actual_rows.append({
                "service_id": sid,
                "first_available_at": first_available,
                "first_available_kind": next(
                    (e["first_available_kind"] for e in episodes
                     if e["first_available_at"] == first_available and first_available),
                    None,
                ),
                "first_real_restored_at": first_real,
                "on_detour_only": any(
                    e["risk_closed_at"] is None and e["mitigated_segments"]
                    for e in episodes
                ),
            })

        availability_order = [
            row["service_id"]
            for row in sorted(
                actual_rows,
                key=lambda r: (r["first_available_at"] is None, r["first_available_at"] or "",
                               r["service_id"]),
            )
        ]
        real_order = [
            row["service_id"]
            for row in sorted(
                actual_rows,
                key=lambda r: (r["first_real_restored_at"] is None,
                               r["first_real_restored_at"] or "", r["service_id"]),
            )
        ]

        def inversions(order_plan: list[str], order_actual: list[str]) -> list[dict[str, Any]]:
            rank = {sid: i for i, sid in enumerate(order_actual)}
            result = []
            for i, left in enumerate(order_plan):
                for right in order_plan[i + 1:]:
                    if rank.get(left, 10**9) > rank.get(right, 10**9):
                        result.append({
                            "planned_first": left,
                            "planned_then": right,
                            "actual_order": [right, left],
                        })
            return result

        # 设施级：计划顺序按所支撑服务最小 RTO，实际顺序按物理真正恢复时刻。
        closures = {
            f.facility_id: self.scenario.propagation_closure({f.facility_id})
            for f in self.scenario.facilities
        }
        support: dict[str, int] = {}
        for facility in self.scenario.facilities:
            rtos = [
                svc.rto_seconds for svc in self.scenario.services
                if closures[facility.facility_id] & set(svc.depends_on)
            ]
            support[facility.facility_id] = min(rtos, default=10**12)

        affected = [fid for fid, state in self._fac.items() if state.outages]
        plan_facility_order = sorted(affected, key=lambda fid: (support[fid], fid))

        def real_close(fid: str) -> tuple[int, str, str]:
            ends = [o.end for o in self._fac[fid].outages if o.end is not None]
            return (0, iso(max(ends)), fid) if ends else (1, "", fid)

        actual_facility_order = sorted(affected, key=real_close)
        not_restored = sorted(
            fid for fid in affected if self._fac[fid].outages[-1].end is None
        )
        on_detour_only = sorted(
            fid for fid in not_restored
            if self._own_effective[fid] and self._own_effective[fid][-1].end is not None
        )

        return {
            "plan_service_order": plan_service_order,
            "actual_first_availability_order": availability_order,
            "actual_real_restoration_order": real_order,
            "availability_order_inversions": inversions(
                plan_service_order, availability_order
            ),
            "services": sorted(
                actual_rows, key=lambda r: plan_service_order.index(r["service_id"])
            ),
            "plan_facility_order": plan_facility_order,
            "actual_facility_order": actual_facility_order,
            "facility_order_inversions": inversions(
                plan_facility_order, actual_facility_order
            ),
            "facilities_not_truly_restored": not_restored,
            "facilities_on_detour_only": on_detour_only,
        }


    def _dispute_dict(self, dispute: _Dispute) -> dict[str, Any]:
        return {
            "dispute_id": dispute.dispute_id,
            "facility_id": dispute.facility_id or None,
            "fact": dispute.fact,
            "opened_at": iso(dispute.opened_at),
            "deadline": iso(dispute.deadline),
            "positions": dispute.positions,
            "status": (
                "open" if dispute.resolved_at is None
                else "resolved" if dispute.resolved_at <= dispute.deadline
                else "resolved_late"
            ),
            "winning": dispute.winning or None,
            "resolution": dispute.resolution or None,
            "resolved_at": iso(dispute.resolved_at) if dispute.resolved_at else None,
            "cited_evidence": (
                {"evidence_id": dispute.evidence_id, "version": dispute.evidence_version}
                if dispute.evidence_id else None
            ),
            "auto_opened": not dispute.manual,
        }

    def _fingerprint(self, report: dict[str, Any]) -> str:
        # 业务重放指纹只取决于场景与有效事件集合（时间线已按发生时间
        # 排序），必须与上报到达顺序无关；journal_tail_hash 是到达顺序
        # 的完整性凭证，另行通过 /verify 校验，不纳入重放指纹。
        excluded = {"replay_fingerprint", "journal_tail_hash"}
        canonical = json.dumps(
            {k: v for k, v in report.items() if k not in excluded},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()
