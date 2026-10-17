# 最新：FlowDesk v1.4 · 源报价因果时钟与影子执行反证

请先看 `README_v14_真实盘口因果与执行严检.md`。原项目直接升级，保留原功能；不修改已有录制，不自动下单。

# 最新：FlowDesk v1.3 · 实盘前验收

请先阅读 `README_v13_实盘前验收与时间表.md`。原工程已加入 `PaperAccountCheck.command`（只读模拟账户发现）与 `Readiness.command`（实盘前严格 NO-GO 审计）。**没有实盘下单功能；研究通过软件测试不等于可以投资本金。**

---

# **当前最新：FlowDesk v1.0 · 真实行情覆盖与动态股票池因果审计**

请先阅读 **`README_v10_全市场因果样本.md`**。这是一份基于原 FlowDesk 项目逐步升级的完整工程，不是另起一个软件。先备份原录制文件夹，再整包解压；直接覆盖旧版可能丢失 `recordings/` 下的历史行情。**v1.0 没有自动交易功能，也没有经过验证的盈利 Alpha。**

新命令：`AuditCoverage.command`（逐交易日覆盖及完整性）和 `ActivateWatchlist.command`（手动确认的下次启动动态股票池）。全市场目录/快照仍使用 `ScanMarket.command`。**打开实时 FlowDesk 期间不允许修改观察池，系统会阻断。**

---

# 注意：本工程当前版本为 FlowDesk v0.9

**请优先阅读 [README_v09_连续采集与审计.md](README_v09_连续采集与审计.md)。** 该版本已从 v0.8 增加连续分段录制、全分段防篡改验收和 `AuditRecorder.command`。后面的 v0.7/v0.8 内容仅为历史变更记录。

---

# FlowDesk v0.7 · 研究完整性与独立盈利证据升级（请先读）

**这是你上传的 Mac FlowDesk 原工程的连续升级，不是第二套量化系统。** 原来的 `Start.command`、`ScanMarket.command`、盘口、成交、CVD、原多 Agent 看板与离线记录保留。

**启动顺序**：备份旧版 → 解压整个新文件夹 → 已登录本机 OpenD → 双击 `Start.command`。研究完成后 Ctrl+C 正常结束并保留全部 `recordings/`，然后运行 `AuditForward.command` 和本轮新增的 **`AuditAlpha.command`**。

v0.7 变化：影子单笔名义规模按 **1000 美元**估算，开仓和平仓均不得超过所见最优价位显示股数的 **10%**；深度不够取消或删失，不把缺失退出算盈利。新增逐股票分钟覆盖的原始回调哈希对账；新增全运行登记，发现已登记而丢失的研究文件夹；新增 **按美东交易日联合符号翻转的 12 假设探索性压力测试**。不足 30 个达到门槛的独立交易日、10 只股票或400个合格退出样本时，不发布统计候选。详细解释见 `README_v07_统计与成交真实性.md`。

**状态不变：不能证明稳定盈利、没有实盘订单功能、没有你的 OpenD 登录权限。统计 p 值不能证明 Alpha，影子穿价不等于真实成交。**

---

# FlowDesk v0.4 原工程整合升级：Moomoo 行情健康守卫 + 全市场快照扫描

> 这是在你上传的 `bridge.py / core.py / dashboard.html / flow.py / intelligence.py / recorder.py / runtime.py / Start.command / watchlist.json` **原工程副本上修改**，不是另一套模拟交易平台，也不是实盘交易机器人。

## 最短使用步骤（Mac）

1. 保留之前的 FlowDesk 文件夹作为备份。把本压缩包**解压为新文件夹**，不要覆盖原正在运行的工程。
2. 启动并登录本机 Moomoo OpenD（本地 `127.0.0.1:11111`）。
3. 双击本包内 **`Start.command`**：保留原网页、观察池、深度盘口、成交、录制功能，同时显示新的 `feedQuality` 行情门禁。
4. 如需**全美股当前股票目录和分批快照**，再单独双击 **`ScanMarket.command`**。它不会订阅几千只股票；结果保存在 `output/market/`，已开启的网页约每30秒重新读取最新报告。
5. 遇到 OpenD 权限或订阅额度错误，扫描报告会标记 `NO_GO_COVERAGE`，绝不会编造覆盖率或收益率。可使用 `bash ScanMarket.command --max-symbols 40` 先做40只的权限试验；此试验永远不能认证全市场。

**测试命令：** `python3 -m unittest discover -s tests -v`。**离线启动：** `python3 bridge.py --offline --no-browser --no-record`。

## 主要修改，都是改在原文件上

| 原文件 | 修改 |
|---|---|
| `bridge.py` | OpenD 登录要求为真，启动后及每60秒独立调用 `query_subscription(is_all_conn=False)` 复核真实订阅；每5秒检查基准数据新鲜度，必要时限速重连；新增网页 `market` 报告接口 |
| `runtime.py` | 记录通过源时间校验的基准报价、有效盘口接收时间，计算 `feedQuality`；队列或录制失败时阻断研究；自动重连每小时最多3次且至少间隔180秒 |
| `dashboard.html` | 显示行情门禁原因、全市场快照统计；OpenD连接正常但基准报价陈旧时不把横幅显示为健康 |
| `feed_guard.py`（新增） | 市场标准交易时间及常见节假日、提前收市检查；强制研究/交易状态分离。非交易所官方完整日历，临时休市需额外验证 |
| `subscription_guard.py`（新增） | 实际订阅清单核验，缺失的订阅不可伪装为成功 |
| `market_scan.py`（新增） | 通过 `get_stock_basicinfo(Market.US, SecurityType.STOCK)` 获得**当前**美股股票目录，再调用 `get_market_snapshot` 每批最多400只；记录缺失、过期行情和上下跌家数。扫描按不快于 0.55 秒/批执行，默认1.1秒，防止触碰官方请求上限 |
| `ScanMarket.command`（新增） | Mac 上独立的一次性全市场快照扫描入口 |

## 行情健康门禁怎样工作

- `NO_GO_CONNECTION`：OpenD不可用或还未确认连接。
- `NO_GO_BENCHMARK_SUBSCRIPTION`：未成功取得 SPY 等基准的真实 QUOTE 订阅。
- `WARMUP`：重连后正在等待源时间合格的报价，不能拿缓存行情做实时判断。
- `NO_GO_STALE`：常规时段基准报价超过20秒未出现合格推送（超过180秒且满足条件时可以受控重连）。
- `NO_GO_INTEGRITY`：队列溢出或录制失败，研究数据存在缺口。
- `OUTSIDE_REGULAR`：非标准常规交易时段，不把盘前/盘后当常规交易数据。
- `RECENT_RESEARCH_ONLY`：基准行情时效通过，但**依旧不代表全市场完整、盘口事件齐全或策略能够成交**。

所有状态均 **`liveOrdersEnabled = false`**。`fullOrderFlowReady = false`：由于 OpenD 的盘口/逐笔并非全美股交易场所逐订单 MBO，不能据此证明完整逐订单因果序列。标准交易日历不包括全部临时休市或特别调整，实际交易所日历需进一步核验。不同低流动性股票不应简单使用 SPY 同一阈值判定没有成交；所以自动重连只依赖流动性较好的基准。

## 研究数据的边界

- `watchlist.json` 原来订阅14只股票，深度观察其中4只；**保留不变**。大规模订阅受账户数据权限与配额限制。
- 市场扫描不是历史回测数据，也不是连续全市场订单流；当前股票池不是回测所需的历史点时成分。拆股、退市和代码迁移未通过此次扫描得到历史验证。
- 批量快照需要行情权限，部分标的停止交易/报价未更新时新鲜度会不足，扫描可标记 `NO_GO_COVERAGE`。上涨比例的分母是当次**新鲜且可用的报价股票**，不是所有上市股票。
- **没有运行真实 Mac OpenD 连接**：当前运行环境无法访问你电脑上的 `127.0.0.1:11111`。
- **没有证明任何 Alpha 或年化收益**，原研究档案中未经独立复现的高收益声明仍是待核实假设。

## 本轮验收

- 新增28项自动化单元测试通过：节假日/提前收盘、过期数据、缺失基准、订阅状态缺失、错误数据、全市场分母、Moomoo快照模拟、自动重连限速等。
- 前端 JavaScript 通过 Node `--check` 语法检查。
- 离线运行 `bridge.py --offline --no-browser --no-record`：HTTP页面成功返回，`state` 返回 `NO_GO_CONNECTION` 和 `liveOrdersEnabled=false`。
- 测试用模拟行情只用于测试逻辑，**没有计入任何真实交易研究统计**。

## 补充注意

`original_sha256.json` 是你上传的十个原文件的 SHA-256，便于核对。原版 README 保留为 `README_原版.md`。**安装本包时请不要上传或传播 Moomoo 密码、密钥、账户信息。**

本轮升级首要目标是**稳定接收和验证行情**，不是自动交易。确认新版本在 Mac 上覆盖完整常规交易时段后，才能进入跨股票、跨交易日的订单流净收益研究。
