"""HTTP API 端到端测试（标准库 http.client，启动真实端口）。"""

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection

from resilience_replay.api import create_server

SCENARIO = {
    "exercise_code": "EXAPI",
    "revision": "r1",
    "coordinator_unit": "U0",
    "facilities": [
        {"facility_id": "P", "name": "电源", "kind": "power", "owner_unit": "U1"},
        {"facility_id": "S", "name": "服务", "kind": "service", "owner_unit": "U0"},
    ],
    "dependencies": [{"facility_id": "S", "on_facility_id": "P", "kind": "hard"}],
    "services": [{"service_id": "SV", "name": "服务", "facility_ids": ["S"], "target": "t"}],
    "units": [{"unit_id": "U0", "name": "指挥"}, {"unit_id": "U1", "name": "电力"}],
}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, self.tmp.name)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _req(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()
        return resp.status, json.loads(raw) if raw else {}

    def test_full_flow(self):
        status, body = self._req("GET", "/healthz")
        self.assertEqual(status, 200)

        status, _ = self._req("PUT", "/exercises/EXAPI/scenarios/r1", SCENARIO)
        self.assertEqual(status, 200)

        status, _ = self._req("POST", "/exercises/EXAPI/scenarios/r1/freeze", {})
        self.assertEqual(status, 200)

        # 冻结前写入的场景状态变更应被拒（409）
        status, _ = self._req("PUT", "/exercises/EXAPI/scenarios/r1", SCENARIO)
        self.assertEqual(status, 409)

        fault = {
            "event_type": "fault", "occurred_at": "2026-10-01T00:05:00+00:00",
            "unit_id": "U1", "payload": {"facility_id": "P"},
        }
        status, body = self._req("POST", "/exercises/EXAPI/scenarios/r1/events", fault)
        self.assertEqual(status, 201)
        event_id = body["event"]["event_id"]

        # 重复上报 -> 409
        status, body = self._req("POST", "/exercises/EXAPI/scenarios/r1/events", fault)
        self.assertEqual(status, 409)
        self.assertEqual(body["duplicate_of"], event_id)

        # 非法单位 -> 422
        bad = dict(fault, unit_id="NOPE",
                   payload={"facility_id": "P", "description": "另一事件以绕过幂等"})
        status, body = self._req("POST", "/exercises/EXAPI/scenarios/r1/events", bad)
        self.assertEqual(status, 422)

        # 重放：服务处于失效
        status, body = self._req("GET", "/exercises/EXAPI/scenarios/r1/replay")
        self.assertEqual(status, 200)
        self.assertTrue(body["conclusion"]["key_service_outcomes"][0]["residual_risk"])

        # 纠正误报
        corr = {
            "target_event_id": event_id, "reason": "误报", "unit_id": "U1",
            "occurred_at": "2026-10-01T00:08:00+00:00",
        }
        status, _ = self._req("POST", "/exercises/EXAPI/scenarios/r1/corrections", corr)
        self.assertEqual(status, 201)

        status, body = self._req("GET", "/exercises/EXAPI/scenarios/r1/replay")
        self.assertEqual(status, 200)
        self.assertFalse(body["conclusion"]["key_service_outcomes"][0]["residual_risk"])

        # 事件列表：原始故障仍在
        status, body = self._req("GET", "/exercises/EXAPI/scenarios/r1/events")
        self.assertEqual(body["count"], 2)

    def test_demo_endpoints(self):
        status, stats = self._req("POST", "/demo/build", {})
        self.assertEqual(status, 200)
        self.assertEqual(stats["rejected"], 1)
        status, body = self._req("POST", "/demo/replay", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["conclusion"]["final"])


if __name__ == "__main__":
    unittest.main()
