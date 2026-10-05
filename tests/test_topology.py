"""设施拓扑构造与图查询测试。"""

import unittest

from basin_dispatch.topology import Facility, FacilityType, build_topology


def _facility(code: str, ftype: FacilityType, sequence: float, **boundaries: object) -> Facility:
    defaults = {
        FacilityType.REACH: {"max_flow": 100.0},
        FacilityType.RESERVOIR: {
            "capacity": 1000.0, "dead_storage": 100.0, "flood_storage": 300.0,
            "max_release": 50.0, "max_level_rate": 0.0},
        FacilityType.GATE: {"max_flow": 80.0, "min_opening": 0.0, "max_opening": 100.0},
        FacilityType.INTAKE: {"max_take": 20.0, "min_guarantee": 5.0},
    }[ftype]
    defaults.update(boundaries)  # type: ignore[arg-type]
    return Facility(code=code, name=code, facility_type=ftype,
                    boundaries=defaults, flow_sequence=sequence,
                    serves="城市" if ftype is FacilityType.INTAKE else None)


class TopologyTests(unittest.TestCase):
    def _chain(self):
        facilities = [
            _facility("R1", FacilityType.RESERVOIR, 1),
            _facility("C1", FacilityType.REACH, 2),
            _facility("G1", FacilityType.GATE, 3),
            _facility("C2", FacilityType.REACH, 4, max_flow=60.0),
            _facility("YK1", FacilityType.INTAKE, 5),
        ]
        return build_topology("B1", "rev-1", facilities,
                              [("R1", "C1"), ("C1", "G1"), ("G1", "C2"), ("C2", "YK1")])

    def test_fingerprint_is_stable(self) -> None:
        topo = self._chain()
        self.assertEqual(topo.fingerprint, self._chain().fingerprint)

    def test_missing_boundary_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Facility(code="X", name="x", facility_type=FacilityType.REACH,
                     boundaries={}, flow_sequence=1)

    def test_intake_must_declare_served_user(self) -> None:
        with self.assertRaises(ValueError):
            Facility(code="YK", name="x", facility_type=FacilityType.INTAKE,
                     boundaries={"max_take": 10, "min_guarantee": 1})

    def test_unknown_edge_endpoint_rejected(self) -> None:
        f = _facility("R1", FacilityType.RESERVOIR, 1)
        with self.assertRaises(ValueError):
            build_topology("B1", "r", [f], [("R1", "NOPE")])

    def test_cycle_rejected(self) -> None:
        facilities = [_facility(f"C{i}", FacilityType.REACH, i) for i in range(1, 4)]
        with self.assertRaises(ValueError):
            build_topology("B1", "r", facilities,
                           [("C1", "C2"), ("C2", "C3"), ("C3", "C1")])

    def test_flow_sequence_direction_checked(self) -> None:
        facilities = [_facility("C1", FacilityType.REACH, 2),
                      _facility("C2", FacilityType.REACH, 1)]
        with self.assertRaises(ValueError):
            build_topology("B1", "r", facilities, [("C1", "C2")])

    def test_intake_cannot_have_downstream(self) -> None:
        facilities = [_facility("C1", FacilityType.REACH, 1),
                      _facility("YK1", FacilityType.INTAKE, 2),
                      _facility("C2", FacilityType.REACH, 3)]
        with self.assertRaises(ValueError):
            build_topology("B1", "r", facilities,
                           [("C1", "YK1"), ("YK1", "C2")])

    def test_affected_range(self) -> None:
        topo = self._chain()
        upstream, downstream = topo.affected_range("G1")
        self.assertEqual(upstream, frozenset({"R1", "C1"}))
        self.assertEqual(downstream, frozenset({"C2", "YK1"}))

    def test_topological_order(self) -> None:
        topo = self._chain()
        order = [f.code for f in topo.topological_order()]
        self.assertEqual(order.index("R1"), 0)
        self.assertLess(order.index("G1"), order.index("C2"))


if __name__ == "__main__":
    unittest.main()
