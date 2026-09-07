# PDD Data MCP 架构基线

> 本文档保存用户提供的《PDD Data MCP 架构设计书 V1.0》中与当前工程直接相关的架构基线。阶段 A/B 已验收；阶段 C 仅增量接入推广概况 TODAY 广告消耗，其真实平台链路在用户确认前保持未运行。其他浏览器数据类型仍是后续边界。

## 1. 系统边界

`pdd-data-mcp` 是独立 Python 进程，通过本地 stdio MCP 与现有主软件集成。它负责采集接口、数据契约、校验、本地快照持久化和查询，不负责模型调用、经营决策、审批、广告或商品修改。

阶段 C 使用 Playwright Python 异步 API连接可信配置中的本机回环 CDP，按精确白名单监听目标页面响应，并用 DOM 独立核对店铺、业务日期和广告消耗。服务只断开自身 CDP 传输，不关闭用户 Chrome、Context 或标签页。未知接口的发现只记录脱敏元数据，不读或保存正文；适配器经人工核实前保持 `NOT_VERIFIED`。

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
- Collector、Validator 和 Repository 用 Protocol 隔离；提供 `SyntheticCollector`、限域的 `PromotionOverviewCdpCollector` 与 `LocalFileSnapshotRepository`。
- Parser 不依赖浏览器或磁盘，Storage 不依赖业务页面。
- Browser 包负责 CDP 生命周期、目标页选择、响应白名单、严格解析和 DOM 核对；实际接口路径、字段和选择器只来自经人工核实的可信本地配置。

## 3. 数据契约

当前可测试契约：

- `store_overview`：店铺概况对象，保存为 `data.json`。
- `product_catalog`：商品/SKU 档案，保存为 `records.jsonl`。
- `inventory`：商品或 SKU 粒度库存，保存为 `records.jsonl`。
- `promotion_overview`：推广账户概况对象，保存为 `data.json`。

`product_metrics`、`campaign_metrics`、`activity_catalog` 只预留枚举和不可用状态。不把账户累计消耗拆成不存在的计划数据，不从缺失记录推断下架或删除。

数值规则：金额使用 Decimal 解析为整数分；比率使用十进制字符串；业务 ID 是字符串；禁止 NaN/Infinity；缺失是 `null` 而不是 0；近似数字必须保存精度标记。

## 4. 时间和 scope

快照分开保存 `requested_at`、`capture_started_at`、`capture_finished_at`、`captured_at`、`metric_window.start/end`、`source_updated_at` 和 `committed_at`。

正文使用带时区 ISO 8601；目录默认按 `Asia/Shanghai` 的 `captured_at` 分区。统计窗口是 `[start,end)`，TODAY 的 end 是本次观察时间，不伪装为完整一天。库存和商品当前值使用时点语义。

`scope_key` 由业务日期、窗口类型、对象、筛选、币种、归因和版本等规范化数据计算，不包含每次变化的 limit 或采集截止时间。“最新”查询始终要求店铺、数据类型和精确 scope。

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

## 9. 后续阶段边界

阶段 C 真实验收仍必须另行验证：专用浏览器归属与回环监听、授权页面的店铺身份、实际只读响应结构、字段时间/单位/归因、DOM 核对和平台合规边界。未完成这些验证之前，不能用合成数据、仅 DOM 成功或未核对适配器代替真实网络成功。阶段 D、定时调度、主软件切换和任何经营写操作不在当前范围。
