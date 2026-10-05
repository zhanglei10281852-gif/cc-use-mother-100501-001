"""流域联合调度 HTTP API（仅依赖标准库）。

启动：
    python -m basin_dispatch.api [--host 127.0.0.1] [--port 8080] \\
        --log data/basin_events.jsonl

身份通过请求头传递：X-Actor（操作人）、X-Role（角色）；
重试请求携带 Idempotency-Key，服务端保证同一键不产生第二条有效指令。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .events import EventStore
from .service import DomainError, DispatchService

DEFAULT_LOG = os.environ.get("BASIN_DISPATCH_LOG", "data/basin_events.jsonl")


class ApiContext:
    def __init__(self, service: DispatchService) -> None:
        self.service = service


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class DispatchHandler(BaseHTTPRequestHandler):
    ctx: ApiContext  # 由 make_server 注入到类属性

    # 静默标准访问日志以外的噪音；保留一行式访问记录
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} - {fmt % args}")

    # ---------- 基础 ----------

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _HttpError(400, f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise _HttpError(400, "请求体必须是 JSON 对象")
        return payload

    def _identity(self, payload: dict[str, Any]) -> tuple[str, str | None, str | None]:
        actor = self.headers.get("X-Actor") or payload.pop("_actor", None)
        role = self.headers.get("X-Role") or payload.pop("_role", None)
        if not actor:
            raise _HttpError(401, "缺少操作人身份（X-Actor 头）")
        idem = self.headers.get("Idempotency-Key") or payload.pop("_idempotency_key", None)
        return actor, role, idem

    def _handle(self, fn: Callable[[], Any]) -> None:
        try:
            result = fn()
        except _HttpError as exc:
            _json_response(self, exc.status, {"error": exc.message})
        except DomainError as exc:
            _json_response(self, 422, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            _json_response(self, 400, {"error": f"请求参数错误: {exc}"})
        else:
            _json_response(self, 200, result if result is not None else {"ok": True})

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)

        def route() -> Any:
            svc = self.ctx.service
            if parts == ["health"]:
                return {"status": "ok", "events": len(svc.store.events),
                        "open_orders": len(svc.state.open_orders())}
            if parts == ["topology"]:
                topo = svc.state.topology
                return {
                    "basin_code": topo.basin_code, "revision": topo.revision,
                    "fingerprint": topo.fingerprint,
                    "facilities": [
                        {"code": f.code, "name": f.name, "type": f.facility_type.value,
                         "boundaries": f.boundaries, "serves": f.serves}
                        for f in sorted(topo.facilities.values(), key=lambda x: x.code)
                    ],
                    "edges": sorted(
                        (up, down) for up, ts in topo.downstream_of.items() for down in ts
                    ),
                }
            if parts == ["scenarios"]:
                return {
                    "scenarios": [
                        {**s.to_data(), "state": svc.state.scenario_states[s.scenario_code].value}
                        for s in sorted(svc.state.scenarios.values(),
                                        key=lambda x: x.scenario_code)
                    ]
                }
            if parts == ["orders"]:
                if query.get("open") == ["1"]:
                    return svc.open_items()
                return {"orders": [svc.order_status(no) for no in
                                   sorted(svc.state.orders)]}
            if len(parts) == 3 and parts[0] == "plans" and parts[2] == "timeline":
                return svc.plan_timeline(parts[1])
            if len(parts) == 2 and parts[0] == "orders":
                return svc.order_status(parts[1])
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "diff":
                return svc.execution_diff(parts[1])
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "range":
                return svc.affected_range(parts[1])
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "chain":
                return {"order_no": parts[1], "chain": svc.causation_chain(parts[1])}
            if parts == ["events"]:
                return {"events": [e.to_dict() for e in svc.store.events]}
            if parts == ["state"]:
                as_of = query.get("as_of", [None])[0]
                if not as_of:
                    raise _HttpError(400, "缺少 as_of 查询参数（ISO 时间）")
                return svc.reconstruct_at(as_of)
            raise _HttpError(404, f"未知路径: {self.path}")

        self._handle(route)

    def do_POST(self) -> None:  # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        payload = self._read_json()

        def route() -> Any:
            svc = self.ctx.service
            actor, role, idem = self._identity(payload)

            if parts == ["topologies"]:
                event = svc.register_topology(
                    basin_code=payload["basin_code"], revision=payload["revision"],
                    facilities=payload["facilities"], edges=payload["edges"],
                    actor=actor, idempotency_key=idem)
                return {"event_id": event.event_id, "fingerprint": event.data["fingerprint"]}

            if parts == ["topologies", "promote"]:
                event = svc.promote_topology(revision=payload["revision"], actor=actor)
                return {"event_id": event.event_id}

            if parts == ["scenarios"]:
                event = svc.freeze_scenario(payload, actor=actor, idempotency_key=idem)
                return {"event_id": event.event_id,
                        "scenario_fingerprint": event.data["fingerprint"]}

            if len(parts) == 3 and parts[0] == "scenarios" and parts[2] == "supersede":
                event = svc.mark_scenario_superseded(scenario_code=parts[1], actor=actor)
                return {"event_id": event.event_id}

            if parts == ["plans"]:
                event, report = svc.create_plan_draft(
                    payload, actor=actor, idempotency_key=idem)
                return {"event_id": event.event_id, "validation": report.to_dict()}

            if len(parts) == 3 and parts[0] == "plans" and parts[2] == "revise":
                payload["plan_code"] = parts[1]
                event, report = svc.revise_plan(
                    payload, actor=actor, idempotency_key=idem)
                return {"event_id": event.event_id, "validation": report.to_dict()}

            if len(parts) == 3 and parts[0] == "plans" and parts[2] == "submit":
                event = svc.submit_for_consultation(
                    plan_code=parts[1],
                    required_reviewers=int(payload.get("required_reviewers", 2)),
                    actor=actor, role=role or "")
                return {"event_id": event.event_id}

            if len(parts) == 3 and parts[0] == "plans" and parts[2] == "consult":
                event = svc.record_consultation(
                    plan_code=parts[1], stance=payload["stance"],
                    comment=payload.get("comment", ""), actor=actor, role=role or "")
                return {"event_id": event.event_id}

            if len(parts) == 3 and parts[0] == "plans" and parts[2] == "close-consultation":
                event = svc.close_consultation(
                    plan_code=parts[1], actor=actor, role=role or "")
                return {"event_id": event.event_id, "passed": event.data["passed"],
                        "reason": event.data["reason"]}

            if len(parts) == 3 and parts[0] == "plans" and parts[2] == "decide":
                event = svc.decide_plan(
                    plan_code=parts[1], approved=bool(payload["approved"]),
                    comment=payload.get("comment", ""), actor=actor, role=role or "")
                return {"event_id": event.event_id, "decision": event.type}

            if len(parts) == 3 and parts[0] == "plans" and parts[2] == "issue":
                events = svc.issue_orders(
                    plan_code=parts[1], actor=actor, role=role or "",
                    deadline_hours=int(payload.get("deadline_hours", 6)),
                    idempotency_key=idem)
                return {"event_ids": [e.event_id for e in events],
                        "orders": [no for e in events for no in
                                   (o["order_no"] for o in e.data.get("orders", []))]}

            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "ack":
                event = svc.acknowledge_order(
                    order_no=parts[1], actor=actor, idempotency_key=idem)
                return {"event_id": event.event_id}

            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "execute":
                event = svc.report_execution(
                    order_no=parts[1], actual=payload.get("actual", {}),
                    note=payload.get("note", ""), actor=actor, idempotency_key=idem)
                return {"event_id": event.event_id}

            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "reject":
                event = svc.reject_order(
                    order_no=parts[1], reason=payload.get("reason", ""),
                    actor=actor, idempotency_key=idem)
                return {"event_id": event.event_id}

            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "cancel":
                event = svc.cancel_order(
                    order_no=parts[1], reason=payload.get("reason", ""),
                    actor=actor, role=role or "", idempotency_key=idem)
                return {"event_id": event.event_id}

            if parts == ["orders", "scan-timeouts"]:
                events = svc.scan_timeouts(now=payload.get("now"), actor=actor)
                return {"timed_out": [e.data["order_no"] for e in events]}

            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "override":
                events = svc.manual_override(
                    order_no=parts[1], reason=payload["reason"],
                    corrective=payload["corrective"], actor=actor, role=role or "",
                    deadline_hours=int(payload.get("deadline_hours", 3)),
                    idempotency_key=idem)
                return {
                    "event_ids": [e.event_id for e in events],
                    "corrective_order": events[-1].data["orders"][0]["order_no"]
                    if events[-1].type == "OrdersIssued" else None,
                }

            raise _HttpError(404, f"未知路径: {self.path}")

        self._handle(route)


class _HttpError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def make_server(host: str, port: int, log_path: str | Path) -> ThreadingHTTPServer:
    store = EventStore(log_path)
    service = DispatchService(store)
    context = ApiContext(service)
    handler = type("BoundDispatchHandler", (DispatchHandler,), {"ctx": context})
    server = ThreadingHTTPServer((host, port), handler)
    server.service = service  # type: ignore[attr-defined]
    server.context = context  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="流域联合调度 API 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log", default=DEFAULT_LOG, help="事件日志 JSONL 路径")
    args = parser.parse_args(argv)

    server = make_server(args.host, args.port, args.log)
    svc: DispatchService = server.service  # type: ignore[attr-defined]
    print(f"流域联合调度服务已启动: http://{args.host}:{args.port}")
    print(f"事件日志: {args.log}（已重放 {len(svc.store.events)} 个事件，"
          f"未闭环指令 {len(svc.state.open_orders())} 条）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断信号，服务退出（状态可从事件日志恢复）")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
