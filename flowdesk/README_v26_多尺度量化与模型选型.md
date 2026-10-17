# FlowDesk v2.6 — 多尺度市场状态、订单流与路径风险研究（仅只读）

## 本轮确切已实现与尚未实现

**已实现：** 基于原录制 `events.jsonl` 的到达时钟微观结构聚合（book / ticker），严格限定已结束的 1m K 线的第一次可见版本；盘口 OFI 及展示规模补充**代理指标**、主动交易压力；外部宏观/新闻事实的 `published_at / first_seen_at / received_at` as-of join；基于外部确认收盘序列的未来 1/3/5 交易日回报和收盘路径回撤标签；基于完整分钟序列的未来 1/5/15 分钟 MFE/MAE、止盈/止损穿越先后不可判别的标记；v2.5 基于旧扫描归档的 1/3/5 日分离模型训练与时间净化框架。

**未实现：** 自动获取/授权分钟级国债利率或新闻收费数据；真实全市场高频 L3/MBO 事件流；将某笔显示数量增大精确归因为下单、撤单、隐藏挂单；跨市场源精确同步；真正已校准的多周期联合概率或行情预测；完整真实成交回报或实盘策略。**新增代码不下单，不生成已验证的 Alpha。**

### 选型

| 预测/分析对象 | 推荐主模型 | 对照与升级条件 |
|---|---|---|
| 市场状态（牛/熊/恐慌/修复） | 滚动收益、波动、广度、回撤等只用过去的规则；样本丰富后用在线过滤 HMM | 与简单阈值基线比较；严禁双向平滑的 HMM 事后状态投入历史实时训练 |
| 1–15 分钟价格微观压力 | OFI + tape imbalance + spread + depth → 强正则化 CatBoost / HistGBDT 或 LightGBM | 先检验买卖档报价可执行性和交易成本，真正 MBO/ITCh 数据大量积累后再考虑 DeepLOB/TCN |
| 1/3/5 日反弹概率 | CatBoost、HistGBDT、L2 Logistic 与 BASE_RATE 按时间挑战 | 最优选择以未来 Brier、logloss、校准误差决定；每个窗口与 horizon 独立，不能拿外层测试调参 |
| 上行空间和下行风险 | 历史波动、分位数回归（如 Quantile GBDT）和日间分钟路径 MFE/MAE | Pinball loss、真实区间覆盖率、Expected Shortfall、按市场状态分层 |
| 最终净 Alpha | 完整委托生命周期、成交回报校准、成交量容量曲线、组合风险优化 | `netPnL = tradePnL - spread - fees - slippage - impact - borrow - unfilled/halts`；没有回报就只做报价压力研究 |

### 本周 AI 产业链抛售研究：可测假设，不是事实结论

例如 AAOI/LITE/COHR/CIEN 和 MU/SNDK/WDC/STX 分别作为**固定探索性篮子**。比较各股相对 QQQ/SMH/XLK/XSD 的 1/5/20 日表现，确认跌幅广度、相关性、成交额、OFI、卖盘量变化、利率变化、宏观事件。先登记：什么时候算“同步崩跌”、什么时候算“卖盘衰竭”、哪些条件允许考虑反弹，事后不得根据赢家改动阈值。

这些不是历史时点准确的官方行业成分列表。需要完整 point-in-time 股票池、退市与拆分处理、历史行业归属，才允许汇报全市场规律。

## 证据如何解读

**1. 订单流 OFI**：遵照报价价位变化规则处理一档变动：`BidContribution = (newBid>=oldBid)*newBidSize - (newBid<=oldBid)*oldBidSize`; `AskContribution=(newAsk<=oldAsk)*newAskSize-(newAsk>=oldAsk)*oldAskSize`; `OFI = BidContribution - AskContribution`。只有两张订单簿快照，不能断言是新增挂单还是撤单/吃单。`displayedBidIncreaseProxy` 仅表示同价位买一展示数量较前值增加，不等于“真实补单数量”。

**2. 逐笔成交**：有明确 BUY/SELL 方向标记时计算 `signedTradeVolume / knownDirectionVolume`；UNKNOWN 方向单独统计，不瞎猜。缓存逐笔被拒；重复 `sequence` 去重。

**3. 1m K 线**：已存在于原 OpenD 客户端订阅。新模块只考虑 RTH 09:30–16:00 ET 且行情抵达时间不早于该分钟结束、时间不超过结束后 180 秒的首个记录；不回填更晚 K 线修订。仍为“到达时钟启发式完成”，不是供应商权威 final 标志，更不是官方收盘。

**4. 宏观新闻 PIT 时序**：外部记录必须携带三种带时区时间。只在 `max(published_at,first_seen_at,received_at)<=decision_at` 时入模。修订值不能回填初始事件。FRED/ALFRED 历史 vintages 有助于处理修订，但**不证明盘中某一时刻已发布**；CPI 事件用 BLS 官方发布时间与独立的供应商接收时间。

**5. 风险路径**：1/3/5 日研究 verified close-to-close 的未来回报与“已观察收盘路径”回撤，并不包括真实日内最低点。1/5/15 分钟路径以每一分钟的未来 OHLC 估计有利/不利摆动，但当止盈止损都在同一分钟触发，返回 `UNKNOWN_BOTH_DURING_WINDOW`，不得假定顺序。

### 推荐候选宏观上下文

- 交易 ETF：`US.SPY`, `US.QQQ`, `US.SMH`, `US.SOXX`, `US.XLK`, `US.XSD`, `US.IWM`（取决于订阅与授权，ETF 列表可扩展）；各标的使用各自的源时钟，新鲜度不达标就拒绝同步研究。
- 利率：UST 2Y/10Y 真正盘中收益率或可比期货变动。FRED `DGS2`,`DGS10` 是**日频指标**，不应拿最终日值在当天早盘预测。
- 经济：BLS CPI、就业、FOMC 事件 `actual - previously archived consensus` 标准化，不能把事后修改的市场预期作为事前预期。
- 新闻：原始链接、发布机构、首次公开时间、vendor first seen、local receive、修改版本、实体及事件类型；LLM 仅用于提取具有原文溯源的类型，不允许生成缺失的时间。

### 1/3/5 日概率和风险标签

现有 `multihorizon_runner.py` 分别调用原有 v2.5 模型比较器：

- 1日：板块中位回报 `>=1%` 且跑赢 SPY `>=0.75%`；
- 3日：回报 `>=2.5%` 且跑赢 SPY `>=2.0%`；
- 5日：回报 `>=4.0%` 且跑赢 SPY `>=2.5%`。

这些只是**事先指定的工程默认研究阈值**，不代表最优策略。1/3/5日固定终点事件不是嵌套事件，概率不要求大小单调。3组模型独立训练、净化、校准及外层测试；尚无多任务联合训练/协同校准的验证结果。

## 验证准则（事前固定）

1. 数据准入：本地录制缺序号/丢数据、无源时钟、分钟缺口、异步 ETF、宏观信息发布时间不明、历史 PIT 股票池不足，一律作为诊断不当作精确信号。
2. 多层次时间分割：按交易日分组时间切块，若最长预测 5 日则净化隔离至少大于标签跨度，所有超参、状态阈值及特征选择只能在内层开发；外层测试结果不能重新选择模型并复用为未污染成绩。
3. 状态验证：只比较当时可识别的状态（HMM **filtered** probabilities），按牛/熊/恐慌、利率冲击日/普通日、开盘/午盘/尾盘分层报告样本量、Brier/logloss、预测校准、MAE/MFE 和回撤。
4. 成本验证：与独立基线（SPY/SMH、同板块等权、随机候选、规则基线）比较；模拟只用信号以后实际可见的报价，并加入多种滑点、冲击、借券、停牌、成交概率和组合资金约束；真实 Alpha 认证最终必须用券商回报。
5. 决策原则：优先选稳健且可执行、以 OOS `Brier/logloss/net PnL/risk` 为证据的简单模型。Transformer/DeepLOB 只有在高频数据授权、样本量、交易成本和严格外层测试都足够时才能进入竞争。

## 用法（Mac）

从 v2.5 安装**增量补丁**（只添加文件，需先关闭原程序）后：

```bash
cd /Users/quyang/Desktop/FlowDesk
bash StudyMultiScale.command --events-file /path/to/complete/events.jsonl
bash StudyMultiHorizon.command
```

没有足够未来历史归档时第二条命令会生成 3 组 `NO_GO_ARCHIVE_INTEGRITY` 报告，这是正确行为。

或导入受独立证明完整性及最终收盘状态的单个标的日频 JSONL：

```json
{"symbol":"US.AAOI","day":"2026-10-15","close":100.0,"close_verified":true}
```

然后执行：

```bash
bash StudyMultiScale.command --daily-file /path/to/verified_daily_closes.jsonl
```

> `close_verified: true` 是数据导入合同，不是软件凭此自动核实第三方真实性；须由可信行情供应商的正式收盘、企业行动处理和交易日历来支撑。**不允许把原 15:30 扫描价格直接换名成已验证收盘。**

导入宏观或新闻 as-of JSONL，每行：

```json
{"type":"macro","key":"US10Y","value":4.6,"provider":"licensed_provider","source_id":"provider_document_id","published_at":"2026-10-15T20:00:00Z","first_seen_at":"2026-10-15T20:00:01Z","received_at":"2026-10-15T20:00:02Z","revision":0}
```

```bash
bash StudyMultiScale.command --context-file /path/to/pit_context.jsonl --asof 2026-10-15T20:01:00Z
```

以上为**格式示例，并非已取得的市场观测值**。不可凭空填写发行/接收时间。完整报告：`output/research/multiscale_v26.json`，多周期模型报告：`output/research/v26_multihorizon/`。报告只能研究，不能触发真实交易。

## 数据来源限制与下一步

- OpenD 原来已订阅 `QUOTE`, `K_1M`, `ORDER_BOOK`, `TICKER`，但深度数据只在 `deepSymbols` 观察池。Moomoo 行情推送不等于覆盖所有上市股票的全部委托/成交。要真正研究撤单、补单和价格队列优先级，需明确具体交易所的逐笔增删改/成交事件和序列完整性，付费授权后再开发适配层。
- 本轮**没有从新闻或利率提供商自动获取真实时点数据**；需经过合规数据许可、凭据和协议兼容审查。绝不利用事后修订数据伪装实时已知。
- 目前提供的旧 `events.jsonl` 是未封存的前缀；没有 `run_stop` 和完整分段哈希。即使能算出来分钟 OFI，也不能作为已认证策略表现。

## ETF / 行业观察池（不自动更改实时数据权限）

`bash ProposeSectorWatchlist.command` 生成包含 SMH、SOXX、XLK、XSD、IWM 以及 CIEN、MU、SNDK、WDC、STX 的**候选观察池 JSON**。它保留现有 `deepSymbols`，不会改变 `watchlist.json`、连接 OpenD、申请行情或触发交易。正式激活时应经过原工程的订阅额度与人工审批流程。
