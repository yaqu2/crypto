# FlowDesk v2.8 · 新闻冲击与市场相对反应（只读）

**目的：** 从 v2.7 的供应商新闻哈希日志、v2.6 的分钟级录制报告，按新闻**本地首次收到**时间进行严格的 **1 / 5 / 15 分钟观察性事件研究**。这不是因果归因、预测器、委托成交或可交易净利润。

## 先决条件

- v2.7 的新闻采集必须真正配置了有效供应商 API Key，且保留了 `research/news/news_events.jsonl` 的哈希链；只有下载历史新闻不能冒充历史事前可用。
- 需要 FlowDesk 1 分钟完整 K 线观测，包含 AAOI 等个股、SPY 和同一交易日的 SMH（**SMH 只是光模块/存储的预设行业代理，并非精确行业基准**）。观察池是否有行情授权、ETF 是否成功订阅，以 Mac 实际数据为准。
- 需要整份原始 `events.jsonl` 和同一份录制产生的 `output/research/multiscale_v26.json`。v2.6 报告的默认原始文件完整性是 `False`；v2.8 **必须逐条重放原始录制**，核对原始字节哈希、事件数量、已结束录制以及所有分钟观察记录。重放只能证明本地两份文件一致，不能证明交易所推送完整、无漏报。
- `--asof` 是你发起本次研究时的真实截止时点，**必须显式给出**，不能回填为新闻供应商的发表时刻。

## 运行方法

```bash
cd /Users/quyang/Desktop/FlowDesk
bash StudyMultiScale.command --events-file /path/to/closed/events.jsonl
bash StudyNewsImpact.command --asof 2026-10-16T15:30:00Z --events-file /path/to/closed/events.jsonl
```

若你的新闻记录默认路径不是 `research/news/news_events.jsonl`，可以加 `--news-log /path/to/audited/news_events.jsonl`。若多尺度报告不是 `output/research/multiscale_v26.json`，加 `--multiscale-report /path/to/report.json`。也可以使用 `--out` 指定输出。

**注意：** v2.6 CLI 默认最多读取 500,000 条事件；超过该限制会标注截断，v2.8 会拒绝。原始录制必须正常包含结束事件；不要为了通过门禁自行修改 `run_stop` / `sourceCompleteness`。

输出是 `output/research/news_impact_v28.json`，网页“新闻事件与产业链观察”新增只读链接 `news-impact`。

## 数学口径

研究在个股新闻首次本地接收时刻 t 划定唯一事前事件。前置参考价：在 t 时**已收到且已完成**的最后一个分钟收盘，最大延迟 3 分钟。未来窗口：从新闻之后**第一个完整分钟**起，连续 1、5、15 分钟，不能插值或跳过缺失分钟。

\[r_i(h)=P_{i,t+h}^{close}/P_{i,t^-}^{close}-1\]

\[ER_{i,SPY}(h)=r_i(h)-r_{SPY}(h), \quad ER_{i,SMH}(h)=r_i(h)-r_{SMH}(h)\]

新闻前 5 分钟的 OFI 只能使用在新闻收到时**已经结束**的盘口分钟；新闻后 5 分钟 OFI 只保存在 `futureOutcomeDiagnosticsNeverModelFeature`，不得作为当时模型预测特征。断流的盘口分钟不计入，缺失不会填零。

所有参照物也必须有事前可见的参考价和事后完整、同步分钟记录。`ER` 是**简单收益差**，不表示已建立无成本 SPY/SMH 对冲。事件之后的最优/最劣分钟 OHLC 波动只能做非可成交的路径风险描述。

- 同篇修订不当成新信号，同一股票同日 60 分钟内其他新闻不给第二次独立入选机会；选择在看未来价格前完成。
- 新闻与股价同向变化**不是因果证据**；供应商在波动中集中报道也可能造成反向选择偏差。
- 没有行情、过期、漏分钟、SPY/SMH 不齐、新闻哈希被改、原始录制不完整，返回 `NO-GO` 或保留排除原因。
- 未成熟未来 1/5/15 分钟放入 `pending`，不当成涨跌失败，也不能事后只保留赢家。
- 给出“按事件日等权”的探索性统计，但样本量小、行业聚类和选择偏差会限制显著性。暂**不计算 p 值、置信胜率、声称已找到最优情绪模型**。
- 价格采用分钟 OHLC 完成时间启发式，不是交易所成交签名；**不提供真实 bid/ask、委托排队、券商成交、滑点成本；`netExecutablePnl` 必定为 `null`**。

## 下一阶段

待真实事件和行情数据积累后，先固定新闻匹配及反应阈值，再研究负对照（随机非事件时刻、同板块未报道股票）、同日聚类、新闻首次出现的供应商延迟分布、执行报价和成交校准。只有严格样本外且成本后为正，才考虑训练 CatBoost 新闻冲击模型并提出实盘申请。
