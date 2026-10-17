# FlowDesk v1.0 · 全市场候选可追溯 / 美股日盘连续覆盖验证

**直接升级用户上传的 FlowDesk 原项目。** 保留原 v0.9 连续录制、原生看板、Moomoo OpenD 及多 Agent，新增两条关键证据链。仅研究、只读，无下单、无自动策略执行，也没有认证盈利。

## 新增 1：行情是否真的连续

以前 `AuditRecorder.command` 验证 `events.jsonl`、后续分段、哈希和序号；这不能证明真实市场覆盖。现在：

- `session_coverage.py` 读取**经过完整哈希验证**的全部分段，按美东交易日计算：SPY 有效源时间报价每分钟覆盖、健康心跳分钟、盘口回调分钟、提前收市日的正确分母。
- 报价必须来自 `origin=push`、有合法价格、供应商源时间与回调接收时间相差不超过 20 秒且在同一个市场分钟。`seed`、旧报价和缓存不能刷新覆盖。
- 盘口只统计**本机回调分钟**，不会声称已经拥有交易所完整 MBO；逐笔也不宣称为覆盖所有美股成交场所的完整 tape。
- 研究阶段的保守**工程质量参考门槛**：至少30个不同交易日、完整开收盘监测、SPY源时间有效报价 ≥95% 日盘分钟、健康门禁正常 ≥90% 分钟。**即使达到这些门槛也不表示有 Alpha 或能成交。**
- 仅有录制前缀 (`closed:false`) 的数据可用 `--diagnose-prefix` 做诊断，但始终显示 `NO_GO_PREFIX_EXPORT`。文件哈希正确不是整天行情完整。

运行 `AuditCoverage.command` 审查 `recordings/` 下全部注册记录，报告在 `research/forward_audits/session_coverage.json`，也可以在原网页点击「查看交易日覆盖率」。

针对用户旧数据进行真实验证：64,420条事件，2026-10-12美东日盘共390分钟，只有246个分钟有录制健康心跳、105条有效盘口回调、SPY的合格报价分钟为0；而且 `closed:false`。报告写在 `research/真实日志_v10_行情覆盖率.json`，结论 `NO_GO_PREFIX_EXPORT`。这是数据诊断，不能证明所有其他时段的Moomoo没有数据。

## 新增 2：全市场扫描 → 观察池提案 → 人工批准 → 下次采集

保持 `ScanMarket.command` 获取当前US股票目录、逐批行情和不可覆盖扫描归档。新增 `ActivateWatchlist.command`，只有经过：

1. 全市场行情审核通过、完成时间在10分钟内；
2. `latest_report.json` 与 CSV 源哈希相符，扫描归档哈希链未被改动；
3. `proposed_watchlist.json` 与**重新计算**的排序一致，且基准 SPY/QQQ/IWM 全部在池内；
4. 本机实时 FlowDesk **已经关闭**；
5. 在终端明确输入 `APPLY`；

才会把**下一次启动**的 `watchlist.json` 原子替换，并保留备份、追加本地哈希激活日志 `research/watchlist_activation.jsonl`。**运行中的订阅不会被修改，绝不自动下单。**

- `bridge.py` 在实时运行时独占 `.flowdesk_live.lock`，防止意外同时开启两份采集或运行中热切换观察池；离线模式不占用该锁。
- `recorder.py` 为 `run_start` 写入观察池 JSON 内容的 SHA256。`cohort_lineage.py` 可将每个运行和早于开始时间的最后一次人工批准记录核对，防止把“涨了才追加入观察池”的候选伪装成此前选中的结果。
- 这不是完备的 point-in-time 历史美股全市场目录，股票被挑选不等于其未来表现优秀。所有结果 `tradeEnabled=false`。
- 本地哈希日志不提供第三方不可删除凭据；历史原始数据仍需留在 Mac 上并备份。新候选需要OpenD额度和真实权限，新池可能有订阅失败，仍以 `feedQuality` 和原始事件为准。

## Mac 使用

在备份整个旧版文件夹（尤其是 `recordings`）后解压**完整** v1.0 文件夹，不要与旧版同时运行采集。

1. 登录并启动 Moomoo OpenD，双击 `Start.command`。Mac 上的 SDK 安装和真实订阅权限未在本环境测试。
2. 保持日盘连续录制，结束时按 `Control+C`。然后运行 `AuditCoverage.command`。离线输出：`python3 session_coverage.py --recording recordings/<run_id>`。
3. 运行 `ScanMarket.command` 获得批量快照及候选提案；审查 `output/market/proposed_watchlist.json`。 **新池不是自动买入名单。**
4. 退出原 FlowDesk（以免订阅竞争），双击 `ActivateWatchlist.command`，输入 `APPLY`。下一次运行 `Start.command` 才切换。
5. 继续运行已有 `AuditForward.command`、`AuditPortfolio.command`、`AuditAlpha.command` 验证前瞻交易，不要把覆盖率门禁当作盈利证明。

Python 离线测试：`python3 -m unittest discover -s tests -q`。

## 新旧口径区分

旧数据两只股票常规时段仅1分钟有盘口、0分钟SPY报价，**无盈利认证**。这次新增的验证仅是研究可信度提升，不能保证找到正收益。原 X9 订单流探索性交易成本后多为负，不能忽略或改写为“策略已经盈利”。
