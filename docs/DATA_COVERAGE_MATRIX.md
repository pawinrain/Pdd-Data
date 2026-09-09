# PDD Data MCP 数据覆盖矩阵

- 基准日期：2026-09-08
- 当前内部店铺：`st_pdd_current_01`
- 当前阶段：Stage D4 推广基线及 YESTERDAY v3 口径纠正已完成；未编号 post-D4 商品整体经营只读核实已收口，页面/端点/响应形状证据通过，但正式能力因语义和格式门禁未满足而未启用
- 明确不在本轮：Stage D5 主软件正式接入、AI/模型调用、ExecutionJob、任何平台业务写操作

## 状态定义

| 状态 | 含义 |
|---|---|
| 已重复真实验收 | 已对当前内部店铺完成页面/响应核验，并以同一准确 scope 完成三次独立真实采集及 MCP 全链路验收 |
| 已真实连通（未重复验收） | 已完成一次真实页面/响应、MCP 落盘与读回，只证明该准确 scope 可连通，不等于三次重复稳定性验收 |
| 页面/响应证据已核实，正式能力未启用 | 已真实核实入口、精确响应或形状，但字段或时间语义不足，未进入正式 MCP 采集、落盘和重复验收 |
| NEED_SAMPLE | 本地导入预检在授权范围内没有找到合格真实原始报表；没有样本时停止实现，不表示导入能力通过 |
| 已授权待页面核实/待实现 | 用户已授权本轮只读核实与实现，但尚未取得足够页面证据，不能发布字段或 capability |
| 待实现 | 已属于既定阶段或已列入后续范围，但当前实现尚未完成 |
| 当前店铺无数据 | 页面和权限均可用，且已证实当前店铺在对应范围确实返回空数据；不能用未打开页面或未授权代替 |
| 权限不可用 | 已证实登录账号缺少对应只读权限；不能用页面缺失或采集失败推断 |
| 尚未核实 | 目标或入口尚未获得足够真实证据，字段名称仅为目标，不能视为可读能力 |

> “目标字段”列描述期望核实的业务字段。只有状态为“已重复真实验收”的行，才表示“已验收字段”子项已经过三次独立真实验收；“已真实连通（未重复验收）”仅证明一次准确 scope 的链路。“已授权待页面核实/待实现”及其他未验收状态均不得把目标字段作为 capabilities 中的可读能力发布。

### D4 收口标记

| 标记 | 含义 |
|---|---|
| 本轮已验收 | 页面证据、严格 Schema、适配器及真实 MCP 全链路均已通过；只在已验收的实体和时间范围内发布 capability |
| 一次真实连通 | 页面证据和一次真实 MCP 链路通过，但尚未完成同一准确 scope 的三次重复验收 |
| 本轮未启用 | 页面上可能存在入口或历史证据，但尚未通过当前严格契约与正式 MCP 验收，不发布为可用 capability |
| 当前新授权范围 | 不重新编号 D4/D5；仅按本轮授权核实商品整体经营与流量，页面证据不足时继续保持不可用 |

## 覆盖矩阵

| 数据域 / 建议数据集 | 状态 | 目标字段或已验收字段 | 真实页面入口 | 实体粒度 | 关联 ID | 可用时间范围 | 指标口径 / 时间语义 | 采集来源 | 当前证据与限制 |
|---|---|---|---|---|---|---|---|---|---|
| 店铺经营概况 / `store_overview` | 已重复真实验收 | **已验收：** `gmv`、`order_count`、`visitor_count`、`page_view_count` | 商家后台首页 `/home` | 店铺 | 内部 `store_id`；平台店铺身份仅保存指纹证据 | `TODAY` 截至平台更新时间 | 当日 00:00 至平台显示的实时更新时间；窗口未完成、来源未最终结算 | 已验证 DOM 备用读取 | D1 既有验收与本轮真实回归均 PASS；不包含转化率、退款指标，缺失不填 0 |
| 商品档案 / `product_catalog` | 已重复真实验收 | **已验收：** 平台商品 ID、商品名、上下架状态、SKU 数 | 商家后台商品列表 `/goods/goods_list` | 商品 | `platform_product_id` | 当前时点 | 当前列表状态，不代表历史状态 | 精确白名单网络响应 + DOM 总数核对 | D2 既有验收与本轮真实回归均 PASS；DOM 总数核对已恢复通过；价格、创建/发布时间仍未核实 |
| 商品总库存 / `inventory` | 已重复真实验收 | **已验收：** 商品 ID、商品总库存、`PRODUCT` 粒度、观察时间 | 商家后台商品列表 `/goods/goods_list` | 商品 | `platform_product_id` | 当前时点 | 当前商品总库存；真实 0 与缺失 `null` 分开 | 精确白名单网络响应 + 商品集合核对 | D3 既有验收与本轮真实回归均 PASS；不能解释为 SKU 库存 |
| 推广账户效果 / `promotion_overview` | TODAY 已重复真实验收并完成一次 v3 回归；YESTERDAY v3 部分日窗口已重复真实验收 | **已核实字段：** 概况页 `queryHourlyRangeReport` 的 `spend`、`orderSpend`、`gmv`、`netGmv` 为 `YUAN`（Schema 保存为整数分）；`orderSpendRoiUnified`、`orderSpendNetRoi`、`settlementRoi` 为 `PER_ONE`；`netOrderNum`、`orderNum`、`impression`、`click`、`settlementOrder` 为整数 | 推广后台概况页 `/mains/promotionOverview` | 推广账户 × 单日或同期期限窗口 | 当前店铺身份指纹；未发现可验收的独立推广账户 ID | `TODAY`；`YESTERDAY` 的已验收部分日 scope | TODAY 为当日部分窗口；三次 YESTERDAY v3 均为业务日 `2026-09-07`，请求 `endDayHour=12`、小时行 `0..12` 共 13 行、页面截止 `12:59`，半开窗口为 `[00:00,13:00)`。两者均 `window_complete=false`、`source_finalized=false`；累计值不跨采集相加 | 精确白名单网络响应；YESTERDAY 的请求/小时覆盖来自响应，窗口截止另由页面说明 DOM 核对 | TODAY 保留三次既有真实 MCP 基线并完成一次 v3 回归。YESTERDAY v3 已完成三次独立真实 MCP 采集及 read/latest/keyset 分页/重启/幂等验收，但只证明该部分日 scope，不是完整昨日或最终值。旧 v2 `s_28300358ab6c4a76aa640362c20024d6` 的 `02:59` 半开结束错标已用追加记录 `i_0454ea48bc564be2980d27e1561b38bc` 失效；原 manifest/COMMIT 不变，v2 latest 为 `NOT_FOUND`，read/list 保留审计可见 |
| 推广计划效果 / `campaign_metrics` | 尚未核实 | **目标：** 可验证计划 ID，以及计划粒度页面实际提供的效果字段、统计窗口、单位和来源 | 推广商品页未发现计划入口；报表页 `/goods/report/promotion/overView` 当前仅见“推广商品” | 目标为计划 × 统计日/窗口；当前未证明该维度 | `planId` 仅作为已验收推广商品记录的外键；不得据此推导计划粒度指标 | 尚未核实 | 报表默认请求只证明推广商品维度，不能证明计划维度无数据 | 页面与网络元数据证据；计划粒度业务响应尚未核实 | 当前 capability 为 `UNAVAILABLE`；不标“当前店铺无数据”，也未启用计划适配器或 Schema |
| 推广商品效果 / `product_metrics` | 已重复真实验收 | **已验收：** `spend`、`orderSpend`、`gmv`、`netGmv`=`YUAN`（Schema 保存为整数分）；`orderSpendRoiUnified`、`orderSpendNetRoi`、`settlementRoi`=`PER_ONE`；`netOrderNum`、`orderNum`、`impression`、`click`、`settlementOrder`=整数；`reportLastUpdateTime` 为来源更新时间 | 推广商品页 `/goods/promotion/list`；精确响应 `/mms-gateway/venus/api/goods/promotion/v3/list` | 推广商品 × 请求统计窗口 | `adId` + `planId` + `goodsId` + `mallId`；`goodsId` 与当前 D2 商品目录关联；`planId` 只作外键 | 仅 `TODAY` | 当日 00:00 至采集时间；部分窗口，`window_complete=false`、`source_finalized=false`；响应来源更新时间必须落在统计窗口内；累计值不跨采集相加 | 精确白名单网络响应 + DOM 名称唯一性核对 | 当前店铺已完成三次独立真实 MCP 采集；严格分页、去重、关联、落盘/读回、latest、重启读回和幂等重放均通过；`YESTERDAY` 及更长窗口未启用；`sumReportInfo` 只做响应形状核验，不落盘且不发布为账户概况 |
| 当前投放配置 / `promotion_configuration` | 已重复真实验收 | **已验收：** 推广商品当前观察字段 `maxCost`=`YUAN`（Schema 保存为整数分）、`targetRoi`=`PER_ONE`、`agentBid`=`PER_ONE` 或 `null`、`adStatus`=整数；账户/计划配置字段仍尚未核实 | 推广商品页 `/goods/promotion/list` | 推广商品当前观察状态；账户/计划粒度尚未核实 | `adId` + `planId` + `goodsId` + 店铺身份；`goodsId` 与 D2 `platform_product_id` 关联 | `POINT_IN_TIME` | 字段名保持平台语义；`maxCost` 不改写为已验证“日预算”；当前设置不回填为历史设置；记录观察时间、来源与缺失原因 | 精确白名单网络响应；只读观察 | 当前店铺已完成三次独立真实 MCP 采集，落盘/读回、latest、重启读回和幂等重放均通过；未点击编辑、启停或保存；`agentBid=null` 保持缺失，不能填 0 |
| 推广历史窗口与逐日数据 / `promotion_overview`、`product_metrics` | 混合状态 | **已核实（仅限所列范围）：** 账户 TODAY 三次既有验收并完成一次 v3 回归、账户 YESTERDAY v3 部分日 scope 三次重复验收、推广商品 TODAY 三次重复验收；v3 纠正批次分别保存请求日期、响应业务日期、小时覆盖、窗口边界、来源更新时间及完成状态 | 概况页与推广商品页的只读日期区域 | 账户/推广商品 × 单日或同期期限窗口 | 店铺身份 + 对应推广实体 ID | 账户：TODAY、YESTERDAY 部分日；推广商品：仅 TODAY | 账户 YESTERDAY v3 为 `[2026-09-07 00:00, 2026-09-07 13:00)`；它不是完整自然日或最终值。当前证据不能外推为多日逐日、完整昨日终值或历史配置 | 页面正常触发的精确白名单响应；不构造请求；页面 DOM 独立核对 YESTERDAY 截止 | 推广商品 YESTERDAY 未通过启用门禁；近 7/30/90 天均未启用。页面快捷选项不等于可读能力；旧账户 v2 错标快照只作失效审计历史 |
| 商品整体经营与流量 / `product_business_metrics` | 页面/响应证据已核实，正式能力未启用 | **目标、尚未证明可读：** 商品 ID、支付买家、支付订单、销量/支付件数、支付金额、访客、浏览量；12 个 base+Ytd 字段名已观察，但数值格式和单位未核实 | 商品经营页 `/sycm/goods_effect`；精确 POST `/sydney/api/goodsDataShow/queryGoodsDetailVOListForMMS`；ready 候选 `/sydney/api/goodsDataShow/queryGoodsReadyDate` | 响应候选为商品行；正式日/窗口粒度未确认 | 响应商品 ID 与最新 D2 `product_catalog` 3/3 匹配；真实 ID 不输出 | 无已启用范围；YESTERDAY、最近 7 个已结束自然日和流量来源均不可用 | 所有已观察指标值均含 Unicode Private Use 字符，安全可解析数为 0；`Ytd=UNVERIFIED_COMPARISON_SUFFIX_ONLY`；`readyDate` 仅是 ready-through 候选；无可验证日期控件、7 日逐日证据、来源分类或支付单位，不能建立完整自然日、最终值或推导转化指标 | 项目自身 Playwright Python + CDP 的精确响应形状及零业务写 DOM 核对；accessibility 最终探针因可见身份正文未包含可核验店铺 ID 而以 `IDENTITY_UNVERIFIED` 停止，accessibility reads=0 | 枚举、候选合约/parser/导出 Schema 仅用于 fail-closed 证据约束，不是正式可落盘 payload；capability=`UNAVAILABLE`、supported windows=[]、verified=false，正式三次 MCP 采集/read/latest/重启/幂等=`NOT_RUN` |
| 商品整体经营本地报表导入 / `product_business_metrics`（受控 CLI 候选） | **NEED_SAMPLE；未实现、未验收** | 只允许未来真实样本中已核实名称、定义、数值、单位和缺失规则的字段；当前实际可用字段为 0，至少一个非标识经营指标通过预检后才可继续 | 受信本地原始报表；项目内当前没有合格样本，未核实平台导出入口，不提供猜测的菜单路径 | 目标为商品 × 一个明确的已结束自然日；以样本证据为准 | 商品 ID 必须按标识符原样保存并与当前店铺目录核对；当前无文件可核对，不按名称强行关联 | 当前无已验收范围；首个候选仅限样本证明的一个已结束自然日，七日汇总不得拆成逐日 | 文件生成时间与业务统计时间分开；金额单位、未知缩写、空值、公式缓存、汇总行和最终性均需样本证据，缺失不填 0 | 未来应明确标记为本地文件导入，不得冒充 DOM/网络采集；原文件保留并记录校验值 | 授权目录内无真实商品经营原始报表或导出条件记录，因此 dry-run、CLI、正式 Schema/Validator、验收数据根、快照、stdio MCP 读回、latest/分页/重启/幂等均 `NOT_RUN`；自动采集 capability 仍为 `UNAVAILABLE`，本地导入不能改变其状态 |
| 活动目录及报名状态 / `activity_catalog` | 尚未核实 | **目标：** 活动 ID、名称、类型、报名状态、商品关联、报名/生效时间 | 商家后台活动/营销页面，具体入口待核实 | 活动或活动 × 商品 | 活动 ID + `platform_product_id` | 当前状态及平台明确提供的历史 | 报名状态是观察时点状态，不从页面缺失推断退出或删除 | 待核实 | 仅预留枚举；本轮不打开报名流程、不提交报名、不进入 D5 |
| SKU 价格与库存 / 后续 SKU 级 `product_catalog`、`inventory` | 尚未核实 | **目标：** SKU ID、规格、售价/活动价及单位、SKU 库存、观察时间 | 商品编辑/规格详情的只读区域，具体入口待核实 | SKU | `platform_product_id` + 可验证 `sku_id` | 当前时点 | 价格必须确认单位与价格类型；库存缺失不填 0 | 待核实 | 当前只验收商品总库存与 SKU 数；不得打开或保存商品编辑表单来获取数据 |
| 交易、售后与结算 / 后续数据集 | 尚未核实 | **目标：** 订单/售后/结算记录及状态、金额、时间、商品关联、分页总数 | 订单、售后、资金/结算页面，具体入口待核实 | 订单、售后单、结算单或明细行 | 平台订单/售后/结算 ID + 商品/SKU ID | 尚未核实 | 下单、支付、退款、结算时间和金额口径必须分开；权限与隐私边界需单独验收 | 待核实 | 不在 D4；可能涉及更高敏感度和权限，未获得页面证据前保持不可用 |
| 外部成本输入 / 后续本地数据集 | 待实现 | **目标：** 商品/SKU 成本、物流/包装等成本类型、币种、适用起止时间、来源说明 | 无拼多多页面入口；应由受控本地输入提供 | 商品/SKU × 成本版本 | 内部商品映射 + `platform_product_id` / `sku_id` | 用户声明的生效窗口 | 外部成本不是平台事实，必须标记来源并版本化；不得写回拼多多 | 本地人工/系统输入，方案待定 | 不在本轮 D4，也不接入 AI；尚未定义写入权限、Schema 与验收流程 |

## D4 基线与本轮 post-D4 收口边界

1. 仅 `st_pdd_current_01` 用于本轮正式 D4 验收；旧店铺快照只作为历史基线，不参与 latest 或验收计数。
2. 专用浏览器、CDP、店铺身份与严格白名单门禁均通过；验收过程只读取页面和网络响应，未关闭或接管用户已有标签页。
3. 三类既有基线各完成三次独立真实采集：账户 `TODAY`、推广商品 `TODAY`、推广商品当前配置 `POINT_IN_TIME`。本次账户 YESTERDAY v3 部分日 scope 另完成三次独立真实采集，并完成一次 TODAY v3 回归；新批次通过 MCP 落盘/read/latest、keyset 历史分页、服务重启读回和原幂等键重放。
4. 账户 `TODAY` 与三次 `YESTERDAY [00:00,13:00)` 均保存为 partial/non-finalized；后者属于昨日同期/部分日，不是昨日完整自然日。旧 v2 `02:59` 半开结束是语义错标，现已追加失效但不删除或改写历史。推广商品仅启用 `TODAY`；推广商品 `YESTERDAY`、近 7/30/90 天以及历史配置均未启用，不能由页面选项或当前设置外推。
5. `campaign_metrics` 当前为 `UNAVAILABLE`；`planId` 只作为推广商品记录的外键，不证明计划粒度指标或计划配置可读。`sumReportInfo` 只经过响应形状核验，不落盘，也不作为 `promotion_overview` 账户概况发布。
6. D1/D2/D3 的既有真实验收保持有效；本轮又各完成一次真实 MCP 回归并全部 PASS。最新 D2 目录与商品经营响应的商品 ID 集合 3/3 匹配；一次回归不冒充三次新的重复验收。D4 没有改变旧系统的默认数据读取路线。
7. 商品整体经营与流量的真实入口、精确端点和响应形状证据已核实，但 12 个 base+Ytd 字段均为不可安全解释的 Private Use 字符，Ytd、支付单位、7 日范围和来源分类也未核实。`product_business_metrics` 保持 `UNAVAILABLE`；页面探针不计作正式采集，正式三次 MCP 验收为 `NOT_RUN`。
8. 遇到登录失效、验证码、权限提示或风险提示立即停止，不重放请求、不导出令牌、不绕过限制。
9. 交易、售后、结算、活动和外部成本仍超出 D4，不因本轮推广页面发现而扩展授权。
10. Stage D5 仍只指主软件正式接入，本轮没有启动或重新定义，状态为 `NOT_RUN`。
11. 本地报表导入预检仅检查项目内授权证据范围，没有找到真实商品整体经营原始报表，结论为 `NEED_SAMPLE`。没有开发导入器、修改依赖或创建快照；该状态与自动采集的 `UNAVAILABLE` 分开记录，且不构成导入能力 PASS。
