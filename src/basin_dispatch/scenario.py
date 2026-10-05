"""冻结调度情景。

情景是某次方案编制所依据的全部事实快照：
- 绑定的拓扑修订（fingerprint）；
- 雨情/预报修订号与逐时段边界入流；
- 水库初始库容与逐时段降雨增量（直接入库）；
- 取水口逐时段用水需求（饮水、生态、生产）；
- 时段长度与统一时段序列。

情景一经冻结（FROZEN）即不可变，指纹随内容确定。迟到测报必须新建
情景（forecast_revision 递增），任何后续事件都只引用情景指纹，
因此新测报无法改写已执行方案的依据。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .contracts import canonical_fingerprint
from .topology import FacilityType, Topology


class ScenarioState(str, Enum):
    DRAFT = "draft"
    FROZEN = "frozen"
    SUPERSEDED = "superseded"  # 被更新测报形成的新情景替代（仅标记，不改内容）


class WaterUse(str, Enum):
    DRINKING = "drinking"  # 饮水
    ECOLOGY = "ecology"    # 生态
    PRODUCTION = "production"  # 生产


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_code: str
    basin_code: str
    topology_revision: str
    topology_fingerprint: str
    forecast_revision: str
    horizon_hours: int
    step_hours: int
    # 源头设施（无上游来水连接）逐时段入流 m³/s：{facility_code: [q_t0, q_t1, ...]}
    boundary_inflows: dict[str, tuple[float, ...]]
    # 水库逐时段直接入库增量（万 m³/时段，净降雨-蒸发等）：{code: [...]}
    reservoir_gains: dict[str, tuple[float, ...]]
    # 取水口逐时段需求 m³/s，按用途：{intake_code: {use: [q...]}}
    demands: dict[str, dict[str, tuple[float, ...]]]
    # 水库起始库容（万 m³）
    initial_storage: dict[str, float]
    state: ScenarioState = ScenarioState.DRAFT

    @property
    def steps(self) -> int:
        return self.horizon_hours // self.step_hours

    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "scenario_code": self.scenario_code,
                "basin_code": self.basin_code,
                "topology_revision": self.topology_revision,
                "topology_fingerprint": self.topology_fingerprint,
                "forecast_revision": self.forecast_revision,
                "horizon_hours": self.horizon_hours,
                "step_hours": self.step_hours,
                "boundary_inflows": self.boundary_inflows,
                "reservoir_gains": self.reservoir_gains,
                "demands": self.demands,
                "initial_storage": self.initial_storage,
            }
        )

    def to_data(self) -> dict[str, Any]:
        data = {
            "scenario_code": self.scenario_code,
            "basin_code": self.basin_code,
            "topology_revision": self.topology_revision,
            "topology_fingerprint": self.topology_fingerprint,
            "forecast_revision": self.forecast_revision,
            "horizon_hours": self.horizon_hours,
            "step_hours": self.step_hours,
            "boundary_inflows": {k: list(v) for k, v in self.boundary_inflows.items()},
            "reservoir_gains": {k: list(v) for k, v in self.reservoir_gains.items()},
            "demands": {
                intake: {use: list(series) for use, series in by_use.items()}
                for intake, by_use in self.demands.items()
            },
            "initial_storage": dict(self.initial_storage),
            "state": self.state.value,
        }
        data["fingerprint"] = self.fingerprint()
        return data

    @classmethod
    def from_data(cls, data: dict[str, Any], state: ScenarioState | None = None) -> "Scenario":
        return cls(
            scenario_code=data["scenario_code"],
            basin_code=data["basin_code"],
            topology_revision=data["topology_revision"],
            topology_fingerprint=data["topology_fingerprint"],
            forecast_revision=data["forecast_revision"],
            horizon_hours=data["horizon_hours"],
            step_hours=data["step_hours"],
            boundary_inflows={k: tuple(v) for k, v in data["boundary_inflows"].items()},
            reservoir_gains={k: tuple(v) for k, v in data["reservoir_gains"].items()},
            demands={
                intake: {use: tuple(series) for use, series in by_use.items()}
                for intake, by_use in data["demands"].items()
            },
            initial_storage=dict(data["initial_storage"]),
            state=state or ScenarioState(data["state"]),
        )


def build_scenario(
    *,
    scenario_code: str,
    topology: Topology,
    forecast_revision: str,
    horizon_hours: int,
    step_hours: int,
    boundary_inflows: dict[str, list[float]],
    reservoir_gains: dict[str, list[float]] | None = None,
    demands: dict[str, dict[str, list[float]]] | None = None,
    initial_storage: dict[str, float],
) -> Scenario:
    """构造并校验情景：维度一致、只引用拓扑内设施、库容初始值在边界内。"""
    if horizon_hours <= 0 or step_hours <= 0 or horizon_hours % step_hours != 0:
        raise ValueError("horizon_hours 必须是 step_hours 的正整数倍")
    steps = horizon_hours // step_hours
    if not scenario_code.strip():
        raise ValueError("情景编码不能为空")
    if not forecast_revision.strip():
        raise ValueError("雨情/预报修订号不能为空")

    reservoir_gains = reservoir_gains or {}
    demands = demands or {}

    def _check_series(series: dict[str, list[float]], label: str, types: set[FacilityType]) -> None:
        for code, values in series.items():
            facility = topology.facilities.get(code)
            if facility is None:
                raise ValueError(f"{label}引用了拓扑中不存在的设施: {code}")
            if facility.facility_type not in types:
                raise ValueError(f"{label}不能挂在{facility.facility_type.value}设施 {code} 上")
            if len(values) != steps:
                raise ValueError(f"{label} {code} 的时段数应为 {steps}，实际 {len(values)}")
            if any(v < 0 for v in values):
                raise ValueError(f"{label} {code} 出现负值")

    roots = {f.code for f in topology.roots()}
    for code in boundary_inflows:
        if code not in roots:
            raise ValueError(f"边界入流只能赋给源头设施，{code} 仍有上游来水")
    _check_series(boundary_inflows, "边界入流", {FacilityType.REACH, FacilityType.RESERVOIR})

    # 没有给入流的源头按零处理；非源头设施不允许直接给入流
    for code in roots:
        boundary_inflows.setdefault(code, [0.0] * steps)

    _check_series(reservoir_gains, "水库降雨增量", {FacilityType.RESERVOIR})
    for facility in topology.facilities.values():
        if facility.facility_type is FacilityType.RESERVOIR:
            reservoir_gains.setdefault(facility.code, [0.0] * steps)

    intakes = [
        f for f in topology.facilities.values() if f.facility_type is FacilityType.INTAKE
    ]
    normalized_demands: dict[str, dict[str, tuple[float, ...]]] = {}
    for intake in intakes:
        by_use_raw = demands.get(intake.code, {})
        by_use: dict[str, tuple[float, ...]] = {}
        for use in WaterUse:
            series = by_use_raw.get(use.value, [0.0] * steps)
            if len(series) != steps or any(v < 0 for v in series):
                raise ValueError(f"取水口 {intake.code} 的 {use.value} 需求序列非法")
            by_use[use.value] = tuple(series)
        normalized_demands[intake.code] = by_use
    for code in demands:
        if code not in topology.facilities:
            raise ValueError(f"需求引用了不存在的取水口: {code}")
        if topology.facilities[code].facility_type is not FacilityType.INTAKE:
            raise ValueError(f"需求只能挂在取水口上: {code}")

    for code, storage in initial_storage.items():
        facility = topology.facilities.get(code)
        if facility is None or facility.facility_type is not FacilityType.RESERVOIR:
            raise ValueError(f"初始库容必须且只能赋给水库: {code}")
        if storage < facility.boundary("dead_storage") or storage > facility.boundary("capacity"):
            raise ValueError(f"水库 {code} 初始库容超出死库容/总库容边界")
    for facility in topology.facilities.values():
        if facility.facility_type is FacilityType.RESERVOIR and facility.code not in initial_storage:
            raise ValueError(f"缺少水库 {facility.code} 的初始库容")

    return Scenario(
        scenario_code=scenario_code,
        basin_code=topology.basin_code,
        topology_revision=topology.revision,
        topology_fingerprint=topology.fingerprint,
        forecast_revision=forecast_revision,
        horizon_hours=horizon_hours,
        step_hours=step_hours,
        boundary_inflows={k: tuple(v) for k, v in boundary_inflows.items()},
        reservoir_gains={k: tuple(v) for k, v in reservoir_gains.items()},
        demands=normalized_demands,
        initial_storage=dict(initial_storage),
        state=ScenarioState.FROZEN,
    )
