"""冻结场景构造与校验测试。"""

import unittest

from resilience_replay.scenario import (
    Alternative,
    Dependency,
    Facility,
    PlanStep,
    ScenarioState,
    ServiceObjective,
    UnitResponsibility,
    build_scenario,
    freeze_scenario,
    scenario_from_dict,
)


def _base_kwargs():
    units = [
        UnitResponsibility(unit_id="U1", name="指挥"),
        UnitResponsibility(unit_id="U2", name="电力"),
    ]
    facilities = [
        Facility(facility_id="P", name="电源", kind="power", owner_unit="U2"),
        Facility(facility_id="N", name="网络", kind="network", owner_unit="U1"),
        Facility(facility_id="B", name="备用网", kind="network", owner_unit="U1"),
        Facility(facility_id="C", name="算力", kind="compute", owner_unit="U1"),
        Facility(facility_id="S", name="服务", kind="service", owner_unit="U1"),
    ]
    return dict(
        exercise_code="EX",
        revision="r1",
        coordinator_unit="U1",
        facilities=facilities,
        units=units,
    )


class ScenarioTests(unittest.TestCase):
    def test_minimal_scenario_builds_as_draft(self):
        scn = build_scenario(**_base_kwargs())
        self.assertEqual(scn.state, ScenarioState.DRAFT.value)
        self.assertIsNone(scn.frozen_at)

    def test_freeze_marks_frozen_at(self):
        scn = freeze_scenario(build_scenario(**_base_kwargs()), "2026-10-01T00:00:00+08:00")
        self.assertEqual(scn.state, ScenarioState.FROZEN.value)
        self.assertEqual(scn.frozen_at, "2026-10-01T00:00:00+08:00")
        with self.assertRaises(ValueError):
            freeze_scenario(scn, "2026-10-01T01:00:00+08:00")

    def test_duplicate_facility_rejected(self):
        kw = _base_kwargs()
        kw["facilities"].append(Facility(facility_id="P", name="重复", kind="power", owner_unit="U2"))
        with self.assertRaises(ValueError):
            build_scenario(**kw)

    def test_dependency_on_unknown_facility_rejected(self):
        kw = _base_kwargs()
        kw["dependencies"] = [Dependency(facility_id="N", on_facility_id="X")]
        with self.assertRaises(ValueError):
            build_scenario(**kw)

    def test_hard_dependency_cycle_rejected(self):
        kw = _base_kwargs()
        kw["dependencies"] = [
            Dependency("N", "C", "hard"),
            Dependency("C", "N", "hard"),
        ]
        with self.assertRaises(ValueError):
            build_scenario(**kw)

    def test_soft_cycle_allowed(self):
        kw = _base_kwargs()
        kw["dependencies"] = [
            Dependency("N", "C", "soft"),
            Dependency("C", "N", "soft"),
        ]
        self.assertEqual(build_scenario(**kw).state, ScenarioState.DRAFT.value)

    def test_alternative_validation(self):
        kw = _base_kwargs()
        kw["alternatives"] = [
            Alternative(alternative_id="A", for_facility_id="N", backup_facility_id="B", capacity_ratio=0.5)
        ]
        scn = build_scenario(**kw)
        self.assertEqual(scn.alternatives_for("N")[0].alternative_id, "A")
        bad = _base_kwargs()
        bad["alternatives"] = [
            Alternative(alternative_id="A", for_facility_id="N", backup_facility_id="B", capacity_ratio=1.5)
        ]
        with self.assertRaises(ValueError):
            build_scenario(**bad)

    def test_service_and_plan_validation(self):
        kw = _base_kwargs()
        kw["services"] = [ServiceObjective(service_id="SV", name="服务", facility_ids=("S",), target="t")]
        kw["plan"] = [PlanStep(step_id="P1", target_facility_id="P", action="restore", restores_service_id="SV")]
        scn = build_scenario(**kw)
        self.assertEqual(scn.plan[0].step_id, "P1")
        bad = _base_kwargs()
        bad["services"] = [ServiceObjective(service_id="SV", name="服务", facility_ids=("ZZ",), target="t")]
        with self.assertRaises(ValueError):
            build_scenario(**bad)

    def test_owner_unit_must_be_registered(self):
        kw = _base_kwargs()
        kw["facilities"] = [Facility(facility_id="P", name="x", kind="power", owner_unit="NOPE")]
        with self.assertRaises(ValueError):
            build_scenario(**kw)

    def test_roundtrip_dict_preserves_frozen_state(self):
        kw = _base_kwargs()
        kw["dependencies"] = [Dependency("C", "N", "hard"), Dependency("N", "P", "hard")]
        scn = freeze_scenario(build_scenario(**kw), "2026-10-01T00:00:00+08:00")
        back = scenario_from_dict(scn.to_dict())
        self.assertEqual(back.state, ScenarioState.FROZEN.value)
        self.assertEqual(len(back.dependencies), 2)


if __name__ == "__main__":
    unittest.main()
