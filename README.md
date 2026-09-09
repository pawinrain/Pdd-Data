# pdd-data-mcp

`pdd-data-mcp` 是一个独立的 Python MCP stdio 服务。阶段 A/B 已实现严格数据契约、显式合成测试采集、不可变 JSON/JSONL 快照、安全提交、幂等、恢复、索引、历史查询和分页。

阶段 C 与 Stage D1/D2/D3/D4 的既有真实只读基线已完成。D1/D2/D3 覆盖店铺概况、商品档案和库存；D4 的账户 TODAY、推广商品 TODAY 及推广商品当前配置各有三次既有重复真实验收，账户 TODAY 又完成一次 v3 回归；账户 YESTERDAY 部分日窗口使用纠正后的 `d4-account-v3` 契约完成三次重复真实验收。YESTERDAY 不能解释为完整昨日或最终值；旧 `d4-account-v2` 错标快照保留原文件并通过追加失效记录排除出 latest。本轮未编号的 post-D4 商品整体经营只读核实已收口：真实页面、端点和响应形状证据已取得，但数值格式、单位和 Ytd 时间语义没有达到正式采集门禁，因此 `product_business_metrics` 保持 `UNAVAILABLE`，没有生成正式快照。发布模板保持 `real_collection_enabled=false`，不会默认连接浏览器。服务不调用模型，不修改广告、预算、投产目标、价格、库存、商品或活动，不使用数据库或 Docker。Stage D5 主软件正式接入没有开始。

## 已实现的 MCP 工具

| 工具 | 当前状态 |
|---|---|
| `pdd_get_capabilities` | 可用；明确区分合成测试、真实配置和已验证适配器 |
| `pdd_get_connection_status` | 可用；返回 `NOT_CHECKED`，不会隐式连接 Chrome |
| `pdd_collect_snapshot` | 合成测试可用；真实模式支持已验证的店铺概况、商品档案、库存、推广账户效果、推广商品效果和当前投放配置 |
| `pdd_list_snapshots` | 可用；授权店铺、日期范围和绑定游标分页 |
| `pdd_read_snapshot` | 可用；每次重新鉴权并核对 COMMIT/摘要 |
| `pdd_get_latest_snapshot` | 可用；按规范化 scope 隔离最新可用/完整快照 |

Stage D4 沿用以上六个工具，没有增加或改名 MCP 工具。当前推广能力边界如下：

| 数据集 | 已验证窗口/语义 | 实体粒度 | 状态 |
|---|---|---|---|
| `promotion_overview` | `TODAY`、`YESTERDAY` | ACCOUNT | TODAY 有三次既有基线并完成一次 v3 回归；YESTERDAY v3 部分日窗口完成三次重复真实验收 |
| `product_metrics` | `TODAY` | PROMOTED_PRODUCT | 真实只读已验收 |
| `promotion_configuration` | `POINT_IN_TIME`，仅当前观察 | PROMOTED_PRODUCT | 真实只读已验收 |
| `campaign_metrics` | 无 | CAMPAIGN | `UNAVAILABLE`；仅保留可核验的计划关联 ID，不发布计划粒度效果 |

四类合成契约仍是 `store_overview`、`product_catalog`、`inventory`、`promotion_overview`。合成数据只在显式测试模式及隔离测试目录启用；真实适配器不会退回合成数据。`activity_catalog` 等未验收数据集保持不可用。

商品整体经营与流量不会复用 `product_metrics`。后者已经由推广商品粒度的既有 Schema、适配器、capability 和历史快照占用；新名称 `product_business_metrics` 已作为枚举保留，使 MCP 能明确返回 `UNAVAILABLE`。仓库中的候选合约、parser 和导出 Schema 只用于约束已观察到的响应形状，不是可落盘的正式 payload Schema；当前没有 collector、dispatcher 或 Validator 接入，`pdd_collect_snapshot` 会在连接浏览器前拒绝该数据集。支付买家、支付订单、销量/支付件数、支付金额、访客和浏览量仍是目标字段，不是已发布的可读字段。

## Windows PowerShell 安装

需要 Python 3.12 和 uv。在项目目录执行：

```powershell
Set-Location 'C:\path\to\pdd-data-mcp'
uv sync --locked --python 3.12
```

`uv sync --locked` 会在项目内创建独立 `.venv`，严格按 `uv.lock` 安装。本次 D4 验收使用现有独立 `.venv` 中的 CPython 3.12.14；当前验收 Shell 没有 `uv.exe`，所以本轮如实把 `uv lock --check` 记为 `NOT_RUN`，没有为此安装系统组件。

阶段 C 的受控本地浏览器测试会优先使用锁定版本对应的 Playwright Chromium；未安装时，Windows 测试可使用系统 Chrome 二进制配合全新临时 Profile。需要独立测试浏览器时执行（不是用户的日常 Chrome）：

```powershell
& '.\.venv\Scripts\playwright.exe' install chromium
```

## 启动服务

先复制并按需修改受信配置；默认示例关闭合成数据和真实采集：

```powershell
Set-Location 'C:\path\to\pdd-data-mcp'
Copy-Item '.\config\config.example.toml' '.\config\config.local.toml'
& '.\.venv\Scripts\pdd-data-mcp.exe' doctor --config '.\config\config.local.toml'
& '.\.venv\Scripts\pdd-data-mcp.exe' serve --config '.\config\config.local.toml'
```

stdio 服务正常由 MCP Client 启动和停止。手工前台启动时，按 `Ctrl+C` 停止。stdout 专用于 MCP 协议，日志写 stderr。

## 运行独立 MCP 演示

演示客户端真实启动两个 stdio 子进程，完成工具发现、合成采集、查询/分页、停止、重启读回和幂等重放：

```powershell
Set-Location 'C:\path\to\pdd-data-mcp'
$Run = ".\examples\demo-output\run-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
& '.\.venv\Scripts\python.exe' '.\examples\demo_client.py' --output $Run
Get-ChildItem "$Run\data\snapshots" -Directory -Recurse -Filter 's_*'
Get-Content "$Run\demo-result.json"
```

已交付的实际演示结果在 `examples/demo-output/acceptance/`，它是明确标记的本地合成数据目录，被 `.gitignore` 排除以避免把运行数据提交到代码库。

## 质量门禁和维护

```powershell
Set-Location 'C:\path\to\pdd-data-mcp'
& '.\.venv\Scripts\ruff.exe' check src tests examples
& '.\.venv\Scripts\mypy.exe' src
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' -m pytest tests\test_mcp_integration.py -q
& '.\.venv\Scripts\python.exe' -m pytest tests\test_recovery_and_windows.py -q
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
      "command": "C:\\path\\to\\pdd-data-mcp\\.venv\\Scripts\\pdd-data-mcp.exe",
      "args": [
        "serve",
        "--config",
        "C:\\path\\to\\pdd-data-mcp\\config\\config.local.toml"
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
$DedicatedProfile = Join-Path $env:USERPROFILE 'Documents\pdd-data-mcp-dedicated-chrome'
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
Set-Location 'C:\path\to\pdd-data-mcp'
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

## Stage D4：推广真实只读扩展

Stage D4 已在专用、已登录且只监听回环 CDP 的 Chrome 会话上完成验收。服务复用用户已打开的页面，只连接和断开自身 CDP 通道，不启动、关闭或接管 Chrome、Context、标签页或 Profile。

数据时间和来源语义如下：

- `promotion_overview + TODAY`：账户粒度；效果来自白名单网络响应，`metric_window.end` 与 `source_updated_at` 使用同一平台响应更新时间。纠正验收中的 v3 回归快照 `s_48a017667a0c4cd891e6a447fcf7071d` 为 `[2026-09-08 00:00, 2026-09-08 12:53:56)`；它仍是日内观察，`window_complete=false`、`source_finalized=false`。
- `promotion_overview + YESTERDAY`：账户粒度；三次 `d4-account-v3` 真实采集均核对请求日期、响应业务日期为 `2026-09-07`，`endDayHour=12`，小时行严格覆盖 `0..12` 共 13 行，页面昨日截止为 `12:59`，因此半开窗口为 `[2026-09-07 00:00, 2026-09-07 13:00)`。效果来自白名单网络响应，页面 DOM 独立核对窗口截止，保存 `capture_method=MIXED`；平台更新时间保持 `null` 及明确缺失原因，`window_complete=false`、`source_finalized=false`。这是昨日部分日/同期窗口，不是完整昨日自然日或最终值。
- 旧 YESTERDAY v2 快照 `s_28300358ab6c4a76aa640362c20024d6` 把 `endDayHour=2`、小时行 `0..2` 和页面 `02:59` 错写成半开结束 `02:59`；正确半开结束应为 `03:00`。其 manifest 与 COMMIT 保持不变，追加失效记录 `i_0454ea48bc564be2980d27e1561b38bc` 后，v2 exact-scope latest 返回 `NOT_FOUND`，显式 read/list 仍以 `SEMANTICALLY_INVALIDATED` 保留审计可见性。
- `product_metrics + TODAY`：推广商品粒度；效果、实体关联和平台更新时间来自白名单网络响应。只接受上海业务日午夜起至观察时间的 TODAY 窗口，并要求平台更新时间落在该窗口内。未验证的 YESTERDAY 或更长窗口返回不可用且不提交快照。
- `promotion_configuration + POINT_IN_TIME`：推广商品粒度；保存当前观察到的配置。当前配置不回填为历史设置，也不从历史效果推断配置。
- 推广商品效果与配置通过响应中的推广、计划和商品标识做逐行关联。当前只验证了关联 ID，没有可发布的计划粒度效果来源，因此 `campaign_metrics` 保持 `UNAVAILABLE`。

使用本地受信配置启动服务：

```powershell
Set-Location 'C:\path\to\pdd-data-mcp'
$Config = '.\config\config.stage-d.local.toml'
& '.\.venv\Scripts\pdd-data-mcp.exe' doctor --config $Config
& '.\.venv\Scripts\pdd-data-mcp.exe' serve --config $Config
```

`serve` 正常由 MCP Client 作为 stdio 子进程启动。执行下面的验收客户端前，不要另开一个占用同一数据根目录的 `serve` 进程；客户端会自行启动、停止、重启服务，并通过真实 MCP 协议完成三轮独立采集、分页读回、latest、重启读回和幂等重放：

```powershell
Set-Location 'C:\path\to\pdd-data-mcp'
$RunKey = 'd4-acceptance-' + [guid]::NewGuid().ToString('N')
& '.\.venv\Scripts\python.exe' '.\examples\promotion_sync_client.py' `
  --config '.\config\config.stage-d.local.toml' `
  --connection-id 'conn_pdd_current_01' `
  --store-id 'st_pdd_current_01' `
  --captures-per-dataset 3 `
  --idempotency-prefix $RunKey `
  --limit 50 `
  --wait-seconds 60 `
  --confirm-read-only
```

该命令只读访问已验证页面和响应，不输出店铺平台标识、商品标识、商品名称、经营指标或配置值。若出现登录失效、验证码、身份不符、平台错误、结构变化或时间边界不一致，采集失败且不提交快照。

YESTERDAY v3 纠正验收使用独立客户端；它要求显式指定旧 v2 快照，以防跳过失效验证却误报 PASS：

```powershell
$RunKey = 'd4-yesterday-v3-' + [guid]::NewGuid().ToString('N')
& '.\.venv\Scripts\python.exe' '.\examples\promotion_yesterday_correction_client.py' `
  --config '.\config\config.stage-d.local.toml' `
  --connection-id 'conn_pdd_current_01' `
  --store-id 'st_pdd_current_01' `
  --idempotency-prefix $RunKey `
  --legacy-v2-snapshot-id 's_28300358ab6c4a76aa640362c20024d6' `
  --wait-seconds 60 `
  --confirm-read-only
```

该客户端已通过真实 stdio MCP 完成工具发现 6/6、三次独立 YESTERDAY v3 采集、一次 TODAY v3 回归、read/latest、keyset 历史分页、服务重启读回和幂等重放。它不会把部分日窗口写成完整昨日或最终值。

### 当前未编号的 post-D4 商品经营扩展

本轮已在当前店铺的 `/sycm/goods_effect` 页面完成只读证据核实。页面正常触发的精确 POST 响应为 `/sydney/api/goodsDataShow/queryGoodsDetailVOListForMMS`，数据就绪日期候选来自 `/sydney/api/goodsDataShow/queryGoodsReadyDate`。列表共观察 3 条并读取 3 条，商品 ID 唯一，且与最新 D2 `product_catalog` 的平台商品 ID 集合 3/3 匹配。响应中以下 6 个 base 字段及对应 6 个 `Ytd` 字段都存在：`payOrdrUsrCnt`、`payOrdrCnt`、`payOrdrGoodsQty`、`payOrdrAmt`、`goodsUv`、`goodsPv`。

这些字段的所有已观察值都使用 Unicode Private Use 字符表示，安全可解析数为 0。页面没有提供足以确认支付字段单位的可见标签；`Ytd` 只能标记为 `UNVERIFIED_COMPARISON_SUFFIX_ONLY`，不能据名称猜成“昨日”或“年初至今”。`readyDate=2026-09-07` 只保留为 ready-through 候选，不能证明字段时间口径、完整自然日或来源最终确定。页面也没有可验证的日期切换控件、最近 7 日逐日数据或流量来源分类证据。

标准 accessibility 备用核实在读取 accessibility 属性前发现可见身份正文未包含可核验的店铺 ID，按最小披露门禁以 `IDENTITY_UNVERIFIED` 停止，实际 accessibility 读取次数为 0；开发中的中间版本不作为验收证据。项目没有尝试反向解析字体、CSS、Canvas、图片或脚本，也没有把不可解释字符串保存成业务指标。

因此本轮只把页面、端点、响应形状和 3/3 商品关联记为真实证据连通，不把探针计作正式采集。`product_business_metrics` 仍为 `UNAVAILABLE`，正式三次 MCP 采集、落盘/read/latest、重启读回及幂等验收均为 `NOT_RUN`，README 不提供该数据集的采集命令。

这项工作是 D4 后的独立只读数据覆盖扩展，不重新编号或启动 D5。D5 仍只指主软件正式接入，当前为 `NOT_RUN`。

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
  _invalidations/<snapshot_id>/i_<uuid>.json
  _pointers/
  snapshots/pdd/<store_id>/<dataset_type>/YYYY/MM/DD/
    HHMMSS_mmm_s_<uuid>/
      manifest.json
      data.json | records.jsonl
      validation.json
      COMMIT.json
  audit/YYYY/MM/DD/events.jsonl
```

`captured_at` 是采集观察时间，`metric_window` 是指标口径时间，两者分开保存。正文时间带时区，目录默认按 `Asia/Shanghai` 分区。只有 COMMIT、manifest 和正文摘要全部匹配的快照才可读。语义错标使用 `_invalidations` 中的追加记录处理，不删除或改写原 manifest/COMMIT；失效快照不参与 latest，但显式 read/list 仍返回其状态和失效证据。

阶段 A/B 验收见 `docs/ACCEPTANCE_AB.md`，阶段 C 验收见 `docs/ACCEPTANCE_C.md`，D1/D2/D3 验收见 `docs/ACCEPTANCE_D123.md`，D4 验收见 `docs/ACCEPTANCE_D4.md`，本轮未编号商品经营核实见 `docs/ACCEPTANCE_PRODUCT_BUSINESS.md`，覆盖边界见 `docs/DATA_COVERAGE_MATRIX.md`，数据 Schema 见 `schemas/`。
