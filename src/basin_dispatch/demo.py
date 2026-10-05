"""台风登陆前沿江流域联合调度端到端演示数据。

拓扑（临江流域 LRB，修订 TOPO-2026-1）：

    临江水库 R1 -- 河道RCH1 -- 城区闸 G1 -- 河道RCH2 -- 河道RCH3(出口)
                                              |
                                            取水口 YK1（饮水/生态/生产）

情景 TY-2026-001（雨情 FCST-A）：24 小时、6 小时一步，共 4 步。
方案 P-TY-001 的逐时段决策经过水量守恒与防洪/饮水/生态边界校验。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .service import DispatchService

CHIEF = ("值班长-周敏", "duty_chief")
DISPATCHER = ("调度员-李磊", "dispatcher")
REVIEWERS = [("会商-王工", "reviewer"), ("会商-赵工", "reviewer")]
APPROVER = ("局领导-陈局", "approver")

TOPOLOGY = {
    "basin_code": "LRB",
    "revision": "TOPO-2026-1",
    "facilities": [
        {"code": "R1", "name": "临江水库", "type": "reservoir", "sequence": 10,
         "boundaries": {"capacity": 50000, "dead_storage": 5000,
                        "flood_storage": 20000, "max_release": 1000,
                        "max_level_rate": 10000}},
        {"code": "RCH1", "name": "库下河道", "type": "reach", "sequence": 20,
         "boundaries": {"max_flow": 1200}},
        {"code": "G1", "name": "城区节制闸", "type": "gate", "sequence": 30,
         "boundaries": {"max_flow": 1000, "min_opening": 0, "max_opening": 100}},
        {"code": "RCH2", "name": "闸下河道", "type": "reach", "sequence": 40,
         "boundaries": {"max_flow": 900}},
        {"code": "YK1", "name": "城市取水口", "type": "intake", "sequence": 45,
         "serves": "临江市",
         "boundaries": {"max_take": 80, "min_guarantee": 15}},
        {"code": "RCH3", "name": "出海河道", "type": "reach", "sequence": 50,
         "boundaries": {"max_flow": 900}},
    ],
    "edges": [
        ["R1", "RCH1"], ["RCH1", "G1"], ["G1", "RCH2"],
        ["RCH2", "YK1"], ["RCH2", "RCH3"],
    ],
}

# 台风路径逼近时的雨情/库情快照（FCST-A）
SCENARIO_A = {
    "scenario_code": "TY-2026-001",
    "forecast_revision": "FCST-A",
    "horizon_hours": 24,
    "step_hours": 6,
    "boundary_inflows": {"R1": [600, 900, 700, 400]},
    "reservoir_gains": {"R1": [100, 200, 150, 50]},
    "demands": {"YK1": {"drinking": [18, 18, 18, 18],
                        "ecology": [10, 10, 10, 10],
                        "production": [25, 25, 25, 25]}},
    "initial_storage": {"R1": 30000},
}

# 水库来多少泄多少，库水位小幅上涨但不越界；城区取水 53 m³/s 三用途齐保
FLOWS = {
    "R1>RCH1": [600, 900, 700, 400],
    "RCH1>G1": [600, 900, 700, 400],
    "G1>RCH2": [600, 900, 700, 400],
    "RCH2>YK1": [53, 53, 53, 53],
    "RCH2>RCH3": [547, 847, 647, 347],
}
OPENINGS = {"G1": [60, 90, 70, 40]}

PLAN_A = {
    "plan_code": "P-TY-001",
    "scenario_code": "TY-2026-001",
    "title": "台风海燕前沿江水库预泄与城市保供方案",
    "rationale": (
        "上游水库按来水预泄、维持库水位低于汛限预留防洪库容；城区闸按来水"
        "同步敞泄保证闸下河道不超 900 m³/s；城市取水口保饮水 18、生态 10、"
        "生产 25 m³/s，剩余水量全道出海外排。"
    ),
    "edge_flows": FLOWS,
    "gate_openings": OPENINGS,
}

# 迟到 3 小时的修正测报：台风路径北抬，降雨 stronger → 只能形成新情景
SCENARIO_B = {
    "scenario_code": "TY-2026-002",
    "forecast_revision": "FCST-B",
    "horizon_hours": 24,
    "step_hours": 6,
    "boundary_inflows": {"R1": [700, 1100, 850, 450]},
    "reservoir_gains": {"R1": [150, 320, 200, 60]},
    "demands": {"YK1": {"drinking": [18, 18, 18, 18],
                        "ecology": [10, 10, 10, 10],
                        "production": [20, 20, 20, 20]}},
    "initial_storage": {"R1": 30000},
}

# 闸站拒绝原指令后，值班长越权补发的纠正开度（过流能力仍满足）
CORRECTIVE_G1 = {
    "edge_flows": FLOWS,
    "gate_openings": {"G1": [65, 100, 75, 45]},
}


def build_demo(service: DispatchService) -> None:
    """按完整生命周期灌入演示数据。"""
    # 脚本时钟：让事件按台风调度的时间线推进，便于任意时刻还原
    clock = {"t": datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)}

    def tick(hours: int = 1) -> str:
        clock["t"] += timedelta(hours=hours)
        return clock["t"].isoformat(timespec="seconds")

    service.clock = lambda: clock["t"].isoformat(timespec="seconds")
    service.store.clock = service.clock  # 便捷 append 与业务判断共用脚本时钟

    # ---- 拓扑（台风前 12 小时已固化）----
    clock["t"] = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
    service.register_topology(actor="系统管理员", **TOPOLOGY)
    service.promote_topology(revision=TOPOLOGY["revision"], actor="系统管理员")

    # ---- 冻结情景（台风前 8 小时首份雨情）----
    tick(4)
    service.freeze_scenario(SCENARIO_A, actor=DISPATCHER[0])

    # ---- 编制与校验 ----
    tick(1)
    _, report = service.create_plan_draft(PLAN_A, actor=DISPATCHER[0])
    assert report.valid, "演示方案应当通过全部硬约束校验"

    # ---- 会商 ----
    tick(1)
    service.submit_for_consultation(
        plan_code="P-TY-001", required_reviewers=2,
        actor=CHIEF[0], role=CHIEF[1])
    tick(1)
    for name, role_name in REVIEWERS:
        service.record_consultation(
            plan_code="P-TY-001", stance="agree",
            comment="同意预泄与保供安排", actor=name, role=role_name)
        tick(1)
    service.close_consultation(plan_code="P-TY-001", actor=CHIEF[0], role=CHIEF[1])

    # ---- 批准 ----
    tick(1)
    service.decide_plan(
        plan_code="P-TY-001", approved=True, comment="同意，按序执行",
        actor=APPROVER[0], role=APPROVER[1])

    # ---- 签发（同一幂等键重复请求两次，验证不会产生两条有效指令）----
    tick(1)
    first = service.issue_orders(
        plan_code="P-TY-001", actor=DISPATCHER[0], role=DISPATCHER[1],
        deadline_hours=6, idempotency_key="issue:P-TY-001:20261005")
    retry = service.issue_orders(
        plan_code="P-TY-001", actor=DISPATCHER[0], role=DISPATCHER[1],
        deadline_hours=6, idempotency_key="issue:P-TY-001:20261005")
    assert [e.event_id for e in first] == [e.event_id for e in retry]
    assert len(service.state.orders) == 3  # G1 / R1 / YK1 各一条

    order_g1 = "LRB-ZL-0001"
    order_r1 = "LRB-ZL-0002"
    order_yk1 = "LRB-ZL-0003"

    # ---- 现场：水库签收并按指令执行（实调值略有偏差，进入差异记录）----
    tick(1)
    service.acknowledge_order(order_no=order_r1, actor="R1水库管理处")
    tick(2)
    service.report_execution(
        order_no=order_r1,
        actual={"R1": [595, 880, 705, 410]},
        note="第二步因下游漂浮物短暂拦阻，少泄 20，随后回补",
        actor="R1水库管理处")

    # ---- 现场：闸站拒绝执行原开度，值班长越权补发纠正指令 ----
    tick(1)
    service.reject_order(
        order_no=order_g1, reason="现场复核第二步 90% 开度临近震动限值",
        actor="G1闸站所")
    tick(1)
    override_events = service.manual_override(
        order_no=order_g1, reason="批准将第二步提至 100%、其余分步微调，保持同等泄量",
        corrective=CORRECTIVE_G1,
        actor=CHIEF[0], role=CHIEF[1], deadline_hours=3,
        idempotency_key="override:G1:20261005")
    corrective_no = override_events[-1].data["orders"][0]["order_no"]
    tick(1)
    service.acknowledge_order(order_no=corrective_no, actor="G1闸站所")
    tick(2)
    service.report_execution(
        order_no=corrective_no,
        actual={"G1": [600, 900, 700, 400]},
        note="按纠正开度执行完毕", actor="G1闸站所")

    # ---- 取水口指令超时未闭环（演示重启恢复未闭环事项）----
    deadline = service.state.orders[order_yk1].deadline
    later_dt = max(datetime.fromisoformat(deadline), clock["t"]) + timedelta(hours=1)
    later = later_dt.isoformat(timespec="seconds")
    timed_out = service.scan_timeouts(now=later, actor="system")
    assert len(timed_out) == 1 and timed_out[0].data["order_no"] == order_yk1
    clock["t"] = later_dt

    # ---- 迟到测报到达：只能形成新情景，不能改写已执行依据 ----
    tick(3)
    service.freeze_scenario(SCENARIO_B, actor="气象联络员")
    tick(1)
    service.mark_scenario_superseded(scenario_code="TY-2026-001", actor=CHIEF[0])
