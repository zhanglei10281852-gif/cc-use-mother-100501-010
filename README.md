# 跨网络韧性演练与复盘平台

面向国庆客流高峰等多单位联合演练：在物流枢纽断电、通信切换、算力任务迁移
等场景中，冻结演练前的设施依赖与保障目标，接收带**发生时间/上报时间**的
故障、决策、资源调拨与恢复证据，确定性重放整场演练，明确回答
**“哪一项恢复动作真正解除了旅客与物资保障风险”**，并把每条整改措施
追溯到触发它的发现、接受单位与验证关闭时刻。

仅依赖 Python 3.11+ 标准库，无浏览器、外部数据库或常驻服务依赖。

## 它解决的核心问题

联合演练中各单位在各自系统里标记“已恢复”，但：

* 有的只是**临时绕行**（柴油发电、网络切换、算力迁移上云），不是原设施恢复；
* 多单位对同一事实主张冲突（看板变绿 vs 现场仍失效）却没有结论；
* 误报、重复上报、乱序上报、演练中断重启，都会污染复盘结论。

本平台把“恢复”拆成可计算的三层事实：

| 层次 | 含义 |
|---|---|
| 物理失效 | 设施自身故障沿硬依赖闭包传播（断电→基站→算力） |
| 实质失效 `g[f]` | 扣除**透明替代**（发电给下游等价供给，沿图治愈下游） |
| 业务失效 `b[f]` | 再扣除**排他替代**（网络切换/算力迁移只拯救显式切换节点，不自动治愈下游） |

关键服务的**物理风险窗口**内，进一步切出“实际停机段”与“绕行缓解段”：
窗口关闭（物理根因全部恢复）才算风险真正解除；只靠绕行维持时结论明确标注
“风险被缓解但未真正解除”。

## 快速开始

```bash
# 内置国庆物流枢纽演练：断电 → 发电/双网切换/算力迁移 → 会商 → 真正恢复 → 整改关闭
python3 run_cli.py init-demo --data ./data

# 人类可读复盘报告
python3 run_cli.py replay EX-2026-ND-HUB-01 --data ./data

# 历史时刻重放（会商进行中、尚未真正恢复时的结论）
python3 run_cli.py replay EX-2026-ND-HUB-01 --as-of 2026-09-30T08:10:30Z

# 只看某个结构化片段
python3 run_cli.py replay EX-2026-ND-HUB-01 --format json --section services

# 校验场景指纹与事件哈希链
python3 run_cli.py verify EX-2026-ND-HUB-01 --data ./data
```

启动 HTTP API：

```bash
python3 run_cli.py serve --data ./data --port 8080
```

## HTTP API

| 方法与路径 | 作用 |
|---|---|
| `POST /exercises` | 冻结场景（不可覆盖，修改须新建修订） |
| `GET  /exercises` | 演练清单 |
| `GET  /exercises/{code}/scenario` | 冻结场景与指纹 |
| `POST /exercises/{code}/events` | 单条事件，或 `{"events":[...]}` 批量（乱序亦可） |
| `GET  /exercises/{code}/events` | 追加日志（**含被纠正的原始记录**） |
| `GET  /exercises/{code}/replay?as_of=...` | 确定性重放复盘报告 |
| `GET  /exercises/{code}/verify` | 场景指纹 + 哈希链 + 引用完整性 |

```bash
curl -X POST http://127.0.0.1:8080/exercises \
  -H 'Content-Type: application/json' -d @scenario.json

curl -X POST http://127.0.0.1:8080/exercises/EX-2026-ND-HUB-01/events \
  -H 'Content-Type: application/json' -d '{
    "event_id":"EV-0001","unit_id":"POWER",
    "occurred_at":"2026-09-30T08:00:10Z","reported_at":"2026-09-30T08:00:40Z",
    "kind":"facility_fault","payload":{"facility_id":"F-SUB","cause":"外线跳闸"}}'
```

## 冻结场景

`scenario.json` 包含：参与单位职责 `units`、设施 `facilities`、硬依赖
`dependencies`、预案登记的可用替代 `alternatives`、关键服务目标
`services`（含 RTO 与责任单位）。冻结后生成 `scenario_fingerprint`，
校验内容包括：引用完整性、依赖图无环、替代预案窗口合法、设施/服务的
责任单位均已登记职责。

`alternatives[].transparent` 区分替代的物理性质：

* `true`：透明替代（柴油发电）——下游获得等价供给；
* `false`（默认）：排他替代（微波链路、应急基站、云端算力）——
  只对显式激活该切换的节点生效。

## 事件协议

所有事件携带 `event_id / unit_id / occurred_at（发生时间）/
reported_at（上报时间）/ kind / payload`。

| kind | 含义 | 关键字段 |
|---|---|---|
| `facility_fault` | 设施故障 | `facility_id` |
| `facility_restored` | 恢复标记 | `facility_id`, `restoration_kind=real\|detour` |
| `activate_detour` / `detour_reverted` | 启用/归还替代 | `primary`, `backup` |
| `decision` / `resource_dispatch` | 决策与资源调拨 | `summary` / `resource`,`to_unit` |
| `evidence_reported` / `evidence_confirmed` | 证据版本上报/确认 | `facility_id`,`version` / `evidence_id`,`version` |
| `dispute_opened` / `dispute_position` / `dispute_resolved` | 会商全周期 | `dispute_id`,`positions`,`winning` |
| `action_registered` / `action_accepted` / `action_verified` | 整改措施全生命周期 | `finding_id`,`owner_unit` / `action_id` / `evidence_id` |
| `retract_event` | 纠正误报（**原记录保留**） | `target_event_id`,`reason` |
| `exercise_paused` / `exercise_resumed` | 中断/重启（仅审计标注） | — |

规则：

* **重复上报**：同 `event_id` 同内容幂等忽略；同 `event_id` 不同内容拒绝；
* **乱序上报**：计算一律按 `occurred_at` 排序（平局按 event_id/seq），
  与到达顺序无关；
* **误报纠正**：只能追加 `retract_event`，不能删除或改写原始记录；
* **冲突会商**：一方主张真正恢复、另一方仍主张失效时自动开会，
  有 SLA 期限（默认 900 秒），逾期按保守原则判定**风险不解除**；
* **证据约束**：会商裁决恢复、整改验证关闭、最终风险解除结论，
  都必须引用**已被联合指挥部确认的证据版本**，否则结论不生效；
* **终局裁定权限**：证据确认、会商裁决、整改验证只能由
  `coordinator`（联合指挥部）执行，单位不能自我背书；
* **中断重启**：失效时长按场景时钟计算，暂停记录不扣减时间，
  因而暂停记录缺失也不会改变结果。

## 复盘报告要点

* `services[].episodes[]`：每个物理风险窗口的首可用时间与性质
  （`detour`/`real`）、RTO 是否超标、真正关闭时刻、引用的确认证据；
* `detours[]`：每次绕行的有效覆盖区间，备份自身故障会在覆盖中挖洞；
* `disputes[]`：会商期限、各方立场、裁决结果与引用证据；
* `findings[] → corrective_actions[]`：发现（RTO 超标、仅绕行、
  证据缺失、会商判负等）与整改的“登记→接受→验证关闭”追溯链；
* `plan_vs_actual`：计划恢复顺序（按 RTO）与实际“首可用/真正恢复”
  顺序对比及顺序倒置；
* `replay_fingerprint`：业务重放指纹，只取决于冻结场景与有效事件集合，
  与上报到达顺序无关；`journal_tail_hash` 是到达顺序的哈希链凭证。

## 编程式使用

```python
from resilience_replay.platform import ResiliencePlatform

platform = ResiliencePlatform("./data")
platform.freeze_scenario(scenario_dict)
platform.ingest_many("EX-001", events)          # 乱序、重复都安全
report = platform.replay("EX-001")
report = platform.replay("EX-001", as_of="2026-09-30T08:10:00Z")
```

## 存储布局

```
<data>/<exercise_code>/scenario.json   # 冻结快照 + 指纹
<data>/<exercise_code>/events.jsonl    # 追加日志，每条含前向哈希
```

复制目录即可归档；换机器重放结果不变。篡改任何一行事件，
`verify` 与重放都会报哈希链错误。

## 运行测试与检查

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
```
