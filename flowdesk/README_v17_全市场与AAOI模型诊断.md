# FlowDesk v1.7 — 全市场数据证据与 AAOI 单股模型诊断

这次在 **v1.6 + OTC 修复** 的原工程上升级，没有新建另一套交易台。仍然只读，不发送任何券商交易订单。

## 两项升级的实际意义

1. **全市场报价的分层质量报告**。现在 `ScanMarket.command` 把「获得股票目录」「获得交易所挂牌股票快照」「日盘有效的新鲜报价」区分开。报告新增 `failureDiagnosis`、`snapshotAccessConfirmed`、`quoteDiagnosticCounts`、`exchangeExamples`、`premarketPriceFieldCount`。若盘前拿到行情，`snapshotReceivedCount` 可以大于零，而 `freshUsableCount` 仍为零；这不是同一类故障。新增 `diagnostic` 列及可选 `prePriceUnverified`，仅用于排查，盘前价格不进入日盘 Alpha 评估。
2. **真实模型诊断**。浏览器的 AAOI 诊断链接可以打开当前运行进程的 `symbol-diagnostic?symbol=US.AAOI`，展示 SPY 行情门禁、目标股票报价源时间、盘口接收时间、逐笔分类、八个审查角色与否决原因。没有实时报价时绝不输出方向信号；即使观察条件满足，输出也不是实盘买卖指令。
3. **修复最新扫描结果无法传到多 Agent 的问题**。旧 `runtime.py` 只接受 `RESEARCH_WATCHLIST_ONLY`，漏掉 OTC 修复后的 `LISTED_RESEARCH_WATCHLIST_ONLY`；旧多 Agent 也只识别旧数据范围。这次同时修复，仍然要求哈希已核验、扫描时间不超过 10 分钟且挂牌股票范围完整。

## Mac 使用方法（优先打补丁，不用覆盖数据）

**先结束 FlowDesk 和全市场扫描终端（Control+C）**。如果有正在进行的事前冻结试验，不允许替换同一工程的研究代码；必须复制到一个独立新目录再升级。

下载 `FlowDesk_v17_直接升级补丁.zip` 并解压，打开 Terminal，执行：

```bash
bash '/补丁解压目录/安装进化.command' '/Users/quyang/Desktop/FlowDesk'
```

也可以键入 `bash `，拖入 `安装进化.command`，再加一个空格和 `/Users/quyang/Desktop/FlowDesk`，最后按回车。

安装器检查当前目录与冻结试验，先保存旧代码到 `.flowdesk_v17_backup_时间戳/`，再校验补丁 SHA256、替换文件，并运行全部回归测试。**不会改动** `recordings/`、`output/`、`research/`、`.venv/`、`watchlist.json` 或此前封存数据。失败自动尝试恢复旧文件。

修复后：

```bash
cd /Users/quyang/Desktop/FlowDesk
bash ScanMarket.command
```

查看 `output/market/latest_report.json` 的 `snapshotReceivedCount`、`listedSnapshotCount`、`quoteDiagnosticCounts` 和 `failureDiagnosis`。如果已经接入有效数据，再运行 `Start.command`，从原网页打开“AAOI 个股模型诊断”；复制 JSON 发给我就能客观分析当前模型，而不是猜测盘口。

## 数据质量边界

- 盘前 `OUTSIDE_REGULAR` 表示数据不合格于**当前日盘研究规则**，并不等于 OpenD 接口失效。
- 诊断的盘口 `receivedAt` 是本机接收时间；不是经验证的交易所撮合时间。`prePriceUnverified` 没有独立时间戳，不当成可成交盘前价格。
- 当前 Moomoo 批量快照依赖报价权限。即使市场目录有 13,058 只，仍不代表能够取得全部美股的完整逐订单 MBO。
- 避免本机多实例竞争 OpenD，先关掉旧版本再启动新版本。
- 所有内容只用于研究；未得到可信的未来样本外净盈利证据，不自动下单，不认定 AAOI 应涨应跌。

## 验收

- v1.6 OTC 修复工程基线 254 项自动化测试通过。
- v1.7 新增 13 项含 HTTP 集成验证，合计 **267 项本地测试通过**。
- 仍无法从 ChatGPT 的隔离运行环境直接连接你 Mac 的 OpenD；本地测试不是实盘数据或可持续盈利证明。
