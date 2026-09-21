# 依赖图发布回滚 API（FastAPI + SQLite）

本地可运行的服务节点依赖图发布系统：客户端提交服务节点及依赖关系（节点可带正整数预估工期 `estimated_seconds`），
创建带版本号的发布批次（可选传 `targets` **按目标节点裁剪执行图**：只发布每个目标及其全部传递前置依赖）；
系统按**拓扑层级**生成执行计划，并对依赖 DAG 做**关键路径（CPM）动态规划**给出最短工期、各节点最早/最晚开始时间与零余量关键节点；
接收每个节点的成功/失败回报；节点失败时若**自动重试额度未用尽**则原子递增尝试次数、将节点恢复为 `pending`
并按可配置延迟安排重试（到期前节点不会出现在 `next_nodes`，提前回报返回 `409 retry_not_due`）；
**达到尝试上限**后仅回滚本批次已成功的节点并返回**可重试的断点**；同时拒绝**循环依赖**和**跨版本 / 乱序回报**。
对 `pending` / `running` 批次提供基于**乐观修订号（revision）**的**安全取消** `POST /api/releases/{id}/cancel`：
在同一 SQLite 事务内校验 `expected_revision`，过期返回 `409 state_conflict` 且状态不变；取消成功后未执行节点标记为
`cancelled`、已成功节点按**逆拓扑顺序**回滚，取消与节点回报并发时只有一个状态迁移能成功；取消后的批次拒绝
report / retry（`409 release_cancelled`）。
还提供只读的**批次结构差异与影响面分析**（`GET /api/releases/{id}/diff?base_release_id=...`）：
仅比较两个批次**实际持久化的裁剪后图**，输出 added / removed / changed（字段级 reasons），
并从新增 / 变更节点沿目标批次依赖边向下游传播得到 `impacted_nodes`。

## 一条命令启动

```bash
docker compose up -d --build
```

启动后：

- API 地址：`http://localhost:8000`
- 健康检查：`GET http://localhost:8000/health`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI Schema：`http://localhost:8000/openapi.json`
- SQLite 数据文件：容器内 `/data/releases.db`，通过命名卷 `release-data` 持久化

镜像构建阶段完成 Python 依赖安装；容器启动时由应用自动完成 SQLite 初始化（创建数据目录、建表、开启 WAL），无需手动初始化。

停止 / 清理：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时删除数据卷（清空所有发布记录）
```

## 配置

通过环境变量配置（见 `docker-compose.yml`，可用 `-e` 或 `.env` 覆盖）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | 服务监听地址 |
| `APP_PORT` | `8000` | 服务监听端口（改端口时同步修改 compose 的端口映射） |
| `DB_PATH` | `/data/releases.db` | SQLite 数据库文件路径，其目录会自动创建 |

## 本地裸机运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./data/releases.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 核心语义

**按目标裁剪执行图（可选 `targets`）**

创建批次时可选传 `targets`：非空、元素非空字符串且**不重复**的节点名数组；**不传时保持全量发布**。

- 传入后**先沿用现有规则校验完整节点和依赖图**（重复节点、悬空依赖边、循环依赖等针对整图校验，
  即使问题出在与目标无关的分支也会被拒绝），校验通过后再裁剪；
- 裁剪结果 = 每个目标 + 其**全部传递前置依赖**构成的**诱导子图**：多个目标共享的前置只保留一次，
  无关分支（既非目标、也不是任何目标的前置）不进入批次；
- 仅裁剪后的节点与边会被**持久化并执行**；`levels`、`plan`、`next_nodes`、失败回滚、断点重试与
  **关键路径（CPM）分析统一基于裁剪后的诱导子图**，任何响应中都不会出现已排除节点（排除节点重新分层后
  level 从 0 连续编号）；
- 创建响应中每个保留节点带 `is_target`（是否为显式目标，传递前置为 `false`），批次级返回
  `targets`（显式目标名，全量批次为 `null`）与 `excluded_node_count`（被排除节点数，全量批次为 `0`）；
- `targets: []` 返回 `422 empty_targets`、目标重复返回 `422 duplicate_target`、目标节点不存在返回
  **`422 unknown_target` 且整批零写入**（不创建批次、不写节点 / 边）；
- 旧数据库与旧批次通过启动时幂等迁移兼容（`releases.targets_json` 为 `NULL`、
  `excluded_node_count` 为 `0`、`nodes.is_target` 为 `0`），行为与裁剪前完全一致。

**执行计划（拓扑分层 + 工期分析）**

- 依赖边 `from -> to` 表示 `to` 依赖 `from`，`from` 必须先执行。
- 系统用 Kahn 算法分层：level 0 无依赖可先执行，level N 的所有前置节点都位于更低层级。
- 同一 level 内节点互不依赖，可并行回报；跨 level 必须按顺序。
- 存在环时创建批次返回 `422 cycle_detected`，并给出环上节点。
- 节点可携带 `estimated_seconds`（**正整数，秒**）作为预估工期；不传按 `0` 处理。
- `GET /api/releases/{id}/plan` 在拓扑分层之外，基于依赖 DAG 做关键路径（CPM）动态规划：
  - 前向 DP 得各节点最早开始时间 `earliest_start` 与**最短总工期** `shortest_duration_seconds`
    （同层节点可并行，总工期 = DAG 上的最长路径长度）；
  - 后向 DP 得各节点最晚开始时间 `latest_start` 与总时差 `total_slack = latest_start - earliest_start`；
  - `total_slack = 0` 的节点全部列入 `critical_nodes`，`is_critical` 在逐节点结果中同步标出；
  - `critical_path` 给出一条最长源-汇路径；并列最长路径存在时，返回**节点名序列字典序最小**的一条。
- 计划接口支持可选查询参数 `deadline_seconds`（非负整数秒）：
  - `deadline_seconds >= 最短总工期`：`feasible=true`、`gap_seconds=0`；
  - `deadline_seconds < 最短总工期`：仍返回**完整**分析（各时间、余量、关键节点/路径照常给出），
    同时 `feasible=false`、`gap_seconds = 最短总工期 - deadline_seconds`（缺口秒数）；
  - 该接口为**只读**分析，任何参数都不会改变批次或节点状态。
- 旧批次（创建时未提供工期 / 升级前的数据）缺省工期一律按 `0` 处理，CPM 仍可输出完整结果；
  服务启动时会自动为旧版 SQLite 文件补列（`ALTER TABLE` 迁移，幂等）。

**发布批次结构差异与影响面（只读 diff）**

`GET /api/releases/{release_id}/diff?base_release_id={id}` 以路径上的批次为**目标批次**、
查询参数批次为**基线批次**（比较方向 base → target），比较两者的结构差异：

- 仅比较两个批次**实际持久化的裁剪后图**：提交但因 `targets` 裁剪未入库的节点 / 边完全不可见
  （例如某节点在基线全量入库、在目标批次被裁剪掉，会表现为 `removed`）；比较不涉及节点回报状态、
  尝试次数等运行时数据。
- `added`：目标有、基线没有的节点名；`removed`：基线有、目标没有的节点名。
- `changed`：两边同名节点在以下任一维度不同即命中，并给出**字段级 `reasons`**（固定顺序、
  每项含 `field` / `base` / `target` 两侧取值）：
  - 节点类型 `node_type`
  - 工期 `estimated_seconds`
  - 重试配置 `max_attempts`、`retry_delay_seconds`（任一不同分别列出）
  - 元数据 `metadata`（整体按 JSON 对象比较）
  - 前置依赖集合 `dependencies`（按前置节点名集合比较，输出两侧排序后的列表）
- `impacted_nodes`：以 added ∪ changed 为起点，沿**目标批次**依赖边（`from -> to`）向下游
  传播的传递闭包，**包含起点本身**，按节点名排序；用于评估本次发布变更在新图中波及的全部下游。
- `removed` 节点**单独报告，不参与影响面传播**：其下游在目标图中可能已不存在，无法映射。
- `added` / `removed` / `changed` / `impacted_nodes` 均按节点名字典序**稳定排序**，
  `counts` 给出四个列表各自的计数；`compared_scope` 恒为 `persisted_pruned_graph`。
- 接口为**只读**：不开启写事务、不改变任何批次 / 节点状态；目标或基线批次不存在均返回
  `404 release_not_found`；缺少 `base_release_id` 返回 `422`；两个 ID 相同或图完全一致时
  四个列表均为空。

**回报与并发控制**

- 回报必须携带与批次一致的 `version`，否则返回 `409 version_mismatch`（拒绝跨版本回报）。
- 节点的所有前置节点均为 `success` 后才允许回报，否则返回 `409 out_of_order`（拒绝乱序回报）。
- 已回报过的节点不可重复回报（`409 already_reported`）。
- 批次失败回滚后不可继续回报，需先调用 `/retry` 续发（`409 release_aborted`）。

**有限自动重试**

每个节点可在创建时配置：

| 字段 | 默认 | 取值 | 说明 |
| --- | --- | --- | --- |
| `max_attempts` | `1` | `1 ～ 10` | 最大尝试次数（含首次回报）；`1` 表示不自动重试，首次失败即终态 |
| `retry_delay_seconds` | `0` | `0 ～ 3600` | 失败后自动重试的延迟（秒）；`0` 表示到期立即可再次回报 |

两个字段均持久化；旧批次（升级前创建）自动迁移并按 `max_attempts=1`、`retry_delay_seconds=0` 兼容，行为与升级前一致。

- 节点回报 `failed` 时，在同一事务内**原子递增** `attempt_count`：
  - **尚未达到上限**（`attempt_count < max_attempts`）：**不触发批次回滚**，批次保持 / 进入 `running`；
    失败节点恢复为 `pending`，已成功的前置节点保持 `success`；
    设置 `next_retry_at = 失败时间 + retry_delay_seconds`，并标记 `retry_scheduled=true`；
    响应 `result=retry_scheduled`，并记录 `retry_scheduled` 事件。
  - 等待期内（`next_retry_at` 未到期）该节点被排除在 `GET /api/releases/{id}` 的 `next_nodes` 之外；
    到期且所有前置仍为 `success` 时自动重新出现。
  - 到期前对该节点的任何回报都返回 `409 retry_not_due`（携带 `next_retry_at` / `attempt_count` / `max_attempts`），
    **不改变任何状态**（不递增计数、不改变批次 / 节点状态）。
  - **达到上限**：保持既有失败语义——逆拓扑回滚本批次已成功节点、批次进入 `failed` /
    `partially_rolled_back`、返回可重试 `checkpoint`；清除 `retry_scheduled` 与 `next_retry_at`。
- 等待期后回报 `success`：节点成功，清除 `retry_scheduled` / `next_retry_at`；
  `attempt_count` 保留为历史失败次数。
- `POST /retry` 从终态续发时，重置节点的 `attempt_count=0`、`retry_scheduled=false`、
  `next_retry_at=null`，自动重试额度重新按 `max_attempts` 计满。
- 节点详情返回 `attempt_count`、`max_attempts`、`retry_delay_seconds`、`retry_scheduled`、
  `next_retry_at`（UTC ISO-8601）。

**失败回滚与断点重试**

- 任一节点回报 `failed`：批次标记为 `partially_rolled_back`（此前无成功节点时为 `failed`）。
- 回滚范围**仅限本批次已成功的节点**（历史批次完全不受影响），按**逆拓扑顺序**回滚：
  层级高的先回滚，同层按成功时间倒序；失败节点本身保持 `failed`。
- 回报响应中返回可重试断点 `checkpoint`：含 `/retry` 地址、`restart_level` 与 `restart_nodes`
  （调用重试后最先可执行的节点，即最低待执行层级）。
- `POST /retry` 把所有 `failed` / `rolled_back` 节点重置为 `pending`，从最低待执行层级继续，
  已经成功过的节点（如发生在“部分成功后才失败”的重试场景）保留成功状态，不重复执行。

节点状态：`pending` → `success` / `failed`；失败未达重试上限时节点保持 `pending`
（`retry_scheduled=true`、等待 `next_retry_at` 到期）；成功节点失败回滚后变为 `rolled_back`，
终态重试重置回 `pending`。
批次状态：`pending` / `running` / `succeeded` / `failed` / `partially_rolled_back` / `cancelled`。

**安全取消（乐观修订号）**

`POST /api/releases/{id}/cancel`，请求体：

```json
{"reason": "线上故障，人工中止", "expected_revision": 3}
```

- 每个批次带一个单调递增的 **`revision`**（创建时为 `1`，每次批次级状态迁移 +1：节点回报推进批次状态、
  失败终态回滚、`/retry` 续发、`/cancel` 都会使其 +1；节点成功使批次 `running → succeeded` 时同样 +1）。
  批次详情 / 列表的每个响应都返回当前 `revision`，作为下一次写操作的乐观锁凭据。
- **仅 `pending` / `running` 批次可取消**；`succeeded` / `failed` / `partially_rolled_back`
  返回 `409 cancel_not_allowed`（后两者请用 `/retry` 续发）。
- `expected_revision` 与当前修订号在**同一 SQLite 写事务**内比较：
  - 一致才执行取消；**过期返回 `409 state_conflict`**（响应含 `expected_revision` 与
    `current_revision`），事务回滚、**批次与节点状态均不变**，调用方应重新 `GET` 状态后决定是否重试；
  - 批次已是 `cancelled` 且修订号匹配时返回 `409 release_cancelled`（不可重复取消）；
    修订号不匹配时仍先返回 `state_conflict`。
- 取消成功（批次进入终态 `cancelled`，`revision` +1）后，同一事务内：
  - **未执行节点**（`pending`，含自动重试等待期中的节点）全部标记为 `cancelled`
    （等待期节点同时清除 `retry_scheduled` / `next_retry_at`，`attempt_count` 历史保留），
    按 level / 名字稳定排序后放入 `cancelled_nodes`；
  - **已成功节点**按**逆拓扑顺序**回滚为 `rolled_back`（层级高的先回滚，同层按成功时间倒序，
    与失败回滚同一套顺序），放入 `rolled_back_nodes`；
  - 响应返回 `result=cancelled`、`reason`、`cancelled_at`（UTC ISO-8601）、
    **新 `revision`**、`cancelled_nodes`、`rolled_back_nodes` 及完整 `release` 视图。
  - 处于 `failed` 终态的节点（达到尝试上限后的失败节点）在 running 批次中不存在
    （那种情况下批次本身已是 failed / partially_rolled_back，不在可取消范围内）。
- **并发仲裁**：取消的状态迁移用
  `UPDATE ... SET status='cancelled', revision=revision+1 WHERE id=? AND status IN ('pending','running') AND revision=?`
  做条件更新；所有写事务经同一把锁串行执行，因此取消与节点回报（或两个取消）并发时，
  **恰好一个**状态迁移成功（条件更新 `rowcount=1`），落败方得到 `409 state_conflict` /
  `release_cancelled` 且不做任何写入，修订号不会重复 +1。
- **取消后的批次拒绝 `report` 与 `retry`**，统一返回 **`409 release_cancelled`**
  （错误体含 `cancelled_at` 与 `cancel_reason`），取消不可撤销、不可续发。
- `GET /api/releases/{id}` 与列表接口均展示 `revision`、`cancel_reason`、`cancelled_at`
  （非取消批次后两者为 `null`）；批次不存在返回 `404 release_not_found`；
  缺少 `reason` / `expected_revision` 或取值非法（如修订号 < 1）返回 `422`。
- 旧版 SQLite 文件启动时幂等迁移：`releases` 自动补 `revision`（旧批次统一为 1）、
  `cancel_reason`、`cancelled_at` 列，旧批次行为与升级前一致。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/releases` | 创建发布批次（提交节点 + 依赖；可选非空不重复 `targets` 按目标裁剪为诱导子图；节点可填正整数 `estimated_seconds`、`max_attempts`(1~10)、`retry_delay_seconds`(0~3600)，自动拓扑分层，拒绝环） |
| `GET` | `/api/releases?limit=50` | 发布批次列表 |
| `GET` | `/api/releases/{id}` | 查询发布状态（批次 `revision`、节点状态含 `attempt_count` / `max_attempts` / `next_retry_at`、进度、next_nodes，未到期重试节点不出现；取消批次含 `cancel_reason` / `cancelled_at`） |
| `GET` | `/api/releases/{id}/plan?deadline_seconds=` | 拓扑分层 + CPM 工期分析（最短工期、最早/最晚开始、total_slack、零余量关键节点、字典序最小关键路径；可选截止工期可行性，只读） |
| `GET` | `/api/releases/{id}/diff?base_release_id=` | 两批次结构差异与影响面（仅比较实际持久化的裁剪后图；added/removed/changed 字段级 reasons；impacted_nodes 沿目标批次边向下游传递含起点；稳定排序 + 计数；只读，缺失批次 404） |
| `POST` | `/api/releases/{id}/report` | 节点成功 / 失败回报（跨版本、乱序返回 409；自动重试等待期返回 409 retry_not_due；未达上限返回 retry_scheduled，达上限失败回滚；已取消批次返回 409 release_cancelled） |
| `POST` | `/api/releases/{id}/retry` | 从断点续发（已取消批次返回 409 release_cancelled） |
| `POST` | `/api/releases/{id}/cancel` | 安全取消 pending/running 批次（body：`reason` + `expected_revision`；同一事务校验修订号，过期 409 state_conflict 且状态不变；未执行节点标 cancelled、已成功节点逆拓扑回滚，返回 cancelled_nodes / rolled_back_nodes / 新 revision；终态批次 409 cancel_not_allowed） |
| `GET` | `/health` | 健康检查 |

错误响应统一格式：`{"error": {"code": "...", "message": "...", ...}}`。

## 端到端示例

```bash
# 1) 创建批次：a、b 是底层，c 依赖 a/b，d 依赖 c
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "2026.09.20-001",
    "nodes": [
      {"name": "svc-a"}, {"name": "svc-b"},
      {"name": "svc-c"}, {"name": "svc-d"}
    ],
    "dependencies": [
      {"from_node": "svc-a", "to_node": "svc-c"},
      {"from_node": "svc-b", "to_node": "svc-c"},
      {"from_node": "svc-c", "to_node": "svc-d"}
    ]
  }'
# -> 计划分层: level0=[svc-a,svc-b]  level1=[svc-c]  level2=[svc-d]

# 2) 查看执行计划（含 CPM 关键路径分析）
curl -s http://localhost:8000/api/releases/<ID>/plan
#    （上例未填 estimated_seconds，所有工期按 0：最短工期 0、全部节点 total_slack=0）

# 2b) 带工期的批次 + deadline 可行性分析（示例：a=2s, b=5s, c=3s, d=1s）
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "2026.09.21-002",
    "nodes": [
      {"name": "svc-a", "estimated_seconds": 2},
      {"name": "svc-b", "estimated_seconds": 5},
      {"name": "svc-c", "estimated_seconds": 3},
      {"name": "svc-d", "estimated_seconds": 1}
    ],
    "dependencies": [
      {"from_node": "svc-a", "to_node": "svc-c"},
      {"from_node": "svc-b", "to_node": "svc-c"},
      {"from_node": "svc-c", "to_node": "svc-d"}
    ]
  }'
# 关键路径 svc-b -> svc-c -> svc-d（5+3+1=9 秒，svc-a 有 3 秒余量）
curl -s 'http://localhost:8000/api/releases/<ID>/plan?deadline_seconds=8'
# -> schedule:
# {
#   "shortest_duration_seconds": 9,
#   "deadline_seconds": 8,
#   "feasible": false,
#   "gap_seconds": 1,
#   "critical_nodes": ["svc-b", "svc-c", "svc-d"],
#   "critical_path": ["svc-b", "svc-c", "svc-d"]
# }
# 每个节点附带：estimated_seconds / earliest_start / latest_start / total_slack / is_critical

# 2c) 按目标裁剪：同一批节点只发布 svc-d（带上其传递前置 a/b/c），svc-e 无关分支不入库
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "2026.09.21-004",
    "nodes": [
      {"name": "svc-a"}, {"name": "svc-b"}, {"name": "svc-c"},
      {"name": "svc-d"}, {"name": "svc-e"}
    ],
    "dependencies": [
      {"from_node": "svc-a", "to_node": "svc-c"},
      {"from_node": "svc-b", "to_node": "svc-c"},
      {"from_node": "svc-c", "to_node": "svc-d"},
      {"from_node": "svc-e", "to_node": "svc-d"}
    ],
    "targets": ["svc-d"]
  }'
# -> targets=["svc-d"], excluded_node_count=0（e 是 d 的前置，同样保留）
#    levels=[[svc-a,svc-b,svc-e],[svc-c],[svc-d]]
#    仅 svc-d 的 is_target=true；next_nodes=[svc-a,svc-b,svc-e]
#
# 多目标共享前置只保留一次；无关分支被排除：
#   节点 a,b,c,d,e；边 a->c, b->c, c->d, e（独立）；targets=["c"]
#   -> 保留 {a,b,c}，excluded_node_count=2（d、e 排除），levels=[[a,b],[c]]
#
# 目标不存在 -> 422 且整批零写入：
#   {"error":{"code":"unknown_target","targets":["ghost"], ...}}
# targets=[] -> 422 empty_targets；targets=["c","c"] -> 422 duplicate_target
# 不传 targets 或传 null -> 与旧版本一致的全量发布（targets=null, excluded_node_count=0）

# 2d) 批次结构差异与影响面：以新批次为目标、旧批次为基线（只读）
curl -s 'http://localhost:8000/api/releases/<NEW_ID>/diff?base_release_id=<OLD_ID>'
# -> {
#   "base_release_id": "<OLD_ID>", "target_release_id": "<NEW_ID>",
#   "base_version": "2026.09.20-001", "target_version": "2026.09.21-002",
#   "added": ["svc-e"],
#   "removed": ["svc-b"],
#   "changed": [
#     {"node": "svc-a", "reasons": [
#       {"field": "node_type", "base": "service", "target": "job"},
#       {"field": "estimated_seconds", "base": 2, "target": 5},
#       {"field": "max_attempts", "base": 1, "target": 3},
#       {"field": "retry_delay_seconds", "base": 0, "target": 30},
#       {"field": "metadata", "base": {"team": "infra"},
#        "target": {"region": "cn", "team": "infra"}}
#     ]},
#     {"node": "svc-c", "reasons": [
#       {"field": "dependencies", "base": ["svc-a", "svc-b"], "target": ["svc-a"]}
#     ]}
#   ],
#   "impacted_nodes": ["svc-a", "svc-c", "svc-d", "svc-e"],
#   "counts": {"added": 1, "removed": 1, "changed": 2, "impacted_nodes": 4},
#   "compared_scope": "persisted_pruned_graph"
# }
# 说明：
# - 仅比较两批次实际持久化的裁剪后图；被 targets 裁剪掉的节点在对应侧不可见（可能表现为 removed）
# - impacted_nodes 从 added/changed 沿目标批次依赖边向下游传播（含起点）；removed 不参与传播
# - 各列表按节点名稳定排序；目标或基线批次不存在 -> 404 release_not_found；接口不改变批次状态

# 3) 回报 level0 成功（同层可并行/乱序回报）
curl -s -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-a","status":"success","version":"2026.09.20-001"}'

# 4) 乱序回报会被拒绝（svc-b 尚未成功，svc-c 前置不齐）
#    -> 409 {"error":{"code":"out_of_order", ...}}

# 5) 跨版本回报会被拒绝
#    {"node":"svc-b","status":"success","version":"9999-old"}
#    -> 409 version_mismatch

# 6) svc-c 失败：svc-a/svc-b 被逆序回滚，响应含 checkpoint
curl -s -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-c","status":"failed","version":"2026.09.20-001",
       "error_code":"DEPLOY_TIMEOUT","error_message":"健康检查超时"}'
# -> status=partially_rolled_back
#    rolled_back_nodes=[{svc-b},{svc-a}]（同层按成功时间倒序）

# 7) 从断点重试：节点重置为 pending，next_nodes 重新变为 [svc-a, svc-b]
curl -s -X POST http://localhost:8000/api/releases/<ID>/retry

# 8) 查询状态
curl -s http://localhost:8000/api/releases/<ID>
```

有限自动重试示例（最多 3 次尝试、每次间隔 60 秒）：

```bash
# 1) 创建带重试策略的批次
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "2026.09.21-003",
    "nodes": [{"name": "svc-a", "max_attempts": 3, "retry_delay_seconds": 60}]
  }'

# 2) 首次失败：不回滚，节点恢复 pending、attempt_count=1、安排 next_retry_at
curl -s -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-a","status":"failed","version":"2026.09.21-003","error_code":"BOOT_FAIL"}'
# -> {"result":"retry_scheduled","attempt_count":1,"max_attempts":3,
#     "next_retry_at":"...+00:00","release":{"status":"running",
#     "next_nodes":[] /* 未到期，svc-a 不出现 */}}

# 3) 到期前再次回报 -> 409，状态不变
#    {"error":{"code":"retry_not_due","next_retry_at":"...","attempt_count":1,"max_attempts":3}}

# 4) 到期（next_retry_at 之后）：svc-a 重新出现在 next_nodes，可再次回报；
#    第 3 次失败（attempt_count=3 == max_attempts）才触发失败回滚与 checkpoint。
#    POST /retry 从终态续发后 attempt_count 清零，重试额度重新计满。
```

安全取消示例（乐观修订号 + 逆拓扑回滚）：

```bash
# 批次：level0=[svc-a,svc-b] level1=[svc-c] level2=[svc-d]；svc-a/svc-b 已成功
curl -s http://localhost:8000/api/releases/<ID>
# -> {"status":"running","revision":3, ...}

# 1) 携带旧修订号取消 -> 409，状态不变（事务回滚）
curl -s -i -X POST http://localhost:8000/api/releases/<ID>/cancel \
  -H 'Content-Type: application/json' \
  -d '{"reason":"线上故障，人工中止","expected_revision":1}'
# -> 409 {"error":{"code":"state_conflict",
#        "message":"修订号已过期：expected_revision=1，当前 revision=3，取消请求未执行",
#        "expected_revision":1,"current_revision":3}}

# 2) 携带最新修订号取消 -> 未执行节点 cancelled，已成功节点逆拓扑回滚
curl -s -X POST http://localhost:8000/api/releases/<ID>/cancel \
  -H 'Content-Type: application/json' \
  -d '{"reason":"线上故障，人工中止","expected_revision":3}'
# -> {
#   "result": "cancelled",
#   "reason": "线上故障，人工中止",
#   "cancelled_at": "2026-09-21T12:00:00.000000+00:00",
#   "revision": 4,
#   "cancelled_nodes": [{"node": "svc-c", "level": 1}, {"node": "svc-d", "level": 2}],
#   "rolled_back_nodes": [{"node": "svc-b", "level": 0}, {"node": "svc-a", "level": 0}],
#   "release": {"status": "cancelled", "revision": 4,
#               "cancel_reason": "线上故障，人工中止", "cancelled_at": "...", ...}
# }
# 说明：
# - pending 批次取消时 rolled_back_nodes 为空，全部节点进入 cancelled_nodes
# - 自动重试等待中的 pending 节点同样标 cancelled（清除 next_retry_at，保留 attempt_count）
# - 取消与节点回报并发时，条件状态迁移保证只有一方成功（落败方 409 state_conflict）

# 3) 取消后的批次拒绝 report / retry
curl -s -i -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-c","status":"success","version":"2026.09.20-001"}'
# -> 409 {"error":{"code":"release_cancelled",
#        "message":"该批次已被安全取消，取消后的批次不再接受节点回报，也不可重试",
#        "cancelled_at":"...","cancel_reason":"..."}}
curl -s -i -X POST http://localhost:8000/api/releases/<ID>/retry
# -> 409 {"error":{"code":"release_cancelled", ...}}

# 4) 状态查询展示取消原因与时间；succeeded/failed 批次取消 -> 409 cancel_not_allowed
curl -s http://localhost:8000/api/releases/<ID>
# -> {"status":"cancelled","revision":4,"cancel_reason":"线上故障，人工中止",
#     "cancelled_at":"...", "nodes":[...cancelled / rolled_back...]}
```

循环依赖示例（返回 `422`）：

```bash
curl -i -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"bad","nodes":[{"name":"x"},{"name":"y"}],
       "dependencies":[{"from_node":"x","to_node":"y"},{"from_node":"y","to_node":"x"}]}'
```

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、启动初始化、统一错误处理
│   ├── config.py      # 环境变量配置
│   ├── database.py    # SQLite 连接、建表、事务
│   ├── models.py      # 请求模型
│   ├── errors.py      # 业务异常
│   └── service.py     # 拓扑分层 / 批次 / 回报 / 回滚 / 重试 / 安全取消 / 批次差异与影响面
├── requirements.txt
├── smoke_test.py      # 端到端冒烟测试（pip install httpx 后 python3 smoke_test.py）
├── Dockerfile         # 构建期安装依赖
├── docker-compose.yml # 一条命令启动 + 数据卷持久化
└── README.md
```

## 测试

```bash
pip install -r requirements.txt httpx   # httpx 仅 TestClient 需要
python3 smoke_test.py
```

覆盖：循环依赖拒绝、拓扑分层、跨版本回报拒绝、乱序回报拒绝、重复回报拒绝、
失败逆序回滚本批次已成功节点、断点重试后走完全程、部分回滚、历史批次隔离、重复初始化幂等、
CPM 前向/后向分析与零余量关键节点、并列最长路径字典序最小、deadline 可行性与缺口秒数、
查询不改变批次状态、estimated_seconds 正整数校验、旧版 SQLite 文件自动迁移且旧工期按 0、
max_attempts / retry_delay_seconds 取值校验与持久化、失败未达上限原子递增并调度重试（不回滚）、
等待期排除 next_nodes 且提前回报 409 retry_not_due 状态不变、到期后重新出现、达到上限走终态回滚、
retry_delay_seconds=0 立即重试、手动 /retry 清零计数刷新额度、旧批次 max_attempts 默认 1 兼容、
targets 按目标裁剪诱导子图（传递前置闭包、共享前置去重、无关分支排除、levels 连续重编号）、
裁剪批次的 plan/CPM/next_nodes/回报/回滚/重试不含已排除节点、裁剪改变关键路径分析、
targets 空/重复/空白/类型校验、unknown_target 422 且整批零写入、裁剪前先校验整图（环/悬空边）、
targets=null 全量发布、旧库补列（targets_json/excluded_node_count/is_target）且旧批次按全量兼容、
发布批次结构差异与影响面（added/removed/changed 五类字段级 reasons、仅比较持久化裁剪后图、
impacted_nodes 沿目标批次依赖边向下游传递含起点、removed 不参与传播、各列表稳定排序与计数、
相同批次 / 自身比较为空、只读不改状态、缺失批次 404、缺少 base_release_id 422）、
安全取消（revision 从 1 起且每次状态迁移 +1、pending 整批取消、running 取消未执行标 cancelled 与
已成功逆拓扑回滚顺序、cancelled_at/cancel_reason 持久化与状态/列表查询展示、过期修订号 409
state_conflict 携带两侧修订号且状态不变、已取消批次重复取消 409 release_cancelled、
succeeded/failed 取消 409 cancel_not_allowed、取消后 report/retry 409 release_cancelled 且状态不变、
取消与取消/取消与失败回报并发恰一个状态迁移成功且 revision 恰好 +1、自动重试等待期节点随取消标
cancelled 并清等待标记保留尝试历史、旧库补列 revision/cancel_reason/cancelled_at 且旧批次可取消）。
