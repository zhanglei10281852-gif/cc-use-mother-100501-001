"""水量守恒与四类约束校验引擎测试。"""

import unittest

from basin_dispatch.engine import Plan, Severity, validate_plan
from basin_dispatch.scenario import build_scenario
from basin_dispatch.topology import Facility, FacilityType, build_topology


def _basin():
    # R 水库 -> C1 河段 -> G 闸 -> C2 河段，C2 分叉到 YK 取水口与 C3 出口
    facilities = [
        Facility("R", "水库", FacilityType.RESERVOIR,
                 {"capacity": 10000, "dead_storage": 1000, "flood_storage": 3000,
                  "max_release": 500, "max_level_rate": 0}, 1),
        Facility("C1", "河段1", FacilityType.REACH, {"max_flow": 600}, 2),
        Facility("G", "闸", FacilityType.GATE,
                 {"max_flow": 500, "min_opening": 0, "max_opening": 100}, 3),
        Facility("C2", "河段2", FacilityType.REACH, {"max_flow": 600}, 4),
        Facility("YK", "取水口", FacilityType.INTAKE,
                 {"max_take": 50, "min_guarantee": 10}, 5, "临江市"),
        Facility("C3", "出口河段", FacilityType.REACH, {"max_flow": 600}, 6),
    ]
    return build_topology("B1", "rev-1", facilities,
                          [("R", "C1"), ("C1", "G"), ("G", "C2"),
                           ("C2", "YK"), ("C2", "C3")])


def _scenario(**overrides):
    defaults = dict(
        scenario_code="S1",
        topology=_basin(),
        forecast_revision="F1",
        horizon_hours=12,
        step_hours=6,
        boundary_inflows={"R": [200, 200]},
        demands={"YK": {"drinking": [10, 10], "ecology": [5, 5], "production": [20, 20]}},
        initial_storage={"R": 5000},
    )
    defaults.update(overrides)
    return build_scenario(**defaults)


def _plan(**overrides):
    flows = {
        "R>C1": [200, 200],
        "C1>G": [200, 200],
        "G>C2": [200, 200],
        "C2>YK": [35, 35],
        "C2>C3": [165, 165],
    }
    defaults = dict(
        plan_code="P1", scenario_code="S1",
        scenario_fingerprint=_scenario().fingerprint(),
        title="t", rationale="r",
        edge_flows=flows, gate_openings={"G": [50, 50]},
    )
    defaults.update(overrides)
    return Plan(**defaults)


class EngineTests(unittest.TestCase):
    def test_balanced_plan_passes(self) -> None:
        scenario = _scenario()
        report = validate_plan(_basin(), scenario, _plan())
        self.assertTrue(report.valid, [v.message for v in report.violations])

    def test_wrong_scenario_fingerprint_rejected(self) -> None:
        report = validate_plan(_basin(), _scenario(), _plan(scenario_fingerprint="deadbeef"))
        self.assertFalse(report.valid)
        self.assertTrue(any("指纹" in v.message for v in report.violations))

    def test_reach_overtopping_is_flood_error(self) -> None:
        flows = dict(_plan().edge_flows)
        # 闸泄 650 超过 C2 安全流量 600
        flows["G>C2"] = [650, 650]
        flows["C2>C3"] = [615, 615]
        flows["C1>G"] = [650, 650]
        flows["R>C1"] = [650, 650]
        plan = _plan(edge_flows=flows, gate_openings={"G": [100, 100]})
        report = validate_plan(_basin(), _scenario(boundary_inflows={"R": [650, 650]}), plan)
        self.assertFalse(report.valid)
        self.assertTrue(any(v.category == "flood" and v.facility in ("R", "C2", "C3", "G")
                            for v in report.violations))

    def test_gate_rating_curve_enforced(self) -> None:
        plan = _plan(gate_openings={"G": [10, 10]})  # 10% 仅允许 50 m³/s
        report = validate_plan(_basin(), _scenario(), plan)
        self.assertFalse(report.valid)
        self.assertTrue(any("率定过流" in v.message for v in report.violations))

    def test_reservoir_overtopping_detected(self) -> None:
        # 来 500 泄 100，每步净增 (500-100)*2.16=864 万m³；库容 5000 -> 6728 未超，
        # 两步后仍未超 10000，改为极端入流制造漫坝
        scenario = _scenario(boundary_inflows={"R": [3000, 3000]})
        flows = dict(_plan().edge_flows)
        flows["R>C1"] = [100, 100]
        flows["C1>G"] = [100, 100]
        flows["G>C2"] = [100, 100]
        flows["C2>C3"] = [65, 65]
        plan = _plan(edge_flows=flows, gate_openings={"G": [100, 100]})
        report = validate_plan(_basin(), scenario, plan)
        self.assertFalse(report.valid)
        self.assertTrue(any("超过总库容" in v.message or "超过设施最大泄量" in v.message
                            for v in report.violations))

    def test_drinking_shortage_is_error(self) -> None:
        flows = dict(_plan().edge_flows)
        flows["C2>YK"] = [8, 8]   # 低于保供底线 10
        flows["C2>C3"] = [192, 192]
        plan = _plan(edge_flows=flows)
        report = validate_plan(_basin(), _scenario(), plan)
        self.assertFalse(report.valid)
        self.assertTrue(any(v.category == "drinking" for v in report.violations))

    def test_ecology_shortage_is_error(self) -> None:
        # 仅给 12：饮水 10 满足，生态 5 不足（饮水优先分配后只剩 2）
        flows = dict(_plan().edge_flows)
        flows["C2>YK"] = [12, 12]
        flows["C2>C3"] = [188, 188]
        plan = _plan(edge_flows=flows)
        report = validate_plan(_basin(), _scenario(), plan)
        self.assertFalse(report.valid)
        self.assertTrue(any(v.category == "ecology" for v in report.violations))

    def test_production_shortage_is_warning_only(self) -> None:
        # 取水 20：饮水 10 + 生态 5 满足，生产只能供 5 < 20 -> 告警但方案仍有效
        flows = dict(_plan().edge_flows)
        flows["C2>YK"] = [20, 20]
        flows["C2>C3"] = [180, 180]
        plan = _plan(edge_flows=flows)
        report = validate_plan(_basin(), _scenario(), plan)
        self.assertTrue(report.valid)
        self.assertTrue(any(v.severity is Severity.WARN and v.category == "production"
                            for v in report.violations))

    def test_balance_break_rejected(self) -> None:
        flows = dict(_plan().edge_flows)
        flows["G>C2"] = [100, 100]  # 闸出流与 C1 入流 200 不守恒
        plan = _plan(edge_flows=flows)
        report = validate_plan(_basin(), _scenario(), plan)
        self.assertFalse(report.valid)
        self.assertTrue(any(v.category == "balance" and v.facility in ("G", "C2")
                            for v in report.violations))

    def test_simulation_trajectory_records_storage(self) -> None:
        report = validate_plan(_basin(), _scenario(), _plan())
        # 稳态来泄相同，库容保持 5000
        self.assertEqual(report.simulation.storage["R"], [5000, 5000, 5000])


if __name__ == "__main__":
    unittest.main()
