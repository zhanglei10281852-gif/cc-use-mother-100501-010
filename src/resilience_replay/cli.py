"""命令行入口：冻结场景、上报事件、重放复盘、启动 API。

示例：

    python run_cli.py init-demo --data ./data
    python run_cli.py replay EX-2026-ND-HUB-01
    python run_cli.py replay EX-2026-ND-HUB-01 --format json --section services
    python run_cli.py serve --data ./data --port 8080
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import demo_data
from .engine import DEFAULT_DISPUTE_SLA_SECONDS
from .errors import DomainError
from .platform import ResiliencePlatform


# ---------------------------------------------------------------- 文本报告

def render_text(rep: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add(f"演练 {rep['exercise_code']}  场景修订 {rep['scenario_revision']}")
    add(f"场景指纹 {rep['scenario_fingerprint'][:16]}…  "
        f"日志尾哈希 {rep['journal_tail_hash'][:16]}…  评估至 {rep['as_of']}")
    add(f"事件记录 {rep['records_total']} 条（截至评估时刻有效 {rep['records_active']}，"
        f"纠正 {rep['records_retracted']}"
        + (f"，评估时刻之后 {rep['records_beyond_as_of']} 条未纳入"
           if rep.get("records_beyond_as_of") else "")
        + f"）  重放指纹 {rep['replay_fingerprint'][:16]}…")
    add("=" * 72)

    add("\n【关键服务：哪项恢复真正解除风险】")
    for svc in rep["services"]:
        add(f"  ● {svc['service_id']} {svc['name']}（责任：{svc['owner_unit']}，"
            f"RTO {svc['rto_seconds']}s）")
        if not svc["episodes"]:
            add("    全程未受影响")
            continue
        for ep in svc["episodes"]:
            window = ep["risk_window"]
            kind_label = {"detour": "临时绕行/迁移", "real": "真正恢复"}.get(
                ep["first_available_kind"] or "", "—"
            )
            add(f"    风险窗口 {window['start']} → {window['end'] or '仍开放'}")
            add(f"      首次可用 {ep['first_available_at'] or '—'}（{kind_label}），"
                f"用时 {ep['time_to_first_availability_active_seconds']}s "
                f"[{'超 RTO' if ep['rto_breached'] else '满足 RTO'}]")
            if ep["mitigated_segments"]:
                for seg in ep["mitigated_segments"]:
                    via = "、".join(seg["via"])
                    add(f"      绕行缓解 {seg['start']} → {seg['end'] or '仍在绕行'}（{via}）")
            if ep["risk_closed_at"]:
                if ep["evidence_refs"]:
                    refs = "，".join(
                        f"{x['facility_id']}:{x['evidence_id']} v{x['version']}"
                        f"（{x['confirmed_by']} 确认）"
                        for x in ep["evidence_refs"]
                    )
                    add(f"      ✓ 风险于 {ep['risk_closed_at']} 真正解除，引用证据：{refs}")
                else:
                    add(f"      △ {ep['risk_closed_at']} 物理恢复但缺少已确认证据，结论不生效")
            elif ep["mitigated_segments"]:
                add("      ✗ 截至评估时刻仅靠绕行维持，旅客与物资保障风险未真正解除")
            else:
                add("      ✗ 截至评估时刻仍未恢复")

    add("\n【会商】")
    if not rep["disputes"]:
        add("  无")
    for d in rep["disputes"]:
        deadline_state = {
            "open": "进行中",
            "resolved": "按期裁决",
            "resolved_late": "超期后裁决",
        }[d["status"]]
        cited = d["cited_evidence"]
        cited_text = f"，证据 {cited['evidence_id']} v{cited['version']}" if cited else ""
        add(f"  {d['dispute_id']}（{deadline_state}{'，自动开启' if d['auto_opened'] else ''}）")
        add(f"    争议：{d['fact']}；期限 {d['deadline']}；结论：{d.get('winning') or '未决'}")
        if d["resolution"]:
            add(f"    裁决说明：{d['resolution']}{cited_text}")

    add("\n【发现 → 整改措施 追溯链】")
    if not rep["findings"]:
        add("  无发现")
    action_by_finding: dict[str, list[dict[str, Any]]] = {}
    for action in rep["corrective_actions"]:
        action_by_finding.setdefault(action["finding_id"], []).append(action)
    for finding in rep["findings"]:
        add(f"  [{finding['severity']}] {finding['finding_id']}  {finding['title']}")
        for action in action_by_finding.get(finding["finding_id"], []):
            evidence = action["closed_evidence"]
            chain = (
                f"登记 {action['registered_at']} → "
                f"接受 {action['accepted_by']} @ {action['accepted_at']} → "
                f"验证关闭 @ {action['verified_at']}"
                if action["status"] == "verified"
                else f"登记 {action['registered_at']} → 状态 {action['status']}"
            )
            add(f"      └ 整改 {action['action_id']}：{action['title']}")
            add(f"        {chain}")
            if evidence:
                add(f"        关闭证据 {evidence['evidence_id']} v{evidence['version']}"
                    f"（{evidence['confirmed_by']} 确认）")
    orphan = [a for a in rep["corrective_actions"] if not a["finding_known"]]
    for action in orphan:
        add(f"  ! 整改 {action['action_id']} 引用的发现 {action['finding_id']} 不在本报告清单")

    add("\n【计划恢复顺序 vs 实际】")
    pva = rep["plan_vs_actual"]
    add(f"  计划：{' → '.join(pva['plan_service_order'])}")
    add(f"  实际首可用：{' → '.join(pva['actual_first_availability_order'])}")
    add(f"  实际真正恢复：{' → '.join(pva['actual_real_restoration_order'])}")
    for inv in pva["availability_order_inversions"]:
        add(f"  ⚠ 顺序倒置：计划 {inv['planned_first']} 先于 {inv['planned_then']}，"
            f"实际相反")
    if pva["facilities_on_detour_only"]:
        add(f"  仅绕行未真正恢复设施：{', '.join(pva['facilities_on_detour_only'])}")
    if pva["facilities_not_truly_restored"]:
        add(f"  完全未恢复设施：{', '.join(pva['facilities_not_truly_restored'])}")

    if rep["warnings"]:
        add("\n【数据与过程告警】")
        for warning in rep["warnings"]:
            add(f"  · {warning}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 参数解析

def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default="./data", help="演练数据目录（默认 ./data）")

    parser = argparse.ArgumentParser(
        prog="resilience-replay",
        description="跨网络韧性演练与复盘平台",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", parents=[common], help="列出演练")

    p_freeze = sub.add_parser("freeze", parents=[common], help="从 JSON 文件冻结场景")
    p_freeze.add_argument("--file", required=True)

    sub.add_parser("init-demo", parents=[common], help="写入内置国庆枢纽演练场景与事件")

    p_events = sub.add_parser("events", parents=[common], help="查看追加日志（含被纠正原始记录）")
    p_events.add_argument("exercise")

    p_report = sub.add_parser("report", parents=[common], help="上报单条事件（JSON 文件）")
    p_report.add_argument("exercise")
    p_report.add_argument("--file", required=True)

    p_ingest = sub.add_parser("ingest", parents=[common], help="批量上报事件（JSON 数组文件，乱序亦可）")
    p_ingest.add_argument("exercise")
    p_ingest.add_argument("--file", required=True)

    p_replay = sub.add_parser("replay", parents=[common], help="确定性重放整场演练")
    p_replay.add_argument("exercise")
    p_replay.add_argument("--as-of", default=None, help="评估截止时间（ISO8601）")
    p_replay.add_argument("--format", choices=("text", "json"), default="text")
    p_replay.add_argument(
        "--section",
        choices=(
            "services", "facilities", "detours", "disputes", "evidence", "timeline",
            "plan_vs_actual", "findings", "corrective_actions", "conclusions",
            "propagation", "warnings",
        ),
        default=None,
    )
    p_replay.add_argument("--sla-seconds", type=int, default=DEFAULT_DISPUTE_SLA_SECONDS)

    p_verify = sub.add_parser("verify", parents=[common], help="校验场景指纹与事件哈希链")
    p_verify.add_argument("exercise")

    p_serve = sub.add_parser("serve", parents=[common], help="启动 HTTP API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    platform = ResiliencePlatform(args.data)

    try:
        if args.command == "list":
            for code in platform.list_exercises():
                print(code)
            return 0

        if args.command == "freeze":
            result = platform.freeze_scenario(_load_json(args.file))
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.command == "init-demo":
            result = platform.freeze_scenario(demo_data.scenario_dict())
            print(json.dumps(result, ensure_ascii=False))
            counts = platform.ingest_many(
                demo_data.scenario_dict()["exercise_code"], demo_data.events()
            )
            print(json.dumps(counts, ensure_ascii=False))
            return 0

        if args.command == "events":
            print(json.dumps(platform.list_events(args.exercise), ensure_ascii=False, indent=2))
            return 0

        if args.command == "report":
            event = _load_json(args.file)
            result = platform.report_event(args.exercise, **event)
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if args.command == "ingest":
            events = _load_json(args.file)
            print(json.dumps(platform.ingest_many(args.exercise, events), ensure_ascii=False))
            return 0

        if args.command == "verify":
            print(json.dumps(platform.verify_integrity(args.exercise), ensure_ascii=False, indent=2))
            return 0

        if args.command == "replay":
            rep = platform.replay(
                args.exercise, as_of=args.as_of, dispute_sla_seconds=args.sla_seconds
            )
            if args.format == "json":
                payload = rep if args.section is None else rep[args.section]
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(render_text(rep))
            return 0

        if args.command == "serve":
            from .api import serve
            serve(args.data, host=args.host, port=args.port, verbose=True)
            return 0

    except DomainError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
