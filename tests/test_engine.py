"""重放引擎：传播、失效区间、绕行/真恢复、会商、纠正、确定性与计划对比。"""

import tempfile
import unittest
from pathlib import Path

from resilience_replay.engine import (
    FACILITY_BYPASSED,
    FACILITY_FAILED,
    MODE_BYPASS,
    MODE_RESTORE,
    replay,
    validate_ingest,
)
from resilience_replay.events import EventLog
from resilience_replay.scenario import (
    Alternative,
    Dependency,
    Facility,
    PlanStep,
    ServiceObjective,
    UnitResponsibility,
    build_scenario,
    freeze_scenario,
)


def build_frozen():
    # P(电源) -> N(主网络)   B(备用网络) 可替代 N，容量 0.6
    # N -> C(算力主)        E(边缘算力) 可替代 C，容量 0.8，requires N
    # C -> S(服务入口)，目标阈值 0.5
    units = [
        UnitResponsibility("U0", "指挥"),
        UnitResponsibility("UP", "电力"),
        UnitResponsibility("UN", "网络"),
        UnitResponsibility("UC", "算力"),
        UnitResponsibility("UL", "物流"),
    ]
    facilities = [
        Facility("P", "电源", "power", "UP", "critical"),
        Facility("N", "主网络", "network", "UN", "critical"),
        Facility("B", "备用网络", "network", "UN", "important"),
        Facility("C", "主算力", "compute", "UC", "critical"),
        Facility("E", "边缘算力", "compute", "UC", "important"),
        Facility("S", "服务", "service", "UL", "critical"),
    ]
    deps = [Dependency("N", "P", "hard"), Dependency("C", "N", "hard"), Dependency("S", "C", "hard")]
    alts = [
        Alternative("A-NET", "N", "B", capacity_ratio=0.6),
        Alternative("A-CMP", "C", "E", capacity_ratio=0.8, requires=("N",)),
    ]
    services = [ServiceObjective("SV", "关键服务", ("S",), "目标", required_capacity_ratio=0.5)]
    plan = [
        PlanStep("P1", "N", "activate_alternative", alternative_id="A-NET", planned_offset_minutes=10,
                 responsible_unit="UN", restores_service_id="SV"),
        PlanStep("P2", "P", "restore", planned_offset_minutes=60, responsible_unit="UP",
                 restores_service_id="SV"),
    ]
    scn = build_scenario(
        exercise_code="EX", revision="r1", coordinator_unit="U0",
        facilities=facilities, dependencies=deps, alternatives=alts,
        services=services, units=units, plan=plan,
    )
    return freeze_scenario(scn, "2026-10-01T00:00:00+00:00")


def t(minute: int) -> str:
    h, m = divmod(minute, 60)
    return f"2026-10-01T{h:02d}:{m:02d}:00+00:00"


def build_log(scn, specs):
    """按给定顺序接入；payload 中 '@EVID' 解析为该 evidence_id 事件的真实 id。

    支持乱序：引用事件尚未接入时延后处理。
    spec = (etype, unit, occurred, payload, reported?)
    """
    log = EventLog()
    ids: dict[str, str] = {}
    ingested: set[int] = set()
    progress = True
    while progress:
        progress = False
        for i, spec in enumerate(specs):
            if i in ingested:
                continue
            etype, unit, occurred, payload_in = spec[:4]
            reported = spec[4] if len(spec) > 4 else None
            payload = dict(payload_in)
            blocked = False
            for k, v in list(payload.items()):
                if isinstance(v, str) and v.startswith("@"):
                    key = v[1:]
                    if key not in ids:
                        blocked = True
                        break
                    payload[k] = ids[key]
            if blocked:
                continue
            validate_ingest(scn, log.all(), etype, payload, unit)
            rec = log.append(etype, occurred_at=occurred, reported_at=reported, unit_id=unit, payload=payload)
            evid = payload.get("evidence_id")
            if evid:
                ids[evid] = rec.event_id
            ingested.add(i)
            progress = True
    if len(ingested) != len(specs):
        raise AssertionError(f"无法解析引用，未接入 {len(specs) - len(ingested)} 条")
    return log, ids


def started():
    return ("exercise_phase", "U0", t(0), {"phase": "started"})


class PropagationTests(unittest.TestCase):
    def test_power_fault_propagates_downstream(self):
        scn = build_frozen()
        log, _ = build_log(scn, [started(), ("fault", "UP", t(3), {"facility_id": "P"})])
        r = replay(scn, log)
        for fid in ("P", "N", "C", "S"):
            self.assertIn(FACILITY_FAILED, [iv.state for iv in r.facility_intervals[fid]], fid)
        self.assertEqual(r.facility_intervals["B"], [])
        svc = r.conclusion["key_service_outcomes"][0]
        self.assertTrue(svc["residual_risk"])

    def test_bypass_holds_service_but_marks_workaround(self):
        scn = build_frozen()
        log, _ = build_log(scn, [
            started(),
            ("fault", "UP", t(3), {"facility_id": "P"}),
            ("recovery_evidence", "UN", t(10),
             {"evidence_id": "E1", "facility_id": "N", "mode": MODE_BYPASS, "alternative_id": "A-NET"}),
            ("recovery_evidence", "UC", t(12),
             {"evidence_id": "E1C", "facility_id": "N", "mode": MODE_BYPASS,
              "alternative_id": "A-NET", "confirms_evidence": "@E1"}),
        ])
        r = replay(scn, log)
        self.assertEqual(r.facility_intervals["N"][0].state, FACILITY_FAILED)
        self.assertEqual(r.facility_intervals["N"][1].state, FACILITY_BYPASSED)
        # C 在绕行生效前随上游失效，生效后不再 FAILED
        c_states = [iv.state for iv in r.facility_intervals["C"]]
        self.assertIn(FACILITY_FAILED, c_states)
        self.assertNotEqual(c_states[-1], FACILITY_FAILED)
        lifts = r.conclusion["key_service_outcomes"][0]["lifts"]
        self.assertTrue(any(e["mode"] == "workaround" for e in lifts))


class BypassVsRestoreTests(unittest.TestCase):
    def test_real_restore_replaces_bypass_and_recovers_full_capacity(self):
        scn = build_frozen()
        log, _ = build_log(scn, [
            started(),
            ("fault", "UP", t(3), {"facility_id": "P"}),
            ("recovery_evidence", "UN", t(10),
             {"evidence_id": "E1", "facility_id": "N", "mode": MODE_BYPASS, "alternative_id": "A-NET"}),
            ("recovery_evidence", "UC", t(12),
             {"evidence_id": "E1C", "facility_id": "N", "mode": MODE_BYPASS,
              "alternative_id": "A-NET", "confirms_evidence": "@E1"}),
            ("recovery_evidence", "UP", t(30), {"evidence_id": "EP", "facility_id": "P", "mode": MODE_RESTORE}),
            ("recovery_evidence", "UN", t(32),
             {"evidence_id": "EPC", "facility_id": "P", "mode": MODE_RESTORE, "confirms_evidence": "@EP"}),
        ])
        r = replay(scn, log)
        # 供电恢复在异单位佐证（32 分）时才成为确认证据
        self.assertEqual(r.facility_intervals["P"][-1].end_at, t(32))
        lifts = r.conclusion["key_service_outcomes"][0]["lifts"]
        self.assertIn("recovery", [e["mode"] for e in lifts])
        self.assertFalse(r.conclusion["key_service_outcomes"][0]["residual_risk"])

    def test_self_reported_restore_without_corroboration_not_effective(self):
        scn = build_frozen()
        log, _ = build_log(scn, [
            started(),
            ("fault", "UP", t(3), {"facility_id": "P"}),
            ("recovery_evidence", "UN", t(10),
             {"evidence_id": "E1", "facility_id": "N", "mode": MODE_BYPASS, "alternative_id": "A-NET"}),
            ("recovery_evidence", "UC", t(12),
             {"evidence_id": "E1C", "facility_id": "N", "mode": MODE_BYPASS,
              "alternative_id": "A-NET", "confirms_evidence": "@E1"}),
            ("recovery_evidence", "UL", t(20),
             {"evidence_id": "ES", "facility_id": "S", "mode": MODE_RESTORE}),
        ])
        r = replay(scn, log)
        view = next(v for v in r.evidences.values() if v.record.payload["evidence_id"] == "ES")
        self.assertEqual(view.status, "submitted")
        self.assertTrue(any("未获" in f.title for f in r.findings))


class ConsultationTests(unittest.TestCase):
    def test_conflicting_claims_open_consultation_and_resolution_confirms(self):
        scn = build_frozen()
        log, ids = build_log(scn, [
            started(),
            ("fault", "UP", t(3), {"facility_id": "P"}),
            ("recovery_evidence", "UC", t(20),
             {"evidence_id": "A", "facility_id": "C", "mode": MODE_BYPASS, "alternative_id": "A-CMP"}),
            ("recovery_evidence", "UL", t(22),
             {"evidence_id": "B", "facility_id": "C", "mode": MODE_BYPASS,
              "alternative_id": "A-CMP", "disputes_evidence": "@A"}),
        ])
        r0 = replay(scn, log)
        auto_id = next(iter(r0.consultations))
        resolve = {"consultation_id": auto_id, "resolution": "confirmed",
                   "evidence_id": ids["A"], "agreed_facts": "确认绕行"}
        validate_ingest(scn, log.all(), "consultation_resolve", resolve, "U0")
        log.append("consultation_resolve", occurred_at=t(25), unit_id="U0", payload=resolve)
        r = replay(scn, log)
        self.assertEqual(r.consultations[auto_id].status, "confirmed")
        self.assertEqual(r.evidences[ids["A"]].status, "confirmed")

    def test_explicit_consultation_merges_auto_case_and_resolves(self):
        scn = build_frozen()
        log, ids = build_log(scn, [
            started(),
            ("fault", "UP", t(3), {"facility_id": "P"}),
            ("recovery_evidence", "UC", t(20),
             {"evidence_id": "A", "facility_id": "C", "mode": MODE_BYPASS, "alternative_id": "A-CMP"}),
            ("recovery_evidence", "UL", t(22),
             {"evidence_id": "B", "facility_id": "C", "mode": MODE_BYPASS,
              "alternative_id": "A-CMP", "disputes_evidence": "@A"}),
            ("consultation_open", "U0", t(23),
             {"consultation_id": "CONS1", "deadline_at": t(50), "subject": "会商",
              "parties": ["UC", "UL"], "claim_event_ids": ["@A", "@B"]}),
            ("consultation_resolve", "U0", t(26),
             {"consultation_id": "CONS1", "resolution": "confirmed", "evidence_id": "@A", "agreed_facts": "ok"}),
        ])
        r = replay(scn, log)
        self.assertIn("CONS1", r.consultations)
        self.assertEqual(r.consultations["CONS1"].status, "confirmed")
        self.assertEqual(r.evidences[ids["A"]].status, "confirmed")

    def test_open_consultation_becomes_overdue(self):
        scn = build_frozen()
        log, _ = build_log(scn, [
            started(),
            ("fault", "UP", t(3), {"facility_id": "P"}),
            ("consultation_open", "U0", t(10),
             {"consultation_id": "C1", "deadline_at": t(20), "subject": "x", "parties": ["UN", "UL"]}),
            ("exercise_phase", "U0", t(40), {"phase": "ended"}),
        ])
        r = replay(scn, log)
        self.assertEqual(r.consultations["C1"].status, "overdue")
        self.assertTrue(any("会商超期" in f.title for f in r.findings))


class CorrectionTests(unittest.TestCase):
    def test_false_alarm_corrected_leaves_no_outage(self):
        scn = build_frozen()
        log = EventLog()
        validate_ingest(scn, [], "exercise_phase", {"phase": "started"}, "U0")
        log.append("exercise_phase", occurred_at=t(0), unit_id="U0", payload={"phase": "started"})
        validate_ingest(scn, log.all(), "fault", {"facility_id": "B"}, "UN")
        fault = log.append("fault", occurred_at=t(5), unit_id="UN", payload={"facility_id": "B"})
        corr = {"target_event_id": fault.event_id, "reason": "仪表误报"}
        validate_ingest(scn, log.all(), "correction", corr, "UN")
        log.append("correction", occurred_at=t(8), unit_id="UN", payload=corr)
        r = replay(scn, log)
        self.assertEqual(r.facility_intervals["B"], [])
        self.assertIn(fault.event_id, {x.event_id for x in log.all()})

    def test_orphan_restore_when_no_live_fault(self):
        scn = build_frozen()
        log, _ = build_log(scn, [
            started(),
            ("recovery_evidence", "UP", t(10), {"evidence_id": "EX", "facility_id": "P", "mode": MODE_RESTORE}),
            ("recovery_evidence", "UN", t(12),
             {"evidence_id": "EXC", "facility_id": "P", "mode": MODE_RESTORE, "confirms_evidence": "@EX"}),
        ])
        r = replay(scn, log)
        view = next(v for v in r.evidences.values() if v.record.payload["evidence_id"] == "EX")
        self.assertEqual(view.status, "orphan")


class DeterminismTests(unittest.TestCase):
    SPECS = [
        started(),
        ("fault", "UP", t(3), {"facility_id": "P"}, t(6)),
        ("recovery_evidence", "UN", t(10),
         {"evidence_id": "E1", "facility_id": "N", "mode": MODE_BYPASS, "alternative_id": "A-NET"}),
        ("recovery_evidence", "UC", t(12),
         {"evidence_id": "E1C", "facility_id": "N", "mode": MODE_BYPASS,
          "alternative_id": "A-NET", "confirms_evidence": "@E1"}),
        ("recovery_evidence", "UP", t(30), {"evidence_id": "EP", "facility_id": "P", "mode": MODE_RESTORE}),
        ("recovery_evidence", "UN", t(32),
         {"evidence_id": "EPC", "facility_id": "P", "mode": MODE_RESTORE, "confirms_evidence": "@EP"}),
        ("exercise_phase", "U0", t(40), {"phase": "ended"}),
    ]

    def _core(self, d):
        ev_sig = sorted(
            (v["facility_id"], v["mode"], v["confirmed_at"])
            for v in d["evidences"].values()
            if v["status"] in ("confirmed", "superseded")
        )
        return {
            "facility_P": [(i["start_at"], i["end_at"], i["state"]) for i in d["facility_intervals"]["P"]],
            "lifts": [(l["lifted_at"], l["mode"]) for l in d["conclusion"]["key_service_outcomes"][0]["lifts"]],
            "final": d["conclusion"]["final"],
            "evidence_signatures": ev_sig,
        }

    def test_replay_independent_of_ingestion_order(self):
        scn = build_frozen()
        r1 = replay(scn, build_log(scn, self.SPECS)[0]).to_dict()
        # 乱序接入：佐证（12 分）先于主张（10 分）接入
        shuffled = [self.SPECS[i] for i in (0, 1, 3, 2, 5, 4, 6)]
        r2 = replay(scn, build_log(scn, shuffled)[0]).to_dict()
        self.assertEqual(self._core(r1), self._core(r2))

    def test_restart_via_reload_gives_same_result(self):
        scn = build_frozen()
        log = build_log(scn, self.SPECS)[0]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "e.jsonl"
            log.save_jsonl(path)
            reloaded = EventLog.load_jsonl(path)
            r1 = self._core(replay(scn, log).to_dict())
            r2 = self._core(replay(scn, reloaded).to_dict())
            self.assertEqual(r1, r2)
            self.assertEqual(log.head_hash, reloaded.head_hash)

    def test_as_of_truncation_is_prefix_stable(self):
        scn = build_frozen()
        log = build_log(scn, self.SPECS)[0]
        early = replay(scn, log, as_of=t(8))
        late = replay(scn, log, as_of=t(35))
        full = replay(scn, log)
        # 8 分时供电已故障、绕行尚未确认 -> 有残余风险
        self.assertTrue(early.conclusion["key_service_outcomes"][0]["residual_risk"])
        # 35 分已真恢复
        self.assertFalse(late.conclusion["key_service_outcomes"][0]["residual_risk"])
        self.assertFalse(full.conclusion["key_service_outcomes"][0]["residual_risk"])


class PlanComparisonTests(unittest.TestCase):
    def test_plan_vs_actual_deltas(self):
        scn = build_frozen()
        log, _ = build_log(scn, [
            started(),
            ("fault", "UP", t(3), {"facility_id": "P"}),
            ("recovery_evidence", "UN", t(12),
             {"evidence_id": "E1", "facility_id": "N", "mode": MODE_BYPASS, "alternative_id": "A-NET"}),
            ("recovery_evidence", "UC", t(14),
             {"evidence_id": "E1C", "facility_id": "N", "mode": MODE_BYPASS,
              "alternative_id": "A-NET", "confirms_evidence": "@E1"}),
            ("recovery_evidence", "UP", t(70), {"evidence_id": "EP", "facility_id": "P", "mode": MODE_RESTORE}),
            ("recovery_evidence", "UN", t(72),
             {"evidence_id": "EPC", "facility_id": "P", "mode": MODE_RESTORE, "confirms_evidence": "@EP"}),
            ("exercise_phase", "U0", t(80), {"phase": "ended"}),
        ])
        r = replay(scn, log)
        pc = r.plan_comparison
        self.assertEqual(pc["actual_order"], ["P1", "P2"])
        by_id = {row["step_id"]: row for row in pc["steps"]}
        # 网络绕行确认于 14 分（计划 T+10）
        self.assertEqual(by_id["P1"]["actual_at"], t(14))
        # 电源真恢复确认于 72 分（计划 T+60）
        self.assertEqual(by_id["P2"]["actual_at"], t(72))


class RemediationTrackingTests(unittest.TestCase):
    def test_finding_to_remediation_lifecycle(self):
        scn = build_frozen()
        log, _ = build_log(scn, [
            started(),
            ("finding", "U0", t(10), {"finding_id": "F1", "title": "问题", "severity": "major"}),
            ("remediation", "U0", t(15),
             {"remediation_id": "R1", "finding_id": "F1", "action": "整改", "owner_unit": "UN"}),
            ("remediation", "U0", t(20),
             {"remediation_id": "R1", "finding_id": "F1", "action": "整改",
              "status": "accepted", "accepted_by": "值班长"}),
            ("remediation", "U0", t(40),
             {"remediation_id": "R1", "finding_id": "F1", "action": "整改",
              "status": "closed", "verified_by": "审计"}),
            ("exercise_phase", "U0", t(50), {"phase": "ended"}),
        ])
        r = replay(scn, log)
        rem = r.remediations["R1"]
        self.assertEqual(rem.status, "closed")
        self.assertEqual(rem.accepted_by, "值班长")
        self.assertEqual(rem.verified_by, "审计")
        self.assertIsNotNone(rem.closed_event)

    def test_remediation_cannot_skip_registration(self):
        scn = build_frozen()
        log = EventLog()
        validate_ingest(scn, log.all(), "exercise_phase", {"phase": "started"}, "U0")
        log.append("exercise_phase", occurred_at=t(0), unit_id="U0", payload={"phase": "started"})
        validate_ingest(scn, log.all(), "finding", {"finding_id": "F1", "title": "x"}, "U0")
        log.append("finding", occurred_at=t(5), unit_id="U0", payload={"finding_id": "F1", "title": "x"})
        with self.assertRaises(ValueError):
            validate_ingest(scn, log.all(), "remediation",
                            {"remediation_id": "R1", "finding_id": "F1", "action": "a",
                             "status": "accepted", "accepted_by": "x"}, "U0")


class IngestGuardTests(unittest.TestCase):
    def test_cannot_ingest_before_freeze(self):
        scn = build_scenario(
            exercise_code="EX", revision="d", coordinator_unit="U0",
            facilities=[Facility("P", "p", "power", "U0")],
            units=[UnitResponsibility("U0", "指挥")],
        )
        with self.assertRaises(ValueError):
            validate_ingest(scn, [], "fault", {"facility_id": "P"}, "U0")

    def test_unknown_unit_rejected(self):
        scn = build_frozen()
        with self.assertRaises(ValueError):
            validate_ingest(scn, [], "fault", {"facility_id": "P"}, "NOBODY")


if __name__ == "__main__":
    unittest.main()
