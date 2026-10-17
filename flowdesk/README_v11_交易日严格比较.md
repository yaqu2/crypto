# FlowDesk v1.1 — 订单流策略逐日研究增强版

本版本**直接修改原来的 FlowDesk v1.0 工程**，不是独立应用，也不接入交易下单接口。保留 `Start.command`、Moomoo OpenD、原网页、多 Agent、全市场扫描、录制、影子实验和资金审计。

## 这次具体新增什么

1. **组合已完成影子交易的逐日、逐规则净损益**：`portfolio_audit.py` 现在将实际准入的反事实交易（限制账户总资金、同标的重复、做空借券证据）按美东交易日归集，输出 `ruleExitDayClosedOnlyPnlUsd` 与 `ruleExitDayClosedOnlyCounts`。此前按 UTC 日汇总的部分字段改为美东日，新增 `dayTimezone` 明示口径。未完成持仓仍然是未知损益，不能在账户收益中忽略。
2. **时间顺序检验**：`chronological_validation.py` 从所有已完整审计的录制中选取独立有效美东日，固定前 60% 为训练、其后 2 个有效交易日为空窗、余下为测试；只按训练期的单账户 40bps 成本模型收益挑选一条规则，后段只能检验，不能重选。至少需要 60 个合格日，且测试期至少 20 天；训练期需达到基础活跃度。**这仍然不是提前封存的真正样本外测试**，尤其不能用已反复查看过的历史记录证明 Alpha。
3. **零交易日纳入分母**：测试期中当天没有完成交易的合格市场日，账户模型贡献记为零，而不是被有收益的交易日筛掉。反之，如果有无法退出的交易，组合审计将阻止统计通过，而非当作盈亏零。
4. **5日移动块重抽样**：检查后段收益均值对日期相关性的敏感程度；常数收益样本没有有效区间。该区间是探索性指标，不保证真实统计覆盖率。
5. **全交易日断流最长缺口**：此前即使 95% 分钟覆盖合格，也可能存在连续十几分钟缺失。现在 SPY 合格报价和健康状态的最长连续缺失必须各不超过 2 分钟，否则当日研究覆盖审计拒绝通过。这是预设的工程验收门槛，不是数据供应商保证。
6. **浏览器查看**：新增 `chronological-validation` 只读接口和页面链接，未生成审计文件时返回 HTTP 404。

## Mac 使用

先备份旧 `FlowDesk/`、`recordings/`、`research/`、`output/` 及 `watchlist.json`。解压本包并保持完整目录，不要混装不同版本的 Python 文件。

- `Start.command`：开启你 Mac 上本机 OpenD 行情与原网页。OpenD 必须已经登录且拥有匹配权限。只读，不下单。
- `ScanMarket.command`：全市场当前股票目录及批量快照，**不是**全市场逐订单流。
- `AuditCoverage.command`：校验分段原始记录及每个美东日有效报价覆盖。
- `AuditAlpha.command`：原有组合与联合统计审计，**现在附加逐日时间顺序报告**。
- `AuditChronological.command`：单独运行新增研究报告（无需启动 OpenD）。

终端直接运行：

```bash
python3 -m unittest discover -s tests -q
python3 chronological_validation.py --recordings recordings
python3 bridge.py --offline --no-record --no-browser
```

报告在 `research/forward_audits/chronological_validation.json`。页面上的相关链接也是只读。

## 数据和盈利必须分开

- 所有前瞻实验都基于未来**接收到的 Moomoo 盘口快照**，不是券商成交回报，不保证能在显示价成交；不是整合市场 NBBO，也不是交易所级逐订单 MBO。
- 观察池的全市场扫描是现时目录，不能无证据地回推成历史可交易股票池。
- 带有未来结果的旧记录即使按日期切分，也已经受到了开发者查看和模型迭代的影响；**不得叫做外部独立样本外**。
- 报告字段 `EXPLORATORY_HISTORICAL_TIME_SPLIT_POSITIVE` 仅表示某套**反事实报价模型**在该次切分里的描述性正向压力测试，绝不代表交易资格；所有分支 `tradeEnabled=false`、`profitCertified=false`。
- 连续真实交易日、券商成交数据和未触碰的事前冻结未来样本，是后续必须跨过的门槛。无保证收益率、无自动交易、无杠杆。

## 2026-10-12 旧样本重新验收

旧 `events.jsonl` 记录 64,420 条事件，原始数据仍是一个 `closed=false` 的前缀文件。当天美东常规交易时段应有 390 分钟、健康心跳覆盖 246 分钟、合格 SPY 报价 **0/390** 分钟、常规盘口回调 **105** 条。新增最长连续 SPY 合格报价缺失是 **390 分钟**，不能用于认证实时订单流盈利。

## 软件验收边界

本环境运行 146 项 Python 单元/对抗测试通过、JavaScript 语法检查通过、原服务器离线启动与新统计接口检查通过。**没有**在用户 Mac 上连接实际 OpenD，也没有实盘成交测试。上述测试只能证明被测软件行为，不证明金融策略有效。
