# PDD Data MCP — 未编号 post-D4 商品整体经营只读核实报告

- 验收日期：2026-09-08
- 当前内部店铺：`st_pdd_current_01`
- 范围：D4 YESTERDAY 时间口径补核、D1/D2/D3 必要回归、商品整体经营与流量只读证据核实
- Stage D5 主软件正式接入：**NOT_RUN**

## 1. 结论

| 项目 | 状态 | 说明 |
|---|---|---|
| D4 `d4-account-v3` YESTERDAY 纠错与三次真实 MCP 验收 | **PASS** | 三次独立首次提交及 read/latest/keyset 分页、重启读回、幂等重放均通过 |
| 旧 `d4-account-v2` 错标快照处理 | **PASS** | 原快照和 COMMIT 未改写；追加语义失效记录后不再参与 latest，read/list 仍可审计 |
| D1/D2/D3 本轮必要真实回归 | **PASS** | 三个既有数据集各完成一次真实 MCP 回归；不是三次新的重复采集 |
| 商品页、端点和响应形状证据 | **PASS** | 只证明真实只读连通和候选响应形状，不证明字段值可读 |
| `product_business_metrics` 正式 capability | **NOT_ENABLED / UNAVAILABLE** | 数值格式、单位和 Ytd 时间语义未通过门禁 |
| 商品经营三次独立 MCP 采集及落盘/读回 | **NOT_RUN** | 正式 adapter 未启用，未生成该数据集快照 |
| 最近 7 个已结束自然日逐日数据 | **NOT_RUN / UNAVAILABLE** | 页面没有可验证的日期控件或逐日证据 |
| 流量来源分类 | **NOT_RUN / UNAVAILABLE** | 来源分类和口径没有真实证据 |
| Stage D5 | **NOT_RUN** | 本轮未重新编号、未启动主软件正式接入 |

页面/响应探针的成功不能计作正式 MCP 采集；幂等重放也不能计作独立首次提交。

## 2. D4 时间口径与历史修正

旧 YESTERDAY v2 快照 `s_28300358ab6c4a76aa640362c20024d6` 把页面 `02:59` 误作半开窗口的结束点。小时行实际覆盖 `0..2`，正确半开结束应为 `03:00`。项目没有静默修改原 manifest、正文或 COMMIT，而是追加失效记录 `i_0454ea48bc564be2980d27e1561b38bc`；该快照不再作为有效 latest 返回。

纠正后的三次 `d4-account-v3` YESTERDAY 首次提交为：

- `s_0953d8e810b6441fbdd8fe3ca8a38f56`
- `s_b17f1a1fc6a749feb155c422e92032de`
- `s_e33050b13b5c436aaae56c402336eaa2`

三次均核实业务日 `2026-09-07`、`endDayHour=12`、小时行 `0..12` 共 13 行和页面截止 `12:59`，保存窗口为 `[2026-09-07 00:00, 2026-09-07 13:00)`。它是昨日部分日/同期窗口，`window_complete=false`、`source_finalized=false`、`source_updated_at=null`，不能表述成完整昨日或最终值。另有一次 TODAY v3 共享日期逻辑回归 `s_48a017667a0c4cd891e6a447fcf7071d` 通过。

## 3. D1/D2/D3 必要真实回归

本轮通过真实 stdio MCP 各执行一次：

| 数据集 | snapshot_id | 结果 |
|---|---|---|
| `store_overview` | `s_9d47d8f7f45f4094ac02ced65cdb14b5` | 1/1 COMPLETE；read/latest/重启/幂等 PASS |
| `product_catalog` | `s_b32cc57a34f042ce9c5737fd12053a96` | 3/3 COMPLETE；read/latest/重启/幂等 PASS |
| `inventory` | `s_56eca8603437428d80b239f33c0db754` | 3/3 COMPLETE；read/latest/重启/幂等 PASS |

历史查询以 page size 2 读取，每类现有 5 份快照均分 3 页返回且 ID 唯一。该结果是对既有 D1/D2/D3 重复验收的本轮一次回归，不将它写成三次新采集。

## 4. 商品整体经营真实证据

### 页面、端点与关联

- 页面入口：`/sycm/goods_effect`
- 列表响应：POST `/sydney/api/goodsDataShow/queryGoodsDetailVOListForMMS`
- 数据就绪日期候选：POST `/sydney/api/goodsDataShow/queryGoodsReadyDate`
- 主列表请求：`startDate=2026-09-08`、`endDate=2026-09-08`、`pageNum=1`、`pageSize=10`
- 平台总数 3、读取 3、商品 ID 唯一；行 `statDate` 与请求日期一致
- 响应商品 ID 与最新 D2 `product_catalog` 集合 3/3 匹配；真实 ID、名称和指标值未输出

### 字段与阻断原因

每行都观察到 6 个 base 字段及对应 6 个 `Ytd` 字段：

- `payOrdrUsrCnt`、`payOrdrCnt`
- `payOrdrGoodsQty`、`payOrdrAmt`
- `goodsUv`、`goodsPv`

12/12 字段在三行中均存在，但所有已观察值都包含 Unicode Private Use 字符，安全可解析值为 0。没有反向解析自定义字体、CSS、Canvas、图片、脚本或其他混淆机制，也没有把不可解释值当成 0、金额或计数。

`Ytd` 只被标记为 `UNVERIFIED_COMPARISON_SUFFIX_ONLY`。页面中的静态“昨日”文本及 `readyDate=2026-09-07` 都不足以证明 `Ytd` 的含义；readyDate 仅是 ready-through 候选，不证明完整自然日、来源更新时间或最终确定。页面没有可验证的 TODAY/YESTERDAY 日期切换控件、最近 7 日逐日数据、流量来源分类或支付字段单位。可见 DOM 只对访客和浏览量标签提供有限结构线索，不能据此解释网络字段值。

标准 accessibility 备用核实的最终版本在读取 accessibility 属性之前发现可见身份正文未包含可核验的店铺 ID，按最小披露门禁以 `IDENTITY_UNVERIFIED` 停止；实际 accessibility reads 为 0。中间短暂版本不纳入验收证据。

## 5. 能力与持久化边界

`product_business_metrics` 与已验收的推广商品归因数据集 `product_metrics` 保持独立。当前代码中的枚举、候选合约、候选 parser 和导出 Schema 仅用于 fail-closed 的响应形状约束，不是正式、可落盘的 snapshot payload：

- capability：`UNAVAILABLE`
- supported windows：空
- verified：`false`
- collector / dispatcher / Validator：未接入
- `pdd_collect_snapshot`：连接浏览器前返回 `DATASET_UNVERIFIED`
- 正式商品经营快照：0

因此没有运行该数据集的三次独立 MCP 首次提交，也没有可宣称通过的 latest、分页、重启读回或幂等验收。

## 6. 安全结果

- 真实模型调用：**0**
- 平台业务写操作：**0**
- ExecutionJob：**0**
- 预算、目标投产比、价格、库存、上下架或活动修改：**0**
- 真实商品经营快照：**0**
- 最终验收版 accessibility 属性读取：**0**；一次开发中间版只产生脱敏聚合计数，但因身份读取边界不符合最终最小披露约束而整次拒收，未作为证据、未输出属性值
- 字体/CSS/Canvas/图片/脚本逆向解码：**0**
- Stage D5 操作：**0**

专用 Chrome 和既有标签页保持运行；本轮未关闭用户 Browser、Context 或标签页。

## 7. 最终质量门禁

| 门禁 | 通过 | 失败 | 跳过 | 未执行 | 实际结果 |
|---|---:|---:|---:|---:|---|
| 全量 pytest | 522 | 0 | 0 | 0 | `522 passed in 17.05s` |
| Ruff format | 102 个文件 | 0 | 0 | 0 | `102 files already formatted` |
| Ruff lint | 1 | 0 | 0 | 0 | `All checks passed` |
| mypy strict | 40 个源文件 | 0 | 0 | 0 | `Success: no issues found in 40 source files` |
| Schema 导出 | 19 个 | 0 | 0 | 0 | 包含 candidate-only 商品经营 Schema；运行时与检入文件一致性测试 PASS |
| 真正 stdio MCP 子进程专项 | 1 | 0 | 0 | 0 | `tests/test_mcp_integration.py`：1 passed；六工具、重启和幂等链路包含在全量测试中 |
| D4 MCP/存储专项 | 44 | 0 | 0 | 0 | D4 商品、配置、账户 v3、失效与重启读回测试全部 PASS |
| 商品经营候选门禁专项 | 12 | 0 | 0 | 0 | capability 保持 UNAVAILABLE，采集在连接浏览器前拒绝 |
| accessibility 探针离线专项 | 52 | 0 | 0 | 0 | 身份/动作/输出边界测试全部 PASS；真实运行按门禁 STOPPED |
| 包构建 | wheel 1 + sdist 1 | 0 | 0 | 0 | 离线使用本机已有且版本匹配的 `hatchling 1.27.0` 缓存；未安装、未升级、未联网；wheel 导入及内容安全 smoke PASS |
| 真实仓库存储校验 | 34 个有效快照 | 0 | 0 | 0 | 1 个语义失效快照；损坏快照、损坏失效记录、未知状态均为 0 |
| `git diff --check` | 1 | 0 | 0 | 0 | PASS；仅有 Git 的 LF/CRLF 提示，无 whitespace error |
| `uv lock --check` | 0 | 0 | 0 | 1 | 当前 Shell 没有 `uv.exe`；按约束未安装系统组件，保留现有 `uv.lock` |

首次直接执行无隔离构建时，当前虚拟环境因没有安装构建后端而安全失败；随后只把本机离线缓存中的锁定版本 `hatchling 1.27.0` 及其既有依赖临时加入 `PYTHONPATH`，使用 `--skip-dependency-check --no-isolation` 成功构建。最终包内容检查确认不包含本地 Stage-D 配置、真实数据根或运行态目录。

最终构建产物：

- `dist/pdd_data_mcp-0.1.0-py3-none-any.whl`：已成功构建并通过独立 zip-import smoke；构建元数据会改变同名产物哈希，交付时用 `Get-FileHash` 即时核验
- `dist/pdd_data_mcp-0.1.0.tar.gz`：已成功构建；本报告本身包含在 sdist 中，因此不在正文写入会改变自身的递归哈希，交付时用 `Get-FileHash` 即时核验

```powershell
Get-FileHash '.\dist\pdd_data_mcp-0.1.0-py3-none-any.whl' -Algorithm SHA256
Get-FileHash '.\dist\pdd_data_mcp-0.1.0.tar.gz' -Algorithm SHA256
```

正式商品经营 MCP 首次提交为 0，不能由上述 522 项离线测试、页面探针或候选解析测试替代。最近 7 日、流量来源分类和 Stage D5 均按上表之外的功能范围保持 `NOT_RUN`。

## 8. 停止条件

本轮已在语义和格式证据不足处安全停止。未调用模型，未执行平台业务写操作，未创建 ExecutionJob，没有进入 Stage D5。
