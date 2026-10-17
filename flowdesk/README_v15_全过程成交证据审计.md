# FlowDesk v1.5 · 持仓路径真实性审计

## 这不是另一套量化项目

本版本在用户的 FlowDesk v1.4 完整工程之上增量修改，保留 Moomoo OpenD、多 Agent、全市场扫描、原网页、研究冻结、原始录制、影子交易、资金审计与已有命令。

**没有下单功能，没有券商真实成交，也没有发现经过认证的盈利策略。**

## 这轮找出的实际漏洞

原 `prospective.py` 会在收到 **信号、2 秒以后的盘口和 60/300 秒以后的盘口**后，按照跨买卖价差计算模拟净收益。如果中间缺少连续盘口观察，**只用开始与结束两个报价不能确认持仓过程的风险、可退出性或数据连续性**。例如 3 秒入场、63 秒退出，过程中 60 秒没有任何订单簿推送，旧模型仍可生成一个 `PAPER_EXIT`。

本版选择**不改写原来的冻结影子引擎**，而是加上一层独立的 `execution_path_audit.py`，针对 `PAPER_EXIT` 逐条回放原始录制：

1. 核验整次录制 `metadata.json`、全部分段 SHA-256、事件数量和连续序号，拒绝未结束的前缀录制。
2. 核验原影子日志哈希链、固定研究协议、独立入场/退出价格与原始 Moomoo 盘口回调相匹配。
3. 从真正的信号时点到退出时点，检查原始盘口回调路径是否含 **大于 15 秒的未观察间隔**。该默认阈值是研究保守假设，**不表示安静行情一定断流**。
4. 对路径上每条盘口，使用在它**之前已经收到**的个股和基准股票报价推送；进一步核验报价供应商源时间必须足够新。只有缓存、未来收到、旧源时间或缺失 SPY，均不能证明路径质量。
5. 检查持仓时段的断流与重置事件；计算路径**可观察**的最不利穿价变动指标（不是完整最大不利波动，也不代表真实可成交价）。
6. 一条路径不合格，它的记录**不会被删除或自动算成零亏损**：会列入逐笔失败原因。正式 `AuditAlpha.command` 把“所有已完成模拟交易路径均可审计”作为研究候选进一步评估的额外硬门槛。

### 使用（Mac）

先备份旧目录，尤其是 `recordings/`、`research/`、`output/`。如已有仍在进行的封存试验，**保持旧版项目及指纹不变**，不可中途换成 v1.5；新研究使用独立文件夹和新的封存试验。

1. 登录 Moomoo OpenD，使用 `Start.command` 启动原量化台；完整日盘录制后正常关闭程序以完成元数据。
2. 双击 **`AuditExecutionPath.command`**：审核 `recordings/` 中全部 `forward_shadow` 的原始持仓路径，输出 `research/forward_audits/execution_paths.json`。
3. 双击 `AuditAlpha.command`：额外执行该路径审计，再执行资金与日聚类研究门禁。
4. 网页的“组合资金审计”面板点击“**持仓路径与断流审计**”查看 JSON。

命令行示例：

```bash
python3 execution_path_audit.py --recordings recordings --out research/forward_audits/execution_paths.json
python3 execution_path_audit.py --run-dir recordings/<run-id>/forward_shadow --out research/one_path.json
python3 -m unittest discover -s tests -q
```

`--max-gap-seconds` 可以改变探索性诊断阈值；但**不能事后调整该阈值再把历史试验冒充原来封存的结果**。固定规则的正式比较应在未来样本到来之前预注册本审计版本和阈值。

## 真实旧日志重跑

重新审计用户旧的 **64,420** 条事件、未结束的 ZIP 前缀录制，仍只看见 **105** 条常规交易时段盘口推送。源时间因果配对结果 **0**，且完全没有合格的已结束前瞻运行目录。因此路径审计返回 **NO-GO**，不会报告已发现 Alpha。参见 `research/v15_real_old_prefix_pair_audit.json` 和 `research/forward_audits/execution_paths.json`。

## 证据边界

- 这些盘口是 Moomoo/OpenD 本地收到的 **快照**，不是全国最佳报价或完整逐订单事件。官方文档说明 `svr_recv_time_bid/ask` 有时为零且部分服务端时间字段只支持港股，因此本版**没有擅自将美股盘口时间解释成经过交易所核实的时间**。
- 即使路径上 15 秒内持续有盘口，也不能证明行情中间没有丢失交易事件，更不能证明真实限价单能成交。
- `PATH_EVIDENCE_COMPLETE_NOT_FILL_VERIFIED` 只表示本地录制与所设数据条件匹配，**不是**可交易、盈利或实盘准入许可。
- 程序不会在后台替用户的 Mac 采集行情；需要 Mac 本机运行 OpenD 和研究程序。
- 所有本地哈希证据无法抵抗拥有文件系统修改权的人同时重建全部历史，需要额外不可修改的独立存储做外部公证。
