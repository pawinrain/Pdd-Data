# PDD Data MCP 阶段 C 验收报告

- 验收日期：2026-09-07
- 环境：Windows 10 `10.0.19045` / Asia/Shanghai
- 范围：仅 `promotion_overview + TODAY + 广告消耗`
- 阶段 A：**PASS**
- 阶段 B：**PASS**
- 阶段 C：**PASS**
- 阶段 D：**NOT_RUN**

## 1. 结论

真实链路已经按用户授权完成：独立 MCP stdio 客户端启动 Python 服务，通过 Playwright Python CDP 连接专用 Chrome，监听推广概况页正常产生的精确白名单响应，解析唯一 TODAY 广告消耗，完成业务成功、店铺身份、日期、单位和 DOM 核对，并通过原 `SnapshotRepository` 提交本地不可变快照。

共执行三次相互独立的真实观察，每次使用新幂等键并遵守 60 秒最小触发间隔，形成三个不同快照。每次均完成 MCP 读取、停止服务、重启服务后读取，以及原键幂等重放。没有输出或写入本报告任何真实广告金额。

店铺身份不是把配置值原样回传：推广域的 `user/info` 身份字段先取得内存指纹，再与同一专用 Chrome 中拼多多商家后台店铺基础信息页的独立页面启动状态进行 SHA-256 指纹比对。两者一致，原始店铺编号没有输出或写入报告；manifest 只保存带 `sha256:` 前缀的指纹和 `MERCHANT_PAGE_STATE_SHA256` 验证方法。

## 2. 已验证实现

- Playwright Python `1.62.0` 异步 CDP；仅允许可信配置中的显式回环端点。
- 精确匹配目标页面及响应 host/path/method/status/content-type；MCP 参数不能传任意 URL。
- 先挂响应监听再执行受控 `RELOAD`；不读取连接前缓存，不重放私有请求，不读取 Cookie、Header 或 Storage。
- 指标响应：`getAdvertiserDailyCosts`；身份响应：`user/info`。两者必须在同一采集窗口到达并通过各自业务成功检查。
- 严格选择唯一 TODAY 记录，核对 `YUAN` 单位，并无损换算整数分；缺失、重复日期、未知单位或错误业务状态均拒绝提交。
- DOM 使用唯一“今日花费 (元)”标签及其相邻金额节点核对网络值；差异返回 `DATA_MISMATCH`。
- 身份证据、响应字段路径、字段来源、采集时间、指标窗口和解析器版本进入 manifest。
- 服务只断开自身 CDP 传输和监听，不关闭用户 Chrome、Context 或标签页。
- 真实配置、`real-data/`、`real-runtime/` 均排除 Git；原始响应正文不落盘。

## 3. 自动化质量门禁

专项测试包含在 pytest 总数 55 中，不重复相加。A/B 的 34 项及本机 CDP 的 1 项都是总套件子集。

| 分类 | 通过 | 失败 | 跳过/排除 | 未执行 |
|---|---:|---:|---:|---:|
| A/B 回归 pytest | 34 | 0 | 0 | 0 |
| pytest 总套件 | 55 | 0 | 0 | 0 |
| 其中：离线 pytest | 54 | 0 | 1（排除本机 CDP） | 0 |
| 其中：受控本机 CDP pytest | 1 | 0 | 54（排除非专项） | 0 |
| Ruff 格式与检查 | 2 | 0 | 0 | 0 |
| mypy | 1 | 0 | 0 | 0 |
| Python 包构建 | 1 | 0 | 0 | 0 |
| Schema 导出 | 1 | 0 | 0 | 0 |
| 真实仓库 verify-storage | 1 | 0 | 0 | 0 |
| 真实仓库索引重建 | 1 | 0 | 0 | 0 |
| `uv lock --check` | 0 | 0 | 0 | 1（当前 Shell 无 `uv.exe`，未安装系统组件；依赖未改动） |

最终结果摘要：

```text
ruff format --check src tests examples
  46 files already formatted
ruff check src tests examples
  All checks passed!
mypy src
  Success: no issues found in 31 source files
pytest -q
  55 passed in 8.85s
pytest <A/B test files> -q
  34 passed in 4.40s
pytest -m "not local_cdp" -q
  54 passed, 1 deselected in 4.64s
pytest -m local_cdp -q
  1 passed, 54 deselected in 4.23s
python -m build
  Successfully built sdist and wheel
verify-storage
  PASS, 3 valid, 0 invalid
rebuild-index
  3 indexed, 0 invalid
```

Wheel SHA-256：`9553AF5EEE858DC3E8CC9B724820F759B88B5E02BAFEC8DCE7C8AB5AC926B343`。

## 4. 真实链路门禁

| 门禁 | 状态 | 证据 |
|---|---|---|
| CODE_TEST | **PASS** | Ruff、mypy、55 项 pytest、Schema、构建通过 |
| MCP_INTEGRATION | **PASS** | 六工具发现；真实采集、分页、读取、最新快照均经 MCP stdio 子进程 |
| CDP_CONNECTION | **PASS** | `127.0.0.1:9222`，Chrome 进程与独立 Profile 已核实，目标页唯一 |
| STORE_IDENTITY | **PASS** | 推广身份响应与商家后台基础信息页独立状态的 SHA-256 指纹一致 |
| REAL_NETWORK_CAPTURE | **PASS** | 三次均为精确白名单真实响应，`capture_method=NETWORK_RESPONSE` |
| DOM_CROSSCHECK | **PASS** | 唯一 TODAY 标签及金额节点与网络值匹配，数值未输出 |
| REAL_FILE_COMMIT | **PASS** | 3 个 manifest/data/validation/COMMIT 完整快照；verify-storage 3/3 |
| MCP_READBACK | **PASS** | 三个快照均可读；两页游标查询共返回 3 个唯一快照 |
| SERVICE_RESTART_READBACK | **PASS** | 每次采集后均重启独立 MCP 子进程并读回同一 ID |
| IDEMPOTENT_REPLAY | **PASS** | 三个原键均返回各自原 snapshot_id，不产生额外提交 |
| CHROME_PRESERVED | **PASS** | 采集及服务退出后 Chrome 进程仍运行，目标标签页仍存在 |
| AB_REGRESSION | **PASS** | 34/34 |
| REAL_MODEL_CALLS | **0** | 无模型客户端、调用或数据上传 |
| REAL_PLATFORM_BUSINESS_WRITE_ACTIONS | **0** | 无广告、预算、投产目标、商品或活动修改 |

## 5. 真实快照

| snapshot_id | 相对目录 | 范围 | 来源 / 方式 | 读取状态 |
|---|---|---|---|---|
| `s_fc5cc70284f74c4594a17b7c73211e71` | `snapshots/pdd/st_real_001/promotion_overview/2026/09/07/011053_016_s_fc5cc70284f74c4594a17b7c73211e71` | TODAY | PDD_BROWSER_CDP / NETWORK_RESPONSE | MCP + 重启读回 PASS |
| `s_4936baae79364ad592e75557fcd0e66b` | `snapshots/pdd/st_real_001/promotion_overview/2026/09/07/011202_742_s_4936baae79364ad592e75557fcd0e66b` | TODAY | PDD_BROWSER_CDP / NETWORK_RESPONSE | MCP + 重启读回 PASS |
| `s_db69675df3054fdc894d73f44f9db870` | `snapshots/pdd/st_real_001/promotion_overview/2026/09/07/011311_425_s_db69675df3054fdc894d73f44f9db870` | TODAY | PDD_BROWSER_CDP / NETWORK_RESPONSE | MCP + 重启读回 PASS |

最新完整快照查询返回 `s_db69675df3054fdc894d73f44f9db870`。累计指标没有跨快照相加；每份快照保留各自采集时间和 TODAY 指标窗口。

## 6. 不可用与故障路径

未关闭用户 Chrome。通过临时本机配置将 CDP 指向确认未监听的随机回环端口，真实 MCP 子进程仍成功读取已有快照；新幂等键采集返回 `CDP_UNAVAILABLE`、`committed=false`，快照数保持 3。该安全故障注入 **PASS**。

实际关闭用户 Chrome 的人工测试：**NOT_RUN**（需要另行确认，且没有必要为本轮制造破坏性状态）。身份错配、验证码、业务失败、超时、迟到/重复响应和 DOM 冲突由离线测试覆盖，没有故意触发真实平台风控。

## 7. 安全计数与停止状态

- 真实独立成功采集：**3**。
- 真实拼多多读取：**RUN，限定推广概况 TODAY 广告消耗**。
- 用户专用 Chrome：**保留运行**。
- 真实模型调用：**0**。
- 真实平台业务写操作：**0**。
- ExecutionJob 创建：**0**。
- 定时或无人监督任务：**0**。
- 旧主软件默认数据源修改：**0**。
- 阶段 D：**NOT_RUN**。

阶段 C 已完成并停止；没有进入阶段 D。
