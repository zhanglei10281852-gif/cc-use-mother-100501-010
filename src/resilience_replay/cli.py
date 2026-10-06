"""命令行：构建演示、重放演练、查看时间线/失效区间/计划偏差/整改链路、校验日志链。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .demo import build_demo
from .engine import (
    FACILITY_BYPASSED,
    FACILITY_DEGRADED,
    FACILITY_FAILED,
    SERVICE_OUTAGE,
    ReplayResult,
)
from .events import EventLog
from .jsonio import canonical_dumps
from .scenario import scenario_from_dict
from .service import ExerciseService
from .scenario import ScenarioState


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _fmt_minutes(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    m = seconds / 60.0
    if abs(m - round(m)) < 0.05:
        return f"{round(m)} 分钟"
    return f"{m:.1f} 分钟"


def _header(text: str) -> None:
    print(f"\n== {text} ==")


def _result(service: ExerciseService, code: str, revision: str, as_of: str | None) -> ReplayResult:
    return service.replay(code, revision, as_of=as_of)


# ---------------------------------------------------------------------------
# 各子命令
# ---------------------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    service = ExerciseService(args.data_dir)
    stats = build_demo(service)
    print(f"已构建演示演练 {stats['exercise']}@{stats['revision']}")
    print(f"事件接入 {stats['ingested']} 条，拒绝重复/无效上报 {stats['rejected']} 条")
    print(f"存储目录: {args.data_dir}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    service = ExerciseService(args.data_dir)
    for item in service.list_exercises():
        print(f"{item['exercise_code']}@{item['revision']}  {item['state']}  frozen_at={item['frozen_at']}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    service = ExerciseService(args.data_dir)
    records = service.events(args.code, args.revision)
    if args.json:
        _print_json([
            {
                "seq": r.sequence,
                "event_id": r.event_id,
                "type": r.event_type,
                "occurred_at": r.occurred_at,
                "reported_at": r.reported_at,
                "unit_id": r.unit_id,
                "payload": r.payload,
                "hash": r.record_hash[:10],
            }
            for r in records
        ])
        return 0
    for r in records:
        late = "" if r.reported_at == r.occurred_at else f"  (迟报，上报于 {r.reported_at})"
        print(f"[{r.sequence:02d}] {r.occurred_at} {r.event_type:<22} {r.unit_id:<10} {r.event_id}{late}")
        if args.verbose:
            print(f"     {canonical_dumps(r.payload)}")
    print(f"共 {len(records)} 条，链头 {records[-1].record_hash[:12] if records else 'GENESIS'}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    service = ExerciseService(args.data_dir)
    path = service._events_path(args.code, args.revision)  # noqa: SLF001
    log = EventLog.load_jsonl(path, verify_chain=True)
    print(f"哈希链校验通过：{path}（{len(log)} 条记录，链头 {log.head_hash[:12]}）")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    service = ExerciseService(args.data_dir)
    if args.scenario and args.events_file:
        with open(args.scenario, "r", encoding="utf-8") as fh:
            scenario = scenario_from_dict(json.load(fh))
        if scenario.state != ScenarioState.FROZEN.value:
            print("错误：场景文件不是冻结版本", file=sys.stderr)
            return 2
        from .engine import replay

        result = replay(scenario, EventLog.load_jsonl(args.events_file), as_of=args.as_of)
    else:
        result = _result(service, args.code, args.revision, args.as_of)
    _print_json(result.to_dict())
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    result = _result(ExerciseService(args.data_dir), args.code, args.revision, args.as_of)
    if args.json:
        _print_json(result.timeline)
        return 0
    for item in result.timeline:
        late = " 迟报" if item["reported_at"] != item["at"] else ""
        print(f"{item['at']}  {item['type']:<20}{late}  {item['unit_id']}")
        if item["effect"]:
            print(f"    -> {item['effect']}")
    if result.interrupted_segments:
        _header("中断区间")
        for start, end in result.interrupted_segments:
            print(f"{start} ~ {end}（重放结果不受影响）")
    if result.warnings:
        _header("重放告警")
        for w in result.warnings:
            print(f"- {w}")
    return 0


def cmd_services(args: argparse.Namespace) -> int:
    result = _result(ExerciseService(args.data_dir), args.code, args.revision, args.as_of)
    if args.json:
        _print_json(result.conclusion["key_service_outcomes"])
        return 0
    for svc in result.scenario.services:
        ivs = result.service_intervals.get(svc.service_id, [])
        _header(f"{svc.service_id} {svc.name}")
        print(f"目标: {svc.target}（容量阈值 {svc.required_capacity_ratio:.0%}）")
        if not ivs:
            print("全程满足目标，无失效区间")
        for iv in ivs:
            if iv.state == SERVICE_OUTAGE:
                print(f"  失效 {iv.start_at} -> {iv.end_at or '（未解除）'}  持续 {_fmt_minutes(iv.duration_seconds() or 0)}")
        for lift in (e for e in result.lift_events if e.service_id == svc.service_id):
            kind = "临时绕行" if lift.mode == "workaround" else "真正恢复"
            print(f"  [{kind}] {lift.lifted_at}  证据版本: {', '.join(lift.evidence_ids) or '（无确认证据）'}")
            print(f"      {lift.detail}")
    _header("设施状态区间（风险传播）")
    for fid, ivs in sorted(result.facility_intervals.items()):
        for iv in ivs:
            label = {FACILITY_FAILED: "失效", FACILITY_BYPASSED: "绕行", FACILITY_DEGRADED: "降级"}.get(iv.state, iv.state)
            print(f"  {fid:<10} {label} {iv.start_at} -> {iv.end_at}  ({iv.reason})")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    result = _result(ExerciseService(args.data_dir), args.code, args.revision, args.as_of)
    pc = result.plan_comparison
    if args.json:
        _print_json(pc)
        return 0
    _header("计划顺序")
    print(" -> ".join(pc["planned_order"]) or "（无计划）")
    _header("实际顺序")
    print(" -> ".join(pc["actual_order"]) or "（尚无步骤实际完成）")
    _header("步骤偏差")
    for row in pc["steps"]:
        delta = row["delta_minutes"]
        delta_txt = "未执行" if delta is None else (f"{'滞后' if delta > 0 else '提前'} {abs(delta)} 分钟")
        print(
            f"  {row['step_id']:<10} 设施 {row['target_facility_id']:<10} "
            f"计划 T+{row['planned_offset_minutes']}min  实际 {row['actual_at'] or '-'}  {delta_txt}"
        )
    for inv in pc["order_inversions"]:
        print(f"  顺序颠倒: 实际先执行 {inv['earlier_than_planned']}，晚于计划的 {inv['later_than_planned']} 反而在后")
    if pc["steps_not_executed"]:
        print(f"  未执行步骤: {', '.join(pc['steps_not_executed'])}")
    return 0


def cmd_findings(args: argparse.Namespace) -> int:
    result = _result(ExerciseService(args.data_dir), args.code, args.revision, args.as_of)
    if args.json:
        _print_json([f.__dict__ for f in result.findings])
        return 0
    for f in result.findings:
        src = "自动派生" if f.source == "auto" else "人工登记"
        print(f"[{f.severity.upper():<8}] {f.finding_id} {f.title}  ({src}, {f.created_at})")
        print(f"    {f.description}")
        print(f"    触发事件: {', '.join(f.triggering_event_ids) or '-'}")
        linked = [r for r in result.remediations.values() if r.finding_id == f.finding_id]
        for r in linked:
            print(f"    -> 整改 {r.remediation_id} 状态={r.status} 责任单位={r.owner_unit}")
    if not result.findings:
        print("无发现")
    return 0


def cmd_remediations(args: argparse.Namespace) -> int:
    result = _result(ExerciseService(args.data_dir), args.code, args.revision, args.as_of)
    if args.json:
        _print_json(result.to_dict()["remediations"])
        return 0
    finding_titles = {f.finding_id: f.title for f in result.findings}
    for r in sorted(result.remediations.values(), key=lambda x: x.created_at):
        print(f"{r.remediation_id}  [{r.status}]")
        print(f"  措施: {r.action}")
        print(f"  由发现触发: {r.finding_id}（{finding_titles.get(r.finding_id, '未知发现')}）")
        print(f"  责任单位: {r.owner_unit}  登记于 {r.created_at}（{r.created_event.event_id}）  期限 {r.due_at or '-'}")
        if r.accepted_at:
            print(f"  接受: {r.accepted_by} 于 {r.accepted_at}（{r.accepted_event.event_id if r.accepted_event else '-'}）")
        if r.verified_at:
            print(f"  验证关闭: {r.verified_by} 于 {r.verified_at}（{r.closed_event.event_id if r.closed_event else '-'}）")
    if not result.remediations:
        print("无整改措施")
    return 0


def cmd_consultations(args: argparse.Namespace) -> int:
    result = _result(ExerciseService(args.data_dir), args.code, args.revision, args.as_of)
    for c in sorted(result.consultations.values(), key=lambda x: x.opened_at):
        print(f"{c.consultation_id}  [{c.status}]")
        print(f"  议题: {c.subject}")
        print(f"  开启 {c.opened_at}  期限 {c.deadline_at}  各方: {', '.join(c.parties)}")
        if c.resolved_at:
            print(f"  裁决于 {c.resolved_at}；确认证据版本: {c.resolution_evidence_id or '-'}")
            print(f"  一致事实: {c.agreed_facts}")
        print(f"  主张事件: {', '.join(c.claim_event_ids)}")
    if not result.consultations:
        print("无会商")
    return 0


def cmd_conclusion(args: argparse.Namespace) -> int:
    result = _result(ExerciseService(args.data_dir), args.code, args.revision, args.as_of)
    if args.json:
        _print_json(result.conclusion)
        return 0
    c = result.conclusion
    print(c["title"])
    print(f"演练 {c['exercise_code']}  场景版本 {c['scenario_revision']}")
    print(f"场景指纹 {c['scenario_fingerprint'][:16]}…  日志链头 {c['evidence_basis']['log_head'][:16]}…")
    print("结论引用的确认证据版本:")
    for eid in c["evidence_basis"]["confirmed_evidence_versions"]:
        print(f"  - {eid}")
    for item in c["key_service_outcomes"]:
        total = sum((iv["seconds"] or 0) for iv in item["outage_intervals"])
        flag = "仍有残余风险" if item["residual_risk"] else "风险已解除"
        lifts = "；".join(f"{e['lifted_at']} {'绕行' if e['mode'] == 'workaround' else '恢复'}" for e in item["lifts"])
        print(f"  {item['service_id']}: {flag}，累计失效 {_fmt_minutes(total)}，解除节点: {lifts or '无'}")
    rs = c["remediation_summary"]
    print(f"整改: 待接受 {rs['registered']} / 已接受待验证 {rs['accepted_open']} / 已验证关闭 {rs['verified_closed']}")
    print(f"发现总数 {c['finding_count']}；最终结论成立: {'是' if c['final'] else '否'}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .api import main as api_main

    return api_main(["--host", args.host, "--port", str(args.port), "--data-dir", args.data_dir])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rr", description="跨网络韧性演练与复盘平台")
    parser.add_argument("--data-dir", default=".rr-data", help="持久化目录（默认 .rr-data）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="写入内置国庆枢纽演练演示数据").set_defaults(func=cmd_demo)
    sub.add_parser("list", help="列出演练场景版本").set_defaults(func=cmd_list)

    p_events = sub.add_parser("events", help="查看原始只追加日志")
    p_events.add_argument("code")
    p_events.add_argument("revision")
    p_events.add_argument("--json", action="store_true")
    p_events.add_argument("-v", "--verbose", action="store_true")
    p_events.set_defaults(func=cmd_events)

    p_verify = sub.add_parser("verify", help="校验事件日志哈希链")
    p_verify.add_argument("code")
    p_verify.add_argument("revision")
    p_verify.set_defaults(func=cmd_verify)

    def add_replay_args(p: argparse.ArgumentParser, files: bool = False) -> None:
        if files:
            p.add_argument("--scenario", help="冻结场景 JSON 文件")
            p.add_argument("--events-file", help="事件 JSONL 文件")
        p.add_argument("code", nargs="?")
        p.add_argument("revision", nargs="?")
        p.add_argument("--as-of", help="只重放发生时间不晚于该时刻的事件")

    p_replay = sub.add_parser("replay", help="重放整场演练并输出完整 JSON 结果")
    add_replay_args(p_replay, files=True)
    p_replay.set_defaults(func=cmd_replay)

    for name, help_text, fn, has_json in (
        ("timeline", "按发生时间重放的事件时间线", cmd_timeline, True),
        ("services", "关键服务失效区间与绕行/恢复判定", cmd_services, True),
        ("plan", "原计划与实际恢复顺序对比", cmd_plan, True),
        ("findings", "发现清单及其触发来源", cmd_findings, True),
        ("remediations", "整改措施登记/接受/验证关闭追踪", cmd_remediations, True),
        ("consultations", "有期限会商状态", cmd_consultations, False),
        ("conclusion", "引用确认证据版本的最终结论", cmd_conclusion, True),
    ):
        p = sub.add_parser(name, help=help_text)
        add_replay_args(p)
        p.set_defaults(func=fn)
        if has_json:
            p.add_argument("--json", action="store_true")

    p_serve = sub.add_parser("serve", help="启动 HTTP API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
