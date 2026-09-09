# PDD Data MCP 架构基线

> 本文档保存用户提供的《PDD Data MCP 架构设计书 V1.0》中与当前工程直接相关的架构基线。阶段 A/B/C 与 Stage D1/D2/D3/D4 的既有基线已完成；D4 账户 TODAY 保留三次既有重复真实验收并完成一次 v3 回归，纠正后的 YESTERDAY v3 部分日窗口完成三次重复真实验收。旧 v2 YESTERDAY 错标快照保留原文件并追加语义失效记录。未编号的 post-D4 商品整体经营只读核实已取得真实页面、端点和响应形状证据，但因数值格式、单位及 Ytd 时间语义不能安全确认而没有启用正式能力。Stage D5 主软件正式接入仍为 `NOT_RUN`。

## 1. 系统边界

`pdd-data-mcp` 是独立 Python 进程，通过本地 stdio MCP 与现有主软件集成。它负责采集接口、数据契约、校验、本地快照持久化和查询，不负责模型调用、经营决策、审批、广告或商品修改。

阶段 C 至 D4 使用 Playwright Python 异步 API 连接可信配置中的本机回环 CDP；当前商品整体经营扩展继续遵守同一技术和安全边界。采集按精确白名单监听目标页面响应，并在各适配器要求的位置使用 DOM 独立核对店铺、业务日期、截止时间或页面总数。服务只断开自身 CDP 传输，不关闭用户 Chrome、Context 或标签页。未知接口的发现只记录脱敏元数据，不读或保存正文；适配器经人工核实前保持 `NOT_VERIFIED`。

## 2. 模块与依赖方向

```text
MCP server / CLI
        |
application service
        |
collector + validator + repository protocols
        |
contracts / parsers / security / local-file storage
```

- MCP 层只负责协议、参数 Schema、工具权限提示和受控错误。
- Application 层编排连接授权、幂等、采集、校验、提交和查询。
- Collector、Validator 和 Repository 用 Protocol 隔离；提供 `SyntheticCollector`、`CoreDataCdpCollector`、限域的 `PromotionOverviewCdpCollector`、`PromotionAccountCdpCollector`、`PromotionMetricsCdpCollector` 与 `LocalFileSnapshotRepository`。
- Parser 不依赖浏览器或磁盘，Storage 不依赖业务页面。
- Browser 包负责 CDP 生命周期、目标页选择、响应白名单、严格解析和 DOM 核对；实际接口路径、字段和选择器只来自经人工核实的可信本地配置。
- `product_business_metrics` 当前只有用于 fail-closed 核实的枚举、候选合约、候选 parser 和导出 Schema；它们不构成正式 snapshot payload。Application 在连接浏览器前拒绝该数据集，真实 dispatcher、collector 和 Validator 均未接入，因此候选数据不能提交到 Repository。

## 3. 数据契约

当前可测试契约：

- `store_overview`：店铺概况对象，保存为 `data.json`。
- `product_catalog`：商品/SKU 档案，保存为 `records.jsonl`。
- `inventory`：商品或 SKU 粒度库存，保存为 `records.jsonl`。
- `promotion_overview`：推广账户效果对象，保存为 `data.json`；Stage D4 账户契约支持 TODAY 和 YESTERDAY。
- `product_metrics`：推广商品 TODAY 效果，保存为 `records.jsonl`。
- `promotion_configuration`：推广商品当前配置观察，保存为 `records.jsonl`，使用 `POINT_IN_TIME`。

`campaign_metrics` 仍为 `UNAVAILABLE`。推广商品记录中的计划标识只用于经响应验证的实体关联，不构成计划粒度效果或投放配置；不把账户或推广商品指标拆成不存在的计划数据。`activity_catalog` 等未验收数据集仍保持不可用。当前投放配置作为时点观察与历史效果分开建模，不从当前配置推断历史设置，也不从缺失记录推断下架或删除。

商品整体经营与流量不能复用 `product_metrics`：该名称已由推广商品粒度的既有 Schema、capability、适配器和历史快照占用。`product_business_metrics` 已作为独立枚举注册，但 capability 明确为 `UNAVAILABLE`，支持窗口为空且 `verified=false`。候选合约/parser/Schema 只表达已观察响应的边界和未验证标记，不代表字段可读、可落盘或完成适配。只有字段数值格式、单位、实体 ID、日期范围、逐日/汇总形态、分页总数和来源分类全部核实后，才能设计并验收正式快照 Schema。

数值规则：金额使用 Decimal 解析为整数分；比率使用十进制字符串；业务 ID 是字符串；禁止 NaN/Infinity；缺失是 `null` 而不是 0；近似数字必须保存精度标记。

## 4. 时间和 scope

快照分开保存 `requested_at`、`capture_started_at`、`capture_finished_at`、`captured_at`、`metric_window.start/end`、`source_updated_at` 和 `committed_at`。

正文使用带时区 ISO 8601；目录默认按 `Asia/Shanghai` 的 `captured_at` 分区。统计窗口统一使用 `[start,end)`。Stage D4 账户 TODAY 以平台响应更新时间作为截止时间；最新 v3 回归为 `[2026-09-08 00:00, 2026-09-08 12:53:56)`，`source_updated_at` 与 end 相同。三次账户 YESTERDAY v3 验收均核对请求/响应业务日期 `2026-09-07`、`endDayHour=12`、小时行 `0..12` 共 13 行及页面昨日截止 `12:59`，由此得到 `[2026-09-07 00:00, 2026-09-07 13:00)`。TODAY 与 YESTERDAY 都保留 `window_complete=false`、`source_finalized=false`；YESTERDAY 的 `source_updated_at=null`，且该范围只是昨日部分日/同期窗口，不是完整自然日或最终值。推广商品效果当前只支持 TODAY，平台更新时间必须位于业务日午夜和观察截止时间之间。库存、商品档案和推广配置当前值使用时点语义。

采集来源按字段保存在 `field_sources`：账户 TODAY 的效果和窗口截止来自网络响应；账户 YESTERDAY 的效果来自网络响应，`endDayHour` 与小时覆盖来自同一白名单响应，页面 DOM 再独立核对 `HH:59` 截止，因此整体为 `MIXED` 且窗口 end 标记为 DOM 来源。推广商品效果和当前配置来自网络响应。响应没有提供的平台更新时间保持 `null` 并保存精确缺失原因，不用观察时间或 0 替代。

`scope_key` 由业务日期、窗口类型、对象、筛选、币种、归因和版本等规范化数据计算，不包含每次变化的 limit 或采集截止时间。“最新”查询始终要求店铺、数据类型和精确 scope。

商品整体经营页当前响应请求和行日期只能证明页面在观察时加载了 `2026-09-08` 数据。响应中的 `Ytd` 后缀保持 `UNVERIFIED_COMPARISON_SUFFIX_ONLY`；`readyDate=2026-09-07` 只是 ready-through 候选；响应 `timestamp` 只作观察时刻候选。三者都不建立 YESTERDAY 完整自然日窗口、来源更新时间或最终确定状态。页面无可验证的日期切换控件和最近 7 日逐日证据，因此该数据集没有 metric window、latest scope 或正式快照。

## 5. 本地存储

```text
data-root/
  _meta/storage.json
  _locks/
  _requests/
  _runs/YYYY/MM/DD/
  _tmp/
  _quarantine/
  _indexes/pdd/<store_id>/
  _invalidations/<snapshot_id>/i_<uuid>.json
  _pointers/pdd/<store_id>/
  snapshots/pdd/<store_id>/<dataset_type>/YYYY/MM/DD/
    HHMMSS_mmm_s_<uuid>/
      manifest.json
      data.json | records.jsonl
      validation.json
      COMMIT.json
  audit/YYYY/MM/DD/events.jsonl
```

`data_root` 由本地受信配置给出，相对路径相对配置文件解析。`runtime_root` 与 `data_root` 不得相同或嵌套。内部 ID 用于路径，不使用店铺名、商品名或个人信息。

每次采集是新快照，完整 UUID 保证同毫秒不冲突。JSONL 每行是一个完整 UTF-8 JSON 对象。不使用 pickle，不用 CSV/Excel 作为主仓库。

## 6. 安全提交和恢复

1. 持久化幂等请求与规范化参数摘要，分配 `snapshot_id`。
2. 在同一文件系统 `_tmp/<snapshot_id>/` 写正文和 validation。
3. flush/fsync，计算正文和 validation SHA-256，写 manifest 并复读 Schema。
4. 原子移到唯一正式目录。
5. 最后安全发布带 manifest SHA-256 的 `COMMIT.json`。
6. 更新请求记录、索引和指针，再向调用方返回。

读取只接受存在有效 COMMIT、manifest 和匹配文件摘要的快照。启动时隔离 `_tmp` 遗留和损坏/缺失 COMMIT 批次，从已提交目录重建索引；有 COMMIT 但请求仍是 RUNNING 时修复为 COMMITTED，无 COMMIT 的 RUNNING 改为 INTERRUPTED，不自动重访平台。

已提交快照的语义错误不通过删除或重写历史修复，而是在 `_invalidations` 下追加严格绑定 snapshot/store/dataset/旧 scope/旧 parser 的失效记录。有效失效记录使该快照不再参与 latest；list/read 继续返回 `SEMANTICALLY_INVALIDATED` 和失效证据供审计。损坏、重复或与 manifest 不匹配的失效状态按 unknown fail-closed，不得把旧快照重新视为 latest。旧 v2 快照 `s_28300358ab6c4a76aa640362c20024d6` 的原 manifest/COMMIT 未改，追加记录为 `i_0454ea48bc564be2980d27e1561b38bc`。

同一 `data_root` 使用真实跨进程文件锁，只允许一个服务写。幂等定位键是 `store_id + dataset_type + idempotency_key`，同键同参数返回原状态/快照，异参数冲突，进行中返回 BUSY。

## 7. 覆盖率、索引和查询

coverage 保存 `total_observed`、`captured`、`limit`、`pages_read`、`coverage`、`truncated`、`stop_reason`。来源 55 条而 limit 50 时，快照可提交，但业务状态是 PARTIAL、coverage 是 TRUNCATED。未知总数是 `null`，不是 0。

索引可从已提交快照重建。指针区分 `last_attempt`、`latest_usable`、`latest_complete`，失败不覆盖最近有效快照。列表和记录查询使用受限页大小、查询绑定且带校验和的游标；每次通过 snapshot ID 读取仍核对配置中的店铺授权。

## 8. 安全和运维限制

- MCP 参数不接受文件路径、任意 URL、CDP 地址、请求头或脚本。
- 所有路径都使用组件校验、`resolve` 和根目录归属检查，拒绝绝对路径、穿越和根外链接。
- 敏感字段使用白名单与禁止键校验，日志不写正文、密钥或完整请求。
- 网页/商品文本始终是不可信数据，不是可执行指令。
- 本地 JSON/JSONL 是明文；SHA-256 用于损坏检测，不宣称为签名、加密或多租户隔离。
- 配额或最低剩余空间不满足时停止新增，不自动删除历史。布局版本/分区时区不符时停止，不静默迁移。

## 9. 当前与后续阶段边界

Stage D4 已完成的推广基线是账户 TODAY、账户 YESTERDAY 部分日窗口、推广商品 TODAY 效果和推广商品当前配置观察。账户 TODAY、推广商品 TODAY 和当前配置各保留三次既有重复真实验收；账户 TODAY 的共享日期逻辑另完成一次 v3 回归。账户 YESTERDAY v3 完成三次重复真实验收，已验收范围是 `[2026-09-07 00:00, 2026-09-07 13:00)`，明确不能称为完整昨日或最终值。各适配器均保留专用浏览器归属与回环监听、当前店铺身份、精确只读响应、字段时间/单位/归因、分页、实体关联、DOM 核对和平台合规门禁。后台未验证的更长时间窗口保持不可用；计划标识仅作为关联键，`campaign_metrics` 没有通过验收。

Stage D5 只指主软件正式接入，当前状态为 `NOT_RUN`；它不自动包含新的平台数据采集范围。商品整体经营与流量的独立、未编号 post-D4 只读核实已经安全收口：页面与响应形状证据通过，最新 D2 目录关联为 3/3，但 12 个 base+Ytd 字段的已观察值均为 Private Use 字符且安全可解析数为 0，Ytd、支付单位、来源分类和 7 日范围未获证明。正式 capability 保持 `UNAVAILABLE`，正式三次 MCP 验收为 `NOT_RUN`。活动目录及报名状态、SKU 价格库存、交易售后结算和外部成本输入仍是未获本轮授权的独立后续候选能力。

定时调度、AI/模型调用、ExecutionJob、预算/投产目标/商品/价格/库存/活动修改以及任何其他平台写操作都不在当前只读服务边界内。D4 YESTERDAY 口径补核与 v3 重复真实验收已经完成；`product_business_metrics` 因证据门禁未满足而停止在候选层，不得写成可读能力。当前工作到此停止，不启动 Stage D5 或其他候选能力。
