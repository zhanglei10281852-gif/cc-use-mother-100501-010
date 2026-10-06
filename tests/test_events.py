"""只追加事件日志：幂等、双时间戳、哈希链、纠正不抹除、持久化。"""

import tempfile
import unittest
from pathlib import Path

from resilience_replay.events import (
    CORRECTION,
    DECISION,
    FAULT,
    RECOVERY_EVIDENCE,
    DuplicateEventError,
    EventLog,
)


class EventLogTests(unittest.TestCase):
    def test_idempotent_duplicate_rejected(self):
        log = EventLog()
        payload = {"facility_id": "F"}
        log.append(FAULT, occurred_at="2026-10-01T00:00:00+00:00", unit_id="U1", payload=payload)
        with self.assertRaises(DuplicateEventError) as ctx:
            log.append(FAULT, occurred_at="2026-10-01T00:00:00+00:00", unit_id="U1", payload=dict(payload))
        self.assertTrue(ctx.exception.existing.event_id)

    def test_explicit_idempotency_key_allows_same_content_at_other_time(self):
        log = EventLog()
        log.append(FAULT, occurred_at="2026-10-01T00:00:00+00:00", unit_id="U1",
                   payload={"facility_id": "F"}, idempotency_key="k1")
        log.append(FAULT, occurred_at="2026-10-01T02:00:00+00:00", unit_id="U1",
                   payload={"facility_id": "F"}, idempotency_key="k2")
        self.assertEqual(len(log), 2)

    def test_reported_before_occurred_rejected(self):
        log = EventLog()
        with self.assertRaises(ValueError):
            log.append(FAULT, occurred_at="2026-10-01T02:00:00+00:00", unit_id="U1",
                       payload={}, reported_at="2026-10-01T01:00:00+00:00")

    def test_naive_timestamp_rejected(self):
        log = EventLog()
        with self.assertRaises(ValueError):
            log.append(FAULT, occurred_at="2026-10-01 00:00:00", unit_id="U1", payload={})

    def test_out_of_order_ingest_replays_by_occurrence_time(self):
        log = EventLog()
        log.append(DECISION, occurred_at="2026-10-01T03:00:00+00:00", unit_id="U1", payload={"d": 3})
        log.append(FAULT, occurred_at="2026-10-01T01:00:00+00:00", unit_id="U1", payload={"facility_id": "F"})
        log.append(DECISION, occurred_at="2026-10-01T02:00:00+00:00", unit_id="U1", payload={"d": 2})
        ordered = log.ordered_by_occurrence(include_retracted=True)
        self.assertEqual([r.payload["d"] if r.event_type == DECISION else 1 for r in ordered], [1, 2, 3])

    def test_correction_does_not_erase_original(self):
        log = EventLog()
        first = log.append(FAULT, occurred_at="2026-10-01T00:00:00+00:00", unit_id="U1",
                           payload={"facility_id": "F"})
        log.append(CORRECTION, occurred_at="2026-10-01T01:00:00+00:00", unit_id="U1",
                   payload={"target_event_id": first.event_id, "reason": "误报"})
        # 原始记录仍在日志中
        self.assertIn(first.event_id, {r.event_id for r in log.all()})
        retracted = log.retraction_map()
        self.assertIn(first.event_id, retracted)
        # effective 中排除但 all 中保留
        self.assertNotIn(first.event_id, {r.event_id for r in log.effective()})

    def test_save_and_load_roundtrip_with_chain(self):
        log = EventLog()
        e1 = log.append(FAULT, occurred_at="2026-10-01T00:00:00+00:00", unit_id="U1", payload={"facility_id": "F"})
        log.append(CORRECTION, occurred_at="2026-10-01T01:00:00+00:00", unit_id="U1",
                   payload={"target_event_id": e1.event_id, "reason": "x"})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            log.save_jsonl(path)
            loaded = EventLog.load_jsonl(path)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded.head_hash, log.head_hash)

    def test_tampered_chain_detected(self):
        log = EventLog()
        log.append(FAULT, occurred_at="2026-10-01T00:00:00+00:00", unit_id="U1", payload={"facility_id": "F"})
        log.append(RECOVERY_EVIDENCE, occurred_at="2026-10-01T01:00:00+00:00", unit_id="U1",
                   payload={"facility_id": "F", "evidence_id": "E", "mode": "restore"})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            log.save_jsonl(path)
            lines = path.read_text(encoding="utf-8").splitlines()
            import json
            row = json.loads(lines[1])
            row["payload"] = {"facility_id": "F", "evidence_id": "E", "mode": "bypass"}
            lines[1] = json.dumps(row, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                EventLog.load_jsonl(path)

    def test_broken_predecessor_link_detected(self):
        log = EventLog()
        log.append(FAULT, occurred_at="2026-10-01T00:00:00+00:00", unit_id="U1", payload={"facility_id": "F"})
        log.append(FAULT, occurred_at="2026-10-01T02:00:00+00:00", unit_id="U1",
                   payload={"facility_id": "G"}, idempotency_key="k2")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            log.save_jsonl(path)
            lines = path.read_text(encoding="utf-8").splitlines()
            import json
            row = json.loads(lines[1])
            row["predecessor_hash"] = "DEADBEEF"
            lines[1] = json.dumps(row, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                EventLog.load_jsonl(path)


if __name__ == "__main__":
    unittest.main()
