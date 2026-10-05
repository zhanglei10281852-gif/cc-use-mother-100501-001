"""调度方案与逐时段水量平衡模拟、约束校验。

校验四类约束：
- flood（防洪）：河段安全流量、水库库容/最大下泄/变幅、闸门率定过流；
- drinking（饮水）：取水口供水量必须满足饮水需求与保供底线；
- ecology（生态）：生态基流需求必须满足；
- production（生产）：生产缺水记为告警，不阻断批准，但进入决策理由。

所有节点在每个时段满足水量守恒（欧拉显式平衡），误差超出容差即违规。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .scenario import Scenario, WaterUse
from .topology import FacilityType, Topology

# 流量平衡容差（m³/s）与库容容差（万 m³，仅用于边界比较）
FLOW_TOL = 0.05
STORAGE_TOL = 1e-6

# 用水满足优先级（数值越小越优先）
USE_PRIORITY = {
    WaterUse.DRINKING.value: 0,
    WaterUse.ECOLOGY.value: 1,
    WaterUse.PRODUCTION.value: 2,
}


class Severity(str, Enum):
    ERROR = "error"
    WARN = "warning"


@dataclass(frozen=True, slots=True)
class Violation:
    severity: Severity
    category: str          # flood / drinking / ecology / production / balance
    facility: str
    step: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "facility": self.facility,
            "step": self.step,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """在冻结情景上编制的调度方案（决策变量集合）。

    edge_flows 的键为 "上游编码>下游编码"，值为逐时段流量（m³/s）；
    gate_openings 为逐时段开度百分比；
    intake_allocation 可选，声明各取水口逐用途供水序列，缺省时按优先级自动分配。
    """

    plan_code: str
    scenario_code: str
    scenario_fingerprint: str
    title: str
    rationale: str
    edge_flows: dict[str, tuple[float, ...]]
    gate_openings: dict[str, tuple[float, ...]]
    intake_allocation: dict[str, dict[str, tuple[float, ...]]] = field(default_factory=dict)

    def fingerprint(self) -> str:
        from .contracts import canonical_fingerprint

        return canonical_fingerprint(
            {
                "plan_code": self.plan_code,
                "scenario_code": self.scenario_code,
                "scenario_fingerprint": self.scenario_fingerprint,
                "title": self.title,
                "rationale": self.rationale,
                "edge_flows": self.edge_flows,
                "gate_openings": self.gate_openings,
                "intake_allocation": self.intake_allocation,
            }
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "plan_code": self.plan_code,
            "scenario_code": self.scenario_code,
            "scenario_fingerprint": self.scenario_fingerprint,
            "title": self.title,
            "rationale": self.rationale,
            "edge_flows": {k: list(v) for k, v in self.edge_flows.items()},
            "gate_openings": {k: list(v) for k, v in self.gate_openings.items()},
            "intake_allocation": {
                code: {use: list(series) for use, series in alloc.items()}
                for code, alloc in self.intake_allocation.items()
            },
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "Plan":
        return cls(
            plan_code=data["plan_code"],
            scenario_code=data["scenario_code"],
            scenario_fingerprint=data["scenario_fingerprint"],
            title=data.get("title", ""),
            rationale=data.get("rationale", ""),
            edge_flows={k: tuple(v) for k, v in data["edge_flows"].items()},
            gate_openings={k: tuple(v) for k, v in data["gate_openings"].items()},
            intake_allocation={
                code: {use: tuple(series) for use, series in alloc.items()}
                for code, alloc in data.get("intake_allocation", {}).items()
            },
        )


@dataclass(slots=True)
class SimulationState:
    """逐时段模拟轨迹，用于审计实际执行差异对照。"""

    storage: dict[str, list[float]]              # 水库库容（万 m³），含 t=0 初始值
    node_inflow: dict[str, list[float]]          # 各节点每时段总入流
    node_outflow: dict[str, list[float]]         # 各节点每时段总出流/取水
    edge_flow: dict[str, list[float]]
    supply: dict[str, dict[str, list[float]]]    # 取水口逐用途实供

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage": {k: list(v) for k, v in self.storage.items()},
            "node_inflow": {k: list(v) for k, v in self.node_inflow.items()},
            "node_outflow": {k: list(v) for k, v in self.node_outflow.items()},
            "edge_flow": {k: list(v) for k, v in self.edge_flow.items()},
            "supply": {
                code: {use: list(s) for use, s in alloc.items()}
                for code, alloc in self.supply.items()
            },
        }


@dataclass(slots=True)
class ValidationReport:
    valid: bool
    violations: list[Violation]
    simulation: SimulationState

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "violations": [v.to_dict() for v in self.violations],
            "simulation": self.simulation.to_dict(),
        }


def _edge_key(up: str, down: str) -> str:
    return f"{up}>{down}"


def validate_plan(topology: Topology, scenario: Scenario, plan: Plan) -> ValidationReport:
    """对方案做全时段、全拓扑的守恒与边界校验。"""
    violations: list[Violation] = []

    if plan.scenario_code != scenario.scenario_code:
        violations.append(
            Violation(Severity.ERROR, "balance", "-", -1,
                      f"方案引用情景 {plan.scenario_code} 与当前情景 {scenario.scenario_code} 不符")
        )
    if plan.scenario_fingerprint != scenario.fingerprint():
        violations.append(
            Violation(Severity.ERROR, "balance", "-", -1,
                      "方案引用的情景指纹与冻结情景不一致，依据可能已被调换")
        )

    steps = scenario.steps
    factor = 0.36 * scenario.step_hours  # m³/s -> 万 m³/时段

    expected_edges = {
        _edge_key(up, down) for up, targets in topology.downstream_of.items() for down in targets
    }
    edge_values: dict[str, list[float]] = {}
    for key in expected_edges:
        series = plan.edge_flows.get(key)
        if series is None:
            violations.append(
                Violation(Severity.ERROR, "balance", key, -1, f"缺少边 {key} 的流量决策")
            )
            continue
        if len(series) != steps:
            violations.append(
                Violation(Severity.ERROR, "balance", key, -1,
                          f"边 {key} 时段数应为 {steps}，实际 {len(series)}")
            )
        if any(q < -FLOW_TOL for q in series):
            violations.append(
                Violation(Severity.ERROR, "flood", key, -1, f"边 {key} 出现负流量")
            )
        edge_values[key] = list(series)
    for key in plan.edge_flows:
        if key not in expected_edges:
            violations.append(
                Violation(Severity.ERROR, "balance", key, -1, f"流量决策引用了拓扑之外的边: {key}")
            )

    gates = [f for f in topology.facilities.values() if f.facility_type is FacilityType.GATE]
    openings: dict[str, list[float]] = {}
    for gate in gates:
        series = plan.gate_openings.get(gate.code)
        if series is None or len(series) != steps:
            violations.append(
                Violation(Severity.ERROR, "flood", gate.code, -1, "闸门缺少完整逐时段开度决策")
            )
            continue
        lo, hi = gate.boundary("min_opening"), gate.boundary("max_opening")
        for t, opening in enumerate(series):
            if opening < lo - 1e-9 or opening > hi + 1e-9:
                violations.append(
                    Violation(Severity.ERROR, "flood", gate.code, t,
                              f"开度 {opening} 超出设施边界 [{lo}, {hi}]")
                )
        openings[gate.code] = list(series)
    for code in plan.gate_openings:
        if code not in openings:
            violations.append(
                Violation(Severity.ERROR, "flood", code, -1, "开度决策引用了非闸站设施")
            )

    storage_traj: dict[str, list[float]] = {}
    inflow_traj: dict[str, list[float]] = {f.code: [] for f in topology.facilities.values()}
    outflow_traj: dict[str, list[float]] = {f.code: [] for f in topology.facilities.values()}
    edge_traj: dict[str, list[float]] = {key: [] for key in expected_edges}
    supply_traj: dict[str, dict[str, list[float]]] = {}

    reservoirs = {
        f.code: f for f in topology.facilities.values()
        if f.facility_type is FacilityType.RESERVOIR
    }
    storage_prev = dict(scenario.initial_storage)
    for code, facility in reservoirs.items():
        storage_traj[code] = [storage_prev[code]]

    for t in range(steps):
        for facility in topology.topological_order():
            code = facility.code
            incoming_edges = [
                _edge_key(up, code) for up in topology.upstream_of(code)
            ]
            outgoing_edges = [
                _edge_key(code, down) for down in topology.downstream_of.get(code, ())
            ]
            q_in_edges = sum(edge_values.get(key, [0.0] * steps)[t] for key in incoming_edges)
            q_boundary = scenario.boundary_inflows.get(code, (0.0,) * steps)[t]
            q_in = q_in_edges + q_boundary
            q_out = sum(edge_values.get(key, [0.0] * steps)[t] for key in outgoing_edges)
            inflow_traj[code].append(q_in)
            outflow_traj[code].append(q_out)
            for key in outgoing_edges + incoming_edges:
                if key in edge_traj and len(edge_traj[key]) == t:
                    edge_traj[key].append(edge_values[key][t])

            if facility.facility_type is FacilityType.RESERVOIR:
                gain = scenario.reservoir_gains[code][t]
                storage = storage_prev[code] + gain + (q_in - q_out) * factor
                storage_traj[code].append(storage)
                # 防洪：库容边界
                if storage < facility.boundary("dead_storage") - STORAGE_TOL:
                    violations.append(
                        Violation(Severity.ERROR, "flood", code, t,
                                  f"库容 {storage:.2f} 万m³ 低于死库容 "
                                  f"{facility.boundary('dead_storage'):.2f}")
                    )
                if storage > facility.boundary("capacity") + STORAGE_TOL:
                    violations.append(
                        Violation(Severity.ERROR, "flood", code, t,
                                  f"库容 {storage:.2f} 万m³ 超过总库容 "
                                  f"{facility.boundary('capacity'):.2f}，存在漫坝风险")
                    )
                max_rate = facility.boundary("max_level_rate")
                if max_rate > 0 and abs(storage - storage_prev[code]) > max_rate + STORAGE_TOL:
                    violations.append(
                        Violation(Severity.ERROR, "flood", code, t,
                                  f"库容单时段变幅 {abs(storage - storage_prev[code]):.2f} "
                                  f"超过允许值 {max_rate:.2f} 万m³")
                    )
                # 防洪：最大下泄
                if q_out > facility.boundary("max_release") + FLOW_TOL:
                    violations.append(
                        Violation(Severity.ERROR, "flood", code, t,
                                  f"下泄 {q_out:.2f} m³/s 超过设施最大泄量 "
                                  f"{facility.boundary('max_release'):.2f}")
                    )
                storage_prev[code] = storage

            elif facility.facility_type is FacilityType.REACH:
                # 末端河道为流域出口：来水全部流出流域，仍受安全流量约束
                if not outgoing_edges:
                    outflow_traj[code][-1] = q_in
                    if q_in > facility.boundary("max_flow") + FLOW_TOL:
                        violations.append(
                            Violation(Severity.ERROR, "flood", code, t,
                                      f"出口流量 {q_in:.2f} m³/s 超过河段安全流量 "
                                      f"{facility.boundary('max_flow'):.2f}")
                        )
                else:
                    # 普通河段不蓄水：入流必须等于出流
                    if abs(q_in - q_out) > FLOW_TOL * (1 + abs(q_in)):
                        violations.append(
                            Violation(Severity.ERROR, "balance", code, t,
                                      f"河段水量不平衡：入流 {q_in:.2f} vs 出流 {q_out:.2f} m³/s")
                        )
                    if q_out > facility.boundary("max_flow") + FLOW_TOL:
                        violations.append(
                            Violation(Severity.ERROR, "flood", code, t,
                                      f"河段流量 {q_out:.2f} m³/s 超过安全流量 "
                                      f"{facility.boundary('max_flow'):.2f}")
                        )

            elif facility.facility_type is FacilityType.GATE:
                if abs(q_in - q_out) > FLOW_TOL * (1 + abs(q_in)):
                    violations.append(
                        Violation(Severity.ERROR, "balance", code, t,
                                  f"闸站水量不平衡：入流 {q_in:.2f} vs 出流 {q_out:.2f} m³/s")
                    )
                opening = openings[code][t]
                rated = opening / 100.0 * facility.boundary("max_flow")
                if q_out > rated + FLOW_TOL:
                    violations.append(
                        Violation(Severity.ERROR, "flood", code, t,
                                  f"开度 {opening}% 率定过流上限 {rated:.2f}，实际 {q_out:.2f} m³/s")
                    )

            elif facility.facility_type is FacilityType.INTAKE:
                if q_out > FLOW_TOL:
                    violations.append(
                        Violation(Severity.ERROR, "balance", code, t,
                                  "取水口为汇点，不能再向其下游分配流量")
                    )
                if q_in > facility.boundary("max_take") + FLOW_TOL:
                    violations.append(
                        Violation(Severity.ERROR, "flood", code, t,
                                  f"取水 {q_in:.2f} m³/s 超过取水能力 "
                                  f"{facility.boundary('max_take'):.2f}")
                    )
                _allocate_and_check(
                    topology, scenario, plan, code, t, q_in, violations, supply_traj
                )

    valid = not any(v.severity is Severity.ERROR for v in violations)
    return ValidationReport(
        valid=valid,
        violations=violations,
        simulation=SimulationState(
            storage=storage_traj,
            node_inflow=inflow_traj,
            node_outflow=outflow_traj,
            edge_flow=edge_traj,
            supply=supply_traj,
        ),
    )


def _allocate_and_check(
    topology: Topology,
    scenario: Scenario,
    plan: Plan,
    code: str,
    t: int,
    available: float,
    violations: list[Violation],
    supply_traj: dict[str, dict[str, list[float]]],
) -> None:
    """校验/生成取水口逐用途供水，并按饮水、生态、生产分类记录缺口。"""
    demands = scenario.demands.get(code, {})
    declared = plan.intake_allocation.get(code)
    allocation: dict[str, float] = {}

    if declared:
        for use in WaterUse:
            series = declared.get(use.value)
            allocation[use.value] = series[t] if series and t < len(series) else 0.0
        total = sum(allocation.values())
        if total > available + FLOW_TOL * (1 + abs(available)):
            violations.append(
                Violation(Severity.ERROR, "balance", code, t,
                          f"申报供水 {total:.2f} 超过可取水 {available:.2f} m³/s")
            )
    else:
        # 未显式分配时按饮水 > 生态 > 生产优先供水
        remaining = max(available, 0.0)
        for use_value in sorted(USE_PRIORITY, key=lambda u: USE_PRIORITY[u]):
            want = demands.get(use_value, (0.0,) * scenario.steps)[t]
            give = min(want, remaining)
            allocation[use_value] = give
            remaining -= give

    alloc_by_use = supply_traj.setdefault(
        code, {use.value: [] for use in WaterUse}
    )
    for use in WaterUse:
        alloc_by_use.setdefault(use.value, []).append(allocation[use.value])

    facility = topology.facilities[code]
    # 饮水：需求 + 保供底线均为硬约束
    drinking = allocation.get(WaterUse.DRINKING.value, 0.0)
    drinking_need = demands.get(WaterUse.DRINKING.value, (0.0,) * scenario.steps)[t]
    if drinking + FLOW_TOL < drinking_need:
        violations.append(
            Violation(Severity.ERROR, "drinking", code, t,
                      f"饮水供水 {drinking:.2f} 低于需求 {drinking_need:.2f} m³/s")
        )
    guarantee = facility.boundary("min_guarantee")
    if drinking + FLOW_TOL < guarantee:
        violations.append(
            Violation(Severity.ERROR, "drinking", code, t,
                      f"饮水供水 {drinking:.2f} 低于旱区保供底线 {guarantee:.2f} m³/s")
        )
    # 生态：生态基流硬约束
    ecology = allocation.get(WaterUse.ECOLOGY.value, 0.0)
    ecology_need = demands.get(WaterUse.ECOLOGY.value, (0.0,) * scenario.steps)[t]
    if ecology + FLOW_TOL < ecology_need:
        violations.append(
            Violation(Severity.ERROR, "ecology", code, t,
                      f"生态供水 {ecology:.2f} 低于基流需求 {ecology_need:.2f} m³/s")
        )
    # 生产：缺口仅告警
    production = allocation.get(WaterUse.PRODUCTION.value, 0.0)
    production_need = demands.get(WaterUse.PRODUCTION.value, (0.0,) * scenario.steps)[t]
    if production + FLOW_TOL < production_need:
        violations.append(
            Violation(Severity.WARN, "production", code, t,
                      f"生产供水 {production:.2f} 低于需求 {production_need:.2f} m³/s，"
                      f"缺口 {production_need - production:.2f}")
        )
