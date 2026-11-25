"""导演台后端 HTTP API（标准库 ThreadingHTTPServer）。

所有写操作在 service 内部的同一把台账锁上串行化，保证同一资源不会许给两队。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .engine import EngineError
from .gateway import OfflineReceiptClient, ReceiptError
from .ledger import Ledger
from .service import RehearsalService


class RehearsalServer(ThreadingHTTPServer):
    def __init__(self, port: int, service: RehearsalService,
                 receipt_client=None):
        self.service = service
        self.receipt_client = receipt_client
        super().__init__(("127.0.0.1", port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: RehearsalServer

    # ---------- 收发工具 ----------

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 1_048_576:
            raise EngineError("invalid_body", "请求体缺失或过大", 400)
        try:
            payload = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc:
            raise EngineError("invalid_json", str(exc), 400) from exc
        if not isinstance(payload, dict):
            raise EngineError("invalid_body", "请求体必须是 JSON 对象", 400)
        return payload

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            result = fn()
        except EngineError as exc:
            self._send(exc.status, {"code": exc.code, "message": str(exc)})
        except ReceiptError as exc:
            status = 409 if exc.code == "reference_reused" else (
                503 if exc.retryable else 422)
            self._send(status, {"code": exc.code, "message": str(exc),
                                "retryable": exc.retryable})
        except (KeyError, ValueError, TypeError) as exc:
            self._send(400, {"code": "invalid_request", "message": str(exc)})
        else:
            self._send(200, result if result is not None else {"ok": True})

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        service = self.server.service

        def handle() -> Any:
            if path == "/v1/health":
                return {"ok": True,
                        "version": service.ledger.current_sheet.version}
            if path == "/v1/registers":
                return {
                    "segments": {seg: {"stops": stops,
                                       "reports": [r.to_dict()
                                                   for r in reports]}
                                 for seg, (stops, reports) in (
                                     (s, (service.ledger.segment_geometry[s],
                                          service.ledger.segment_reports[s]))
                                     for s in service.ledger.segment_geometry)},
                    "buffers": {k: {"costume": v.costume, "movement": v.movement}
                                for k, v in service.ledger.buffers.items()},
                    "substitutes": [s.to_dict() for s in service.ledger.substitutes],
                    "tour_groups": [g.to_dict()
                                    for g in service.ledger.groups.values()],
                }
            if path == "/v1/segments":
                minute = int(query.get("minute", ["0"])[0])
                return {"minute": minute, "segments": service.segment_status(minute)}
            if path == "/v1/route":
                result = service.route(query["from"][0], query["to"][0],
                                       int(query.get("minute", ["0"])[0]))
                result["blocked"] = [f.to_dict() for f in result["blocked"]]
                return result
            if path == "/v1/sheets/current":
                return service.sheet().to_dict()
            if path.startswith("/v1/sheets/"):
                return service.sheet(path.rsplit("/", 1)[-1]).to_dict()
            if path == "/v1/plans":
                return {"plans": service.list_plans()}
            if path == "/v1/resources":
                return service.resource_view()
            if path == "/v1/progress":
                return {"progress": [p.to_dict()
                                     for p in service.ledger.progress]}
            if path == "/v1/reconciliation":
                return service.reconciliation()
            if path == "/v1/promises":
                from .engine import promise_status
                return {"promises": promise_status(service.ledger)}
            if path == "/v1/events":
                return {"events": [e.to_dict()
                                   for e in service.ledger.events],
                        "cursor": service.ledger.event_cursor}
            if path == "/v1/groups":
                return {"groups": [g.to_dict()
                                   for g in service.ledger.groups.values()]}
            self._send(404, {"code": "not_found"})
            return None

        self._run(handle)

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path
        service = self.server.service

        def handle() -> Any:
            payload = self._body()

            if path == "/v1/segments/reports":
                report = service.report_segment(
                    payload["segment"], payload["direction"],
                    int(payload["minute"]))
                return report.to_dict()

            if path == "/v1/audience/arrivals":
                group = service.register_audience(
                    payload["group"], payload["stop"], int(payload["minute"]),
                    payload.get("disposition", "waiting"))
                return group.to_dict()

            if path == "/v1/plans":
                plan = service.create_plan(
                    payload["changes"], int(payload["minute"]),
                    payload.get("base_version"))
                return plan.to_dict()

            if path.startswith("/v1/plans/") and path.endswith("/confirm"):
                plan_id = path.split("/")[3]
                sheet = service.confirm_plan(
                    plan_id, int(payload["minute"]), payload.get("version"))
                return sheet.to_dict()

            if path == "/v1/progress":
                record = service.record_progress(
                    kind=payload["kind"], minute=int(payload["minute"]),
                    booking_id=payload.get("booking_id"),
                    team=payload.get("team"), stop=payload.get("stop"),
                    groups=payload.get("groups"),
                    version=payload.get("version"))
                return record.to_dict()

            if path == "/v1/fences":
                fence = service.record_fence(
                    payload["stop"], payload["version"],
                    payload["action"], int(payload["minute"]))
                return fence.to_dict()

            if path == "/v1/deliveries":
                state = service.note_delivery(
                    payload["stop"], payload["version"],
                    bool(payload.get("delivered", True)),
                    bool(payload.get("in_effect", False)),
                    int(payload["minute"]))
                return state.to_dict()

            if path == "/v1/distribute":
                client = self.server.receipt_client or OfflineReceiptClient(
                    payload.get("fixtures", "fixtures"))
                return service.distribute(
                    payload["stop"], payload["version"],
                    int(payload["minute"]), client)

            if path == "/v1/guide/route":
                return service.guide_route(
                    payload["group"], payload["to"], int(payload["minute"]))

            if path == "/v1/events/ingest":
                return service.ingest_event(
                    cursor=int(payload["cursor"]),
                    source=payload["source"],
                    minute=int(payload["minute"]),
                    payload=payload["payload"])

            self._send(404, {"code": "not_found"})
            return None

        self._run(handle)

    def log_message(self, *args: Any) -> None:
        return


def build_server(port: int, fixtures_folder: str,
                 snapshot_path: str | None = None,
                 receipt_client=None) -> RehearsalServer:
    """从一组 fixture 引导：装载台账，摄入事件，挂上回执客户端。"""
    from .gateway import load_fixture_bundle
    from .sync import EventSync

    ledger = Ledger(snapshot_path)
    restored = bool(ledger.sheet_order)
    if not restored:
        registers, events, _ = load_fixture_bundle(fixtures_folder)
        ledger.load_registers(registers)
        service = RehearsalService(ledger)
        EventSync(service).ingest_bundle(events)
    else:
        service = RehearsalService(ledger)
    return RehearsalServer(port, service, receipt_client)
