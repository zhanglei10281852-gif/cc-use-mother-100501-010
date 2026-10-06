"""HTTP API：仅依赖标准库 http.server。

端点：
  GET  /healthz
  GET  /exercises                              列出场景版本
  PUT  /exercises/{code}/scenarios/{revision}  写入 DRAFT 场景（整份 JSON）
  POST /exercises/{code}/scenarios/{revision}/freeze
  GET  /exercises/{code}/scenarios/{revision}
  POST /exercises/{code}/scenarios/{revision}/events
       body: {event_type, occurred_at, reported_at?, unit_id, payload, event_id?, idempotency_key?}
  POST /exercises/{code}/scenarios/{revision}/corrections
  GET  /exercises/{code}/scenarios/{revision}/events
  GET  /exercises/{code}/scenarios/{revision}/replay[?as_of=...]
  POST /demo/build
  POST /demo/replay

重复上报返回 409；语义错误返回 422；场景冲突返回 409。
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import json
import re
from typing import Any, Callable

from .demo import build_demo
from .events import DuplicateEventError
from .service import ExerciseService, ScenarioConflictError
from .engine import IngestValidationError

EVENT_TYPES_REQUIRED = {"event_type", "occurred_at", "unit_id", "payload"}


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    data = json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _HttpError(400, f"请求体不是合法 JSON: {exc}") from exc


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ApiHandler(BaseHTTPRequestHandler):
    service: ExerciseService  # 由 create_server 注入到类

    server_version = "ResilienceReplay/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        try:
            handler, kwargs = self._match(method, path)
            handler(query, **kwargs)
        except _HttpError as exc:
            _json_response(self, exc.status, {"error": exc.message})
        except (ValueError, KeyError, TypeError) as exc:
            _json_response(self, 400, {"error": str(exc)})
        except FileNotFoundError as exc:
            _json_response(self, 404, {"error": str(exc)})
        except NotImplementedError:
            _json_response(self, 405, {"error": f"{method} {path} 不支持"})

    def _match(self, method: str, path: str) -> tuple[Callable[..., None], dict[str, str]]:
        routes: list[tuple[str, str, Callable[..., None]]] = [
            ("GET", r"^/healthz$", self._h_health),
            ("GET", r"^/exercises$", self._h_list),
            ("POST", r"^/demo/build$", self._h_demo_build),
            ("POST", r"^/demo/replay$", self._h_demo_replay),
            ("PUT", r"^/exercises/(?P<code>[^/]+)/scenarios/(?P<revision>[^/]+)$", self._h_scenario_put),
            ("GET", r"^/exercises/(?P<code>[^/]+)/scenarios/(?P<revision>[^/]+)$", self._h_scenario_get),
            ("POST", r"^/exercises/(?P<code>[^/]+)/scenarios/(?P<revision>[^/]+)/freeze$", self._h_freeze),
            ("POST", r"^/exercises/(?P<code>[^/]+)/scenarios/(?P<revision>[^/]+)/events$", self._h_event),
            ("POST", r"^/exercises/(?P<code>[^/]+)/scenarios/(?P<revision>[^/]+)/corrections$", self._h_correction),
            ("GET", r"^/exercises/(?P<code>[^/]+)/scenarios/(?P<revision>[^/]+)/events$", self._h_events),
            ("GET", r"^/exercises/(?P<code>[^/]+)/scenarios/(?P<revision>[^/]+)/replay$", self._h_replay),
        ]
        for m, pattern, fn in routes:
            mt = re.match(pattern, path)
            if mt and m == method:
                return (lambda query, _fn=fn, _kw=mt.groupdict(): _fn(query, **_kw)), {}
        raise NotImplementedError(path)

    # -- handlers -----------------------------------------------------------

    def _h_health(self, query: dict[str, list[str]]) -> None:
        _json_response(self, 200, {"status": "ok"})

    def _h_list(self, query: dict[str, list[str]]) -> None:
        _json_response(self, 200, {"exercises": self.service.list_exercises()})

    def _h_scenario_put(self, query: dict[str, list[str]], code: str, revision: str) -> None:
        data = _read_json(self)
        if not isinstance(data, dict):
            raise _HttpError(400, "场景必须是 JSON 对象")
        data["exercise_code"] = code
        data["revision"] = revision
        try:
            scenario = self.service.save_draft(data)
        except ScenarioConflictError as exc:
            raise _HttpError(409, str(exc)) from exc
        _json_response(self, 200, {"scenario": scenario.to_dict()})

    def _h_scenario_get(self, query: dict[str, list[str]], code: str, revision: str) -> None:
        _json_response(self, 200, {"scenario": self.service.load_scenario(code, revision).to_dict()})

    def _h_freeze(self, query: dict[str, list[str]], code: str, revision: str) -> None:
        body = _read_json(self)
        frozen_at = body.get("frozen_at") if isinstance(body, dict) else None
        scenario = self.service.freeze(code, revision, frozen_at=frozen_at)
        _json_response(self, 200, {"scenario": scenario.to_dict()})

    def _h_event(self, query: dict[str, list[str]], code: str, revision: str) -> None:
        body = _read_json(self)
        missing = EVENT_TYPES_REQUIRED - (body.keys() if isinstance(body, dict) else set())
        if missing:
            raise _HttpError(400, f"缺少字段: {sorted(missing)}")
        try:
            record = self.service.ingest(
                code,
                revision,
                body["event_type"],
                occurred_at=body["occurred_at"],
                reported_at=body.get("reported_at"),
                unit_id=body["unit_id"],
                payload=body["payload"],
                event_id=body.get("event_id"),
                idempotency_key=body.get("idempotency_key"),
            )
        except DuplicateEventError as exc:
            _json_response(
                self,
                409,
                {"error": str(exc), "duplicate_of": exc.existing.event_id},
            )
            return
        except IngestValidationError as exc:
            raise _HttpError(422, str(exc)) from exc
        _json_response(self, 201, {"event": _record_dict(record)})

    def _h_correction(self, query: dict[str, list[str]], code: str, revision: str) -> None:
        body = _read_json(self)
        for key in ("target_event_id", "reason", "unit_id", "occurred_at"):
            if not isinstance(body.get(key), str):
                raise _HttpError(400, f"缺少字段: {key}")
        try:
            record = self.service.correct(
                code,
                revision,
                target_event_id=body["target_event_id"],
                reason=body["reason"],
                unit_id=body["unit_id"],
                occurred_at=body["occurred_at"],
                reported_at=body.get("reported_at"),
            )
        except DuplicateEventError as exc:
            _json_response(self, 409, {"error": str(exc), "duplicate_of": exc.existing.event_id})
            return
        except IngestValidationError as exc:
            raise _HttpError(422, str(exc)) from exc
        _json_response(self, 201, {"event": _record_dict(record)})

    def _h_events(self, query: dict[str, list[str]], code: str, revision: str) -> None:
        records = self.service.events(code, revision)
        _json_response(self, 200, {"events": [_record_dict(r) for r in records], "count": len(records)})

    def _h_replay(self, query: dict[str, list[str]], code: str, revision: str) -> None:
        as_of = query.get("as_of", [None])[0]
        result = self.service.replay(code, revision, as_of=as_of)
        _json_response(self, 200, result.to_dict())

    def _h_demo_build(self, query: dict[str, list[str]]) -> None:
        stats = build_demo(self.service)
        _json_response(self, 200, stats)

    def _h_demo_replay(self, query: dict[str, list[str]]) -> None:
        from .demo import DEMO_CODE, DEMO_REVISION

        result = self.service.replay(DEMO_CODE, DEMO_REVISION)
        _json_response(self, 200, result.to_dict())


def _record_dict(record: Any) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "event_type": record.event_type,
        "occurred_at": record.occurred_at,
        "reported_at": record.reported_at,
        "ingested_at": record.ingested_at,
        "unit_id": record.unit_id,
        "sequence": record.sequence,
        "payload": record.payload,
        "idempotency_key": record.idempotency_key,
        "record_hash": record.record_hash,
        "predecessor_hash": record.predecessor_hash,
    }


def create_server(host: str = "127.0.0.1", port: int = 8080, data_dir: str | Path = ".rr-data") -> ThreadingHTTPServer:
    service = ExerciseService(data_dir)

    class _BoundHandler(ApiHandler):
        pass

    _BoundHandler.service = service
    server = ThreadingHTTPServer((host, port), _BoundHandler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="跨网络韧性演练复盘 API 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", default=".rr-data")
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port, args.data_dir)
    print(f"API listening on http://{args.host}:{args.port} (data={args.data_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
