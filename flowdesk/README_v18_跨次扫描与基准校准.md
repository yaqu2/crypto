# FlowDesk v1.8 · 全市场基准校准与跨次扫描研究

## 这轮研究解决的问题

v1.7 的 Moomoo 股票目录/批量快照可以扫描 NYSE、Nasdaq、AMEX 等交易所当前挂牌股票，但**单次异常幅度不等于未来 Alpha**，而且 `SecurityType.STOCK` 的股票目录**不能保证**包含 SPY、QQQ、IWM 这样的 ETF 基准。本版在原项目增加三个实际变化：

1. **独立市场基准采集。** `ScanMarket.command` 仍先对支持的当前美股上市股票分批获取快照，另通过 Moomoo 的批量报价接口单独请求 `US.SPY`、`US.QQQ`、`US.IWM`。行情权限失败会记录在 `benchmarkFailure`，不会被解释成上涨/下跌信号。股票与 ETF 的覆盖分母完全分离。
2. **完整来源归档。** 每次扫描归档 `us_stock_universe.csv`、`latest_quotes.csv`、`latest_report.json`，有基准采集时还归档 `latest_benchmarks.csv` 并加入 SHA-256。缺少文件、改动基准或删去整次归档都进入研究门禁。**本地哈希链不是第三方时间公证，也不能阻止有权限者整体替换资料**。
3. **跨次扫描的先选后检。** 第一次扫描只使用当时的股票价格、当日变化和成交额固定观察候选（不读取未来）。之后只有当同一美东交易日出现**15–45 分钟后**的独立归档扫描，才对照 SPY 计算未来市场相对价格变化。每组个股/基准时间戳最多相差 30 秒、相对于扫描完成时间不超过 45 秒。不满足条件的机会标为 `PENDING`、`MISSING`、`UNSYNC` 等，不能悄悄删除。

`market_forward_panel.py` 的预设观察阈值仅为探索性规则：股价 $5–5000、累计成交额至少 $200万、较前收盘绝对变化 2%–50%，每次最多 25 个异常股票。**这些不是最优参数，更不是实盘买卖策略。** 不允许因为后来出现一个很高的收益而改动起始扫描的名单。

## 在你的 Mac 上使用

你的现有安装目录是 `/Users/quyang/Desktop/FlowDesk`。如使用**安全补丁**，先关闭 `Start.command` 与扫描终端；若存在已冻结的未来试验，**不要在原地升级**，改为独立新文件夹并保留原运行。升级后打开已登录的 Moomoo OpenD，Terminal 执行：

```bash
cd /Users/quyang/Desktop/FlowDesk
bash ScanMarket.command
```

在同一个美东正常交易时段，再过 **15–45 分钟**运行一次：

```bash
bash ScanMarket.command
bash StudySnapshots.command
```

观察结果保存在：

```
output/market/latest_report.json
output/market/latest_benchmarks.csv
output/market/archive/
output/market/forward_panel_report.json
```

你可以在运行中的 FlowDesk 浏览器页面点击 **「跨次扫描未来表现（非交易净收益）」** 打开报告，或将 `forward_panel_report.json` 发回本对话进行审查。每次扫描单独消耗 Moomoo API 请求配额；未连接、限速或权限不足时应先检查 `benchmarkFailure` / `batchErrors` / `failureDiagnosis`。即使报价返回了，盘前或历史价格也**不能冒充**正常时段的新鲜行情。

注意：未来扫描完成时若第一份扫描距离超过 45 分钟、源报价过期、两个源报价不同步、候选退市或丢失报价，结果仍会是 **NO-GO 或未完成**，这正是正确的研究行为。用户可多次扫描以积累不同的机会，但同一交易日内样本高度相关，不能算作很多独立交易日。

## 关键风险边界

- `marketRelativePriceMoveBps` 是**最近成交价的未来市场相对变化**，不是买卖盘口可执行净收益。当前的全市场快照未覆盖每只股票完整 L2、成交、撤单、排队位置或 NBBO/SIP 成本，不能计算真实盈利。
- 当前股票目录不能重建历史退市/上市状态，不能替代证券的 PIT 历史股票池；同日并非完全避免公司行为数据异常。
- 无法从隔离的开发容器访问你的 Mac OpenD；当前真实日志中的 SPY 常规时段同步报价仍不够，盈利认证继续 `NO-GO`。
- `StudySnapshots.command` 不调用下单接口，也不接入券商资产及订单；`tradeEnabled=false`，`alphaCertified=false`。
- 初次安装不要覆盖 `recordings/`、`research/`、`output/`、`watchlist.json` 或 `.venv/`。完整工程包用于**新文件夹**；在已有目录升级请选择安全补丁。

## 本轮工程验收

重新测试 v1.7 基线时有两项交易日审计测试失败：旧审计器会把 `run_start/run_stop` 行政事件当作一个有行情的美东交易日，现已修复。旧 267 项全部回归通过，并新增 15 项跨扫描时间、防未来泄漏、基准归档、异常缺失及价格标签反例测试。**总计 282 项通过**。离线软件通过测试，不等于新版本已在你的 Mac 获得完整报价，不代表产生持续净收益。
