# PDD Data MCP V0.1 阶段 A/B 验收报告

- 验收日期：2026-09-06
- 验收环境：Windows 10 `10.0.19045` / Asia/Shanghai
- 项目位置：`C:\path\to\pdd-data-mcp`（验收时使用独立本地工作目录）
- 阶段 A：**PASS**
- 阶段 B：**PASS**
- 阶段 C：**NOT_RUN**
- 阶段 D：**NOT_RUN**

## 1. 交付结论

V0.1 已能够独立启动 Python MCP stdio 服务，使用显式测试配置和 `source=SYNTHETIC` 的数据完成：

```text
启动子进程
-> MCP 工具发现
-> 合成采集
-> JSONL + manifest + validation + COMMIT 落盘
-> 返回 snapshot_id
-> MCP 列表/分页读取
-> 停止
-> 新子进程重启
-> 读回原 snapshot_id
-> 同键同参数幂等重放返回原提交
```

本轮没有修改或启动旧 TypeScript 主软件、API、Web、Worker、数据库或浏览器 Profile。开工时工作区是空 Git 仓库；所有修改仅位于新建 `pdd-data-mcp/`，未推送 Git。

## 2. 实际版本和依赖锁

| 组件 | 实测版本 |
|---|---:|
| Python | 3.12.14 |
| uv | 0.12.10 |
| MCP Python SDK | 2.1.1 |
| Pydantic | 2.13.5 |
| filelock | 3.19.1 |
| tzdata | 2025.2 |
| pytest | 8.4.1 |
| Ruff | 0.12.11 |
| mypy | 1.17.1 |
| build | 1.3.0 |

依赖在项目独立 `.venv` 内解析，并写入 `uv.lock`。没有安装管理员级系统组件。MCP SDK 实际 API 按 v2.1.1 的 `MCPServer` 和 `Client(StdioServerParameters(...))` 验证，没有照搬其他主版本示例。

## 3. 阶段 A 验收

**状态：PASS**

- 独立 `src` 布局 Python 项目、`pyproject.toml`、`uv.lock`、项目 `.venv`。
- MCP stdio 服务 stdout 专用协议；日志经 logging 写 stderr。官方 Client 的真实子进程发现/调用成功，未因 stdout 杂音破坏协议。
- 配置严格禁止额外字段，相对路径按配置文件目录解析，`data_root`/`runtime_root` 不可嵌套。
- 输入/输出模型实现枚举、ID 格式、日期/时区、范围、额外字段和跨字段规则校验；13 份 JSON Schema 已输出到 `schemas/`。
- `DatasetCollector`、`SnapshotRepository` Protocol 与 `SnapshotValidator` 已分层。
- 六个稳定工具已全部发现：
  - `pdd_get_capabilities`
  - `pdd_get_connection_status`
  - `pdd_collect_snapshot`
  - `pdd_list_snapshots`
  - `pdd_read_snapshot`
  - `pdd_get_latest_snapshot`
- 采集工具声明会写本地文件，没有标为完全只读；查询工具使用 read-only/idempotent 提示。
- 普通模式的支持数据集采集返回 `REAL_COLLECTION_DISABLED`；预留类型返回 `DATASET_UNVERIFIED`，不会回退到合成数据伪报真实成功。

## 4. 阶段 B 验收

**状态：PASS**

- 快照按 `store_id / dataset_type / Asia/Shanghai captured date / unique snapshot` 分区。
- 对象使用 `data.json`，列表使用每行一个完整对象的 UTF-8 `records.jsonl`。
- manifest 保存请求、scope、采集时间、指标时间窗、来源、覆盖率、文件字节数/记录数/SHA-256；COMMIT 保存 manifest SHA-256。
- 提交遵循 request -> `_tmp` 正文/validation -> fsync -> manifest/复读 -> 唯一正式目录 -> COMMIT -> request/index/pointer。
- 读取重新校验 COMMIT、manifest、正文和 validation 摘要。写一半、缺失 COMMIT 或损坏的目录不可查并会隔离。
- 同店铺+类型+幂等键保存参数摘要；同键同参数返回原 `snapshot_id`，同键异参数拒绝。
- 真实跨进程锁限制单写者；不通过删锁文件抢占。
- 恢复覆盖子进程在正文后/清单后强制终止、缺失 COMMIT、COMMIT 完成但 request 未更新、损坏正文和损坏索引。
- 索引可从有效快照重建；失败尝试不覆盖 `latest_usable/latest_complete`。
- 快照列表、记录分页、游标查询绑定/校验和、最大响应字节数、最大 31 天范围均实现。
- 每次用 `snapshot_id` 读取仍依 manifest 店铺重新授权；工具参数不接受路径、URL、CDP 或任意代码。
- 55 条只保存 50 条的演示返回 `PARTIAL + TRUNCATED + captured=50 + total_observed=55`。未知总数可为 `null`，缺失指标保存 `null` 和 `missing_fields`。
- 累计指标两次观察是两个独立快照，没有跨快照相加逻辑。
- 当配额/剩余空间、权限或文件占用导致写入失败时抛出存储错误，不返回保存成功；不自动删除历史。

## 5. 实际 MCP 演示证据

演示命令：

```powershell
& '.\.venv\Scripts\python.exe' '.\examples\demo_client.py' --output '.\examples\demo-output\acceptance'
```

实际结果：

| 项目 | 结果 |
|---|---|
| 演示状态 | PASS |
| 传输 | stdio |
| 启动服务子进程 | 2 |
| 发现工具 | 6 |
| 来源 | SYNTHETIC |
| 快照 ID | `s_8a81dcb8bb4441599ec0e6ec44730512` |
| 第一进程采集 | committed=true, PARTIAL/TRUNCATED, 50/55 |
| 第二进程回读 | 同一 snapshot ID，20 条第一页 |
| 第二进程幂等重放 | 同一 snapshot ID, `idempotent_replay=true` |

实际快照目录：

```text
examples/demo-output/acceptance/data/snapshots/pdd/st_synthetic_001/product_catalog/
2026/09/06/124352_617_s_8a81dcb8bb4441599ec0e6ec44730512/
```

目录包含 `records.jsonl`、`manifest.json`、`validation.json`、`COMMIT.json`。完整查询结果保存在 `examples/demo-output/acceptance/demo-result.json`。

`verify-storage` 结果：`PASS, valid_snapshots=1, invalid_snapshots=[]`。

`rebuild-index` 结果：`indexed=1, invalid=[]`，重建后仍指向上述 snapshot ID。

## 6. 自动化测试与质量门禁

### 最终统计

| 分类 | 通过 | 失败 | 跳过 | 未执行 |
|---|---:|---:|---:|---:|
| pytest 自动化测试 | 34 | 0 | 0 | 0 |
| Ruff | 1 | 0 | 0 | 0 |
| mypy | 1 | 0 | 0 | 0 |
| Python 包构建 | 1 | 0 | 0 | 0 |
| doctor | 1 | 0 | 0 | 0 |
| verify-storage | 1 | 0 | 0 | 0 |
| rebuild-index | 1 | 0 | 0 | 0 |
| 真实拼多多读取 | 0 | 0 | 0 | 1 |
| 真实模型调用 | 0 | 0 | 0 | 1 |
| 真实平台业务写操作 | 0 | 0 | 0 | 1 |

最终命令和输出摘要：

```text
ruff check src tests examples
  All checks passed!

mypy src
  Success: no issues found in 28 source files

pytest -q
  34 passed

pytest tests/test_mcp_integration.py -q
  1 passed

pytest tests/test_recovery_and_windows.py -q
  6 passed

python -m build
  Successfully built pdd_data_mcp-0.1.0.tar.gz
  and pdd_data_mcp-0.1.0-py3-none-any.whl
```

Wheel SHA-256：

```text
313E81CD81258971BE7280B134F9C0B0311296FEED774558494FD5EC60FB08F5
```

### 测试覆盖的重点

- 六个工具的发现、Schema/权限提示和真实跨进程 MCP 调用。
- 对象/JSONL 契约、摘要、分区、UTC/北京跨日和同毫秒唯一性。
- 采集时间与指标窗口分离、TODAY 非全天、累计指标不相加。
- 55/50 截断、未知总量、缺失值、金额精度、重复 ID、NaN/Infinity 防护。
- 幂等重放/冲突、scope 隔离、分页、游标篡改、跨进程锁竞争。
- 子进程正文后/清单后中断、缺失 COMMIT、已提交响应丢失、正文损坏、索引重建。
- 越权 store/snapshot、路径穿越、敏感字段、不可信文本、超大响应。
- 配额不足、失败不覆盖最新、真实采集默认禁用。

## 7. Windows 专有存储验收

**WINDOWS_STORAGE=PASS**

所有故障注入仅使用 pytest 本地临时目录和测试子进程，没有填满真实磁盘或中断用户服务。

| Windows 检查 | 状态 | 方法 |
|---|---|---|
| 中断恢复 | PASS | Windows 子进程在正文后及 manifest 后使用非正常退出，重启隔离且标记 INTERRUPTED |
| 文件占用 | PASS | Windows `CreateFile` share mode 0 实际独占请求文件，替换失败被报告为存储失败 |
| 空间不足 | PASS | 测试配置将快照配额限制为 1 字节，不真实占满磁盘，确认无 COMMIT 生成 |
| 单写者 | PASS | 独立 Windows 进程持锁时第二个 repository 获锁返回 LOCK_BUSY |

本次 Windows 特有项均实际执行，没有用其他系统结果代替，因此未标记 Windows 项为 NOT_RUN。NTFS ACL 的生产目录权限收紧没有自动执行，因为用户未指定最终生产 `data_root`；这不影响测试目录的文件占用/写入失败验收。

## 8. 总门禁状态

| 门禁 | 状态 |
|---|---|
| CODE_TEST | **PASS** |
| MCP_INTEGRATION | **PASS** |
| WINDOWS_STORAGE | **PASS** |
| STAGE_A | **PASS** |
| STAGE_B | **PASS** |
| REAL_PDD_READ | **NOT_RUN** |
| REAL_MODEL_CALLS | **0** |
| REAL_PLATFORM_BUSINESS_WRITES | **0** |
| ExecutionJob 创建 | **0** |

## 9. 未执行项和原因

- **REAL_PDD_READ=NOT_RUN**：属于阶段 C；用户明确要求本轮不连接真实拼多多或 Chrome。
- **真实模型调用=0**：服务不包含模型客户端，测试和演示也没有模型请求。
- **真实平台业务写操作=0**：未连接平台，未修改广告、预算、投产目标、商品或活动。
- **阶段 C/D=NOT_RUN**：用户要求完成 A/B 后停止。

## 10. 阶段 C 前置条件（未开始）

后续若获得新授权，阶段 C 仍需单独核实：专用 Chrome/Profile 边界、CDP 连接参数、店铺身份校验、已授权页面的实际只读响应结构、字段时间/单位/归因口径、DOM 核对策略和平台合规边界。本轮没有预先编造这些适配逻辑。
