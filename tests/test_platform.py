"""平台端到端行为测试：确定性、不可篡改留痕、绕行/真恢复、会商、整改。"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from resilience_replay import demo_data
from resilience_replay.errors import (
    EventConflictError,
    EventValidationError,
    JournalIntegrityError,
    ScenarioValidationError,
)
from resilience_replay.platform import ResiliencePlatform
from resilience_replay.api import make_server

CODE = "EX-TEST"


def _scenario_dict(**overrides):
    data = demo_data.scenario_dict()
    data["exercise_code"] = CODE
    data = json.loads(json.dumps(data))
    data.update(overrides)
    return data


def _ev(event_id, unit, occurred, reported, kind, payload):
    return {
        "event_id": event_id, "unit_id": unit,
        "occurred_at": occurred, "reported_at": reported,
        "kind": kind, "payload": payload,
    }


class PlatformTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.platform = ResiliencePlatform(self.data_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def freeze(self, **overrides) -> str:
        self.platform.freeze_scenario(_scenario_dict(**overrides))
        return CODE

    def ingest(self, events):
        return self.platform.ingest_many(CODE, events)

    def replay(self, **kwargs):
        return self.platform.replay(CODE, **kwargs)


class ScenarioFreezeTests(PlatformTestBase):
    def test_dangling_reference_rejected(self) -> None:
        with self.assertRaises(ScenarioValidationError):
            self.freeze(dependencies=[{"upstream": "F-SUB", "downstream": "F-GHOST"}])

    def test_dependency_cycle_rejected(self) -> None:
        deps = list(demo_data.DEPENDENCIES) + [
            {"upstream": "F-COMPUTE", "downstream": "F-SUB"}
        ]
        with self.assertRaises(ScenarioValidationError):
            self.freeze(dependencies=deps)

    def test_window_must_be_ordered(self) -> None:
        alts = json.loads(json.dumps(demo_data.ALTERNATIVES))
        alts[0]["valid_to"] = alts[0]["valid_from"]
        with self.assertRaises(ScenarioValidationError):
            self.freeze(alternatives=alts)

    def test_facility_owner_must_have_role(self) -> None:
        facilities = json.loads(json.dumps(demo_data.FACILITIES))
        facilities[0]["owner_unit"] = "GHOST-UNIT"
        with self.assertRaises(ScenarioValidationError):
            self.freeze(facilities=facilities)

    def test_frozen_scenario_cannot_be_overwritten(self) -> None:
        self.freeze()
        with self.assertRaises(FileExistsError):
            self.platform.freeze_scenario(_scenario_dict())


class JournalTests(PlatformTestBase):
    def test_idempotent_duplicate_conflict_and_retraction(self) -> None:
        self.freeze()
        event = _ev("E1", "POWER", "2026-09-30T08:00:00Z", "2026-09-30T08:00:10Z",
                    "facility_fault", {"facility_id": "F-SUB"})
        first = self.platform.report_event(CODE, **event)
        second = self.platform.report_event(CODE, **event)
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "duplicate")

        tampered = dict(event, reported_at="2026-09-30T09:00:00Z")
        with self.assertRaises(EventConflictError):
            self.platform.report_event(CODE, **tampered)

        self.platform.report_event(
            CODE, **_ev("E2", "POWER", "2026-09-30T08:30:00Z", "2026-09-30T08:30:10Z",
                        "retract_event", {"target_event_id": "E1", "reason": "误报"})
        )
        events = self.platform.list_events(CODE)
        # 原始记录仍在，未被抹去。
        self.assertEqual([e["event_id"] for e in events], ["E1", "E2"])
        report = self.replay()
        self.assertEqual(report["records_total"], 2)
        self.assertEqual(report["records_active"], 1)
        self.assertEqual(report["records_retracted"], 1)

    def test_unknown_unit_rejected(self) -> None:
        self.freeze()
        with self.assertRaises(EventValidationError):
            self.platform.report_event(
                CODE, **_ev("X", "OUTSIDER", "2026-09-30T08:00:00Z",
                            "2026-09-30T08:00:10Z",
                            "facility_fault", {"facility_id": "F-SUB"})
            )

    def test_occurred_after_reported_rejected(self) -> None:
        self.freeze()
        with self.assertRaises(EventValidationError):
            self.ingest([_ev("X", "POWER", "2026-09-30T09:00:00Z",
                             "2026-09-30T08:00:00Z",
                             "facility_fault", {"facility_id": "F-SUB"})])

    def test_hash_chain_detects_tampering(self) -> None:
        self.freeze()
        self.ingest([
            _ev("E1", "POWER", "2026-09-30T08:00:00Z", "2026-09-30T08:00:10Z",
                "facility_fault", {"facility_id": "F-SUB"}),
        ])
        path = Path(self.data_dir) / CODE / "events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        record["payload"]["cause"] = "篡改原因"
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        with self.assertRaises(JournalIntegrityError):
            self.platform.replay(CODE)


class DeterminismTests(PlatformTestBase):
    def test_order_duplicates_and_restart_do_not_change_result(self) -> None:
        self.freeze()
        events = demo_data.events()
        # 1) 正常批量；2) 乱序批量；3) 逐条上报，三者结果必须一致。
        self.ingest(events)
        baseline = self.replay()

        shuffled = list(reversed(events[1:-1])) + [events[0], events[-1]]
        dir_b = tempfile.mkdtemp()
        pb = ResiliencePlatform(dir_b)
        pb.freeze_scenario(_scenario_dict())
        pb.ingest_many(CODE, shuffled)
        shuffled_report = pb.replay(CODE)

        dir_c = tempfile.mkdtemp()
        pc = ResiliencePlatform(dir_c)
        pc.freeze_scenario(_scenario_dict())
        for event in shuffled:
            pc.report_event(CODE, **event)
        one_by_one = pc.replay(CODE)

        # 重放结果只取决于事件集合与发生时间，与到达顺序无关。
        for rep_b, rep_c in ((shuffled_report, one_by_one),):
            self.assertEqual(baseline["replay_fingerprint"], rep_b["replay_fingerprint"])
            self.assertEqual(baseline["replay_fingerprint"], rep_c["replay_fingerprint"])
            self.assertEqual(baseline["services"], rep_b["services"])
            self.assertEqual(baseline["findings"], rep_c["findings"])

        # 中断重启（同一存储日志）连哈希链尾也必须完全一致。
        restarted = ResiliencePlatform(self.data_dir).replay(CODE)
        self.assertEqual(baseline["journal_tail_hash"], restarted["journal_tail_hash"])
        self.assertEqual(baseline["replay_fingerprint"], restarted["replay_fingerprint"])

    def test_pause_records_do_not_change_downtime_accounting(self) -> None:
        # 暂停/重启是组织动作：失效时长、RTO 判定与结论不得随之改变。
        events = [e for e in demo_data.events()
                  if e["kind"] not in ("exercise_paused", "exercise_resumed")]
        self.freeze()
        self.ingest(events)
        without_pause = self.replay()

        with_pause = tempfile.mkdtemp()
        pp = ResiliencePlatform(with_pause)
        pp.freeze_scenario(_scenario_dict())
        pp.ingest_many(CODE, demo_data.events())
        paused_report = pp.replay(CODE)

        for section in ("services", "findings", "conclusions", "plan_vs_actual", "disputes"):
            self.assertEqual(
                without_pause[section], paused_report[section], section
            )


class PropagationAndRestorationTests(PlatformTestBase):
    BASE = "2026-09-30T08:{:02d}:00Z"

    def test_transparent_detour_heals_downstream_but_is_not_real_recovery(self) -> None:
        self.freeze()
        t = self.BASE
        self.ingest([
            _ev("F1", "POWER", t.format(0), t.format(0), "facility_fault",
                {"facility_id": "F-SUB"}),
            _ev("D1", "POWER", t.format(2), t.format(2), "activate_detour",
                {"primary": "F-SUB", "backup": "F-GEN"}),
        ])
        rep = self.replay(as_of=t.format(10))
        compute = next(s for s in rep["services"] if s["service_id"] == "S-COMPUTE")
        ep = compute["episodes"][0]
        # 发电（透明）后业务恢复，但物理风险窗口仍开放。
        self.assertEqual(ep["first_available_kind"], "detour")
        self.assertIsNone(ep["risk_closed_at"])
        self.assertTrue(compute["on_detour_only"])
        # 算力节点自身没有故障，其物理失效是传播来的。
        facility = next(f for f in rep["facilities"] if f["facility_id"] == "F-COMPUTE")
        self.assertTrue(facility["propagated_outage"])
        self.assertEqual(facility["physical_outages"], [])

    def test_exclusive_detour_serves_only_switched_node(self) -> None:
        self.freeze()
        t = self.BASE
        self.ingest([
            _ev("F1", "POWER", t.format(0), t.format(0), "facility_fault",
                {"facility_id": "F-SUB"}),
            _ev("D1", "POWER", t.format(1), t.format(1), "activate_detour",
                {"primary": "F-SUB", "backup": "F-GEN"}),
            _ev("F2", "TELCO-A", t.format(3), t.format(3), "facility_fault",
                {"facility_id": "F-FIBER-A"}),
            # 只切微波，不切基站：基站 A 的回传由谁提供？场景里 FIBER-B->BS-B，
            # 因此 BS-A 不会被这条绕行自动治愈；这里验证排他语义。
            _ev("D2", "TELCO-A", t.format(4), t.format(4), "activate_detour",
                {"primary": "F-FIBER-A", "backup": "F-FIBER-B"}),
        ])
        rep = self.replay(as_of=t.format(20))
        comms = next(s for s in rep["services"] if s["service_id"] == "S-COMMS")
        ep = comms["episodes"][0]
        # 没有激活 BS-A->BS-B：发电让 01:00–03:00 短暂可用，
        # 但光缆 03:00 中断后通信再次失效，且最后一段必须仍开放。
        self.assertEqual(len(ep["unavailable_segments"]), 2)
        self.assertIsNone(ep["unavailable_segments"][-1]["end"])
        self.assertIsNone(ep["risk_closed_at"])

    def test_backup_failure_punches_hole_in_detour_coverage(self) -> None:
        self.freeze()
        t = self.BASE
        self.ingest([
            _ev("F1", "TELCO-A", t.format(0), t.format(0), "facility_fault",
                {"facility_id": "F-FIBER-A"}),
            _ev("D1", "TELCO-A", t.format(2), t.format(2), "activate_detour",
                {"primary": "F-FIBER-A", "backup": "F-FIBER-B"}),
            # 备用微波 05:00–08:00 自身故障，绕行中断、风险重新暴露。
            _ev("F2", "TELCO-B", t.format(5), t.format(5), "facility_fault",
                {"facility_id": "F-FIBER-B"}),
            _ev("R2", "TELCO-B", t.format(8), t.format(8), "facility_restored",
                {"facility_id": "F-FIBER-B", "restoration_kind": "real"}),
        ])
        rep = self.replay(as_of=t.format(12))
        detour = rep["detours"][0]
        covered = detour["effective_coverage"]
        # 02:00 激活，05–08 被备份故障挖洞 → 两段覆盖。
        ends = [(seg["start"], seg["end"]) for seg in covered]
        self.assertIn((t.format(2), t.format(5)), ends)
        self.assertIn((t.format(8), t.format(12)), ends)

    def test_real_restoration_closes_risk_window_with_confirmed_evidence(self) -> None:
        self.freeze()
        t = self.BASE
        self.ingest([
            _ev("F1", "POWER", t.format(0), t.format(0), "facility_fault",
                {"facility_id": "F-SUB"}),
            _ev("E1", "POWER", t.format(3), t.format(3), "evidence_reported",
                {"facility_id": "F-SUB", "version": 1, "summary": "复电"}),
            # 未确认就恢复：物理恢复但结论不能引用证据。
            _ev("R1", "POWER", t.format(4), t.format(4), "facility_restored",
                {"facility_id": "F-SUB", "restoration_kind": "real"}),
        ])
        rep = self.replay(as_of=t.format(9))
        finding_ids = {f["finding_id"] for f in rep["findings"]}
        self.assertTrue(any("UNVERIFIED" in fid for fid in finding_ids))
        conclusion = rep["conclusions"][0]
        self.assertEqual(conclusion["type"], "risk_closed_unverified")


class DisputeTests(PlatformTestBase):
    t = "2026-09-30T08:{:02d}:00Z"

    def _conflicting_reports(self):
        return [
            _ev("F1", "COMPUTE", self.t.format(0), self.t.format(0), "facility_fault",
                {"facility_id": "F-COMPUTE"}),
            _ev("R1", "HUB", self.t.format(2), self.t.format(2), "facility_restored",
                {"facility_id": "F-COMPUTE", "restoration_kind": "real"}),
        ]

    def test_conflicting_claims_open_timed_dispute_and_timeout_is_conservative(self) -> None:
        self.freeze()
        self.ingest(self._conflicting_reports())
        # 会商期限内未裁决 → 保守判定仍失效。
        rep = self.replay(as_of=self.t.format(30), dispute_sla_seconds=900)
        dispute = rep["disputes"][0]
        self.assertTrue(dispute["auto_opened"])
        self.assertEqual(dispute["winning"], "fault")
        # 超时裁决不应关闭物理失效区间。
        facility = next(f for f in rep["facilities"] if f["facility_id"] == "F-COMPUTE")
        self.assertTrue(facility["currently_down"])

    def test_resolution_must_cite_confirmed_evidence(self) -> None:
        self.freeze()
        events = self._conflicting_reports() + [
            _ev("E1", "COMPUTE", self.t.format(5), self.t.format(5), "evidence_reported",
                {"facility_id": "F-COMPUTE", "version": 1, "summary": "心跳缺失"}),
            # 引用未确认证据裁决为 restored → 按保守原则翻为 fault。
            _ev("D1", "HQ", self.t.format(6), self.t.format(6), "dispute_resolved",
                {"dispute_id": "D/F-COMPUTE/20260930T080200Z",
                 "resolution": "存疑裁决", "winning": "restored",
                 "evidence_id": "E1"}),
        ]
        self.ingest(events)
        rep = self.replay(as_of=self.t.format(20))
        dispute = rep["disputes"][0]
        self.assertEqual(dispute["winning"], "fault")


class CorrectiveActionTests(PlatformTestBase):
    def test_action_lifecycle_and_closure_requires_confirmed_evidence(self) -> None:
        self.freeze()
        t = "2026-09-30T{:02d}:00Z"
        self.ingest([
            _ev("F1", "POWER", t.format(8), t.format(8), "facility_fault",
                {"facility_id": "F-SUB"}),
            # 让 S-COMMS 超 RTO：很久之后才恢复。
            _ev("E1", "POWER", t.format(9), t.format(9), "evidence_reported",
                {"facility_id": "F-SUB", "version": 1, "summary": "复电核相正确"}),
            _ev("C1", "HQ", t.format(9), t.format(9), "evidence_confirmed",
                {"evidence_id": "E1", "version": 1}),
            _ev("R1", "POWER", t.format(9), t.format(9), "facility_restored",
                {"facility_id": "F-SUB", "restoration_kind": "real"}),
        ])
        rep = self.replay(as_of="2026-09-30T12:00:00Z")
        rto_finding = next(f for f in rep["findings"] if f["finding_id"].startswith("F/RTO/"))

        # 先尝试用未确认证据关闭整改 → 应被拒绝且不改变状态。
        self.ingest([
            _ev("A1", "HQ", "2026-09-30T10:00:00Z", "2026-09-30T10:00:00Z",
                "action_registered",
                {"finding_id": rto_finding["finding_id"], "title": "整改",
                 "owner_unit": "POWER"}),
            _ev("A2", "POWER", "2026-09-30T10:10:00Z", "2026-09-30T10:10:00Z",
                "action_accepted", {"action_id": "A1", "accepted_by": "POWER"}),
            _ev("E2", "POWER", "2026-09-30T09:30:00Z", "2026-09-30T09:30:00Z",
                "evidence_reported",
                {"facility_id": "F-SUB", "version": 2, "summary": "未确认的整改验证"}),
            _ev("A3", "HQ", "2026-09-30T10:20:00Z", "2026-09-30T10:20:00Z",
                "action_verified", {"action_id": "A1", "evidence_id": "E2"}),
        ])
        rep = self.replay(as_of="2026-09-30T12:00:00Z")
        action = next(a for a in rep["corrective_actions"] if a["action_id"] == "A1")
        self.assertEqual(action["status"], "accepted")

        # 再确认证据并关闭 → verified，追溯链完整。
        self.ingest([
            _ev("C2", "HQ", "2026-09-30T10:30:00Z", "2026-09-30T10:30:00Z",
                "evidence_confirmed", {"evidence_id": "E2", "version": 2}),
            _ev("A4", "HQ", "2026-09-30T10:40:00Z", "2026-09-30T10:40:00Z",
                "action_verified", {"action_id": "A1", "evidence_id": "E2"}),
        ])
        rep = self.replay(as_of="2026-09-30T12:00:00Z")
        action = next(a for a in rep["corrective_actions"] if a["action_id"] == "A1")
        self.assertEqual(action["status"], "verified")
        self.assertEqual(action["closed_evidence"]["version"], 2)
        self.assertTrue(action["finding_known"])


class DemoSmokeTest(PlatformTestBase):
    def test_demo_full_chain(self) -> None:
        self.platform.freeze_scenario(demo_data.scenario_dict())
        counts = self.platform.ingest_many(
            demo_data.scenario_dict()["exercise_code"], demo_data.events()
        )
        self.assertEqual(counts["duplicates_ignored"], 1)
        rep = self.platform.replay(demo_data.scenario_dict()["exercise_code"])

        comms = next(s for s in rep["services"] if s["service_id"] == "S-COMMS")
        ep = comms["episodes"][0]
        self.assertTrue(ep["rto_breached"])
        self.assertEqual(ep["first_available_kind"], "detour")
        self.assertTrue(ep["validated_by_confirmed_evidence"])

        dispute = rep["disputes"][0]
        self.assertEqual(dispute["winning"], "fault")
        self.assertIsNotNone(dispute["cited_evidence"])

        action = rep["corrective_actions"][0]
        self.assertEqual(action["status"], "verified")
        self.assertTrue(action["finding_known"])
        # 所有最终结论必须引用确认过的证据版本。
        for conclusion in rep["conclusions"]:
            self.assertEqual(conclusion["type"], "risk_lifted")
            self.assertTrue(conclusion["cited_evidence"])


class HistoricalReplayTests(PlatformTestBase):
    def test_future_retraction_does_not_change_past_conclusion(self) -> None:
        self.freeze()
        t = "2026-09-30T08:{:02d}:00Z"
        self.ingest([
            _ev("F1", "POWER", t.format(0), t.format(0), "facility_fault",
                {"facility_id": "F-SUB"}),
            # 09:00 才纠正 08:00 的误报。
            _ev("X1", "POWER", "2026-09-30T09:00:00Z", "2026-09-30T09:00:00Z",
                "retract_event", {"target_event_id": "F1", "reason": "误报"}),
        ])
        # 08:30 的历史重放：纠正尚未发生，故障仍计入。
        early = self.replay(as_of="2026-09-30T08:30:00Z")
        fac = next(f for f in early["facilities"] if f["facility_id"] == "F-SUB")
        self.assertTrue(fac["currently_down"])
        self.assertEqual(early["records_active"], 1)
        self.assertTrue(any("之后才被纠正" in w for w in early["warnings"]))

        # 最终重放：纠正生效，物理故障消失。
        final = self.replay()
        fac = next(f for f in final["facilities"] if f["facility_id"] == "F-SUB")
        self.assertFalse(fac["currently_down"])
        self.assertEqual(final["records_active"], 1)  # F1 被剔除，X1 是纠正
        self.assertEqual(final["records_retracted"], 1)


class CoordinatorAuthorityTests(PlatformTestBase):
    def test_only_coordinator_can_confirm_evidence_and_verify_action(self) -> None:
        self.freeze()
        t = "2026-09-30T{:02d}:00Z"
        self.ingest([
            _ev("F1", "POWER", t.format(8), t.format(8), "facility_fault",
                {"facility_id": "F-SUB"}),
            _ev("E1", "POWER", t.format(9), t.format(9), "evidence_reported",
                {"facility_id": "F-SUB", "version": 1, "summary": "复电"}),
            # 非指挥部（上报单位自己）确认证据 → 不生效。
            _ev("C-BAD", "POWER", t.format(9), t.format(9), "evidence_confirmed",
                {"evidence_id": "E1", "version": 1}),
        ])
        rep = self.replay(as_of="2026-09-30T12:00:00Z")
        evidence = next(x for x in rep["evidence"] if x["evidence_id"] == "E1")
        self.assertIsNone(evidence["confirmed_by"])
        self.assertTrue(any("只能由联合指挥部" in w for w in rep["warnings"]))


class ApiTests(PlatformTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.httpd = make_server(self.data_dir, port=0)
        self.port = self.httpd.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_api_freeze_ingest_replay(self) -> None:
        status, body = self._request("POST", "/exercises", demo_data.scenario_dict())
        self.assertEqual(status, 201)
        code = demo_data.scenario_dict()["exercise_code"]
        status, body = self._request(
            "POST", f"/exercises/{code}/events", {"events": demo_data.events()}
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["duplicates_ignored"], 1)
        status, report = self._request("GET", f"/exercises/{code}/replay")
        self.assertEqual(status, 200)
        self.assertIn("replay_fingerprint", report)
        status, verify = self._request("GET", f"/exercises/{code}/verify")
        self.assertTrue(verify["chain_valid"])


if __name__ == "__main__":
    unittest.main()
