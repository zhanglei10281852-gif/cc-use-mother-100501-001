"""流域联合调度命令行。

示例：
    python -m basin_dispatch.cli --log data/basin.jsonl status
    python -m basin_dispatch.cli --log data/basin.jsonl issue \
        --plan P-001 --actor zhang --role dispatcher --idempotency-key issue-1
    python -m basin_dispatch.cli --log data/basin.jsonl reconstruct \
        --as-of 2026-10-05T00:00:00+00:00
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .events import EventStore
from .service import DomainError, DispatchService, ROLE_CHIEF, ROLE_DISPATCHER, ROLE_REVIEWER, ROLE_APPROVER


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="basin-dispatch", description="流域联合调度命令行")
    parser.add_argument("--log", default="data/basin_events.jsonl", help="事件日志 JSONL 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-topology", help="登记拓扑修订")
    p.add_argument("--file", required=True)

    p = sub.add_parser("promote", help="将拓扑修订设为生效")
    p.add_argument("--revision", required=True)

    p = sub.add_parser("freeze-scenario", help="登记并冻结情景（迟到测报另建）")
    p.add_argument("--file", required=True)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("supersede-scenario")
    p.add_argument("--code", required=True)

    p = sub.add_parser("make-plan", help="编制方案草稿并校验")
    p.add_argument("--file", required=True)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("revise-plan", help="会商驳回后修订方案")
    p.add_argument("--file", required=True)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("submit", help="值班长发起会商")
    p.add_argument("--plan", required=True)
    p.add_argument("--reviewers", type=int, default=2)

    p = sub.add_parser("consult", help="会商成员登记意见")
    p.add_argument("--plan", required=True)
    p.add_argument("--stance", choices=["agree", "disagree", "abstain"], required=True)
    p.add_argument("--comment", default="")

    p = sub.add_parser("close-consultation")
    p.add_argument("--plan", required=True)

    p = sub.add_parser("decide", help="批准领导批准/驳回")
    p.add_argument("--plan", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")
    p.add_argument("--comment", default="")

    p = sub.add_parser("issue", help="批准后签发带序号执行指令")
    p.add_argument("--plan", required=True)
    p.add_argument("--deadline-hours", type=int, default=6)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("ack", help="现场签收回执")
    p.add_argument("--order", required=True)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("execute", help="现场上报实际执行结果")
    p.add_argument("--order", required=True)
    p.add_argument("--file", required=True, help="JSON：{\"actual\": {设施: [逐时段值]}, \"note\": ...}")
    p.add_argument("--idempotency-key")

    p = sub.add_parser("reject", help="现场拒绝指令")
    p.add_argument("--order", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("scan-timeouts", help="扫描超时未闭环指令")
    p.add_argument("--now", help="覆盖当前时间（ISO），便于演练")

    p = sub.add_parser("cancel", help="值班长作废未闭环指令")
    p.add_argument("--order", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("override", help="值班长越权处置并补发纠正指令")
    p.add_argument("--order", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--file", required=True, help="纠正指令载荷（与方案决策同构）")
    p.add_argument("--deadline-hours", type=int, default=3)
    p.add_argument("--idempotency-key")

    p = sub.add_parser("status", help="总览；--open 仅列未闭环指令")
    p.add_argument("--open", action="store_true")

    p = sub.add_parser("order", help="查看单条指令状态与回执")
    p.add_argument("--no", required=True)

    p = sub.add_parser("diff", help="计划值与实际执行差异")
    p.add_argument("--order", required=True)

    p = sub.add_parser("range", help="指令影响的上下游范围")
    p.add_argument("--order", required=True)

    p = sub.add_parser("chain", help="因果关系链")
    p.add_argument("--order", required=True)

    p = sub.add_parser("timeline", help="方案全周期决策理由")
    p.add_argument("--plan", required=True)

    p = sub.add_parser("reconstruct", help="还原任一时刻的情景/决策/执行")
    p.add_argument("--as-of", required=True)

    p = sub.add_parser("recover", help="从事件日志重放并报告未闭环事项")

    p = sub.add_parser("verify-log", help="校验事件日志哈希链完整性")

    p = sub.add_parser("demo", help="灌入台风情景端到端演示数据")
    p.add_argument("--force", action="store_true", help="允许非空日志")

    for sub_parser in sub.choices.values():
        sub_parser.add_argument("--actor", default="cli-operator")
        sub_parser.add_argument("--role", default=None,
                                help="dispatcher/duty_chief/reviewer/approver/field")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    store = EventStore(args.log)
    service = DispatchService(store)
    cmd = args.command
    actor, role = args.actor, args.role

    try:
        if cmd == "init-topology":
            payload = _load_json(args.file)
            event = service.register_topology(
                basin_code=payload["basin_code"], revision=payload["revision"],
                facilities=payload["facilities"], edges=payload["edges"],
                actor=actor)
            _print({"event_id": event.event_id, "fingerprint": event.data["fingerprint"]})

        elif cmd == "promote":
            _print({"event_id": service.promote_topology(
                revision=args.revision, actor=actor).event_id})

        elif cmd == "freeze-scenario":
            payload = _load_json(args.file)
            event = service.freeze_scenario(
                payload, actor=actor, idempotency_key=args.idempotency_key)
            _print({"event_id": event.event_id,
                    "scenario_fingerprint": event.data["fingerprint"]})

        elif cmd == "supersede-scenario":
            _print({"event_id": service.mark_scenario_superseded(
                scenario_code=args.code, actor=actor).event_id})

        elif cmd in ("make-plan", "revise-plan"):
            payload = _load_json(args.file)
            method = service.create_plan_draft if cmd == "make-plan" else service.revise_plan
            event, report = method(payload, actor=actor,
                                   idempotency_key=args.idempotency_key)
            _print({"event_id": event.event_id, "validation": report.to_dict()})

        elif cmd == "submit":
            event = service.submit_for_consultation(
                plan_code=args.plan, required_reviewers=args.reviewers,
                actor=actor, role=role or ROLE_CHIEF)
            _print({"event_id": event.event_id})

        elif cmd == "consult":
            event = service.record_consultation(
                plan_code=args.plan, stance=args.stance, comment=args.comment,
                actor=actor, role=role or ROLE_REVIEWER)
            _print({"event_id": event.event_id})

        elif cmd == "close-consultation":
            event = service.close_consultation(
                plan_code=args.plan, actor=actor, role=role or ROLE_CHIEF)
            _print({"event_id": event.event_id, "passed": event.data["passed"],
                    "reason": event.data["reason"]})

        elif cmd == "decide":
            event = service.decide_plan(
                plan_code=args.plan, approved=args.approve, comment=args.comment,
                actor=actor, role=role or ROLE_APPROVER)
            _print({"event_id": event.event_id, "decision": event.type})

        elif cmd == "issue":
            events = service.issue_orders(
                plan_code=args.plan, actor=actor, role=role or ROLE_DISPATCHER,
                deadline_hours=args.deadline_hours,
                idempotency_key=args.idempotency_key)
            _print({
                "event_ids": [e.event_id for e in events],
                "orders": [o["order_no"] for e in events for o in e.data.get("orders", [])],
            })

        elif cmd == "ack":
            _print({"event_id": service.acknowledge_order(
                order_no=args.order, actor=actor,
                idempotency_key=args.idempotency_key).event_id})

        elif cmd == "execute":
            payload = _load_json(args.file)
            _print({"event_id": service.report_execution(
                order_no=args.order, actual=payload["actual"],
                note=payload.get("note", ""), actor=actor,
                idempotency_key=args.idempotency_key).event_id})

        elif cmd == "reject":
            _print({"event_id": service.reject_order(
                order_no=args.order, reason=args.reason, actor=actor,
                idempotency_key=args.idempotency_key).event_id})

        elif cmd == "scan-timeouts":
            events = service.scan_timeouts(now=args.now, actor=actor)
            _print({"timed_out": [e.data["order_no"] for e in events]})

        elif cmd == "cancel":
            event = service.cancel_order(
                order_no=args.order, reason=args.reason,
                actor=actor, role=role or ROLE_CHIEF,
                idempotency_key=args.idempotency_key)
            _print({"event_id": event.event_id, "status": "cancelled"})

        elif cmd == "override":
            corrective = _load_json(args.file)
            events = service.manual_override(
                order_no=args.order, reason=args.reason, corrective=corrective,
                actor=actor, role=role or ROLE_CHIEF,
                deadline_hours=args.deadline_hours,
                idempotency_key=args.idempotency_key)
            _print({
                "event_ids": [e.event_id for e in events],
                "corrective_order": events[-1].data["orders"][0]["order_no"]
                if events[-1].type == "OrdersIssued" else None,
            })

        elif cmd == "status":
            if args.open:
                _print(service.open_items())
            else:
                _print({
                    "events": len(store.events),
                    "topology": service.state.current_topology_revision,
                    "scenarios": [
                        {"code": s.scenario_code,
                         "forecast_revision": s.forecast_revision,
                         "state": service.state.scenario_states[s.scenario_code].value}
                        for s in service.state.scenarios.values()
                    ],
                    "plans": [
                        {"code": v.plan.plan_code, "status": v.status, "valid": v.valid}
                        for v in service.state.plans.values()
                    ],
                    "orders": [
                        {"no": no, "status": service.state.orders[no].status}
                        for no in sorted(service.state.orders)
                    ],
                    "open_orders": [o.order_no for o in service.state.open_orders()],
                })

        elif cmd == "order":
            _print(service.order_status(args.no))

        elif cmd == "diff":
            _print(service.execution_diff(args.order))

        elif cmd == "range":
            _print(service.affected_range(args.order))

        elif cmd == "chain":
            _print({"order_no": args.order, "chain": service.causation_chain(args.order)})

        elif cmd == "timeline":
            _print(service.plan_timeline(args.plan))

        elif cmd == "reconstruct":
            _print(service.reconstruct_at(args.as_of))

        elif cmd == "recover":
            service.recover()
            _print({"replayed_events": len(store.events),
                    "open_orders": [o.order_no for o in service.state.open_orders()]})

        elif cmd == "verify-log":
            # 重新打开即触发全链校验
            EventStore(args.log)
            _print({"ok": True, "events": len(store.events), "log": args.log})

        elif cmd == "demo":
            from .demo import build_demo
            if store.events and not args.force:
                raise DomainError("事件日志非空，加 --force 可在其后继续追加演示数据")
            build_demo(service)
            _print({"ok": True, "events": len(store.events),
                    "open_orders": [o.order_no for o in service.state.open_orders()]})

    except DomainError as exc:
        _print({"error": str(exc)})
        return 2
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        _print({"error": f"输入文件或参数错误: {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
