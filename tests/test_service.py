"""应用服务层：冻结、接入、纠正、持久化、重启重放一致。"""

import tempfile
import unittest

from resilience_replay.demo import DEMO_CODE, DEMO_REVISION, build_demo
from resilience_replay.events import DuplicateEventError
from resilience_replay.engine import IngestValidationError
from resilience_replay.service import ExerciseService, ScenarioConflictError
from resilience_replay.scenario import ScenarioState

MINI_SCENARIO = {
    "exercise_code": "EX1",
    "revision": "r1",
    "coordinator_unit": "U0",
    "facilities": [
        {"facility_id": "P", "name": "电源", "kind": "power", "owner_unit": "U1"},
        {"facility_id": "S", "name": "服务", "kind": "service", "owner_unit": "U0"},
    ],
    "dependencies": [{"facility_id": "S", "on_facility_id": "P", "kind": "hard"}],
    "services": [{"service_id": "SV", "name": "服务", "facility_ids": ["S"], "target": "t"}],
    "units": [
        {"unit_id": "U0", "name": "指挥"},
        {"unit_id": "U1", "name": "电力"},
    ],
}


class ServiceTests(unittest.TestCase):
    def test_freeze_and_ingest_guard(self):
        with tempfile.TemporaryDirectory() as d:
            svc = ExerciseService(d)
            svc.save_draft(MINI_SCENARIO)
            with self.assertRaises(IngestValidationError):
                svc.ingest("EX1", "r1", "fault", occurred_at="2026-10-01T00:00:00+00:00",
                           unit_id="U1", payload={"facility_id": "P"})
            svc.freeze("EX1", "r1", frozen_at="2026-10-01T00:00:00+00:00")
            rec = svc.ingest(
                "EX1", "r1", "fault", occurred_at="2026-10-01T00:05:00+00:00",
                unit_id="U1", payload={"facility_id": "P"},
            )
            self.assertTrue(rec.event_id)
            # 重复上报
            with self.assertRaises(DuplicateEventError):
                svc.ingest("EX1", "r1", "fault", occurred_at="2026-10-01T00:05:00+00:00",
                           unit_id="U1", payload={"facility_id": "P"})

    def test_frozen_scenario_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            svc = ExerciseService(d)
            svc.save_draft(MINI_SCENARIO)
            svc.freeze("EX1", "r1")
            with self.assertRaises(ScenarioConflictError):
                svc.save_draft(MINI_SCENARIO)

    def test_correction_convenience_and_persistent_replay(self):
        with tempfile.TemporaryDirectory() as d:
            svc = ExerciseService(d)
            svc.save_draft(MINI_SCENARIO)
            svc.freeze("EX1", "r1")
            fault = svc.ingest("EX1", "r1", "fault", occurred_at="2026-10-01T00:05:00+00:00",
                               unit_id="U1", payload={"facility_id": "P"})
            r1 = svc.replay("EX1", "r1")
            self.assertTrue(r1.facility_intervals["P"])
            svc.correct("EX1", "r1", target_event_id=fault.event_id, reason="误报",
                        unit_id="U1", occurred_at="2026-10-01T00:08:00+00:00")
            # 用一个全新服务实例模拟进程重启
            svc2 = ExerciseService(d)
            r2 = svc2.replay("EX1", "r1")
            self.assertEqual(r2.facility_intervals["P"], [])
            # 原始记录仍在
            self.assertIn(fault.event_id, {e.event_id for e in svc2.events("EX1", "r1")})
            self.assertEqual(len(svc2.events("EX1", "r1")), 2)


class DemoServiceTests(unittest.TestCase):
    def test_demo_builds_and_conclusion_is_final(self):
        with tempfile.TemporaryDirectory() as d:
            svc = ExerciseService(d)
            stats = build_demo(svc)
            self.assertEqual(stats["rejected"], 1)
            r = svc.replay(DEMO_CODE, DEMO_REVISION)
            self.assertTrue(r.conclusion["final"])
            # 整改闭环
            self.assertEqual(r.remediations["R-UNIFY"].status, "closed")
            # 至少一次会商被裁决确认
            self.assertTrue(any(c.status == "confirmed" for c in r.consultations.values()))
            # 列出演练
            listed = svc.list_exercises()
            self.assertTrue(any(x["exercise_code"] == DEMO_CODE for x in listed))


if __name__ == "__main__":
    unittest.main()
