# 跨网络韧性演练与复盘平台

面向国庆等大客流联合演练的**跨网络韧性演练与复盘平台**。它解决联合演练中的核心痛点：
各单位在各自系统里都标记了“恢复”，却没有人能说明**哪一项恢复动作真正解除了旅客与物资保障风险**。

平台在演练前**冻结**场景（设施依赖、可用替代、服务目标、单位职责、计划恢复顺序），
演练中接收带**发生时间**与**上报时间**的故障、决策、资源调拨与恢复证据，
依据依赖图确定性地计算**风险传播**与关键服务**失效区间**，区分**临时绕行**与**真正恢复**；
多单位对同一事实意见不一致时自动进入**有期限会商**；最终结论只引用**确认过的证据版本**。
**重复上报、乱序事件、演练中断重启都不会改变重放结果。**

纯 Python 3.11+ 标准库实现，无外部数据库或服务依赖。

## 它如何回答“什么真正解除了风险”

1. **冻结场景**：设施节点、硬/软依赖、可替代能力（含容量比例与前置条件）、
   关键服务目标（容量阈值）、参与单位职责、计划恢复步骤，演练开始前冻结且不可改。
2. **只追加事件日志**：每条事件同时记录 `occurred_at`（发生时间）与 `reported_at`（上报时间），
   按接入顺序形成**哈希链**落盘 JSONL；误报用追加 `correction` 事件纠正，**原始记录永不删除或改写**。
3. **证据版本与异单位佐证**：恢复证据默认只是“主张”，须经**异单位佐证**或**会商裁决**才成为确认版本；
   本单位自报不算数。新版本可 `supersedes` 旧版本，旧版本保留并标记被取代。
4. **确定性重放**：引擎只依赖“冻结场景 + 事件序列”。按发生时间排序后两遍扫描：
   - 先解析证据佐证/异议/取代关系（对乱序免疫）与会商；
   - 再沿硬依赖图计算每个时刻的全网容量快照，得到设施/服务失效区间。
5. **绕行 vs 真恢复**：主用路径不可用时替代能力才承载，容量沿依赖链与前置设施继续衰减；
   服务重新达标若靠替代链路，记为**临时绕行（workaround）**，主用设施修复后才记为**真正恢复（recovery）**。
6. **有期限会商**：异单位对同设施、不同结论的主张对立即自动立案；也可人工开启并合并同一立案。
   期限前未裁决即超期，并自动派生发现。
7. **发现 → 整改闭环**：每条整改措施记录由哪个发现触发、谁登记、谁接受、何时由谁验证关闭。
8. **计划对比**：比较冻结的计划恢复顺序与实际顺序，给出每步滞后/提前分钟数与顺序颠倒。

## 内置演示（国庆枢纽断电）

物流枢纽变电站断电 → 通信交换机、算力集群级联失效 → 5G 备用链路绕行（容量 60%）
→ 算力任务迁移到边缘节点（综合容量约 51%，依赖已绕行的通信链路）
→ 物流组在本系统自报“已恢复”但无佐证 → 算力组与物流组分歧进入有期限会商
→ 变电站真正修复并经异单位确认 → 两个服务目标分别恢复。
事件**乱序接入**（决策迟报、佐证晚到），包含一次重复上报、一次误报纠正、一次演练中断/恢复。

```bash
PYTHONPATH=src python3 run_cli.py demo
# 已构建演示演练 EX-2026-NATDAY-HUB@2026-09-30-r1
# 事件接入 22 条，拒绝重复/无效上报 1 条
```

复盘视图：

```bash
PYTHONPATH=src python3 run_cli.py services      EX-2026-NATDAY-HUB 2026-09-30-r1  # 失效区间/绕行/真恢复
PYTHONPATH=src python3 run_cli.py plan          EX-2026-NATDAY-HUB 2026-09-30-r1  # 计划 vs 实际顺序
PYTHONPATH=src python3 run_cli.py consultations EX-2026-NATDAY-HUB 2026-09-30-r1  # 有期限会商
PYTHONPATH=src python3 run_cli.py findings      EX-2026-NATDAY-HUB 2026-09-30-r1  # 发现及触发来源
PYTHONPATH=src python3 run_cli.py remediations  EX-2026-NATDAY-HUB 2026-09-30-r1  # 整改登记/接受/关闭
PYTHONPATH=src python3 run_cli.py conclusion    EX-2026-NATDAY-HUB 2026-09-30-r1  # 引用确认证据的结论
PYTHONPATH=src python3 run_cli.py timeline      EX-2026-NATDAY-HUB 2026-09-30-r1  # 按发生时间的重放时间线
PYTHONPATH=src python3 run_cli.py verify        EX-2026-NATDAY-HUB 2026-09-30-r1  # 哈希链完整性校验
```

演示结论要点：

- `S-PINFO`（旅客信息推送，阈值 50%）：失效 47 分钟后由**边缘绕行（51%）**解除，但性质是临时绕行；
  供电真正恢复后转为**真正恢复**。
- `S-DISP`（物资调度，阈值 100%）：绕行容量 51% 不达标，**绕行不算恢复**，
  直到供电修复经异单位确认才真正解除（失效 71 分钟）。
- 物流组自报的“已恢复”因无佐证停留在 `submitted`，自动派生为发现，不得进入结论证据。

## 命令行

```bash
# 列出场景版本 / 查看原始只追加日志
PYTHONPATH=src python3 run_cli.py list
PYTHONPATH=src python3 run_cli.py events <code> <revision> [-v] [--json]

# 整场重放（可用存储中的场景，也可用离线文件）
PYTHONPATH=src python3 run_cli.py replay <code> <revision> [--as-of 2026-09-30T08:20:00+08:00]
PYTHONPATH=src python3 run_cli.py replay --scenario scenario.json --events-file events.jsonl

# HTTP API 服务
PYTHONPATH=src python3 run_cli.py serve --host 127.0.0.1 --port 8080
```

`--as-of` 截断到某发生时刻，用于逐步复盘或演练中的态势回看；前缀稳定（早时刻结果是晚时刻的前缀）。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| GET | `/exercises` | 列出场景版本 |
| PUT | `/exercises/{code}/scenarios/{rev}` | 写入 DRAFT 场景（冻结后返回 409） |
| POST | `/exercises/{code}/scenarios/{rev}/freeze` | 冻结场景 |
| GET | `/exercises/{code}/scenarios/{rev}` | 读取冻结场景 |
| POST | `/exercises/{code}/scenarios/{rev}/events` | 接入事件（重复 409，语义错误 422） |
| POST | `/exercises/{code}/scenarios/{rev}/corrections` | 追加误报纠正（不抹除原记录） |
| GET | `/exercises/{code}/scenarios/{rev}/events` | 读取只追加日志 |
| GET | `/exercises/{code}/scenarios/{rev}/replay?as_of=` | 确定性重放结果 |
| POST | `/demo/build` · `/demo/replay` | 构建/重放内置演示 |

事件请求体：

```json
{
  "event_type": "recovery_evidence",
  "occurred_at": "2026-09-30T08:15:00+08:00",
  "reported_at": "2026-09-30T08:16:00+08:00",
  "unit_id": "U-NET",
  "payload": { "evidence_id": "EV-NET", "facility_id": "F-SWITCH",
               "mode": "bypass", "alternative_id": "ALT-NET" }
}
```

事件类型：`fault`、`decision`、`resource_allocation`、`recovery_evidence`、`correction`、
`consultation_open`、`consultation_resolve`、`finding`、`remediation`、`exercise_phase`。
恢复证据的 `mode` 为 `restore`（真恢复）或 `bypass`（绕行，需带 `alternative_id`）；
用 `confirms_evidence` / `disputes_evidence` / `supersedes` 表达佐证、异议与版本取代。

## 确定性与审计保证

- **幂等**：同一事实（事件类型 + 单位 + 发生时间 + 规范化 payload）重复上报返回 409，不产生第二条记录；
  也可显式传 `idempotency_key`。
- **乱序免疫**：重放按 `occurred_at` 排序；佐证晚于主张到达、裁决引用自动会商等都在重放时整体解析。
- **纠正不抹除**：`correction` 是追加事件；被纠正记录保留在日志中，仅在风险计算中失效。
- **哈希链**：每条记录含前驱摘要与自身摘要，加载时校验链与序号，篡改即报错（`verify` 命令）。
- **中断重启一致**：重放是纯函数 `(冻结场景, 事件日志) → 结果`；落盘后重新加载结果完全一致。

## 运行环境与测试

- Python 3.11+，仅标准库。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v   # 45 个测试
python3 -m compileall -q src tests run_cli.py             # 编译检查
python3 run_cli.py                                        # 基础契约冒烟
```

## 代码结构

```
src/resilience_replay/
  clock.py        统一带时区时钟
  jsonio.py       确定性 JSON 与摘要
  contracts.py    基础稳定标识/指纹契约
  scenario.py     冻结场景：设施/依赖/替代/服务目标/职责/计划步骤与校验
  events.py       只追加事件日志：幂等、双时间戳、哈希链、JSONL 持久化
  engine.py       确定性重放：证据图、会商、传播快照、失效区间、计划对比、结论
  service.py      应用服务：冻结、接入校验、持久化、重放编排
  demo.py         国庆枢纽断电演示数据
  api.py          标准库 HTTP API
  cli.py          命令行复盘工具
```
