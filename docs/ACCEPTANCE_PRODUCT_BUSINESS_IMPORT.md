# PDD Data MCP — 商品经营本地报表导入预检报告

- 日期：2026-09-08
- 项目：`pdd-data-mcp`
- 当前内部店铺：`st_pdd_current_01`
- 阶段对应关系：未编号的 post-D4 本地只读导入预检；**不是 Stage D5**
- 最终状态：**NEED_SAMPLE**

## 1. 结论

项目内授权证据范围没有合格的真实商品整体经营原始报表，也没有与报表配套的店铺归属、统计日期、筛选条件和字段口径记录。按照本轮明确的停止条件，本轮在样本盘点后停止：没有开发导入器，没有生成 dry-run 字段映射，没有创建或提交商品经营快照，也没有把合成数据、测试 fixture、推广归因数据或现有快照冒充真实导出报表。

| 验收项 | 状态 | 结果 |
|---|---|---|
| 合格真实样本 | **NEED_SAMPLE** | 项目内授权证据目录没有原始商品经营导出报表 |
| 样本来源与店铺归属确认 | **NOT_AVAILABLE** | 没有文件，因此无法核实或取得用户针对该文件的归属确认 |
| 字段、单位、缺失规则与统计窗口预检 | **NOT_RUN** | 无真实样本，不能猜测 |
| 本地 dry-run | **NOT_RUN** | 未实现；无样本时不得先建设通用导入平台 |
| 本地报表导入实现 | **NOT_STARTED** | CLI、Schema、Validator、来源类型和依赖均未修改 |
| 验收快照 | **0 / NOT_RUN** | 无 `snapshot_id`、无导入 scope、无新 `COMMIT` |
| stdio MCP 读回、latest、分页、重启、幂等 | **NOT_RUN** | 没有可合法提交的真实快照 |
| 商品经营自动采集 | **NOT_ENABLED / UNAVAILABLE** | 原状态保持不变；本地导入也不能改写成自动采集通过 |
| D1/D2/D3/D4 浏览器真实回归 | **NOT_RUN（本轮）** | 按要求不操作浏览器；既有验收事实保持不变 |
| Stage D5 | **NOT_RUN** | 未启动或重新定义主软件正式接入 |

`NEED_SAMPLE` 不是“导入能力 PASS”。它表示停止条件被正确触发，正式导入链路尚未开始。

## 2. 本轮范围与既有基线

本轮是 D4 后的独立本地报表导入预检。D4 仍是推广数据基线，D5 仍只指旧主软件正式接入。没有复用或重新解释已验收的推广商品数据集 `product_metrics`。

保留且未改写的既有事实包括：

- D1/D2/D3/D4 已提交快照及原始证据；
- 旧 D4 YESTERDAY v2 快照及其 append-only 语义失效记录；
- `latest` 排除非 `ACTIVE` 快照、`read/list` 保留审计可见的规则；
- `product_business_metrics` 自动采集 capability 为 `UNAVAILABLE`、`supported_window_kinds=[]`、`verified=false`；
- 现有六个 MCP 工具及本地文件仓库架构。

本轮没有运行浏览器、连接 CDP、访问平台接口或重跑浏览器真实采集。

## 3. 授权范围内的样本盘点

只检查了项目根中的显式证据/样本候选位置和文档，没有扫描父目录、桌面、下载目录、整台电脑或其他店铺目录，也没有把快照 payload 当作原始报表读取。

| 检查位置或类别 | 结果 | 判定 |
|---|---|---|
| 项目根的 evidence/sample/import/report/export 命名候选目录 | 未发现可用样本目录 | 无候选 |
| `docs/` | 仅阶段报告、架构与覆盖矩阵等 Markdown 文档；无 XLSX/XLS/CSV/TSV/ODS/ZIP 报表 | 非原始样本 |
| `real-runtime/discovery/` | 仅既有推广发现元数据 | 推广证据，不是商品整体经营导出 |
| `stage-d-real-runtime/` | 仅运行状态文件 | 非报表 |
| `examples/demo-output/` | 明确为演示/合成输出 | 不得冒充真实样本 |
| `tests/` fixture 与各数据根 | 测试或派生快照 | 按约束排除，不作为候选 |

因此无法执行店铺归属、商品 ID、统计日期、时区、筛选范围、字段定义、数值单位、缺失规则、汇总行、重复行、隐藏筛选、覆盖完整性、导出时间与统计时间分离等逐项预检。

## 4. 为什么没有提前实现导入器

当前 `product_business_metrics` 只有 fail-closed 的候选响应契约，不是正式可落盘的商品经营 Schema；其自动采集在服务层会于 collector 之前拒绝。CLI 也没有报表导入命令，正式 Validator 没有接受该数据集。

此外，快照 `source` 目前只有 `SYNTHETIC` 和 `PDD_BROWSER_CDP`，不能把未来的本地文件导入伪装成浏览器网络采集。项目依赖与锁文件中也没有 XLS/XLSX 解析库。样本格式尚未知时，新增格式依赖、字段映射、文件来源契约或通用导入框架都会构成猜测；本轮因此未修改代码、Schema、CLI、依赖或锁文件，也未安装任何组件。

## 5. 最小合格样本要求

下一轮只需要一份最小、真实、授权的商品级整体经营报表，不要求一次覆盖所有日期或 12 个目标字段。请提供：

1. 一份未经修改的商品经营原始导出文件，放入用户明确授权的项目内样本目录；
2. 该文件属于 `st_pdd_current_01` 的明确确认；如果文件没有店铺标识，报告会把依据记录为“用户确认”，不会伪装成文件自带的平台凭证；
3. 一个明确的已结束自然日、业务时区及导出时使用的筛选条件；
4. 至少一个非标识类经营指标的原始名称、业务定义、数值单位和缺失规则；
5. 如能取得，附上不含个人信息的字段口径说明或导出条件记录。

优先提供商品级汇总，不需要也不应包含客户姓名、电话、地址等个人信息。由于本轮没有核实平台导出入口，本报告不编造后台菜单路径或导出步骤。

## 6. 数据与安全结果

| 项目 | 实际结果 |
|---|---:|
| 浏览器/CDP 操作 | 0 |
| 平台接口调用 | 0 |
| 真实模型调用 | 0 |
| 平台业务写操作 | 0 |
| ExecutionJob | 0 |
| 新商品经营快照 | 0 |
| 原始快照或 manifest/COMMIT 改写 | 0 |
| 失效记录删除或改写 | 0 |
| Git commit / push | 0 |

没有读取、复制、打印或提交真实报表内容，因为项目内没有此类样本。没有新增会接受任意路径的 MCP 工具，MCP 工具数量保持六个。

## 7. 实际修改文件

- `docs/ACCEPTANCE_PRODUCT_BUSINESS_IMPORT.md`：新增本次 `NEED_SAMPLE` 预检报告；
- `docs/DATA_COVERAGE_MATRIX.md`：把“自动采集不可用”与“本地报表导入等待真实样本”分开记录。

代码、Schema、配置、依赖、锁文件和任何数据根均未因本轮修改。

## 8. 实际执行与检查

只读盘点使用项目内的目录/文件元数据枚举和 `rg` 检索；没有向项目外扩展搜索。最终离线门禁结果：

| 类型 | 实际命令或检查 | 结果 |
|---|---|---|
| 授权范围样本盘点 | 项目根候选目录 + `docs/` + `real-runtime/discovery/` + `stage-d-real-runtime/` 的受限元数据枚举 | **PASS**：候选目录 0；三个位置分别为 8/6/1 个文件；表格或压缩报表候选均为 0 |
| Ruff 格式 | `.\.venv\Scripts\python.exe -m ruff format --check src tests examples` | **PASS**：102 files already formatted |
| Ruff 静态检查 | `.\.venv\Scripts\python.exe -m ruff check src tests examples` | **PASS**：All checks passed |
| mypy | `.\.venv\Scripts\python.exe -m mypy src\pdd_data_mcp` | **PASS**：40 source files，0 issues |
| pytest | `.\.venv\Scripts\python.exe -m pytest -q` | **PASS**：522 passed in 19.13s |
| Schema 一致性 | `.\.venv\Scripts\python.exe -m pytest -q tests\test_contracts.py::test_checked_in_schemas_match_runtime_models` | **PASS**：1 passed in 0.07s（也包含在全套 522 项中，不重复计入测试总数） |
| 既有仓库只读校验 | `.\.venv\Scripts\python.exe -m pdd_data_mcp.cli verify-storage --config config\config.stage-d.local.toml` | **PASS**：34 个有效快照、1 个语义失效快照、0 个无效快照、0 个未知失效状态 |
| Markdown 表格结构 | PowerShell 逐节 pipe 数校验 | **PASS**：0 个异常表格行 |
| 补丁空白检查 | `git diff --check` | **PASS**：无 whitespace error；仅报告工作树既有 LF→CRLF 提示 |
| uv 锁检查 | `uv lock --check` | **NOT_RUN**：环境没有 `uv.exe`，按约束未安装 |

检查统计（不把嵌套的 Schema 单测重复计入 pytest 数量）：

- PASS：9 项工程/边界检查；pytest 为 522/522 通过；
- FAIL：0；
- SKIP：0；
- NOT_RUN：1 项环境检查（`uv lock --check`），以及下文所有依赖真实样本或浏览器的验收链路。

浏览器真实采集、真实样本 dry-run、真实导入提交、stdio MCP 导入快照读回、分页、重启和幂等均为 `NOT_RUN`。离线测试或既有快照校验不能替代这些真实导入验收项。若环境没有 `uv.exe`，`uv lock --check` 保持 `NOT_RUN`，不会安装 uv。

## 9. 最终判定与唯一下一步

最终判定：**NEED_SAMPLE**。

唯一下一步：把一份当前店铺的原始商品级整体经营报表放入一个明确授权的项目内样本目录，并同时提供店铺归属、一个已结束自然日的统计条件，以及至少一个经营指标的定义、单位和缺失规则。收到后先做只读 dry-run 预检；只有预检通过，才实现该具体格式所需的最小导入通路。
