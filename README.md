# 美股量化研究工作区

研究团队的职责、第一轮可否证实验和当前证据状态见 [RESEARCH_TEAM.md](RESEARCH_TEAM.md)；盈利能力的验证标准与数据门禁见 [VALIDATION.md](VALIDATION.md)。当前尚无足够真实盘中样本证明 PaperDesk 策略盈利；演示成交不能作为此证据。

本仓库以用户提供的研究归档为依据，提供美股研究与本地模拟交易路径：

1. `flowdesk/`：从归档恢复的 FlowDesk v2.8 工程，可运行离线网页、历史行情完整性及覆盖审计。实时 Moomoo OpenD 连接需用户本机服务、账号和行情权限，本云环境只验证离线部分。
2. `quantlab/`：新建的美股 60 个交易日动量、Top-50 日频组合基线。它并非原 usquant 源码的复刻；归档未包含完整原引擎和对应原始回测。目标波动率、集中度和原策略的其他细节尚无足够材料确认，因此此处只实现可复算的基础动量规则。

3. `paperdesk/`：可视化的本地全市场机会雷达和可启停机器人。机器人仅记录模拟股票买卖，不调用券商交易接口；报价和模拟价格不是经纪商成交或盈利认证。

## 美股模拟交易面板

在**运行并登录 Moomoo OpenD 的同一台 Mac**，双击 [Mac_打开美股模拟交易面板.command](Mac_打开美股模拟交易面板.command)，或运行：

```bash
.venv/bin/python -m paperdesk
```

OpenD 需监听本机 `127.0.0.1:11111`，并有美股快照权限。命令打印并打开仅本机可访问的网页地址；网页每 3 秒刷新，OpenD 全市场批量扫描约每 90 秒发起一次。面板显示当前上市股票目录的扫描覆盖、可用报价、SPY/QQQ 与市场广度、候选、模拟账户、持仓和成交。可在网页启动或暂停机器人，**每次启动程序默认暂停**。状态写在忽略版本控制的 `data/paperdesk/live.json`，重启后保留模拟账户。

真实模式每轮扫描会先写入 `data/paperdesk/scans/` 下独立的压缩 JSON，再交给模拟账户；文件名含内容 SHA-256，不完整扫描也会保存以供审计。行情或存档失败会暂停机器人，需要排查后手动再启动。长期运行请定期检查该目录的磁盘占用与备份，勿删除尚未分析的失败轮次。可用 `--scan-dir /path/to/archive` 指定其他保存位置。

机器人是一个固定、便于审计的短线实验规则：筛选价格 $5–$500、当日上涨 1%–8%、成交额至少 $500 万、显示价差不超过 30 bps 的股票；按涨幅、成交额和价差评分。仅在完整扫描至少 2000 只美股主要交易所上市股票、至少 80% 新鲜可用报价、SPY 和 QQQ 均涨超 0.2%、上涨股票占比超 52%、且处于常规美股交易时段时新增模拟仓位。最多 3 只，每只最多权益 15%；单只止损 3%、从峰值回落 2%、止盈 5%、最长持有 2 天，日内权益下跌 2% 后当日不再新增买入。真实模式当轮只产生信号，最早下一轮有更新的有效盘口且间隔不超过 5 分钟时才模拟成交；失败、暂停和重启会取消旧待单。模拟买入按显示卖盘、卖出按显示买盘，再各计 10 bps 不利滑点及 2 bps 费用；持仓权益按估算卖出净价记账。显示盘口不保证该数量可成交，也不模拟真实排队、延迟、跳空或部分成交。

该方向规则只是当前市场描述，未经历史样本外验证，没有证明短线优势或快速翻倍能力。扫描是周期性快照，不是逐笔实时全市场流，也不包括 OTC、未知交易所、期权或历史退市股票。行情权限、速率限制和 OpenD 状态会影响覆盖；面板展示门禁原因，不把残缺扫描称为全市场机会。程序只引用 `OpenQuoteContext`，不访问账户或交易接口。

可先在任意电脑运行合成演示，验证网页与模拟买卖流程：

```bash
.venv/bin/python -m paperdesk --demo
```

Mac 上也可直接双击 [Mac_周末打开模拟面板.command](Mac_周末打开模拟面板.command)，无需启动 OpenD。演示模式明显标为合成数据，与真实模式使用独立的本地模拟账户；休市期间适合熟悉界面和控制流程，不能用于判断下一交易日走势。云环境无法访问用户 Mac 的 OpenD，因此真实行情和权限必须在 Mac 上验收。

### 盘中快照的留出期评估

积累真实 OpenD 扫描文件后，可在同一仓库运行：

```bash
.venv/bin/python -m quantlab.intraday_eval data/paperdesk/scans --out data/paperdesk/eval.json
```

评估器校验压缩文件哈希并拒绝演示源；按扫描完成时间重放，并核对每只股票的源报价时间。第 `t` 轮只发出信号，最早第 `t+1` 轮有效盘口才能模拟成交。买入用显示卖盘、卖出用显示买盘，再计滑点和费用；缺盘口或快照不完整则取消该次模拟成交。报告按交易日 70%/30% 分为研究期与独立留出期，列出输入文件和代码 SHA-256、SPY 成本基准、净收益、最大回撤、成交与缺失，并将留出期滑点和费用各加倍做压力复算。至少 60 个常规交易日、100 笔完成回合、留出期 20 天及 30 笔回合，并满足覆盖、频率、扣成本与压力结果门禁，才可能得到 `RESEARCH_ONLY`；其含义仍只是值得继续验证。短样本、数据缺口或亏损会输出 `NO_GO`。具体门槛见 [VALIDATION.md](VALIDATION.md)。

面板现在会读取同一路径的 `data/paperdesk/eval.json`，显示“尚未评估”“证据不足或未通过”或“初步研究通过”，并列出留出期、SPY 与双倍成本结果。面板不会把模拟账户涨跌当作策略盈利证据；没有真实扫描时显示“尚未评估”。报告在新增行情后需要重新运行上述命令，旧报告的截止时间会显示在面板上。

## 安装与检查

需要 Python 3.10–3.14。在仓库根目录：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r flowdesk/requirements_ml.txt
.venv/bin/python -m unittest discover -s tests -q
(cd flowdesk && ../.venv/bin/python -m unittest discover -s tests -q)
```

## 美股日频回测

### 从本机 OpenD 导出历史日线

Moomoo OpenD 在用户 Mac 的 `127.0.0.1:11111` 运行；云环境的同一地址不是用户 Mac。仓库的 `tools/moomoo_history.py` 只调用官方 SDK 的历史 K 线与除权信息接口，不访问账户或下单接口。需在**运行并登录 OpenD 的同一台 Mac** 上执行，且 Mac Python 已安装 `flowdesk/requirements.txt`：

```bash
python3 tools/moomoo_history.py --codes AAPL,MSFT,NVDA,SPY --start 2023-01-01 --end 2026-10-16 --out /path/to/moomoo_history.csv
```

导出器保存未复权开盘/收盘、前复权收盘、拆股比例及现金分红，并附带来源与 SHA-256 说明文件。它会在无法连接、历史页缺失、原始/复权日期不一致或遇到尚未建模的公司行动时停止。不要将 Mac 的 OpenD 端口公开到互联网。**导出文件还不包含历史股票池资格**；Moomoo 当前股票目录不能证明某只股票在过去某日属于可选池。上传导出文件及原 usquant 历史股票池后，才能组装完整 Top-50 回测输入。若只用当下观察池，结果须标为静态观察池探索，不能称为原策略全市场复现。

输入 CSV 每行是一只股票在一个美股交易日的记录：

```csv
date,symbol,open,close,adj_close,split_factor,dividend,eligible
2026-01-05,AAA,100,101,101,1,0,1
2026-01-06,AAA,102,103,103,1,0,1
```

- `open`/`close` 是未复权交易价格；`adj_close` 是该时点可得的公司行动调整后收盘价，用于 60 日收益排名。
- `split_factor` 是当日生效的新股数/旧股数，普通日为 `1`；`dividend` 是当日每股现金分红，普通日为 `0`。代码在开盘前调整持股数量，收盘后把分红计入现金。必须核对数据供应商的拆股与分红口径。
- `eligible=1` 表示**该日当时**属于可交易股票池。应包含退市、改名等历史记录，不能把当前股票名单回填到过去。缺少已持有或待买股票的当日原始价格时，程序会报错，不会按上一价虚构成交。
- 多只股票、多个交易日合并在一个 CSV；至少要有 `lookback + 2` 个交易日。提供 `SPY` 每日行可显示其调整收盘价收益（未扣交易成本），如不希望 SPY 参与选股，令其 `eligible=0`。
- 每次排名至少要有 `top_n` 只符合条件的历史股票；否则程序拒绝回测，避免把不完整股票池误称为 Top-50。

运行：

```bash
.venv/bin/python -m quantlab path/to/us_daily.csv --lookback 60 --top-n 50 --fee-bps 10 --slippage-bps 5 --initial-cash 5000 --out reports/us_momentum.json
```

前一交易日收盘时对过去 60 个交易日的调整收盘价收益排名；下一交易日以未复权开盘价调整等权仓位。买卖均扣单边手续费和滑点，最终日按收盘价清算。输出包含输入哈希、逐日权益和模拟交易记录。尚未建模部分成交、盘口冲击、停牌后的真实处置、税和不同券商最低收费；不能用它直接推断可执行净收益。

### 短线候选与“翻倍”目标检查

预先列定的研究候选包括 1–2 日反转、5–20 日动量、60 日基线，以及动量策略加市场门禁或权益回撤保护的版本，按 1–10 个交易日间隔调仓。`quantlab.shortlab` 会列出**全部**候选的含成本结果，以及指定时间起的留出期收益和回撤；它不会自动选赢家。须在查看留出期结果前固定参数与 `--test-start`，避免反复试到好看的数字：

```bash
.venv/bin/python -m quantlab.shortlab path/to/us_daily.csv --test-start 2025-01-02 --top-n 50 --fee-bps 10 --slippage-bps 5 --out reports/shortlab.json
```

留出期不足 60 个交易日会给出 `NO_GO_TEST_TOO_SHORT`。单策略报告中的 `doublingDate` 仅表示**模拟权益**首次达到本金两倍的日期；空值表示样本内未达到。即便出现日期，也需检验同日市场、样本外稳定性、2022 年压力、成本上界、回撤及成交可行性，不能据此承诺快速翻倍。

`marketStateKnownAtPriorClose` 仅是方向描述：SPY 与 QQQ 均高于各自 20 个交易日均价且最近 5 个交易日上涨为 `BULL`；两者均低于均价且下跌为 `BEAR`；其他为 `MIXED`，数据不足为 `UNKNOWN`。开启 `--regime-gate` 后只有 `BULL` 时承担股票风险，缺 SPY/QQQ 日线会报错。`--trailing-stop-pct 0.10 --cooldown-sessions 5` 是另一种待检验的组合权益保护：**收盘后**发现从峰值跌超 10%，在**下一可交易日开盘**退出并休息 5 个交易日。隔夜跳空、反复进出和错过反弹都可能使结果更差，需与无保护基线比较。

### 期权墙观察

期权的看涨/看跌、行权价和到期日确定合约；未平仓量是尚未平仓的合约数。某个行权价未平仓量高，可以称为观察用的“墙”，但它没有告诉我们做市商是净买方还是卖方，也不能单独判断价格会靠近或远离该价位。当前快照也不能冒充过去某时刻可见的期权仓位。

同一台已登录 OpenD 的 Mac 可做**只读快照**：

```bash
python3 -m tools.moomoo_option_snapshot --underlying SPY --days 14 --out /path/to/spy_options.json
```

报告按到期日与看涨/看跌分别列出附近未平仓量最高的行权价，并记录采集时刻与报价来源日期。缺失的未平仓量不补零。它不输出交易方向或下单指令；要检验“拉扯”，还需连续保存事前快照，再与事后价格路径及其他行权价作对照。

如果在 Mac 上下载了本仓库，也可以双击仓库根目录的 `Mac_导出美股行情.command` 和 `Mac_观察期权墙.command`。两者首次运行会在本仓库的 `.venv` 安装官方固定版本 SDK，导出到该 Mac 的桌面；仅使用本机 OpenD 的只读行情接口。行情一键脚本默认导出 10 只固定观察标的，不能视为完整历史选股范围。这两个 Mac 启动器尚未在用户的 Mac 上实机验收。

## FlowDesk 离线验收及旧数据

```bash
cd flowdesk
../.venv/bin/python bridge.py --offline --no-browser --no-record
```

命令会打印本机地址，访问其 `state` 路由可检查 `mode=offline`。归档的真实 `events.jsonl` 已提取到忽略版本控制的 `data/recordings/20261012T092334Z-078d54b8/`，SHA-256 为 `681248ac2b8a2cc49954008ed44b40808c9a6a3a561523181a5e218475cd7b5c`。覆盖审计：

```bash
cd flowdesk
../.venv/bin/python session_coverage.py --recording ../data/recordings/20261012T092334Z-078d54b8 --diagnose-prefix --out ../data/coverage_audit.json
```

该录制是未关闭的前缀导出，审计为 `NO_GO_PREFIX_EXPORT`；只有 1 个常规交易日，SPY 合格报价覆盖为 0%。这份数据不足以做可信的订单流盈利回测。要检验原 usquant 结论，仍需原始源码、点时美股日线与历史股票池、公司行动、完整成本配置和独立样本外期间。
