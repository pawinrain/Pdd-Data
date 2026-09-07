# PDD Data MCP — Stage D1 / D2 / D3 验收报告

- 验收日期：2026-09-07
- 环境：Windows 10 `10.0.19045` / Python 3.12.14 / Asia/Shanghai
- 范围：真实店铺概况、商品档案、库存；READ ONLY
- Stage A：**PASS**
- Stage B：**PASS**
- Stage C：**PASS**
- Stage D1：**PASS**
- Stage D2：**PASS**
- Stage D3：**PASS**
- Stage D4 / D5：**NOT_RUN**

## A. Current Store Identity

用户明确确认当前专用 Chrome 登录的店铺就是本轮目标店铺。程序重新执行了身份门禁，没有把旧内部 `store_id` 当作证据：

- 商家后台店铺基础信息页的精确只读身份响应成功；
- 响应中的平台店铺 ID 与同页页面状态中的 ID 做 SHA-256 指纹比对，一致；
- 商品白名单响应的每一行 `mall_id` 再与同一预期指纹核对，一致；
- 首页、商品列表和店铺信息页位于同一专用 Chrome Context；
- 本轮新建内部 ID `st_pdd_current_01`，未复用 Stage C 的内部 `st_real_001`；
- 真实平台 ID 未输出到日志或本报告。

本轮指纹与 Stage C 验证时的指纹相同，但不能据此推断店铺：用户已明确确认当前店铺，平台商品总数又独立显示为 3。本轮始终使用新的内部数据分区，没有读取或复制旧快照。

`CURRENT_STORE_IDENTITY = VERIFIED`。

## B. Store Overview

三次独立真实采集均通过 MCP stdio 子进程完成。首页通用网络网关只有不透明 `type`，响应无法与具体经营指标形成可证明的一一映射，因此没有猜测字段；按阶段规则使用已验证 DOM 备用读取。

每次快照保存 4 个非缺失实时指标、字段级来源、单位、精度、`captured_at` 和平台明确显示的 `source_updated_at`。TODAY 窗口为 Asia/Shanghai 当日 00:00 至平台更新时间，`window_complete=false`、`source_finalized=false`，没有伪装成全天完成数据。

## C. Store Stable Fields

真实数值不在报告中输出。

| 字段 | 含义 | 单位 | 来源 | 精度 | 平台更新时间 |
|---|---|---|---|---|---|
| `gmv` | 成交金额 | `CNY_CENT` | DOM | EXACT | 已记录 |
| `order_count` | 成交订单数 | `COUNT` | DOM | EXACT | 已记录 |
| `visitor_count` | 商品访客数 | `COUNT` | DOM | EXACT | 已记录 |
| `page_view_count` | 商品浏览量 | `COUNT` | DOM | EXACT | 已记录 |

最低要求是 2 个字段；实际稳定读取 4 个。三次均为 `capture_method=DOM`，未虚报为网络来源。

## D. Product Catalog

商品档案来自精确白名单响应：

- host：`mms.pinduoduo.com`
- path：`/vodka/v2/mms/query/display/mall/goodsList`（无查询参数）
- method：`POST`
- purpose：当前店铺商品列表和平台总数
- parser：`pdd-product-list/1.0.0`
- identity：每行 `mall_id` 指纹 + 店铺基础信息页指纹

保存字段为平台商品 ID（落盘为字符串）、商品名、上下架状态和 SKU 数。商品名只作为业务数据，不参与路径、权限或执行。价格、创建和发布时间因本轮未完成足够的单位/时间语义证明而没有保存或推导。

三次真实结果均为：平台 3、读取 3、去重后 3，没有混入推荐商品、旧响应或其他店铺行。

## E. User Expected Product Count

`USER_EXPECTED_PRODUCTS = 3`。

## F. Platform Observed Product Count

`PLATFORM_OBSERVED_PRODUCTS = 3`。数值来自白名单响应 `result.total`，并由商品页 DOM “共有 3 条”独立核对；没有硬编码。

## G. Product Captured Count

三次分别为 `3 / 3 / 3`。每次三个平台商品 ID 均为字符串且唯一；真实 ID 和商品名不在报告中输出。

## H. Product Coverage

三次均为：

```text
total_observed = 3
captured = 3
coverage = COMPLETE
truncated = false
stop_reason = null
```

## I. Pagination

真实小店每次 `pages_read=1`，这是平台总数 3 的正常结果。代码没有把分页写死为一页；合成回归仍验证 `55 total / 50 captured / 6 pages / TRUNCATED`。已修正 3/2 场景，使其明确为 `PARTIAL` 且不是 `TRUNCATED`。

## J. Inventory

库存使用独立 `inventory` JSONL 快照，不写入商品档案。数据来自与商品列表相同的精确白名单响应，但使用独立解析器 `pdd-product-inventory/1.0.0` 和独立提交。

- 本轮可证明的粒度：`PRODUCT`；没有虚构 SKU 库存；
- 三次均为平台商品 3、库存记录 3、`COMPLETE`；
- 商品与库存的平台商品 ID 集合三次均完全一致；
- 本轮 3 条库存均为非缺失值；真实库存数字不输出；
- 真实 0 与缺失 null 的区分由严格解析和回归测试覆盖，缺失不会替换为 0。

## K. Network Response Fields

Product Catalog 网络字段：`success`、`result.total`、`result.goods_list[]` 下的 `id`、`mall_id`、`goods_name`、`is_onsale`、`sku_count`。

Inventory 网络字段：`success`、`result.total`、`result.goods_list[]` 下的 `id`、`mall_id`、`quantity`。

发现阶段先只记录经清理的 host/path/method/status/content-type；确认候选业务用途后才在内存读取精确响应正文。没有保存 raw body、HAR、请求头、Cookie、Token 或 Authorization，没有重放或自行构造私有请求。

## L. DOM Fallback Fields

- Store：4 个经营指标和“实时数据更新时间”，`capture_method=DOM`；
- Product / Inventory：DOM 只核对平台总数，业务记录仍为 `NETWORK_RESPONSE`；
- 未使用 `MIXED` 冒充网络采集；所有字段来源如实写入 manifest。

## M. Local Snapshots

本轮生成 9 个有效快照，`verify-storage = 9 valid / 0 invalid`，`rebuild-index = 9 indexed / 0 invalid`。每个目录均有 `manifest.json`、`validation.json`、`COMMIT.json` 和 `data.json` 或 `records.jsonl`。

| 数据集 | snapshot_id | 相对目录 | 结果 |
|---|---|---|---|
| Store | `s_ed561d2b51a24db39b2e2ba35e815dcf` | `snapshots/pdd/st_pdd_current_01/store_overview/2026/09/07/022108_873_s_ed561d2b51a24db39b2e2ba35e815dcf` | 1/1 COMPLETE |
| Store | `s_ec438b2bb0ff4b0f82b9362920ba04f2` | `snapshots/pdd/st_pdd_current_01/store_overview/2026/09/07/022550_619_s_ec438b2bb0ff4b0f82b9362920ba04f2` | 1/1 COMPLETE |
| Store | `s_85074a76723e49f193a9789614c3ac83` | `snapshots/pdd/st_pdd_current_01/store_overview/2026/09/07/022930_671_s_85074a76723e49f193a9789614c3ac83` | 1/1 COMPLETE |
| Product | `s_42bcc96e977540fca253396adabd4a54` | `snapshots/pdd/st_pdd_current_01/product_catalog/2026/09/07/022243_483_s_42bcc96e977540fca253396adabd4a54` | 3/3 COMPLETE |
| Product | `s_4f37ed5e36ee447fb1a516584b11f2ab` | `snapshots/pdd/st_pdd_current_01/product_catalog/2026/09/07/022654_791_s_4f37ed5e36ee447fb1a516584b11f2ab` | 3/3 COMPLETE |
| Product | `s_4882de3f9c934ed89b99f2d9f86696d5` | `snapshots/pdd/st_pdd_current_01/product_catalog/2026/09/07/023034_886_s_4882de3f9c934ed89b99f2d9f86696d5` | 3/3 COMPLETE |
| Inventory | `s_c58aeb51102d4ba093d6cfba028b21a8` | `snapshots/pdd/st_pdd_current_01/inventory/2026/09/07/022403_514_s_c58aeb51102d4ba093d6cfba028b21a8` | 3/3 COMPLETE |
| Inventory | `s_d34675434d074e94b9994a6a15f14957` | `snapshots/pdd/st_pdd_current_01/inventory/2026/09/07/022757_624_s_d34675434d074e94b9994a6a15f14957` | 3/3 COMPLETE |
| Inventory | `s_4ed68eb7544d4ed9a60b6fa3f04a8c55` | `snapshots/pdd/st_pdd_current_01/inventory/2026/09/07/023137_104_s_4ed68eb7544d4ed9a60b6fa3f04a8c55` | 3/3 COMPLETE |

后两批 Core Sync 分别共享一个 `batch_id`，但三个数据集仍是独立快照和独立提交；不承诺跨数据集原子提交。

## N. MCP Readback

实际客户端使用官方 MCP SDK 启动 stdio Server：

- 发现工具数：6；
- 单数据集采集、提交和读取：PASS；
- Core Sync 两轮：SUCCESS；
- 每类 3 个历史快照用 `limit=2` 分 2 页读出，无重复游标结果；
- `pdd_get_latest_snapshot(require_complete=true)` 对三类均 FOUND；
- 最新快照再用 `pdd_read_snapshot` 读取，ID 一致。

## O. Cross Store Isolation

- 真实 D 数据只在 `st_pdd_current_01` 分区；Stage C 的 `st_real_001` 快照没有复制或参与 D 查询；
- 当前真实配置只授权新的内部店铺 ID；
- 自动化测试使用 `st_old_store` 和 `st_current_store` 验证同数据集、同幂等业务键也生成不同快照，列表和 latest 不串店。

## P. Service Restart

两轮 Core Sync 都在采集后停止 MCP 服务，随后启动新服务实例。新实例分别从 6 个和 9 个已提交快照恢复索引，并读回该批 Store、Product、Inventory 的同一 snapshot ID。最终历史客户端再次从 9 个快照恢复并读回三个 latest。

## Q. Idempotency

每次独立真实采集使用新幂等键。两轮 Core Sync 重启后分别用三个原键重放，全部返回原 snapshot ID 且 `idempotent_replay=true`；快照总数保持 9，没有把 replay 计入 3 次稳定性采集。

## R. Chrome Preserved

最终核对结果：专用 Chrome 仍运行，一个 Context 中的推广概况、店铺信息、首页、商品列表四个标签页仍存在，无重复目标页。服务只断开自身 Playwright CDP 传输，没有关闭 Browser、Context 或页面。

## S. Tests

Stage D 专项 14 项包含在总套件 69 项中，不重复相加。本机 CDP 专项 1 项也包含在总数中。

| 分类 | 通过 | 失败 | 跳过/排除 | 未执行 |
|---|---:|---:|---:|---:|
| pytest 总套件 | 69 | 0 | 0 | 0 |
| A/B/C 回归子集 | 55 | 0 | 0 | 0 |
| Stage D 专项子集 | 14 | 0 | 0 | 0 |
| 受控本机 CDP + MCP | 1 | 0 | 68（专项命令排除） | 0 |
| Ruff 格式 / 检查 | 2 | 0 | 0 | 0 |
| mypy | 1 | 0 | 0 | 0 |
| Schema 导出 | 13 | 0 | 0 | 0 |
| Python sdist / wheel 构建 | 2 | 0 | 0 | 0 |
| 真实仓库 verify / rebuild | 2 | 0 | 0 | 0 |
| Windows 实机真实 Chrome 验收 | 1 | 0 | 0 | 0 |
| `uv lock --check` | 0 | 0 | 0 | 1 |

`uv lock --check = NOT_RUN`：当前 PATH、项目 `.venv/Scripts`、用户常见 `.local/bin` 与 `.cargo/bin` 均未找到 `uv.exe`；没有安装管理员级或系统组件，依赖没有变更。

最终摘要：

```text
ruff format --check src tests examples
  64 files already formatted
ruff check src tests examples
  All checks passed!
mypy src
  Success: no issues found in 35 source files
pytest -q
  69 passed in 10.52s
pytest <A/B/C files> -q
  55 passed in 10.58s
pytest <Stage D files> -q
  14 passed in 0.23s
pytest -m local_cdp -q
  1 passed, 68 deselected in 6.21s
python -m build
  Successfully built sdist and wheel
verify-storage
  PASS, 9 valid, 0 invalid
rebuild-index
  9 indexed, 0 invalid
```

验收开发过程中曾发现并修复两个问题：已知总数 3 但只读 2 时错误要求标记 TRUNCATED，以及历史示例把 latest 的嵌套结果当顶层字段读取。修复后新增回归并通过最终门禁；最终未解决失败数为 0。

最终 Wheel：`dist/pdd_data_mcp-0.1.0-py3-none-any.whl`；SHA-256：`B521B01C9D46A6F460D6DA1C890E1D5BDD24E9BB0A51C07E1F264146819B927A`。

## T. Security

- REAL_MODEL_CALLS：**0**
- REAL_PLATFORM_WRITE_ACTIONS：**0**
- ExecutionJob：**0**
- 商品、库存、价格、上下架修改：**0**
- 广告、ROI、预算或活动修改：**0**
- 自动登录或验证码处理：**0**
- Token / Cookie / Authorization 读取或导出：**0**
- 私有请求重放或页面 fetch 构造：**0**
- raw response / HAR 落盘：**0**
- 真实店铺 ID、商品 ID、商品名、经营金额公开输出：**0**
- 用户 Chrome 关闭：**0**

## U. PASS / FAIL

- D1 Store Overview：**PASS**
- D2 Product Catalog：**PASS**
- D3 Inventory：**PASS**
- Stage D1/D2/D3 整体：**PASS**
- D4 Promotion 扩展：**NOT_RUN**
- D5 主软件接入：**NOT_RUN**

所有 PASS 均来自真实 MCP Client → stdio Server 路径；没有直接 import Collector 冒充最终验收。

## V. Remaining Unverified Fields

以下字段或能力没有足够证据，本轮保持未实现或未验证：

- Store Overview 的稳定网络字段映射（当前如实使用 DOM）；
- 商品价格的真实单位交叉验证；
- 商品 created / published 时间语义；
- SKU 粒度库存（当前只证明 PRODUCT 粒度）；
- D4 推广扩展字段；
- D5 旧主软件正式接入；
- AI 分析、定时任务或任何真实执行。

Stage D1/D2/D3 完成后已停止，没有进入 D4/D5。
