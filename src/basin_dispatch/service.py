"""流域联合调度应用服务：事件溯源的唯一命令入口。

状态全部从 EventStore 重放得到；进程重启后重放事件日志即可恢复
拓扑修订、冻结情景、方案会商与批准记录、未闭环指令等全部事项。

角色：
    dispatcher  值班调度员（编制/修订方案）
    duty_chief  值班长（主持会商、人工越权处置）
    reviewer    会商成员
    approver    批准领导
    field       现场执行单位（回执 / 拒绝 / 执行结果）
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .engine import Plan, Severity, ValidationReport, validate_plan
from .events import Event, EventStore, PendingEvent
from .scenario import Scenario, ScenarioState
from .topology import FacilityType, Topology, build_topology

ROLE_DISPATCHER = "dispatcher"
ROLE_CHIEF = "duty_chief"
ROLE_REVIEWER = "reviewer"
ROLE_APPROVER = "approver"
ROLE_FIELD = "field"

# 指令未终态（服务重启后仍需跟踪的未闭环事项）
OPEN_STATUSES = {"issued", "acknowledged", "rejected", "timed_out"}
TERMINAL_STATUSES = {"executed", "overridden", "cancelled"}


class DomainError(RuntimeError):
    """业务规则被违反。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DomainError(message)


@dataclass(slots=True)
class OrderItem:
    facility: str
    action: str                       # set_gate_opening / set_release / set_intake
    series: list[float]
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return {"facility": self.facility, "action": self.action,
                "series": self.series, "unit": self.unit}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderItem":
        return cls(data["facility"], data["action"], list(data["series"]), data["unit"])


@dataclass(slots=True)
class OrderView:
    order_no: str
    plan_code: str
    scenario_code: str
    scenario_fingerprint: str
    topology_revision: str
    items: list[OrderItem]
    status: str
    created_event_id: str
    issued_at: str
    deadline: str
    issued_by: str
    facility_codes: list[str]
    actual: dict[str, list[float | None]] = field(default_factory=dict)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    superseded_by: str | None = None

    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


@dataclass(slots=True)
class PlanView:
    plan: Plan
    topology_revision: str
    status: str
    valid: bool
    violations: list[dict[str, Any]]
    reviews: list[dict[str, Any]] = field(default_factory=list)
    required_reviewers: int = 0
    approval: dict[str, Any] | None = None
    order_numbers: list[str] = field(default_factory=list)
    created_event_id: str = ""


@dataclass(slots=True)
class Projection:
    """从事件流重放出的当前状态。"""

    topologies: dict[str, Topology] = field(default_factory=dict)
    current_topology_revision: str | None = None
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    scenario_states: dict[str, ScenarioState] = field(default_factory=dict)
    plans: dict[str, PlanView] = field(default_factory=dict)
    orders: dict[str, OrderView] = field(default_factory=dict)
    order_counter: int = 0
    basin_of: dict[str, str] = field(default_factory=dict)      # scenario_code -> basin
    plan_basin: dict[str, str] = field(default_factory=dict)

    @property
    def topology(self) -> Topology:
        _require(self.current_topology_revision is not None, "尚无生效拓扑")
        return self.topologies[self.current_topology_revision]  # type: ignore[index]

    def open_orders(self) -> list[OrderView]:
        return [o for o in self.orders.values() if o.is_open()]


class DispatchService:
    """命令处理与重放。所有写操作都经由 append(_many) 落事件。"""

    def __init__(self, store: EventStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.clock = clock or store.clock
        store.clock = self.clock  # 事件时间与业务判断共用同一时钟
        self.state = Projection()
        self.recover()

    # ================= 恢复 =================

    def recover(self) -> Projection:
        """重放事件日志，重建全部状态（服务启动/重启时调用）。"""
        self.state = self._replay_until(None)
        return self.state

    def _replay_until(self, as_of: str | None) -> Projection:
        state = Projection()
        for event in self.store.events:
            if as_of is not None and event.metadata["occurred_at"] > as_of:
                break
            self._apply(state, event)
        return state

    @staticmethod
    def _apply(state: Projection, event: Event) -> None:
        data = event.data
        t = event.type
        if t == "TopologyRegistered":
            topo = _topology_from_data(data)
            state.topologies[data["revision"]] = topo
        elif t == "TopologyPromoted":
            state.current_topology_revision = data["revision"]
        elif t == "ScenarioDefined":
            scenario = Scenario.from_data(data, ScenarioState.FROZEN)
            state.scenarios[scenario.scenario_code] = scenario
            state.scenario_states[scenario.scenario_code] = ScenarioState.FROZEN
            state.basin_of[scenario.scenario_code] = scenario.basin_code
        elif t == "ScenarioSuperseded":
            state.scenario_states[data["scenario_code"]] = ScenarioState.SUPERSEDED
        elif t in ("PlanDraftCreated", "PlanRevised"):
            plan = Plan.from_data(data["plan"])
            state.plans[plan.plan_code] = PlanView(
                plan=plan,
                topology_revision=data["topology_revision"],
                status="draft" if t == "PlanRevised" else data.get("status", "draft"),
                valid=data["valid"],
                violations=list(data["violations"]),
                reviews=[] if t == "PlanRevised" else [],
                required_reviewers=0,
                created_event_id=event.event_id,
            )
            state.plan_basin[plan.plan_code] = data["basin_code"]
        elif t == "PlanSubmitted":
            view = state.plans[data["plan_code"]]
            view.status = "consulting"
            view.required_reviewers = data["required_reviewers"]
        elif t == "ConsultationRecorded":
            state.plans[data["plan_code"]].reviews.append(
                {"reviewer": data["reviewer"], "stance": data["stance"],
                 "comment": data["comment"], "at": event.metadata["occurred_at"],
                 "event_id": event.event_id}
            )
        elif t == "ConsultationClosed":
            state.plans[data["plan_code"]].status = (
                "consultation_passed" if data["passed"] else "returned"
            )
        elif t == "PlanApproved":
            view = state.plans[data["plan_code"]]
            view.status = "approved"
            view.approval = {"approver": data["approver"], "comment": data["comment"],
                             "at": event.metadata["occurred_at"], "event_id": event.event_id}
        elif t == "PlanRejected":
            view = state.plans[data["plan_code"]]
            view.status = "rejected"
            view.approval = {"approver": data["approver"], "comment": data["comment"],
                             "at": event.metadata["occurred_at"], "event_id": event.event_id}
        elif t == "OrdersIssued":
            state.order_counter = data["last_sequence"]
            for order_data in data["orders"]:
                items = [OrderItem.from_dict(i) for i in order_data["items"]]
                order = OrderView(
                    order_no=order_data["order_no"],
                    plan_code=order_data["plan_code"],
                    scenario_code=order_data["scenario_code"],
                    scenario_fingerprint=order_data["scenario_fingerprint"],
                    topology_revision=order_data["topology_revision"],
                    items=items,
                    status="issued",
                    created_event_id=event.event_id,
                    issued_at=order_data["issued_at"],
                    deadline=order_data["deadline"],
                    issued_by=order_data["issued_by"],
                    facility_codes=sorted({i.facility for i in items}),
                )
                state.orders[order.order_no] = order
                plan_view = state.plans.get(order.plan_code)
                if plan_view is not None:
                    plan_view.order_numbers.append(order.order_no)
        elif t in ("OrderAcked", "OrderExecutionReported", "OrderRejected",
                   "OrderTimedOut", "OrderOverridden", "OrderCancelled"):
            order = state.orders[data["order_no"]]
            receipt = {"type": t, "actor": event.metadata["actor"],
                       "at": event.metadata["occurred_at"], "event_id": event.event_id,
                       "causation_id": event.metadata.get("causation_id"),
                       "data": data}
            order.receipts.append(receipt)
            if t == "OrderAcked" and order.status == "issued":
                order.status = "acknowledged"
            elif t == "OrderExecutionReported":
                order.status = "executed"
                for facility, actual_series in data.get("actual", {}).items():
                    merged = list(order.actual.get(facility, []))
                    for idx, value in enumerate(actual_series):
                        while len(merged) <= idx:
                            merged.append(None)
                        merged[idx] = value
                    order.actual[facility] = merged
            elif t == "OrderRejected":
                order.status = "rejected"
            elif t == "OrderTimedOut":
                if order.status in ("issued", "acknowledged"):
                    order.status = "timed_out"
            elif t == "OrderOverridden":
                order.status = "overridden"
                order.overrides.append({"by": event.metadata["actor"],
                                        "reason": data["reason"],
                                        "at": event.metadata["occurred_at"],
                                        "corrective_order": data.get("corrective_order")})
            elif t == "OrderCancelled":
                order.status = "cancelled"
                if data.get("superseded_by"):
                    order.superseded_by = data["superseded_by"]

    # ================= 拓扑 =================

    def register_topology(self, *, basin_code: str, revision: str,
                          facilities: list[dict[str, Any]], edges: list[list[str]],
                          actor: str, idempotency_key: str | None = None) -> Event:
        _require(basin_code.strip() and revision.strip(), "流域编码与拓扑修订号不能为空")
        # 先构造一遍以触发全部结构校验
        topo = _topology_from_data(
            {"basin_code": basin_code, "revision": revision,
             "facilities": facilities, "edges": edges}
        )
        _require(revision not in self.state.topologies, f"拓扑修订 {revision} 已存在")
        event = self.store.append(
            f"topology:{basin_code}", "TopologyRegistered",
            {"basin_code": basin_code, "revision": revision,
             "facilities": facilities, "edges": edges,
             "fingerprint": topo.fingerprint},
            actor=actor, idempotency_key=idempotency_key,
        )
        self._apply(self.state, event)
        return event

    def promote_topology(self, *, revision: str, actor: str) -> Event:
        _require(revision in self.state.topologies, f"拓扑修订 {revision} 不存在")
        event = self.store.append(
            f"topology:{self.state.topologies[revision].basin_code}",
            "TopologyPromoted", {"revision": revision}, actor=actor,
        )
        self._apply(self.state, event)
        return event

    # ================= 情景 =================

    def freeze_scenario(self, payload: dict[str, Any], *, actor: str,
                        idempotency_key: str | None = None) -> Event:
        """登记并冻结一个情景。迟到测报应使用新的 scenario_code/修订号。"""
        _require(self.state.current_topology_revision is not None, "尚无生效拓扑，无法冻结情景")
        topo = self.state.topology
        if payload.get("topology_revision"):
            _require(
                payload["topology_revision"] == topo.revision,
                f"情景基于拓扑 {payload['topology_revision']}，当前生效 {topo.revision}",
            )
        from .scenario import build_scenario

        scenario = build_scenario(
            scenario_code=payload["scenario_code"],
            topology=topo,
            forecast_revision=payload["forecast_revision"],
            horizon_hours=int(payload["horizon_hours"]),
            step_hours=int(payload["step_hours"]),
            boundary_inflows=payload.get("boundary_inflows", {}),
            reservoir_gains=payload.get("reservoir_gains"),
            demands=payload.get("demands"),
            initial_storage=payload["initial_storage"],
        )
        _require(scenario.scenario_code not in self.state.scenarios,
                 f"情景 {scenario.scenario_code} 已存在；迟到测报必须新建情景，不得改写")
        for existing in self.state.scenarios.values():
            _require(
                not (existing.basin_code == scenario.basin_code
                     and existing.forecast_revision == scenario.forecast_revision),
                f"雨情修订号 {scenario.forecast_revision} 在本流域已被使用",
            )
        data = scenario.to_data()
        event = self.store.append(
            f"scenario:{scenario.scenario_code}", "ScenarioDefined", data,
            actor=actor, idempotency_key=idempotency_key,
        )
        self._apply(self.state, event)
        return event

    def mark_scenario_superseded(self, *, scenario_code: str, actor: str) -> Event:
        """把仅用于编制中的旧情景标记为被替代；已执行依据永不改变。"""
        _require(scenario_code in self.state.scenarios, f"情景 {scenario_code} 不存在")
        event = self.store.append(
            f"scenario:{scenario_code}", "ScenarioSuperseded",
            {"scenario_code": scenario_code}, actor=actor,
        )
        self._apply(self.state, event)
        return event

    # ================= 方案：编制 / 会商 / 批准 =================

    def _draft(self, payload: dict[str, Any], *, actor: str, revise: bool,
               idempotency_key: str | None) -> tuple[Event, ValidationReport]:
        _require(actor, "缺少编制人")
        scenario = self.state.scenarios.get(payload["scenario_code"])
        _require(scenario is not None, f"情景 {payload['scenario_code']} 不存在")
        _require(self.state.scenario_states[scenario.scenario_code] is ScenarioState.FROZEN,
                 "只能在冻结情景上编制方案")
        topo = self.state.topologies.get(scenario.topology_revision)
        _require(topo is not None, "情景绑定的拓扑修订已缺失，无法复算")

        plan = Plan(
            plan_code=payload["plan_code"],
            scenario_code=scenario.scenario_code,
            scenario_fingerprint=scenario.fingerprint(),
            title=payload.get("title", ""),
            rationale=payload.get("rationale", ""),
            edge_flows={k: tuple(v) for k, v in payload["edge_flows"].items()},
            gate_openings={k: tuple(v) for k, v in payload.get("gate_openings", {}).items()},
            intake_allocation={
                code: {use: tuple(s) for use, s in alloc.items()}
                for code, alloc in payload.get("intake_allocation", {}).items()
            },
        )
        report = validate_plan(topo, scenario, plan)

        if revise:
            existing = self.state.plans.get(plan.plan_code)
            _require(existing is not None, f"方案 {plan.plan_code} 不存在，无从修订")
            _require(existing.status in ("draft", "returned", "rejected"),
                     f"方案处于 {existing.status}，不可修订；如需调整请另立新方案")
            _require(existing.plan.scenario_code == plan.scenario_code,
                     "修订不得切换情景；新测报请新建方案")
            expected = self._stream_version(f"plan:{plan.plan_code}")
            event_type = "PlanRevised"
        else:
            _require(plan.plan_code not in self.state.plans,
                     f"方案编号 {plan.plan_code} 已存在")
            expected = None
            event_type = "PlanDraftCreated"

        data = {
            "basin_code": scenario.basin_code,
            "topology_revision": topo.revision,
            "plan": plan.to_data(),
            "valid": report.valid,
            "violations": [v.to_dict() for v in report.violations],
            "status": "draft",
        }
        event = self.store.append(
            f"plan:{plan.plan_code}", event_type, data, actor=actor,
            idempotency_key=idempotency_key, expected_version=expected,
        )
        self._apply(self.state, event)
        return event, report

    def create_plan_draft(self, payload: dict[str, Any], *, actor: str,
                          idempotency_key: str | None = None) -> tuple[Event, ValidationReport]:
        return self._draft(payload, actor=actor, revise=False,
                           idempotency_key=idempotency_key)

    def revise_plan(self, payload: dict[str, Any], *, actor: str,
                    idempotency_key: str | None = None) -> tuple[Event, ValidationReport]:
        return self._draft(payload, actor=actor, revise=True,
                           idempotency_key=idempotency_key)

    def submit_for_consultation(self, *, plan_code: str, required_reviewers: int,
                                actor: str, role: str) -> Event:
        _require(role == ROLE_CHIEF, "只有值班长可以发起会商")
        view = self._plan(plan_code)
        _require(view.status == "draft", f"方案状态 {view.status}，不能发起会商")
        _require(required_reviewers >= 1, "会商至少需要 1 名成员")
        event = self.store.append(
            f"plan:{plan_code}", "PlanSubmitted",
            {"plan_code": plan_code, "required_reviewers": required_reviewers},
            actor=actor, role=role,
            expected_version=self._stream_version(f"plan:{plan_code}"),
        )
        self._apply(self.state, event)
        return event

    def record_consultation(self, *, plan_code: str, stance: str, comment: str,
                            actor: str, role: str) -> Event:
        _require(role == ROLE_REVIEWER, "只有会商成员可以登记会商意见")
        _require(stance in ("agree", "disagree", "abstain"), "会商立场非法")
        view = self._plan(plan_code)
        _require(view.status == "consulting", "方案不在会商中")
        _require(not any(r["reviewer"] == actor for r in view.reviews),
                 f"{actor} 已登记过会商意见，不得重复表态")
        event = self.store.append(
            f"plan:{plan_code}", "ConsultationRecorded",
            {"plan_code": plan_code, "reviewer": actor, "stance": stance,
             "comment": comment},
            actor=actor, role=role,
            expected_version=self._stream_version(f"plan:{plan_code}"),
        )
        self._apply(self.state, event)
        return event

    def close_consultation(self, *, plan_code: str, actor: str, role: str) -> Event:
        _require(role == ROLE_CHIEF, "只有值班长可以结束会商")
        view = self._plan(plan_code)
        _require(view.status == "consulting", "方案不在会商中")
        agrees = sum(1 for r in view.reviews if r["stance"] == "agree")
        disagrees = [r for r in view.reviews if r["stance"] == "disagree"]
        passed = (
            agrees >= view.required_reviewers
            and not disagrees
            and view.valid
        )
        event = self.store.append(
            f"plan:{plan_code}", "ConsultationClosed",
            {"plan_code": plan_code, "passed": passed,
             "agrees": agrees, "disagrees": len(disagrees),
             "reason": ("硬约束校验未通过" if not view.valid else
                        f"同意 {agrees} 人/需 {view.required_reviewers} 人，反对 {len(disagrees)} 人")},
            actor=actor, role=role,
            causation_id=view.reviews[-1]["event_id"] if view.reviews else view.created_event_id,
            expected_version=self._stream_version(f"plan:{plan_code}"),
        )
        self._apply(self.state, event)
        return event

    def decide_plan(self, *, plan_code: str, approved: bool, comment: str,
                    actor: str, role: str) -> Event:
        _require(role == ROLE_APPROVER, "只有批准领导可以批准/驳回方案")
        view = self._plan(plan_code)
        _require(view.status in ("consultation_passed", "returned"),
                 f"方案状态 {view.status}，尚不能审批")
        if approved:
            _require(view.status == "consultation_passed",
                     "会商未通过的方案不能批准")
            _require(view.valid, "存在防洪/饮水/生态硬约束违规，不能批准")
        event_type = "PlanApproved" if approved else "PlanRejected"
        prior = self.store.stream_events(f"plan:{plan_code}")
        causation = next(
            (e.event_id for e in reversed(prior) if e.type == "ConsultationClosed"), None)
        event = self.store.append(
            f"plan:{plan_code}", event_type,
            {"plan_code": plan_code, "approver": actor, "comment": comment},
            actor=actor, role=role, causation_id=causation,
            expected_version=self._stream_version(f"plan:{plan_code}"),
        )
        self._apply(self.state, event)
        return event

    # ================= 指令：编号签发（幂等） =================

    def issue_orders(self, *, plan_code: str, actor: str, role: str,
                     deadline_hours: int = 6,
                     idempotency_key: str | None = None) -> list[Event]:
        """批准后由调度员签发；同幂等键的重试只返回首次批次，绝不产生两条有效指令。"""
        _require(role == ROLE_DISPATCHER, "只有值班调度员可以签发执行指令")
        _require(deadline_hours > 0, "指令签收时限必须为正")
        # 幂等重试优先：同键双击直接返回首次批次
        existing = self.store.find_idempotent(idempotency_key) if idempotency_key else None
        if existing is not None:
            return [existing]
        view = self._plan(plan_code)
        _require(view.status == "approved", "只有批准后的方案才能生成执行指令")
        _require(not view.order_numbers,
                 "该方案已生成执行指令，重复签发被拒绝；重试请携带同一幂等键，"
                 "如需调整请走越权纠正流程")
        scenario = self.state.scenarios[view.plan.scenario_code]
        topo = self.state.topologies[view.topology_revision]

        items_by_facility = _derive_order_items(topo, view.plan)
        now = self.clock()
        start = self.state.order_counter
        basin = self.state.plan_basin[plan_code]
        orders_payload = []
        for offset, (facility_code, items) in enumerate(sorted(items_by_facility.items())):
            order_no = _format_order_no(basin, start + offset + 1)
            orders_payload.append({
                "order_no": order_no,
                "plan_code": plan_code,
                "scenario_code": scenario.scenario_code,
                "scenario_fingerprint": scenario.fingerprint(),
                "topology_revision": topo.revision,
                "facility": facility_code,
                "items": [i.to_dict() for i in items],
                "issued_at": now,
                "deadline": _plus_hours(now, deadline_hours),
                "issued_by": actor,
            })

        # 编号分配走流域指令流，版本号防止并发重复发号
        pending = [
            PendingEvent(
                stream_id=f"orders:{basin}",
                event_type="OrdersIssued",
                data={"plan_code": plan_code, "start_sequence": start + 1,
                      "last_sequence": start + len(orders_payload),
                      "orders": orders_payload},
                actor=actor, role=role, occurred_at=now,
                causation_id=view.approval["event_id"],
                correlation_id=plan_code,
                idempotency_key=idempotency_key,
                expected_version=self._stream_version(f"orders:{basin}"),
            )
        ]
        events = self.store.append_many(pending)
        for event in events:
            self._apply(self.state, event)
        return events

    # ================= 现场闭环 =================

    def _order(self, order_no: str) -> OrderView:
        order = self.state.orders.get(order_no)
        _require(order is not None, f"指令 {order_no} 不存在")
        return order

    def acknowledge_order(self, *, order_no: str, actor: str,
                          idempotency_key: str | None = None) -> Event:
        order = self._order(order_no)
        _require(order.status in ("issued", "acknowledged"),
                 f"指令状态 {order.status}，无需回执")
        event = self.store.append(
            f"order:{order_no}", "OrderAcked", {"order_no": order_no},
            actor=actor, role=ROLE_FIELD,
            causation_id=order.created_event_id, correlation_id=order_no,
            idempotency_key=idempotency_key,
            expected_version=self._stream_version(f"order:{order_no}"),
        )
        self._apply(self.state, event)
        return event

    def report_execution(self, *, order_no: str, actual: dict[str, list[float]],
                         note: str, actor: str,
                         idempotency_key: str | None = None) -> Event:
        order = self._order(order_no)
        _require(order.status in ("issued", "acknowledged", "timed_out"),
                 f"指令已处于终态 {order.status}，不能再报执行")
        for facility in actual:
            _require(facility in order.facility_codes,
                     f"执行结果设施 {facility} 不属于本指令")
        event = self.store.append(
            f"order:{order_no}", "OrderExecutionReported",
            {"order_no": order_no, "actual": actual, "note": note},
            actor=actor, role=ROLE_FIELD,
            causation_id=order.created_event_id, correlation_id=order_no,
            idempotency_key=idempotency_key,
            expected_version=self._stream_version(f"order:{order_no}"),
        )
        self._apply(self.state, event)
        return event

    def reject_order(self, *, order_no: str, reason: str, actor: str,
                     idempotency_key: str | None = None) -> Event:
        _require(reason.strip(), "拒绝指令必须填写原因")
        order = self._order(order_no)
        _require(order.status in ("issued", "acknowledged"),
                 f"指令状态 {order.status}，不能拒绝")
        event = self.store.append(
            f"order:{order_no}", "OrderRejected",
            {"order_no": order_no, "reason": reason},
            actor=actor, role=ROLE_FIELD,
            causation_id=order.created_event_id, correlation_id=order_no,
            idempotency_key=idempotency_key,
            expected_version=self._stream_version(f"order:{order_no}"),
        )
        self._apply(self.state, event)
        return event

    def scan_timeouts(self, *, now: str | None = None, actor: str = "system") -> list[Event]:
        """把超过签收/执行时限仍未闭环的指令置为超时（可重复扫描，幂等由状态守卫保证）。"""
        now = now or self.clock()
        events: list[Event] = []
        for order in list(self.state.orders.values()):
            if order.status in ("issued", "acknowledged") and order.deadline < now:
                event = self.store.append(
                    f"order:{order.order_no}", "OrderTimedOut",
                    {"order_no": order.order_no, "deadline": order.deadline},
                    actor=actor, role="system", occurred_at=now,
                    causation_id=order.created_event_id, correlation_id=order.order_no,
                )
                self._apply(self.state, event)
                events.append(event)
        return events

    def manual_override(self, *, order_no: str, reason: str, corrective: dict[str, Any],
                        actor: str, role: str, deadline_hours: int = 3,
                        idempotency_key: str | None = None) -> list[Event]:
        """值班长对被拒/超时等未闭环指令人工越权：记录因果，作废原指令并补发纠正指令。

        corrective 与方案签发载荷同构（edge_flows/gate_openings/...），但这里只允许
        针对原指令涉及的同一组设施，防止越权扩大调度范围。
        """
        _require(role == ROLE_CHIEF, "只有值班长可以实施人工越权处置")
        _require(reason.strip(), "越权处置必须说明原因")
        # 幂等重试优先，且要把同批的补发指令一并返回
        if idempotency_key:
            prior = self.store.find_idempotent(idempotency_key)
            if prior is not None:
                follow = next(
                    (e for e in self.store.all_after(prior.seq - 1)
                     if e.type == "OrdersIssued"
                     and e.metadata.get("causation_id") == prior.event_id),
                    None,
                )
                return [prior] + ([follow] if follow else [])
        order = self._order(order_no)
        _require(order.is_open(), f"指令已闭环（{order.status}），无需越权")
        topo = self.state.topologies[order.topology_revision]
        scenario = self.state.scenarios[order.scenario_code]

        corrective_plan = Plan(
            plan_code=f"override:{order_no}",
            scenario_code=scenario.scenario_code,
            scenario_fingerprint=scenario.fingerprint(),
            title=f"针对 {order_no} 的越权纠正指令",
            rationale=reason,
            edge_flows={k: tuple(v) for k, v in corrective["edge_flows"].items()},
            gate_openings={k: tuple(v) for k, v in corrective.get("gate_openings", {}).items()},
            intake_allocation={
                code: {use: tuple(s) for use, s in alloc.items()}
                for code, alloc in corrective.get("intake_allocation", {}).items()
            },
        )
        report = validate_plan(topo, scenario, corrective_plan)
        _require(report.valid,
                 "纠正指令未通过硬约束校验："
                 + "；".join(v.message for v in report.violations
                             if v.severity is Severity.ERROR))
        items_by_facility = _derive_order_items(topo, corrective_plan, only=set(order.facility_codes))
        _require(set(items_by_facility) == set(order.facility_codes),
                 f"越权纠正只能作用于原指令设施 {order.facility_codes}，"
                 f"实际 {sorted(items_by_facility)}")
        _require(len(items_by_facility) == 1, "单条指令只对应一个设施")
        facility_code, items = next(iter(items_by_facility.items()))

        basin = scenario.basin_code
        now = self.clock()
        start = self.state.order_counter
        corrective_no = _format_order_no(basin, start + 1)
        corrective_payload = {
            "order_no": corrective_no,
            "plan_code": f"override:{order_no}",
            "scenario_code": scenario.scenario_code,
            "scenario_fingerprint": scenario.fingerprint(),
            "topology_revision": topo.revision,
            "facility": facility_code,
            "items": [i.to_dict() for i in items],
            "issued_at": now,
            "deadline": _plus_hours(now, deadline_hours),
            "issued_by": actor,
            "manual_override_of": order_no,
        }
        pending = [
            PendingEvent(
                stream_id=f"order:{order_no}",
                event_type="OrderOverridden",
                data={"order_no": order_no, "reason": reason,
                      "corrective_order": corrective_no},
                actor=actor, role=role, occurred_at=now,
                causation_id=order.receipts[-1]["event_id"] if order.receipts
                else order.created_event_id,
                correlation_id=order_no,
                idempotency_key=idempotency_key,
                expected_version=self._stream_version(f"order:{order_no}"),
            ),
            PendingEvent(
                stream_id=f"orders:{basin}",
                event_type="OrdersIssued",
                data={"plan_code": f"override:{order_no}",
                      "start_sequence": start + 1, "last_sequence": start + 1,
                      "manual_override": True,
                      "orders": [corrective_payload]},
                actor=actor, role=role, occurred_at=now,
                causation_id="@prev", correlation_id=order_no,
                expected_version=self._stream_version(f"orders:{basin}"),
            ),
        ]
        events = self.store.append_many(pending)
        for event in events:
            self._apply(self.state, event)
        return events

    # ================= 查询 / 任意时刻还原 =================

    def cancel_order(self, *, order_no: str, reason: str, actor: str, role: str,
                     idempotency_key: str | None = None) -> Event:
        """值班长作废未闭环指令（如超时后线下处置完毕、新情景下另行决策）。"""
        _require(role == ROLE_CHIEF, "只有值班长可以作废指令")
        _require(reason.strip(), "作废指令必须说明原因")
        order = self._order(order_no)
        _require(order.is_open(), f"指令已闭环（{order.status}），不能作废")
        event = self.store.append(
            f"order:{order_no}", "OrderCancelled",
            {"order_no": order_no, "reason": reason},
            actor=actor, role=role,
            causation_id=order.created_event_id, correlation_id=order_no,
            idempotency_key=idempotency_key,
            expected_version=self._stream_version(f"order:{order_no}"),
        )
        self._apply(self.state, event)
        return event

    def _plan(self, plan_code: str) -> PlanView:
        view = self.state.plans.get(plan_code)
        _require(view is not None, f"方案 {plan_code} 不存在")
        return view

    def _stream_version(self, stream_id: str) -> int:
        return len(self.store.stream_events(stream_id))

    def plan_timeline(self, plan_code: str) -> dict[str, Any]:
        """还原方案的决策理由链：编制校验、会商、审批、指令与现场反馈。"""
        view = self._plan(plan_code)
        scenario = self.state.scenarios[view.plan.scenario_code]
        topo = self.state.topologies[view.topology_revision]
        return {
            "plan": view.plan.to_data(),
            "status": view.status,
            "scenario": {"code": scenario.scenario_code,
                         "forecast_revision": scenario.forecast_revision,
                         "fingerprint": scenario.fingerprint()},
            "topology_revision": topo.revision,
            "validation": {"valid": view.valid, "violations": view.violations},
            "consultation": view.reviews,
            "required_reviewers": view.required_reviewers,
            "approval": view.approval,
            "orders": [self.order_status(no) for no in view.order_numbers],
        }

    def order_status(self, order_no: str) -> dict[str, Any]:
        order = self._order(order_no)
        return {
            "order_no": order.order_no,
            "plan_code": order.plan_code,
            "scenario_code": order.scenario_code,
            "scenario_fingerprint": order.scenario_fingerprint,
            "status": order.status,
            "facility_codes": order.facility_codes,
            "items": [i.to_dict() for i in order.items],
            "issued_at": order.issued_at,
            "deadline": order.deadline,
            "issued_by": order.issued_by,
            "actual": order.actual,
            "receipts": order.receipts,
            "overrides": order.overrides,
            "superseded_by": order.superseded_by,
            "causation_chain": self.causation_chain(order_no),
        }

    def causation_chain(self, order_no: str) -> list[dict[str, Any]]:
        """沿 causation_id 回溯：现场反馈 -> 越权/签发 -> 批准 -> 会商 -> 编制。"""
        by_id = {e.event_id: e for e in self.store.events}
        order = self._order(order_no)
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        current_id = order.created_event_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            event = by_id.get(current_id)
            if event is None:
                break
            chain.append({
                "event_id": event.event_id,
                "type": event.type,
                "at": event.metadata["occurred_at"],
                "actor": event.metadata["actor"],
                "stream_id": event.stream_id,
                "data_summary": _summarise(event),
            })
            current_id = event.metadata.get("causation_id")
        # 再把现场侧事件（回执链）附上
        for receipt in order.receipts:
            chain.append({
                "event_id": receipt["event_id"],
                "type": receipt["type"],
                "at": receipt["at"],
                "actor": receipt["actor"],
                "stream_id": f"order:{order_no}",
                "data_summary": receipt["data"],
                "caused_by": receipt.get("causation_id"),
            })
        return chain

    def execution_diff(self, order_no: str) -> dict[str, Any]:
        """计划值 vs 实际执行值，逐设施逐时段列出偏差。"""
        order = self._order(order_no)
        rows: list[dict[str, Any]] = []
        for item in order.items:
            actual = order.actual.get(item.facility, [])
            deltas: list[dict[str, Any]] = []
            for t, planned in enumerate(item.series):
                real = actual[t] if t < len(actual) else None
                deltas.append({
                    "step": t,
                    "planned": planned,
                    "actual": real,
                    "delta": None if real is None else round(real - planned, 4),
                })
            rows.append({"facility": item.facility, "action": item.action,
                         "unit": item.unit, "steps": deltas})
        return {"order_no": order_no, "status": order.status, "facilities": rows}

    def affected_range(self, order_no: str) -> dict[str, Any]:
        """受指令影响的上下游设施范围。"""
        order = self._order(order_no)
        topo = self.state.topologies[order.topology_revision]
        upstream: set[str] = set()
        downstream: set[str] = set()
        for code in order.facility_codes:
            up, down = topo.affected_range(code)
            upstream.update(up)
            downstream.update(down)
        return {
            "order_no": order_no,
            "operated": sorted(order.facility_codes),
            "upstream": sorted(
                ({"code": c, "name": topo.facilities[c].name,
                  "type": topo.facilities[c].facility_type.value} for c in upstream),
                key=lambda x: x["code"],
            ),
            "downstream": sorted(
                ({"code": c, "name": topo.facilities[c].name,
                  "type": topo.facilities[c].facility_type.value} for c in downstream),
                key=lambda x: x["code"]),
        }

    def reconstruct_at(self, as_of: str) -> dict[str, Any]:
        """还原任一时刻的采用情景、方案、指令状态与执行差异。"""
        state = self._replay_until(as_of)
        frozen = [
            {"code": s.scenario_code, "forecast_revision": s.forecast_revision,
             "fingerprint": s.fingerprint(),
             "state": state.scenario_states[s.scenario_code].value}
            for s in state.scenarios.values()
        ]
        plans = []
        for view in state.plans.values():
            plans.append({
                "plan_code": view.plan.plan_code,
                "scenario_code": view.plan.scenario_code,
                "status": view.status,
                "valid": view.valid,
                "title": view.plan.title,
                "reviews": len(view.reviews),
                "orders": view.order_numbers,
            })
        open_items = []
        for order in state.orders.values():
            if order.is_open():
                open_items.append(order.order_no)
        return {
            "as_of": as_of,
            "current_topology_revision": state.current_topology_revision,
            "scenarios": sorted(frozen, key=lambda x: x["code"]),
            "plans": sorted(plans, key=lambda x: x["plan_code"]),
            "open_orders": sorted(open_items),
            "orders_issued": len(state.orders),
        }

    def open_items(self) -> dict[str, Any]:
        """重启恢复后列出的未闭环事项。"""
        return {
            "open_orders": [
                self.order_status(no) for no in sorted(
                    o.order_no for o in self.state.open_orders())
            ]
        }


# ================= 辅助函数 =================

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plus_hours(iso_ts: str, hours: int) -> str:
    dt = datetime.fromisoformat(iso_ts)
    return (dt + timedelta(hours=hours)).isoformat(timespec="seconds")


def _format_order_no(basin: str, sequence: int) -> str:
    return f"{basin}-ZL-{sequence:04d}"


def _topology_from_data(data: dict[str, Any]) -> Topology:
    from .topology import Facility

    facilities = [
        Facility(
            code=f["code"], name=f["name"],
            facility_type=FacilityType(f["type"]),
            boundaries=dict(f["boundaries"]),
            flow_sequence=float(f.get("sequence", f.get("flow_sequence", 0.0))),
            serves=f.get("serves"),
        )
        for f in data["facilities"]
    ]
    return build_topology(
        basin_code=data["basin_code"], revision=data["revision"],
        facilities=facilities, edges=[tuple(e) for e in data["edges"]],
    )


def _derive_order_items(
    topo: Topology, plan: Plan, *, only: set[str] | None = None
) -> dict[str, list[OrderItem]]:
    """把方案决策转成按设施分组的执行指令条目。

    only 非空时只派生指定设施的动作（用于越权纠正，避免扩大调度范围）。
    """
    items: dict[str, list[OrderItem]] = defaultdict(list)
    for key, series in sorted(plan.edge_flows.items()):
        up, down = key.split(">", 1)
        up_f = topo.facilities[up]
        # 水库/闸站向下游河道泄放的指令挂在设施上
        if up_f.facility_type in (FacilityType.RESERVOIR, FacilityType.GATE):
            if only is None or up in only:
                items[up].append(OrderItem(
                    facility=up,
                    action="set_release",
                    series=list(series), unit="m3/s",
                ))
        elif topo.facilities[down].facility_type is FacilityType.INTAKE:
            if only is None or down in only:
                items[down].append(OrderItem(
                    facility=down, action="set_intake",
                    series=list(series), unit="m3/s",
                ))
    for code, series in sorted(plan.gate_openings.items()):
        if only is not None and code not in only:
            continue
        items[code].append(OrderItem(
            facility=code, action="set_gate_opening",
            series=list(series), unit="percent",
        ))
    return dict(items)


def _summarise(event: Event) -> dict[str, Any]:
    data = event.data
    if event.type == "OrdersIssued":
        return {"orders": [o["order_no"] for o in data.get("orders", [])],
                "plan_code": data.get("plan_code")}
    if event.type.startswith("Plan") or event.type.startswith("Consultation"):
        return {k: v for k, v in data.items() if k in ("plan_code", "passed", "valid", "stance")}
    return {k: v for k, v in data.items() if k in ("order_no", "reason", "scenario_code")}
