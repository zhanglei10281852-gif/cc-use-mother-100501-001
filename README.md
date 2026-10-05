# 流域联合调度与指令追溯后端

台风登陆前，上游水库预泄、下游城市防洪、旱区保供同时发生。本系统把河段、水库、
闸站、取水口及其上下游关系纳入**同一拓扑**，在**冻结的情景版本**上编制调度方案，
按**防洪、饮水、生态、生产**四类约束校验水量守恒与设施边界，经**会商、批准**后
才生成**带序号的执行指令**；现场回执、拒绝、超时和人工越权全程保留因果，迟到测报
只能形成新情景而不能改写已执行依据。

纯 Python 标准库实现（3.11+），无外部数据库或服务依赖；状态全部由**仅追加事件日志**
重放得到，服务重启后自动恢复未闭环事项。

## 需求与实现对照

| 业务要求 | 实现 |
|---|---|
| 河段/水库/闸站/取水口同一拓扑与上下游关系 | `topology.py`：有向无环图、里程方向校验、设施边界 |
| 冻结情景版本（雨情表、库容表） | `scenario.py`：情景指纹 sha256，冻结后不可变 |
| 水量守恒 + 防洪/饮水/生态/生产边界 | `engine.py`：逐时段欧拉平衡、硬约束错误/生产缺水告警 |
| 会商、批准后才能发指令 | `service.py`：角色门禁 + 状态机（draft→consulting→passed→approved） |
| 带序号执行指令 | `{流域}-ZL-{4位序号}`，流域指令流版本号防并发重号 |
| 重复上报/重试不产生两条有效指令 | 事件 `Idempotency-Key` + 方案不可重复签发双重守卫 |
| 现场回执/拒绝/超时/越权保留因果 | 每个事件带 `causation_id`/`correlation_id`，可回溯完整链条 |
| 迟到测报不改写已执行依据 | 必须新建情景（新 forecast_revision），指令永久记录依据指纹 |
| 重启恢复未闭环事项 | JSONL 事件日志 + 哈希链校验，重放即恢复 |
| 还原任一时刻情景、理由、执行差异、影响范围 | `reconstruct-at` / `timeline` / `diff` / `range` / `chain` |

## 架构

```
API (http.server)  ┐
                   ├─ DispatchService（唯一命令入口；角色与状态机守卫）
CLI (argparse)    ┘            │
                               ├─ topology/scenario/engine（纯领域计算，无副作用）
                               └─ EventStore（JSONL 仅追加、哈希链、幂等键、流版本）
                                      │
                              data/basin_events.jsonl
```

- **事件溯源**：没有"当前状态表"。拓扑登记、情景冻结、会商意见、审批、签发、回执、
  拒绝、超时、越权、作废全部是不可变事件；投影（`Projection`）由重放生成。
- **哈希链**：每条事件含 `prev_hash` 与自身内容哈希，重放时逐行校验，日志被改写即拒绝启动。
- **流与乐观锁**：每个方案/指令/流域指令序列是独立 stream，`expected_version` 防止并发覆盖。
- **时钟可注入**：服务与存储共用同一时钟（`DispatchService(store, clock=...)`），便于演练与时刻还原。

### 指令状态机

```
issued ──ack──▶ acknowledged ──execute──▶ executed（终态）
  │                │
  ├──reject──────▶ rejected ──▶ overridden（越权，补发纠正指令）
  └──deadline────▶ timed_out ──▶ overridden / executed / cancelled（终态）
```

## 运行

```bash
# 测试
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests run_cli.py

# 一键灌入台风端到端演示数据（事件带脚本时间线，便于时刻还原）
PYTHONPATH=src python3 -m basin_dispatch.cli --log data/demo.jsonl demo --force

# 启动 HTTP API
PYTHONPATH=src python3 -m basin_dispatch.api --host 127.0.0.1 --port 8080 --log data/basin.jsonl
```

## CLI 工作流（examples/ 下有全部载荷样例）

```bash
L="--log data/basin.jsonl"
python3 -m basin_dispatch.cli $L init-topology --file examples/topology.json --actor admin
python3 -m basin_dispatch.cli $L promote --revision TOPO-2026-1 --actor admin
python3 -m basin_dispatch.cli $L freeze-scenario --file examples/scenario_a.json --actor lilei
python3 -m basin_dispatch.cli $L make-plan --file examples/plan.json --actor lilei      # 输出校验报告
python3 -m basin_dispatch.cli $L submit --plan P-TY-001 --reviewers 2 --actor zhoumin --role duty_chief
python3 -m basin_dispatch.cli $L consult --plan P-TY-001 --stance agree  --actor wanggong --role reviewer
python3 -m basin_dispatch.cli $L consult --plan P-TY-001 --stance agree  --actor zhaogong --role reviewer
python3 -m basin_dispatch.cli $L close-consultation --plan P-TY-001 --actor zhoumin --role duty_chief
python3 -m basin_dispatch.cli $L decide --plan P-TY-001 --approve --actor chenju --role approver
python3 -m basin_dispatch.cli $L issue  --plan P-TY-001 --actor lilei --role dispatcher \
        --idempotency-key issue-P-TY-001-20261005     # 重试同键返回同一事件
# 现场
python3 -m basin_dispatch.cli $L ack     --order LRB-ZL-0002 --actor r1-station
python3 -m basin_dispatch.cli $L execute --order LRB-ZL-0002 --file examples/execution_r1.json --actor r1-station
python3 -m basin_dispatch.cli $L reject  --order LRB-ZL-0001 --reason "开度临近震动限值" --actor g1-station
python3 -m basin_dispatch.cli $L scan-timeouts                       # 超时扫描（可定时执行）
python3 -m basin_dispatch.cli $L override --order LRB-ZL-0001 \
        --reason "微调开度" --file examples/override_g1.json --actor zhoumin --role duty_chief
# 追溯
python3 -m basin_dispatch.cli $L status --open                       # 未闭环事项（重启恢复）
python3 -m basin_dispatch.cli $L timeline    --plan P-TY-001         # 决策理由全周期
python3 -m basin_dispatch.cli $L chain       --order LRB-ZL-0004     # 因果链
python3 -m basin_dispatch.cli $L diff        --order LRB-ZL-0002     # 计划 vs 实际偏差
python3 -m basin_dispatch.cli $L range       --order LRB-ZL-0004     # 受影响上下游
python3 -m basin_dispatch.cli $L reconstruct --as-of 2026-10-05T03:30:00+00:00
python3 -m basin_dispatch.cli $L verify-log                          # 哈希链完整性
```

## HTTP API

身份头：`X-Actor`（操作人工号）、`X-Role`（角色）；写请求可带 `Idempotency-Key`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/topologies` `/topologies/promote` | 登记/生效拓扑修订 |
| GET  | `/topology` | 当前生效拓扑与指纹 |
| POST | `/scenarios` | 冻结情景（迟到测报用新编码与修订号） |
| POST | `/scenarios/{code}/supersede` | 标记旧情景被替代（内容不变） |
| GET  | `/scenarios` | 全部冻结情景 |
| POST | `/plans` `/plans/{code}/revise` | 编制/修订方案（返回守恒与边界校验报告） |
| POST | `/plans/{code}/submit` `/consult` `/close-consultation` `/decide` | 会商与批准 |
| GET  | `/plans/{code}/timeline` | 方案决策理由链 |
| POST | `/plans/{code}/issue` | 批准后签发带序号指令（幂等） |
| POST | `/orders/{no}/ack` `/execute` `/reject` `/cancel` `/override` | 现场闭环与越权 |
| POST | `/orders/scan-timeouts` | 超时扫描 |
| GET  | `/orders?open=1` `/orders/{no}` | 未闭环事项/指令详情 |
| GET  | `/orders/{no}/chain` `/diff` `/range` | 因果链/执行差异/影响范围 |
| GET  | `/state?as_of=...` | 任意时刻状态还原 |
| GET  | `/events` `/health` | 原始事件/健康检查 |

## 约束校验模型

- 每个时段（默认 6 小时一步）沿水流拓扑序逐节点做质量平衡；
  流量 m³/s 经 `0.36 × 步长(小时)` 换算为万 m³ 库容变化。
- **防洪（error）**：水库死库容/总库容/最大下泄/库容变幅、闸门开度-率定过流曲线、
  河段与出口安全流量。
- **饮水（error）**：城市取水口需同时满足饮水需求与旱区保供底线。
- **生态（error）**：生态基流需求必须满足。
- **生产（warning）**：生产缺口只告警、不阻断批准，告警随方案进入决策理由。
- 未显式分配取水时，按 饮水 > 生态 > 生产 自动优先分配。

## 模块

| 文件 | 职责 |
|---|---|
| `contracts.py` | 规范化 JSON、稳定指纹（原有契约，已泛化） |
| `topology.py` | 设施、边界、有向无环拓扑、影响范围 |
| `scenario.py` | 冻结情景及其构造校验 |
| `engine.py` | 方案、逐时段水量平衡模拟、四类约束校验 |
| `events.py` | 事件、JSONL 存储、哈希链、幂等、流版本 |
| `service.py` | 命令入口、状态机、角色、投影、时刻还原 |
| `api.py` / `cli.py` | HTTP API / 命令行 |
| `demo.py` | 台风情景端到端演示数据 |
