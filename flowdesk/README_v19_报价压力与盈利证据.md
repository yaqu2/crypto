# FlowDesk v1.9：全市场扫描后的买卖报价压力研究（只读）

## 为什么要升级

v1.8 的 `market_forward_panel.py` 通过两次扫描描述了异常股票未来相对 SPY 的**最新成交价变化**。它不是可以按报价成交的交易收益。本轮直接修改原工程：Moomoo `get_market_snapshot` 中的 `bid_price`、`ask_price`、`bid_vol`、`ask_vol` 现在会原样保存为有源时间的扫描数据；新增 `market_quote_stress.py` 将信号、独立延迟入场和未来退出分成**三次扫描**，在展示数量限制下测试跨价差和额外费用，绝不输出已认证实盘盈利。

**数据源含义：** Moomoo 市场快照给出的买一卖一数据不是已验证的美国跨场所 NBBO，也没有各档订单级时间戳、排队位置、可靠成交概率或实际成交回报。`update_time` 不独立证明买一卖一与最新成交价完全同步。出现有利的报价结果不意味着能拿到这些价格。没有借券确认的做空不进入模拟盈利。

## Mac 执行顺序

你的现有工程位于 `/Users/quyang/Desktop/FlowDesk`。如果你已安装 **v1.8**，请先关闭旧版 `Start.command`，再使用 v1.9 **安全补丁**。安装器只替换代码，保留 `.venv/`、`recordings/`、`output/`、`research/`、`watchlist.json`。安装器会检查是否有已经登记的封存未来实验；若存在，则拒绝原地修改，你必须在独立新文件夹中运行新版，保留原冻结实验。

先确认 Moomoo OpenD 已登录，然后在正常美股常规交易时段（美东时间 09:30–16:00，提前收盘日依日历）打开 Mac 终端。至少需要：

1. **A：信号扫描**，`bash ScanMarket.command`，在全市场股票池中预先选择观察候选；
2. **B：延迟入场扫描**，在 A **完成后 2 秒至 5 分钟内**重新运行 `bash ScanMarket.command`；
3. **C：未来退出扫描**，在 A **完成后 15–45 分钟内**再运行 `bash ScanMarket.command`；
4. **结果**：执行 `bash StudySnapshots.command`（原来的“最新成交价相对变化”）和 `bash StudyExecution.command`（新增“买卖报价+额外费用压力”）。

两次扫描只能进行 v1.8 的价格观察，**不够**做 v1.9 的独立延迟入场实验。每次扫描完成时间以 `output/market/archive` 归档时间为准；如果全市场扫描耗时太长，B 就可能错过窗口，系统会如实显示 `PENDING_NO_DELAYED_ENTRY_SCAN`。数据源在盘前、盘后、休市、断线、成交稀疏、缺少权限时不进入实验。

新增报告：

- `output/market/quote_execution_stress.json`：完整逐候选状态和 8/20/40 bps 额外费用压力，不是实盘利润。
- 浏览器原页面的「**买卖报价压力与成本实验（非成交）**」：直接查看本报告；尚未运行时返回 404，不会借用旧结果。
- `output/market/forward_panel_report.json`：v1.8 旧报告，仍然是最新成交价相对 SPY 的描述性结果，不是交易利润。

## 三次扫描的研究规则

- 仅从 A 当时成交价 $5–$5000、当天成交额 >= $200万、绝对日涨跌 2%–50% 的异常股票中选至多 25 只。阈值固定为探索性假设，**不是经过优化的参数**。
- 买涨方向 `UP_EVENT` 才评估买入-卖出报价；`DOWN_EVENT` 因无法核实借券资格，标为 `NO_GO_SHORT_BORROW_UNVERIFIED`。
- A、B、C 中必须同时有源时间有效的目标股票和 SPY 报价，且每份个股-SPY 时间偏差 <=30 秒；个股、SPY 源时间不能晚于对应扫描完成时间；B 的源时间必须晚于 A 完成，C 必须晚于 B 完成。
- B 买入使用 **ask**，C 卖出使用 **bid**；不允许从最新成交价合成缺失买卖盘。买卖盘不得交叉，单次价差不得超过 80 bps。
- 单笔仅作 1,000 美元影子名义金额，按向下取整股份，不足一股拒绝；假设占 B 最优卖量和 C 最优买量各不超过 10%，不足则拒绝。即便满足这些限制，也不能证明订单真的成交。
- 额外往返成本 8/20/40 bps：`(C.bid - B.ask) × shares - (B.ask × shares × cost_bps / 10000)`。**没有**用 SPY 相对涨跌替代实际可成交股票价格；也不按这些结果给策略排名或投资建议。
- A、B、C 的上市股票报价覆盖率各需 >=90%，但这仍不等于完整交易场所覆盖。任何少量假数据的测试只证明软件行为。
- 不因后续价格变得更好而跳过最先到达的 B/C 扫描，失败或未完成样本全部保留，并记录在 `outcomeCounts`。研究结果永远 `tradeEnabled=false`、`alphaCertified=false`、`realizedProfitVerified=false`。

## 先解决实际行情问题

你前一次 Mac 扫描成功获得 13,058 只股票目录，但 0 只有效行情，主要遇到 OTC 权限拒绝。v1.7 的 OTC 分类修复和 v1.8 的独立 SPY/QQQ/IWM 基准采集仍然保留。**如果目前批量报价尚无法成功，v1.9 不会从零行情生成交易机会。** 先检查 `output/market/latest_report.json` 中 `listedSnapshotCount`、`listedFreshUsableCount`、`batchErrors` 和 `benchmarkFailure`。

## 验收边界

本轮完成的是原工程离线自动测试、合成三次扫描、归档哈希与 HTTP 入口测试，**未连接你的 Mac OpenD**；没有完整全市场实时订单簿与成交回报，没有独立未来正净收益样本、风控交易权限或真实盈利证明。即使压力研究正收益，也必须再经历真实时段、多交易日统计、可借券判断、真实成交与资金冲击校准。
