"""仅依赖标准库的 HTTP API。

端点：

* ``POST /exercises``                         冻结场景
* ``GET  /exercises``                         演练清单
* ``GET  /exercises/{code}/scenario``         冻结场景与指纹
* ``POST /exercises/{code}/events``           上报事件（单条或 ``{"events":[...]}`` 批量）
* ``GET  /exercises/{code}/events``           追加日志（含被纠正原始记录）
* ``GET  /exercises/{code}/replay?as_of=...`` 确定性重放复盘报告
* ``GET  /exercises/{code}/verify``           场景指纹与哈希链完整性
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .errors import DomainError
from .platform import ResiliencePlatform


class _Handler(BaseHTTPRequestHandler):
    server_version = "ResilienceReplay/1.0"
    platform: ResiliencePlatform  # 由 make_server 注入到类上

    # ---- 工具 -----------------------------------------------------------

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON: {exc}") from exc

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ---- 路由 -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def _route(self, method: str) -> None:
        parts = [segment for segment in urlsplit(self.path).path.split("/") if segment]
        query = parse_qs(urlsplit(self.path).query)
        try:
            if method == "GET" and parts == ["exercises"]:
                self._send(200, {"exercises": self.platform.list_exercises()})
                return
            if method == "POST" and parts == ["exercises"]:
                self._send(201, self.platform.freeze_scenario(self._read_json()))
                return
            if len(parts) == 3 and parts[0] == "exercises" and parts[2] == "scenario" and method == "GET":
                scenario = self.platform.get_scenario(parts[1])
                self._send(200, {
                    "scenario": scenario.to_dict(),
                    "scenario_fingerprint": scenario.fingerprint(),
                })
                return
            if len(parts) == 3 and parts[0] == "exercises" and parts[2] == "events":
                code = parts[1]
                if method == "GET":
                    self._send(200, {"events": self.platform.list_events(code)})
                else:
                    body = self._read_json()
                    if isinstance(body, dict) and "events" in body:
                        result = self.platform.ingest_many(code, body["events"])
                    else:
                        result = self.platform.report_event(code, **_event_kwargs(body))
                    self._send(202, result)
                return
            if len(parts) == 3 and parts[0] == "exercises" and parts[2] == "replay" and method == "GET":
                as_of = query.get("as_of", [None])[0]
                self._send(200, self.platform.replay(parts[1], as_of=as_of))
                return
            if len(parts) == 3 and parts[0] == "exercises" and parts[2] == "verify" and method == "GET":
                self._send(200, self.platform.verify_integrity(parts[1]))
                return
            self._send(404, {"error": "not_found", "path": self.path})
        except FileExistsError as exc:
            self._send(409, {"error": "ScenarioFrozenError", "detail": str(exc)})
        except FileNotFoundError as exc:
            self._send(404, {"error": "ExerciseNotFoundError", "detail": str(exc)})
        except DomainError as exc:
            self._send(400, {"error": type(exc).__name__, "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal_error", "detail": str(exc)})


def _event_kwargs(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise DomainError("事件必须是 JSON 对象")
    try:
        return {
            "event_id": body["event_id"],
            "unit_id": body["unit_id"],
            "occurred_at": body["occurred_at"],
            "reported_at": body["reported_at"],
            "kind": body["kind"],
            "payload": body.get("payload", {}),
        }
    except KeyError as exc:
        raise DomainError(f"事件缺少字段: {exc.args[0]}") from exc


def make_server(
    data_dir: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    verbose: bool = False,
) -> ThreadingHTTPServer:
    """构造（但不启动）HTTP 服务，便于测试与编程式调用。"""
    handler = type("BoundHandler", (_Handler,), {
        "platform": ResiliencePlatform(data_dir),
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def serve(
    data_dir: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    verbose: bool = False,
    stopper: Callable[[], bool] | None = None,
) -> None:
    """阻塞运行 API；``stopper`` 返回真时优雅退出（测试用）。"""
    httpd = make_server(data_dir, host, port, verbose=verbose)
    actual_host, actual_port = httpd.server_address[0], httpd.server_address[1]
    print(f"跨网络韧性演练复盘 API 已启动：http://{actual_host}:{actual_port}")
    print("Ctrl+C 退出")
    try:
        if stopper is None:
            httpd.serve_forever()
        else:
            while not stopper():
                httpd.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
