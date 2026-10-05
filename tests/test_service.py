"""应用服务工作流与追溯测试：会商批准、角色、幂等、恢复、越权、迟到测报、时刻还原。"""

import tempfile
import unittest
from pathlib import Path

from basin_dispatch.demo import (
    CHIEF, DISPATCHER, PLAN_A, REVIEWERS, APPROVER, SCENARIO_A,
    SCENARIO_B, TOPOLOGY, CORRECTIVE_G1,
)
from basin_dispatch.events import EventStore
from basin_dispatch.service import (
    DomainError, DispatchService, ROLE_CHIEF, ROLE_DISPATCHER, ROLE_REVIEWER,
    ROLE_APPROVER,
)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.svc = DispatchService(EventStore(self.path))
        self.svc.register_topology(actor="admin", **TOPOLOGY)
        self.svc.promote_topology(revision=TOPOLOGY["revision"], actor="admin")
        self.svc.freeze_scenario(SCENARIO_A, actor=DISPATCHER[0])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _approved_plan(self, plan_code: str = "P-TY-001") -> None:
        plan = dict(PLAN_A, plan_code=plan_code)
        _, report = self.svc.create_plan_draft(plan, actor=DISPATCHER[0])
        self.assertTrue(report.valid)
        self.svc.submit_for_consultation(
            plan_code=plan_code, required_reviewers=2,
            actor=CHIEF[0], role=CHIEF[1])
        for name, role_name in REVIEWERS:
            self.svc.record_consultation(
                plan_code=plan_code, stance="agree", comment="ok",
                actor=name, role=role_name)
        self.svc.close_consultation(plan_code=plan_code, actor=CHIEF[0], role=CHIEF[1])
        self.svc.decide_plan(plan_code=plan_code, approved=True, comment="go",
                             actor=APPROVER[0], role=APPROVER[1])

    # ---------- 角色与门禁 ----------

    def test_only_chief_can_submit(self) -> None:
        self.svc.create_plan_draft(dict(PLAN_A, plan_code="P1"), actor=DISPATCHER[0])
        with self.assertRaises(DomainError):
            self.svc.submit_for_consultation(
                plan_code="P1", required_reviewers=1,
                actor=DISPATCHER[0], role=ROLE_DISPATCHER)

    def test_plan_must_be_frozen_scenario(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.create_plan_draft(
                dict(PLAN_A, plan_code="P1", scenario_code="NO-SUCH"),
                actor=DISPATCHER[0])

    def test_consultation_requires_quorum_and_no_objection(self) -> None:
        self.svc.create_plan_draft(dict(PLAN_A, plan_code="P1"), actor=DISPATCHER[0])
        self.svc.submit_for_consultation(
            plan_code="P1", required_reviewers=2, actor=CHIEF[0], role=CHIEF[1])
        self.svc.record_consultation(
            plan_code="P1", stance="agree", comment="",
            actor=REVIEWERS[0][0], role=ROLE_REVIEWER)
        # 同一会商成员不能重复表态
        with self.assertRaises(DomainError):
            self.svc.record_consultation(
                plan_code="P1", stance="agree", comment="",
                actor=REVIEWERS[0][0], role=ROLE_REVIEWER)
        self.svc.record_consultation(
            plan_code="P1", stance="disagree", comment="反对",
            actor=REVIEWERS[1][0], role=ROLE_REVIEWER)
        self.svc.close_consultation(plan_code="P1", actor=CHIEF[0], role=CHIEF[1])
        with self.assertRaises(DomainError):  # 会商未通过不能批准
            self.svc.decide_plan(plan_code="P1", approved=True, comment="",
                                 actor=APPROVER[0], role=ROLE_APPROVER)

    def test_invalid_plan_cannot_be_approved(self) -> None:
        bad = dict(PLAN_A, plan_code="PBAD",
                   gate_openings={"G1": [5, 5, 5, 5]})  # 开度远低于泄量要求
        _, report = self.svc.create_plan_draft(bad, actor=DISPATCHER[0])
        self.assertFalse(report.valid)
        self.svc.submit_for_consultation(
            plan_code="PBAD", required_reviewers=1, actor=CHIEF[0], role=CHIEF[1])
        self.svc.record_consultation(
            plan_code="PBAD", stance="agree", comment="",
            actor=REVIEWERS[0][0], role=ROLE_REVIEWER)
        closed = self.svc.close_consultation(
            plan_code="PBAD", actor=CHIEF[0], role=CHIEF[1])
        self.assertFalse(closed.data["passed"])

    def test_rejected_plan_can_be_revised(self) -> None:
        plan = dict(PLAN_A, plan_code="PR")
        self.svc.create_plan_draft(plan, actor=DISPATCHER[0])
        self.svc.submit_for_consultation(
            plan_code="PR", required_reviewers=1, actor=CHIEF[0], role=CHIEF[1])
        self.svc.record_consultation(
            plan_code="PR", stance="disagree", comment="x",
            actor=REVIEWERS[0][0], role=ROLE_REVIEWER)
        self.svc.close_consultation(plan_code="PR", actor=CHIEF[0], role=CHIEF[1])
        self.svc.decide_plan(plan_code="PR", approved=False, comment="驳回重做",
                             actor=APPROVER[0], role=ROLE_APPROVER)
        _, report = self.svc.revise_plan(plan, actor=DISPATCHER[0])
        self.assertTrue(report.valid)
        with self.assertRaises(DomainError):  # 修订不能换情景
            self.svc.revise_plan(dict(plan, scenario_code="OTHER"), actor=DISPATCHER[0])

    # ---------- 指令编号与幂等 ----------

    def test_orders_get_sequential_numbers_and_issue_is_idempotent(self) -> None:
        self._approved_plan()
        first = self.svc.issue_orders(
            plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER,
            idempotency_key="issue-1")
        retry = self.svc.issue_orders(
            plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER,
            idempotency_key="issue-1")
        self.assertEqual([e.event_id for e in first], [e.event_id for e in retry])
        numbers = [o["order_no"] for e in first for o in e.data["orders"]]
        self.assertEqual(numbers, ["LRB-ZL-0001", "LRB-ZL-0002", "LRB-ZL-0003"])
        # 不带幂等键的重复签发也必须被拒绝
        with self.assertRaises(DomainError):
            self.svc.issue_orders(
                plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER)
        self.assertEqual(len(self.svc.state.orders), 3)

    def test_cannot_issue_before_approval(self) -> None:
        self.svc.create_plan_draft(dict(PLAN_A, plan_code="P2"), actor=DISPATCHER[0])
        with self.assertRaises(DomainError):
            self.svc.issue_orders(
                plan_code="P2", actor=DISPATCHER[0], role=ROLE_DISPATCHER)

    # ---------- 现场闭环 ----------

    def test_ack_and_execute_lifecycle_is_idempotent(self) -> None:
        self._approved_plan()
        self.svc.issue_orders(
            plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER,
            idempotency_key="i")
        self.svc.acknowledge_order(order_no="LRB-ZL-0002", actor="现场",
                                   idempotency_key="ack-1")
        # 重复上报回执不产生第二条
        self.svc.acknowledge_order(order_no="LRB-ZL-0002", actor="现场",
                                   idempotency_key="ack-1")
        self.assertEqual(
            len(self.svc.store.stream_events("order:LRB-ZL-0002")), 1)
        self.svc.report_execution(
            order_no="LRB-ZL-0002", actual={"R1": [600, 900, 700, 400]},
            note="", actor="现场")
        with self.assertRaises(DomainError):  # 已闭环不能再执行上报
            self.svc.report_execution(
                order_no="LRB-ZL-0002", actual={"R1": [1, 1, 1, 1]},
                note="", actor="现场")

    def test_timeout_scan_recovers_open_items(self) -> None:
        self._approved_plan()
        self.svc.issue_orders(
            plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER,
            deadline_hours=6, idempotency_key="i")
        # 重启：新建服务从日志重放
        reopened = DispatchService(EventStore(self.path))
        self.assertEqual(
            sorted(o.order_no for o in reopened.state.open_orders()),
            ["LRB-ZL-0001", "LRB-ZL-0002", "LRB-ZL-0003"])
        timed = reopened.scan_timeouts(now="2030-01-01T00:00:00+00:00")
        self.assertEqual(len(timed), 3)
        # 再扫一次不产生重复超时事件
        self.assertEqual(reopened.scan_timeouts(now="2030-01-01T00:00:00+00:00"), [])

    def test_reject_then_chief_override_emits_corrective_order(self) -> None:
        self._approved_plan()
        self.svc.issue_orders(
            plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER,
            idempotency_key="i")
        self.svc.reject_order(order_no="LRB-ZL-0001", reason="现场无法执行",
                              actor="G1闸站所")
        # 非值班长不能越权
        with self.assertRaises(DomainError):
            self.svc.manual_override(
                order_no="LRB-ZL-0001", reason="x", corrective=CORRECTIVE_G1,
                actor=DISPATCHER[0], role=ROLE_DISPATCHER)
        first = self.svc.manual_override(
            order_no="LRB-ZL-0001", reason="调整开度", corrective=CORRECTIVE_G1,
            actor=CHIEF[0], role=ROLE_CHIEF, idempotency_key="ov-1")
        retry = self.svc.manual_override(
            order_no="LRB-ZL-0001", reason="调整开度", corrective=CORRECTIVE_G1,
            actor=CHIEF[0], role=ROLE_CHIEF, idempotency_key="ov-1")
        self.assertEqual([e.event_id for e in first], [e.event_id for e in retry])
        self.assertEqual(self.svc.state.orders["LRB-ZL-0001"].status, "overridden")
        corrective_no = first[-1].data["orders"][0]["order_no"]
        self.assertEqual(self.svc.state.orders[corrective_no].status, "issued")
        # 越权不能扩大到其他设施
        wider = dict(CORRECTIVE_G1)
        wider["edge_flows"] = dict(wider["edge_flows"], **{"R1>RCH1": [1, 1, 1, 1]})
        with self.assertRaises(DomainError):
            self.svc.manual_override(
                order_no="LRB-ZL-0002", reason="x", corrective=wider,
                actor=CHIEF[0], role=ROLE_CHIEF)

    def test_timeout_then_cancel_closes_item(self) -> None:
        self._approved_plan()
        self.svc.issue_orders(
            plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER,
            deadline_hours=1, idempotency_key="i")
        timed = self.svc.scan_timeouts(now="2030-01-01T00:00:00+00:00")
        self.assertEqual(len(timed), 3)
        target = timed[0].data["order_no"]
        with self.assertRaises(DomainError):  # 非值班长不能作废
            self.svc.cancel_order(order_no=target, reason="线下已处置",
                                  actor=DISPATCHER[0], role=ROLE_DISPATCHER)
        self.svc.cancel_order(order_no=target, reason="线下已处置",
                              actor=CHIEF[0], role=ROLE_CHIEF)
        self.assertNotIn(target, [o.order_no for o in self.svc.state.open_orders()])

    # ---------- 迟到测报 ----------

    def test_late_forecast_forms_new_scenario_without_touching_executed_basis(self) -> None:
        self._approved_plan()
        self.svc.issue_orders(
            plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER,
            idempotency_key="i")
        before = self.svc.state.orders["LRB-ZL-0002"].scenario_fingerprint
        # 迟到测报：不能覆盖 FCST-A
        with self.assertRaises(DomainError):
            self.svc.freeze_scenario(
                dict(SCENARIO_A, boundary_inflows={"R1": [1, 1, 1, 1]}),
                actor="气象")
        self.svc.freeze_scenario(SCENARIO_B, actor="气象")
        self.svc.mark_scenario_superseded(scenario_code="TY-2026-001",
                                          actor=CHIEF[0])
        after = self.svc.state.orders["LRB-ZL-0002"].scenario_fingerprint
        self.assertEqual(before, after)  # 已执行指令依据指纹不变
        self.assertNotEqual(before, self.svc.state.scenarios["TY-2026-002"].fingerprint())

    # ---------- 任意时刻还原与差异 ----------

    def test_reconstruct_at_and_execution_diff(self) -> None:
        self._approved_plan()
        self.svc.issue_orders(
            plan_code="P-TY-001", actor=DISPATCHER[0], role=ROLE_DISPATCHER,
            idempotency_key="i")
        snapshot = self.svc.reconstruct_at("2030-01-01T00:00:00+00:00")
        self.assertEqual(snapshot["orders_issued"], 3)
        self.assertEqual(len(snapshot["plans"]), 1)
        self.svc.report_execution(
            order_no="LRB-ZL-0002", actual={"R1": [590, 900, 700, 400]},
            note="", actor="现场")
        diff = self.svc.execution_diff("LRB-ZL-0002")
        first_step = diff["facilities"][0]["steps"][0]
        self.assertEqual((first_step["planned"], first_step["actual"], first_step["delta"]),
                         (600, 590, -10))


if __name__ == "__main__":
    unittest.main()
