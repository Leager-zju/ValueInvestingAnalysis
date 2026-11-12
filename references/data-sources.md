# 数据获取与时效校验

## 0. 总原则

1. **优先走已连接的结构化数据源**（下表的 `data_*` 是能力占位符，指你所用平台提供的行情/财务数据工具；平台不同名字可能不同，见 `platform-adapters.md`）。命中能力域时不要用网页搜索或记忆替代。
2. 每个数字都要能回答三个问题：**来源**、**时点**、**口径**（TTM / 累计 / 单季 / 静态）。
3. 取不到就写"未取到"，并说明对结论的影响。**禁止用近似值、记忆值或推算值冒充实测值。**
4. 多个无依赖的查询应在同一轮并行发出，不要串行等待。

## 1. 工具映射表

| 需要什么 | 工具 | 关键参数 | 关键返回字段 |
|---|---|---|---|
| 代码确认 | `data_search` | 关键词 | `code`/`name` |
| 实时快照 | `data_quote` | `code` 或 `codes` | `price` `prev_close` `open` `high` `low` `volume` `amount` `volume_ratio` `turnover_rate` `pe_ratio` `pe_lyr` `pe_fwd` `pb_ratio` `dividend_ratio_ttm` `total_market_cap` `high_52week` `low_52week` `chg_5d/10d/20d/60d/ytd` `lot` `price_ceiling` `price_floor` `time` |
| 日K（量价、均线） | `data_kline` | `period=day` `fq=qfq` `limit=120` | `nodes[]`：`date` `open` `high` `low` **`last`（收盘）** `volume`（手） `amount` |
| 技术指标 | `data_technical` | `group=kdj,macd,rsi` | `macd{DIF,DEA,MACD}` `kdj{KDJ_K,KDJ_D,KDJ_J}` `rsi{RSI_6,RSI_12,RSI_24}` |
| 三大报表 | `data_finance` | `type=income\|balance\|cashflow` `num` `fields=core` | `OperatingRevenue` `NPParentCompanyOwners` `ROE` `ROETTM` `GrossIncomeRatio` `NetProfitRatio` `EPSTTM` `DividendTTM` `NPParentCompanyYOY` `ORComGrowRate3Y` `EndDate` `InfoPublDate` |
| 公司概览/所属板块 | `data_profile` | `code` | 主业、行业、上市信息 |
| 卖方一致预期 | `data_consensus` | `code` | 未来 1–3 年 EPS/净利预测 |
| 研报 | `data_report` | `symbol` `mode=list` | 评级、目标价、核心观点 |
| 新闻/资讯 | `data_news` | `symbol` `type=2`（0公告 1研报 2新闻 3全部） `limit` | `title` `time` |
| 公告 | `data_notice` | `symbol` `limit` | 财报、重大事项 |
| 分红/回购/股本 | `data_dividend` `data_events` | `code` `type=dividend\|buyback\|shares` | 每股分红、除权日 |
| 资金流向 | `data_fund_flow` | `code` | 主力/超大单净流入 |
| 行业景气 | `data_sector` | `mode=constituent\|valuation\|forecast\|ranking\|info` | 行业估值、盈利预测、涨幅排名 |
| 大盘环境 | `data_market_overview` | `type=summary\|updown\|valuation\|rotation` | 涨跌分布、成交、市场估值 |
| 多因子诊股 | `data_score` | `code` | 各维度评分（仅作交叉验证，不作决策依据） |
| 交易日校验 | `data_trade_calendar` | `market=hs\|hk` `date` | 是否交易日 |
| 情绪热度 | `data_hot`（`stock`/`news`） | — | 热搜榜（辅助，不能单独构成结论） |

**降级顺序**（前一级不可用才降到下一级，切换时必须说明换了源）：

1. 平台已连接的**结构化行情数据源**（`data_*` 或等价工具，字段要求见第 1 节）
2. **结构化检索能力**（`domain_search` 或平台等价工具）：用一句中文检索意图直接拿结构化表（如"查询某股最近 60 个交易日日K与最新财务"），返回的原始表可直接用；K 线需自行落成 `{"market","elapsed_minutes","nodes":[...]}` 或 `{"nodes":[...]}` 再喂给 `volprice`
3. **网页检索能力**（`WebSearch` 或等价工具）：能拿到数值与发布日期即可，必须标注来源与时点
4. 平台若另装了金融数据技能，可作为补充；需要授权的一律跳过，不要在无人值守场景尝试扫码
5. 明确报告"未取到"，并说明对结论的影响

各层在不同 Agent 里的等价能力名见 `references/platform-adapters.md`。工具名只是占位符，**不要用"平台里没有这个工具"当作跳过取数的理由**。

**定时任务（无人值守）注意**：数据源工具在自动化上下文中**可能未挂载**，需要授权的工具也可能返回 `AUTH_REQUIRED`。此时**直接走第 2/3 级检索能力**，不要反复重试或尝试授权，也不要把取数失败写成"没有数据"。连续两个源失败即降级，不要死磕。

## 2. 政策与行业事件

- 结构化数据源不覆盖政策，用平台的网页检索能力（`WebSearch` 或等价工具，必要时加时间过滤，如近 7 天）检索：行业监管、价格机制、补贴/关税、招标、区域政策、指数调整。
- 每条政策必须写：**发布主体 + 发布日期 + 对该公司的影响路径（量/价/成本/折现率）**。
- 无法核实来源的传闻一律标为"未证实"，不得作为交易依据；只能写入"待验证的风险项"。
- 不引用股吧、雪球帖子、自媒体解读作为事实来源；可引用但必须标注为"市场情绪样本"。

## 2.5 全球利率：10 年期美债收益率

折现率因子的唯一必取项（规则见 `drivers.md` §2.6、公式见 `fundamental-standard.md` §3.4）。

**取什么**

| 指标 | 口径 | 频率 |
|---|---|---|
| 美国 10 年期国债基准收益率（CMT，%） | **纽约尾盘/收盘**，不要用盘中值 | 日度 |
| 美国 2 年期国债收益率（%） | 同上 | 日度 |
| 2Y–10Y 利差（bp） | 10Y − 2Y，自行相减并注明 | 日度 |
| 美元指数 / 人民币中间价 | 收盘 | 日度 |

**去哪里取**（按序降级，切换必须说明换了源）

1. `domain_search`：一句话意图，如"查询最新 10 年期美债收益率与 2 年期收益率"；
2. `WebSearch`：命中新华财经、华尔街见闻、每日经济新闻、金十数据的**当日快讯**最可靠，标题里通常直接带数值与"纽约尾盘/收盘"字样；
3. 取不到时写"未取到"，**不得用记忆值或"大约 5%"之类的估算顶替**，并在报告里说明"本次未做利率修正"。

**时点换算（必须双标）**

美债收盘对应北京时间**次日**凌晨：美东 16:00 = 北京 04:00（夏令时）/ 05:00（冬令时）。因此：

- A 股早盘（9:30）能拿到的是**隔夜美债收盘**，这是当日唯一可用的日频读数；
- 报告里写"10Y 4.83%（2026-11-05 纽约尾盘，对应北京 11-06 凌晨）"，不要只写日期，否则跨时区会错一天。

**两个常见口径坑**

- **尾盘 vs 收盘**：不同源给出 4.83 与 4.84 属正常差异（尾盘报价 vs 官方收盘），**同一份报告内只用一个口径**并注明；
- **盘中值不是当日值**：亚洲时段看到的"最新"往往是美债电子盘报价，波动大，**不能写进当日的 `latest_value`**。

## 3. 时效校验（每份报表都必须做）

| 场景 | 校验动作 |
|---|---|
| 任何快照 | `quote.time` 是否等于当日；不等于则视为过期数据，须明示 |
| 早盘 9:30 | 集合竞价 9:25 已撮合，`open` 为**真实开盘价**；9:25 之前 `open` 可能是上一日或虚拟值，不可当成交价。核对 `prev_close` 是否为昨收、`volume/amount` 是否为竞价匹配量级 |
| 午盘/尾盘 | 用 `volume_ratio`（量比）与已成交分钟数做全天量能折算，折算方法见 `volprice` 脚本，不要手算 |
| 财报 | 区分 `EndDate`（报告期）与 `InfoPublDate`（披露日）。估值只能用**已披露**数据；未披露的预期走 `data_consensus` 并明确标注"预测值" |
| 一致预期 | 标注预测年份与样本机构数，不与实际值混列 |
| 新闻 | 保留 `time`，隔夜消息与盘中消息分开列；超过 48 小时的"消息"不能作为当日触发条件 |
| 港股 | 无涨跌停，`price_ceiling/price_floor` 为空；每手股数取 `quote.lot`；注意 12:00–13:00 午休与 16:00 收市竞价 |

## 4. 常见陷阱

- **A股 `volume` 单位是手**（1 手 = 100 股），港股 `volume` 是股。比较量能时只用同源同市场的量比，不跨市场比绝对量。
- `data_kline` 默认**按日期倒序**返回，喂给 `volprice` 前脚本会自动排序，但人工核对时不要看反。
- `pe_ratio` 为静态/TTM 口径，周期股在盈利高点 PE 反而最低，**周期行业必须用 CAPE 或 PB**，不能直接用 PE 判贵贱。
- `dividend_ratio_ttm` 是股息率（%），不是每股分红。
- 复权口径：判断长期趋势用 `qfq`，判断当日真实价位用不复权；同一张报表内不要混。
