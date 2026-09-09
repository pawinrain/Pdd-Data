# PDD Data MCP — Stage D4 验收报告

- 验收日期：2026-09-08
- 环境：Windows 10 `10.0.19045` / Python 3.12.14 / Asia/Shanghai
- 目标内部店铺：`st_pdd_current_01`
- 范围：推广账户效果、推广商品效果、推广商品当前配置；READ ONLY
- Stage A / B / C：**PASS（基线保留）**
- Stage D1 / D2 / D3：**PASS（本轮真实回归）**
- Stage D4 已核实能力：**PASS**（账户 TODAY、推广商品 TODAY、推广商品当前配置各保留三次既有重复真实验收；账户 TODAY 另完成一次 v3 回归；账户 YESTERDAY v3 部分日窗口完成三次重复真实验收）
- Stage D5 主软件正式接入：**NOT_RUN**

## 1. 结论

本轮在不增加 MCP 工具、不改变旧系统读取路线的前提下，完成了有页面证据支持的 D4 推广只读扩展，并补正了账户 YESTERDAY 时间口径。初始验收的 10 次首次提交中，账户 TODAY、推广商品 TODAY、推广商品当前配置共 9 份继续作为有效基线；旧 YESTERDAY v2 快照因半开结束错标而只保留为失效审计历史。纠正客户端随后通过真实 stdio MCP 新增三次独立 YESTERDAY v3 采集和一次 TODAY v3 回归。新批次全部通过读取、exact-scope latest、keyset 分页、服务重启读回和幂等重放。YESTERDAY v3 只证明准确的部分日/同期窗口，不等于完整昨日自然日或最终值。

计划级指标没有足够的独立页面证据，因此 `campaign_metrics` 保持 `UNAVAILABLE`；商品记录中的 `campaign_id` 只作为经过验证的关联外键，不冒充计划粒度指标。账户/计划级当前设置、推广商品 YESTERDAY、7/30/90 日窗口也没有被擅自启用。

## 2. 已启用能力

现有六个 MCP 工具名称和职责保持不变：

1. `pdd_get_capabilities`
2. `pdd_get_connection_status`
3. `pdd_collect_snapshot`
4. `pdd_list_snapshots`
5. `pdd_read_snapshot`
6. `pdd_get_latest_snapshot`

| 数据集 | 状态 | 粒度 | 已验证窗口 | 说明 |
|---|---|---|---|---|
| `promotion_overview` | `REAL_PROMOTION_WINDOWS` | ACCOUNT | TODAY、YESTERDAY | 当前 D4 账户契约为 `scope.version=d4-account-v3`；TODAY 保留三次既有基线并完成一次 v3 回归，YESTERDAY v3 部分日窗口完成三次重复真实验收 |
| `product_metrics` | `REAL_PROMOTED_PRODUCT_METRICS` | PROMOTED_PRODUCT | TODAY | 每行带 `ad_id`、`campaign_id`、`platform_product_id` 三元关联 |
| `promotion_configuration` | `REAL_PROMOTION_CONFIGURATION_CURRENT` | PROMOTED_PRODUCT | POINT_IN_TIME | 只保存观察时的当前设置，不冒充历史设置 |
| `campaign_metrics` | `UNAVAILABLE` | CAMPAIGN | 无 | 页面证据不足；`planId` 仅作外键 |

账户与推广商品效果使用同一组严格的 12 个业务字段契约：花费、订单花费、订单 ROI、净 ROI、净订单数、订单数、成交额、净成交额、曝光、点击、结算 ROI、结算订单数。金额无损转换为整数分，ROI 保存为规范十进制字符串，计数只接受非负整数；缺失值为 null，并必须携带精确缺失原因，绝不补 0。

推广商品当前配置只保存已核实的 `max_cost_cents`、`target_roi`、`agent_bid`、`ad_status` 及 `configuration_observed_at`。任何 null 同样必须带 `SOURCE_FIELD_MISSING` 或 `SOURCE_VALUE_NULL`。

## 3. 页面、响应与身份门禁

- 账户页面：`https://yingxiao.pinduoduo.com/mains/promotionOverview`
- 推广商品页面：`https://yingxiao.pinduoduo.com/goods/promotion/list`
- 账户响应：精确 POST `/mms-gateway/poseidon/api/report/queryHourlyRangeReport`
- 推广商品响应：精确 POST `/mms-gateway/venus/api/goods/promotion/v3/list`
- 身份响应：按各适配器锁定精确 host、path、method、HTTP 状态、JSON 类型和业务成功字段
- 请求正文：锁定已核实键集合、常量、日期、页码、每页 50 条和无筛选状态；未知键或筛选漂移拒绝提交
- 身份：独立店铺页面状态 SHA-256 门禁与响应身份一致；推广商品每一行再核对店铺身份
- DOM：账户今/昨总花费及说明文本交叉核对；推广商品按页面行一一核对，但不把商品名写入验收输出

原始响应、Cookie、Token、Authorization、请求头和 HAR 均未落盘或输出。服务只断开自身 Playwright CDP 客户端，不关闭 Browser、Context 或页面。

## 4. 时间与来源语义

`captured_at` 与 `metric_window` 独立保存，所有正文时间带时区，文件目录按 Asia/Shanghai 分区。

- 账户 TODAY：窗口从当日 00:00 到网络响应中的平台更新时间；`capture_method=NETWORK_RESPONSE`，`field_sources[metric_window.end]=NETWORK_RESPONSE`。本次 v3 回归窗口为 `[2026-09-08 00:00, 2026-09-08 12:53:56)`，`source_updated_at` 与 end 相同。
- 账户 YESTERDAY：效果来自白名单网络响应；请求 `endDayHour`、响应小时行和页面 DOM 截止共同约束半开结束，`capture_method=MIXED`、`field_sources[metric_window.end]=DOM`。三次 v3 结果都是 `[2026-09-07 00:00, 2026-09-07 13:00)`，`source_updated_at=null` 并保留精确缺失原因；这是昨日部分日/同期窗口，不是完整昨日自然日。
- 推广商品 TODAY：窗口从当日 00:00 到 `captured_at`，并在采集器和快照校验器两层强制 `window.start < source_updated_at <= captured_at`。
- 当前配置：POINT_IN_TIME、无 `metric_window`，只表示观察时设置。

上述效果窗口均 `window_complete=false`、`source_finalized=false`。快照的 `coverage=COMPLETE` 只表示页面实体覆盖完整，不表示统计日已经结束；累计指标不会跨不同采集时刻相加。

### 2026-09-08 YESTERDAY 口径纠正与重复验收 Addendum

对旧快照、请求/响应小时证据和页面截止文本重新核对后，确认原报告把 v2 的半开结束解释错了：

- 旧快照 `s_28300358ab6c4a76aa640362c20024d6` 的请求为 `endDayHour=2`，响应小时行覆盖 `0..2`，页面显示昨日截止 `02:59`。按 `[start,end)` 语义，正确结束应是 `03:00`，原 manifest 写成 `02:59`，因此属于 `METRIC_WINDOW_END_MISLABELED`。
- 历史不可变：旧快照的 manifest、正文、validation 和 COMMIT 均未删除或改写；仓库追加失效记录 `i_0454ea48bc564be2980d27e1561b38bc`，明确从 `d4-account-v2` 迁移到 `d4-account-v3`。
- 失效后，旧 YESTERDAY v2 exact-scope latest 返回 `NOT_FOUND`；显式 read 和 list 返回 `SEMANTICALLY_INVALIDATED` 及同一失效记录，继续保留审计可见性。原幂等键不能把该快照再次伪报为成功。
- 三次新 YESTERDAY v3 快照均核对请求起止日期和响应业务日期为 `2026-09-07`，业务时区 `Asia/Shanghai`；每次 `endDayHour=12`，响应小时行严格为 `0..12` 共 13 行，页面昨日截止严格为 `12:59`，所以半开窗口为 `[2026-09-07 00:00, 2026-09-07 13:00)`。
- 三次新快照均为 `schema_version=1.1.0`、`scope.version=parser_version=d4-account-v3`、`capture_method=MIXED`、`window_complete=false`、`source_finalized=false`、`source_updated_at=null`。`coverage=COMPLETE` 只表示账户实体覆盖 1/1，不表示时间范围完整或指标最终确定。
- 新 v3 scope 已完成三次独立真实采集及 MCP 全链路验收，但该 scope 仍只是昨日部分日/同期范围，不能写成完整昨日、完整自然日或最终值。

## 5. 正式真实 MCP 验收

正式客户端固定业务日为 2026-09-08，并在每次调用前及结束后检查没有跨上海午夜。既有账户 TODAY、推广商品 TODAY 和当前配置各三轮基线保持有效。纠正客户端对 YESTERDAY v3 使用三个不同幂等键并间隔采集，另执行一次 TODAY v3 回归；任一 replay 冒充首次提交、空业务数据、实体覆盖为 PARTIAL、截断、时间证据不一致或字段全 null 都会使最终结果为 FAIL。

| 轮次 | 数据集 / 窗口 | snapshot_id | 捕获 | 结果 |
|---:|---|---|---:|---|
| 1 | Account TODAY（既有基线） | `s_e1fb88387db04cfe9427ba5efbf4c02d` | 1/1 | COMPLETE |
| 1 | Product TODAY | `s_dbaf65c1e42a4bf8ab1e8084e707aac8` | 3/3 | COMPLETE |
| 1 | Configuration current | `s_3807795ad34c45428c34f62bb9831676` | 3/3 | COMPLETE |
| 2 | Account TODAY（既有基线） | `s_8fff43fcca3447409abb24b4c44b80e5` | 1/1 | COMPLETE |
| 2 | Product TODAY | `s_53a547309850461c958ab5a0129bdbb2` | 3/3 | COMPLETE |
| 2 | Configuration current | `s_8f491efebaf44d3aa67055b62f457e99` | 3/3 | COMPLETE |
| 3 | Account TODAY（既有基线） | `s_4b2f3fb1b21947a4a9a8ffa5f08fa013` | 1/1 | COMPLETE |
| 3 | Product TODAY | `s_48be7c94521b4e229d9552e2820b6296` | 3/3 | COMPLETE |
| 3 | Configuration current | `s_2d124fbb684d45e38f5613ea9124c079` | 3/3 | COMPLETE |
| 审计历史 | Account YESTERDAY v2 | `s_28300358ab6c4a76aa640362c20024d6` | 1/1 | `SEMANTICALLY_INVALIDATED`；不参与 latest |
| v3-1 | Account YESTERDAY partial | `s_0953d8e810b6441fbdd8fe3ca8a38f56` | 1/1 | COMPLETE 实体覆盖；窗口 `[00:00,13:00)`、partial/non-final |
| v3-2 | Account YESTERDAY partial | `s_b17f1a1fc6a749feb155c422e92032de` | 1/1 | COMPLETE 实体覆盖；窗口 `[00:00,13:00)`、partial/non-final |
| v3-3 | Account YESTERDAY partial | `s_e33050b13b5c436aaae56c402336eaa2` | 1/1 | COMPLETE 实体覆盖；窗口 `[00:00,13:00)`、partial/non-final |
| v3 回归 | Account TODAY | `s_48a017667a0c4cd891e6a447fcf7071d` | 1/1 | COMPLETE 实体覆盖；窗口 `[00:00,12:53:56)`、partial/non-final |

每个 Product 快照的三元关联集合与同轮 Configuration 完全一致；三轮推广商品 ID 集合都是当前店铺最新 D2 商品目录的非空子集。真实平台 ID、商品名、指标值和配置值未写入本报告或客户端输出。

### MCP 结果

- 工具发现：精确 6/6，PASS；没有新增或改名 MCP 工具。
- YESTERDAY v3 首次提交：3/3 `SUCCEEDED`、`committed=true`、`idempotent_replay=false`，snapshot ID 各不相同。
- TODAY v3 回归首次提交：1/1 `SUCCEEDED`；窗口 end 与 `source_updated_at` 同为 `2026-09-08 12:53:56+08:00`。
- Snapshot read 与 exact-scope latest：四份新 v3 快照全部 PASS；YESTERDAY 每轮 latest 指向当轮新快照。
- 历史列表：keyset cursor 分页至少两页、无重复 snapshot ID，四份新 v3 快照均为 ACTIVE，PASS。
- 旧 v2 纠正：YESTERDAY exact-scope latest 为 `NOT_FOUND`；read/list 为 `SEMANTICALLY_INVALIDATED` 并返回失效证据，PASS。
- 服务重启后完整 read 对比：四份新 v3 快照及旧失效 v2 快照均与重启前完全相等，PASS。
- 幂等重放：重复第一轮 YESTERDAY v3 原键返回原 snapshot ID、`idempotent_replay=true`，未新建快照，PASS。
- 既有 9 份账户 TODAY、推广商品 TODAY、当前配置基线验收结果保持有效；旧 v2 YESTERDAY 不再计入有效 PASS 样本。
- Product YESTERDAY 负向 MCP 探针：`DATASET_UNVERIFIED`、未提交、无 snapshot ID，PASS。

## 6. 实际快照目录

数据根为项目内被 Git 忽略的 `stage-d-real-data/`。每份正式目录均含 manifest、validation、COMMIT 与 data/records 文件。

```text
snapshots/pdd/st_pdd_current_01/promotion_overview/2026/09/08/022809_980_s_e1fb88387db04cfe9427ba5efbf4c02d
snapshots/pdd/st_pdd_current_01/product_metrics/2026/09/08/022912_042_s_dbaf65c1e42a4bf8ab1e8084e707aac8
snapshots/pdd/st_pdd_current_01/promotion_configuration/2026/09/08/023014_223_s_3807795ad34c45428c34f62bb9831676
snapshots/pdd/st_pdd_current_01/promotion_overview/2026/09/08/023116_605_s_8fff43fcca3447409abb24b4c44b80e5
snapshots/pdd/st_pdd_current_01/product_metrics/2026/09/08/023218_666_s_53a547309850461c958ab5a0129bdbb2
snapshots/pdd/st_pdd_current_01/promotion_configuration/2026/09/08/023320_785_s_8f491efebaf44d3aa67055b62f457e99
snapshots/pdd/st_pdd_current_01/promotion_overview/2026/09/08/023423_025_s_4b2f3fb1b21947a4a9a8ffa5f08fa013
snapshots/pdd/st_pdd_current_01/product_metrics/2026/09/08/023525_094_s_48be7c94521b4e229d9552e2820b6296
snapshots/pdd/st_pdd_current_01/promotion_configuration/2026/09/08/023648_806_s_2d124fbb684d45e38f5613ea9124c079
snapshots/pdd/st_pdd_current_01/promotion_overview/2026/09/08/023751_173_s_28300358ab6c4a76aa640362c20024d6
_invalidations/s_28300358ab6c4a76aa640362c20024d6/i_0454ea48bc564be2980d27e1561b38bc.json
snapshots/pdd/st_pdd_current_01/promotion_overview/2026/09/08/125128_439_s_0953d8e810b6441fbdd8fe3ca8a38f56
snapshots/pdd/st_pdd_current_01/promotion_overview/2026/09/08/125230_794_s_b17f1a1fc6a749feb155c422e92032de
snapshots/pdd/st_pdd_current_01/promotion_overview/2026/09/08/125333_080_s_e33050b13b5c436aaae56c402336eaa2
snapshots/pdd/st_pdd_current_01/promotion_overview/2026/09/08/125435_512_s_48a017667a0c4cd891e6a447fcf7071d
```

`s_28300358ab6c4a76aa640362c20024d6` 的快照目录仍完整存在；上面的 `_invalidations` 文件是独立追加元数据，不在原目录内覆盖任何历史文件。

## 7. D1 / D2 / D3 真实回归（与新能力分开）

本轮在 D4 正式验收前用同一当前店铺重新执行一次三类真实 MCP 回归：

| 阶段 | 数据集 | snapshot_id | 结果 |
|---|---|---|---|
| D1 | `store_overview` | `s_c20111b2a5ca4e1f9d288977fb18cff0` | 1/1 COMPLETE，MCP 读回 / latest / 重启 / 幂等 PASS |
| D2 | `product_catalog` | `s_e2b190eed58e4acbaabb60e26675bbe4` | 3/3 COMPLETE，MCP 读回 / latest / 重启 / 幂等 PASS |
| D3 | `inventory` | `s_e6f49788f358453bad824dc75691a4ff` | 3/3 COMPLETE，MCP 读回 / latest / 重启 / 幂等 PASS |

D1/D2/D3 原有验收快照和报告均保留，未覆盖或删除。

## 8. 存储、恢复与被排除的探针

- YESTERDAY v3 纠正验收后完整快照扫描：31 indexed / 0 invalid。
- 语义状态：1 semantically invalidated / 0 invalidation-state unknown / 0 invalid invalidation records。
- 追加失效后索引已重建并复核；v2 exact-scope latest 不再返回旧错标快照。
- 无中断请求、无隔离快照、无自动删除。

31 份是当前 D 数据根中的全部结构有效提交，包含 D1-D3 基线/回归、开发期诊断快照、既有 D4 快照和本次 4 份 v3 快照，不等于正式 D4 有效样本数。语义失效不会删除结构完整的历史快照，因此旧 v2 仍计入结构扫描数量，但不计入 latest 或有效验收样本。

开发期曾产生 `s_bb4e06e45c8c48aa8bdd771ab228ddcd`：它把商品列表页面范围汇总误标为账户全部。该提交结构完整但业务语义不被接受，明确标记为 `REJECTED_SEMANTIC_SMOKE / NOT_ACCEPTED`，不计入任何 D4 PASS。历史不可变原则下没有删除或手改；当前正确账户适配器使用 `d4-account-v3` exact scope，不会命中该诊断快照。商品列表的 `sumReportInfo` 现在只用于响应形状校验，不发布为账户概况。

其余开发诊断快照也全部排除在正式三次计数之外；两次失败的账户证据探针在提交前安全失败，没有产生快照。

## 9. 自动化与构建结果

| 分类 | 通过 | 失败 | 跳过/排除 | 未执行 |
|---|---:|---:|---:|---:|
| pytest 全套（当前树） | 435 | 0 | 0 | 0 |
| D4 时间/失效/MCP 兼容定向专项 | 186 | 0 | 0 | 0 |
| YESTERDAY 纠正客户端 helper 专项 | 28 | 0 | 0 | 0 |
| Ruff lint（全仓） | 1 | 0 | 0 | 0 |
| mypy 严格模式（40 个源码文件） | 1 | 0 | 0 | 0 |
| 已检入 Schema 与运行时导出一致性 | 19 | 0 | 0 | 0 |
| sdist / wheel 构建 | 2 | 0 | 0 | 0 |
| D4 既有正式真实 MCP 客户端（9 份有效基线；旧 YESTERDAY 结论已撤回） | 1 | 0 | 0 | 0 |
| YESTERDAY v3 纠正真实 MCP 客户端 | 1 | 0 | 0 | 0 |
| D1/D2/D3 真实 MCP 回归 | 3 | 0 | 0 | 0 |
| verify / rebuild / re-verify | 3 | 0 | 0 | 0 |
| Windows 专用 Chrome 实机验收 | 1 | 0 | 0 | 0 |
| `uv lock --check` | 0 | 0 | 0 | 1 |

`uv lock --check=NOT_RUN`：本机未找到 `uv.exe`，本轮没有安装管理员级或系统组件；现有 `uv.lock` 保留，运行环境的固定依赖版本由构建、导入和测试验证。

最终构建：

- `dist/pdd_data_mcp-0.1.0-py3-none-any.whl` — D4 验收当时的历史 SHA-256 为 `810b299b82770554966f4f5f12d6ae78929e640566f2cec09f2112c0d510679b`；后续 post-D4 收口会重建同名产物，当前哈希按对应后续验收报告中的命令即时核验
- `dist/pdd_data_mcp-0.1.0.tar.gz` — 已成功构建。验收报告本身包含在 sdist 中，因此不在报告正文写入会改变自身的递归 SHA-256；可用下方命令对交付文件即时核验。

```powershell
Get-FileHash '.\dist\pdd_data_mcp-0.1.0-py3-none-any.whl' -Algorithm SHA256
Get-FileHash '.\dist\pdd_data_mcp-0.1.0.tar.gz' -Algorithm SHA256
```

独立只读复审最终结论：P0=0、P1=0；半开时间边界、小时 `0..endDayHour`、旧 v2 append-only 失效、ACTIVE MCP 向后兼容、索引 fail-closed 和客户端假 PASS 风险均已加入回归。实际失效应用后再次确认 v2 latest=`NOT_FOUND`，read/list=`SEMANTICALLY_INVALIDATED`。

## 10. 可复制的 Windows PowerShell 命令

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pdd_data_mcp.cli doctor --config config\config.stage-d.local.toml
.\.venv\Scripts\python.exe -m pdd_data_mcp.cli serve --config config\config.stage-d.local.toml
```

正式只读 MCP 验收（每次必须使用新的幂等前缀）：

```powershell
.\.venv\Scripts\python.exe examples\promotion_sync_client.py `
  --config config\config.stage-d.local.toml `
  --connection-id conn_pdd_current_01 `
  --store-id st_pdd_current_01 `
  --captures-per-dataset 3 `
  --idempotency-prefix d4-acceptance-YYYYMMDD-unique `
  --limit 50 `
  --wait-seconds 60 `
  --confirm-read-only
```

旧 YESTERDAY v2 错标快照的追加失效命令如下。本次已经执行；命令要求明确的 snapshot、原因、替代 scope 和确认标志，不会修改原 manifest/COMMIT：

```powershell
.\.venv\Scripts\python.exe -m pdd_data_mcp.cli invalidate-snapshot `
  --config config\config.stage-d.local.toml `
  --snapshot-id s_28300358ab6c4a76aa640362c20024d6 `
  --reason-code METRIC_WINDOW_END_MISLABELED `
  --replacement-scope-version d4-account-v3 `
  --confirm-semantic-invalidation
```

YESTERDAY v3 纠正验收（必须传入旧 v2 snapshot ID，防止跳过审计检查）：

```powershell
.\.venv\Scripts\python.exe examples\promotion_yesterday_correction_client.py `
  --config config\config.stage-d.local.toml `
  --connection-id conn_pdd_current_01 `
  --store-id st_pdd_current_01 `
  --idempotency-prefix d4-yesterday-v3-YYYYMMDD-unique `
  --legacy-v2-snapshot-id s_28300358ab6c4a76aa640362c20024d6 `
  --wait-seconds 60 `
  --confirm-read-only
```

离线门禁与存储验证：

```powershell
.\.venv\Scripts\python.exe -m pdd_data_mcp.cli export-schemas --output schemas
.\.venv\Scripts\python.exe -m ruff format --check src tests examples
.\.venv\Scripts\python.exe -m ruff check src tests examples
.\.venv\Scripts\python.exe -m mypy src\pdd_data_mcp
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe -m pdd_data_mcp.cli verify-storage --config config\config.stage-d.local.toml
.\.venv\Scripts\python.exe -m pdd_data_mcp.cli rebuild-index --config config\config.stage-d.local.toml
```

## 11. Chrome 保留与安全计数

验收后专用 Chrome 仍为 1 个 Context、5 个既有标签页：商品列表、推广概况、商家首页、商品推广、店铺信息。没有关闭或新建用户页面。

- 真实拼多多读取：**RUN（仅上述 D4 只读白名单）**
- REAL_MODEL_CALLS：**0**
- REAL_PLATFORM_WRITE_ACTIONS：**0**
- ExecutionJob：**0**
- 预算、ROI 目标、出价、商品、价格、库存或活动修改：**0**
- 私有请求重放：**0**
- Token / Cookie / Authorization 读取或导出：**0**
- 用户 Chrome / Context / 页面关闭：**0**
- 旧系统默认数据路线修改：**0**

## 12. 未启用与停止条件

以下项目如实保持未启用或未核实，不计为已完成能力：

- `campaign_metrics` 计划粒度效果；
- 账户级或计划级当前投放配置；
- 推广商品 YESTERDAY、7/30/90 日及任意自定义历史窗口；
- 商品推广列表汇总作为账户全量指标；
- 活动目录/报名、交易/售后/结算、外部成本；
- AI 分析、ExecutionJob、平台业务写操作；
- D5 旧主软件正式接入。

Stage D4 的已核实只读范围验收完成后已停止，没有自动进入 D5。
