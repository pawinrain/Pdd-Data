# pdd-data-mcp

`pdd-data-mcp` 是一个独立的 Python MCP stdio 服务。阶段 A/B 已实现严格数据契约、显式合成测试采集、不可变 JSON/JSONL 快照、安全提交、幂等、恢复、索引、历史查询和分页。

阶段 C 和 Stage D1/D2/D3 已完成真实只读验收。D1/D2/D3 增加店铺概况、商品档案和库存的严格适配器、分页、DOM 核对、逐行店铺身份核验、共享 batch 编排与 MCP 跨进程验证。当前店铺平台观测商品数为 3，三次独立采集均为 3/3 COMPLETE。发布模板保持 `real_collection_enabled=false`，不会默认连接浏览器。服务不调用模型，不修改广告、预算、库存、商品或活动，不使用数据库或 Docker。

## 已实现的 MCP 工具

| 工具 | 当前状态 |
|---|---|
| `pdd_get_capabilities` | 可用；明确区分合成测试、真实配置和已验证适配器 |
| `pdd_get_connection_status` | 可用；返回 `NOT_CHECKED`，不会隐式连接 Chrome |
| `pdd_collect_snapshot` | 合成测试可用；真实模式支持已验证的推广概况、店铺概况、商品档案和库存 |
| `pdd_list_snapshots` | 可用；授权店铺、日期范围和绑定游标分页 |
| `pdd_read_snapshot` | 可用；每次重新鉴权并核对 COMMIT/摘要 |
| `pdd_get_latest_snapshot` | 可用；按规范化 scope 隔离最新可用/完整快照 |

四类合成契约是 `store_overview`、`product_catalog`、`inventory`、`promotion_overview`。`product_metrics`、`campaign_metrics`、`activity_catalog` 仅预留，返回 `DATASET_UNVERIFIED`/`UNAVAILABLE`。

## Windows PowerShell 安装

需要 Python 3.12 和 uv。在项目目录执行：

```powershell
Set-Location 'C:\Users\jingzu\Documents\ChatGPT\通往山巅的路\pdd-data-mcp'
uv sync --locked --python 3.12
```

`uv sync --locked` 会在项目内创建独立 `.venv`，严格按 `uv.lock` 安装。本次验收实测使用 CPython 3.12.14 和 uv 0.12.10。

阶段 C 的受控本地浏览器测试会优先使用锁定版本对应的 Playwright Chromium；未安装时，Windows 测试可使用系统 Chrome 二进制配合全新临时 Profile。需要独立测试浏览器时执行（不是用户的日常 Chrome）：

```powershell
& '.\.venv\Scripts\playwright.exe' install chromium
```

## 启动服务

先复制并按需修改受信配置；默认示例关闭合成数据和真实采集：

```powershell
Set-Location 'C:\Users\jingzu\Documents\ChatGPT\通往山巅的路\pdd-data-mcp'
Copy-Item '.\config\config.example.toml' '.\config\config.local.toml'
& '.\.venv\Scripts\pdd-data-mcp.exe' doctor --config '.\config\config.local.toml'
& '.\.venv\Scripts\pdd-data-mcp.exe' serve --config '.\config\config.local.toml'
```

stdio 服务正常由 MCP Client 启动和停止。手工前台启动时，按 `Ctrl+C` 停止。stdout 专用于 MCP 协议，日志写 stderr。

## 运行独立 MCP 演示

演示客户端真实启动两个 stdio 子进程，完成工具发现、合成采集、查询/分页、停止、重启读回和幂等重放：

```powershell
Set-Location 'C:\Users\jingzu\Documents\ChatGPT\通往山巅的路\pdd-data-mcp'
$Run = ".\examples\demo-output\run-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
& '.\.venv\Scripts\python.exe' '.\examples\demo_client.py' --output $Run
Get-ChildItem "$Run\data\snapshots" -Directory -Recurse -Filter 's_*'
Get-Content "$Run\demo-result.json"
```

已交付的实际演示结果在 `examples/demo-output/acceptance/`，它是明确标记的本地合成数据目录，被 `.gitignore` 排除以避免把运行数据提交到代码库。

## 质量门禁和维护

```powershell
Set-Location 'C:\Users\jingzu\Documents\ChatGPT\通往山巅的路\pdd-data-mcp'
& '.\.venv\Scripts\ruff.exe' check src tests examples
& '.\.venv\Scripts\mypy.exe' src
& '.\.venv\Scripts\pytest.exe' -q
& '.\.venv\Scripts\pytest.exe' tests\test_mcp_integration.py -q
& '.\.venv\Scripts\pytest.exe' tests\test_recovery_and_windows.py -q
& '.\.venv\Scripts\python.exe' -m build
& '.\.venv\Scripts\pdd-data-mcp.exe' verify-storage --config '.\examples\demo-output\acceptance\demo-config.toml'
& '.\.venv\Scripts\pdd-data-mcp.exe' rebuild-index --config '.\examples\demo-output\acceptance\demo-config.toml'
```

`verify-storage` 和 `rebuild-index` 都获取同一跨进程锁，不删除已提交历史。损坏或未完成批次在启动恢复时移入 `_quarantine`，不会成为正式查询结果。

## MCP Client 启动配置示例

下面是已用 `examples/demo_client.py` 同等参数验证的启动方式。把它放入你所使用的 MCP Client 配置中；本项目不猜测也不自动修改全局配置文件。

```json
{
  "mcpServers": {
    "pdd-data-mcp": {
      "command": "C:\\Users\\jingzu\\Documents\\ChatGPT\\通往山巅的路\\pdd-data-mcp\\.venv\\Scripts\\pdd-data-mcp.exe",
      "args": [
        "serve",
        "--config",
        "C:\\Users\\jingzu\\Documents\\ChatGPT\\通往山巅的路\\pdd-data-mcp\\config\\config.local.toml"
      ]
    }
  }
}
```

Python Client 完整实例见 `examples/demo_client.py`。它使用官方 SDK `Client(StdioServerParameters(...))`，不是进程内伪造调用。

## 阶段 C：真实只读采集

真实配置模板是 `config/config.real.example.toml`。其中没有 Cookie、Token、账号或其他认证信息，真实数据目录 `real-data/`、运行元数据目录 `real-runtime/` 和本地真实配置都已排除 Git。服务不会启动、登录或关闭 Chrome。

每次新的真实运行仍须先取得用户对该轮只读范围的确认，并完成下面的人工准备：

1. 使用已登录的专用调试 Chrome，人工打开 `https://yingxiao.pinduoduo.com/mains/promotionOverview`。不要使用日常 Chrome，不复制旧 Profile 或 Cookie。

若专用调试 Chrome 尚未启动，可由用户在 PowerShell 中手工执行下面的示例。`$DedicatedProfile` 必须是专门为本服务新建或已明确批准的目录，不能指向日常 Chrome Profile：

```powershell
$Chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
$DedicatedProfile = 'C:\Users\jingzu\Documents\pdd-data-mcp-dedicated-chrome'
Start-Process -FilePath $Chrome -ArgumentList @(
  '--remote-debugging-address=127.0.0.1',
  '--remote-debugging-port=9222',
  "--user-data-dir=$DedicatedProfile",
  '--no-first-run',
  'https://yingxiao.pinduoduo.com/mains/promotionOverview'
)
```

登录和验证码均由用户在该可见窗口内手工完成。

2. CDP 必须只监听 `127.0.0.1` 或 `::1`。以下命令只检查端口，不连接浏览器：

```powershell
$Listener = Get-NetTCPConnection -State Listen -LocalPort 9222
$Listener | Select-Object LocalAddress,LocalPort,OwningProcess
Get-Process -Id $Listener.OwningProcess | Select-Object Id,Path,StartTime
```

若输出是 `0.0.0.0`、`[::]`，端口被其他进程占用，或进程归属不清，停止，不要继续。

3. 复制模板并人工填写内部 `store_id`、可验证的平台店铺 ID 或其 SHA-256 指纹，以及专用 CDP 端口。新页面或接口必须先保持 `promotion_adapter.verified=false` 完成有界发现；发现模式只保存经清理的 host/path/method/status/content-type，不读未知响应正文。

```powershell
Copy-Item '.\config\config.real.example.toml' '.\config\config.real.local.toml'
& '.\.venv\Scripts\pdd-data-mcp.exe' doctor --config '.\config\config.real.local.toml'
```

确认候选响应业务用途、精确字段路径、独立店铺证据、TODAY 日期、金额单位和 DOM 选择器后，才能将适配器标记为 `verified=true`。本次已验证配置保存在被 Git 排除的 `config/config.real.local.toml`；公开模板不含账号、认证信息或真实店铺标识。

适配器经核实后，独立客户端通过真实 MCP stdio 子进程完成一次采集、读回、重启读回和幂等重放：

```powershell
$Key = 'real-promotion-' + [guid]::NewGuid().ToString('N')
& '.\.venv\Scripts\python.exe' '.\examples\real_read_client.py' `
  --config '.\config\config.real.local.toml' `
  --connection-id 'conn_pdd_promotion_01' `
  --idempotency-key $Key `
  --confirm-read-only
```

只有本地人工核对需要时才增加 `--show-local-value`；默认输出不包含真实广告消耗。每次独立观察必须使用新幂等键，并遵守配置最小间隔；重复原键不会连接或刷新浏览器。本次三个真实快照及完整门禁见 `docs/ACCEPTANCE_C.md`。

DOM 备用采集默认关闭；只有可信配置显式启用、网络响应超时且 DOM 能独立核实平台店铺 ID、TODAY 和金额时才允许提交，并会保存 `capture_method=DOM` 和字段来源 `DOM`。认证、验证码、身份错配、业务错误或数据冲突不会降级，DOM 成功也不计作真实网络响应通过。

## Stage D1/D2/D3：核心店铺数据

公开模板为 `config/config.stage-d.example.toml`。真实配置须使用新的内部 `store_id`，并重新绑定当前平台店铺身份；不能复用 Stage C 店铺的内部绑定或历史快照。

已验证能力：

- `store_overview + TODAY`：当前以已验证 DOM 备用通道读取 4 个语义明确指标，记录平台更新时间；TODAY 窗口截止该更新时间，不标记为全天完成。
- `product_catalog + POINT_IN_TIME`：响应总数、分页、重复 ID、limit、coverage、truncated 和 DOM 总数核对。
- `inventory + POINT_IN_TIME`：独立 JSONL 快照，严格区分真实 0 与缺失 null，并保留 PRODUCT/SKU 粒度。
- `batch_id`：现有 `pdd_collect_snapshot` 增加可选兼容参数，三个数据集可共享批次但分别提交，允许部分成功。

单数据集只读采集：

```powershell
$Key = 'stage-d-' + [guid]::NewGuid().ToString('N')
& '.\.venv\Scripts\python.exe' '.\examples\real_dataset_client.py' `
  --config '.\config\config.stage-d.local.toml' `
  --connection-id 'conn_pdd_current_01' `
  --dataset 'product_catalog' `
  --idempotency-key $Key `
  --confirm-read-only
```

独立核心同步客户端仍只调用现有六个 MCP 工具：

```powershell
Set-Location 'C:\Users\jingzu\Documents\ChatGPT\通往山巅的路\pdd-data-mcp'
& '.\.venv\Scripts\python.exe' '.\examples\core_sync_client.py' `
  --config '.\config\config.stage-d.local.toml' `
  --connection-id 'conn_pdd_current_01' `
  --confirm-read-only
```

客户端默认在数据集之间等待 60 秒，输出 snapshot ID、计数、覆盖率和映射状态，不输出店铺 ID、商品字段或经营指标值。真实状态与未验证字段见 `docs/ACCEPTANCE_D123.md`。

只查询历史、分页和最新完整快照（不触发浏览器）：

```powershell
& '.\.venv\Scripts\python.exe' '.\examples\real_history_client.py' `
  --config '.\config\config.stage-d.local.toml' `
  --store-id 'st_pdd_current_01'
```

## 存储布局

```text
data-root/
  _meta/storage.json
  _locks/
  _requests/
  _runs/
  _tmp/
  _quarantine/
  _indexes/
  _pointers/
  snapshots/pdd/<store_id>/<dataset_type>/YYYY/MM/DD/
    HHMMSS_mmm_s_<uuid>/
      manifest.json
      data.json | records.jsonl
      validation.json
      COMMIT.json
  audit/YYYY/MM/DD/events.jsonl
```

`captured_at` 是采集观察时间，`metric_window` 是指标口径时间，两者分开保存。正文时间带时区，目录默认按 `Asia/Shanghai` 分区。只有 COMMIT 、manifest 和正文摘要全部匹配的快照才可读。

阶段 A/B 验收见 `docs/ACCEPTANCE_AB.md`，阶段 C 当前门禁见 `docs/ACCEPTANCE_C.md`，数据 Schema 见 `schemas/`。
