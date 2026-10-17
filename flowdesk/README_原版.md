# FlowDesk Mac 多标的证据研究台 v0.4.0

## v0.4.0：实时捕捉与证据解释

解压完整文件夹，保持 OpenD 已登录，双击 `Start.command`。旧版运行窗口先按 Control+C 结束。使用新文件夹启动即可，旧录制不需删除。

- 默认同时观察 **SPY、QQQ、AAPL、MSFT、NVDA、AMD、AMZN、META、GOOGL、TSLA、AAOI、AAOX、LITE、COHR**，全部请求 QUOTE 和 K_1M；AAOI、AAOX、NVDA、SPY 额外请求 ORDER_BOOK 和 TICKER，共36个“标的×类型”订阅请求。实际额度、权限与成功状态以 OpenD 返回为准。
- 不是全市场扫描。编辑 `watchlist.json` 可配置1–60个标的；基准、同业必须也在订阅列表里。扩大深度订阅会增加配额与处理负担。修改后重启。
- 扫描按60秒绝对变化排序；点击标的查看盘口、CVD、逐笔、K线以及事实、可能解释、未知和失效条件。
- 同期60秒/5分钟窗口只接受源时间合格的常规时段推送。SPY基准、LITE/COHR光通信同业对照必须有起止时间在5秒内对齐的数据。启动后需要积累窗口；活跃度不足或缺失锚点时显示原因。
- 界面通过 SSE 接收完整最新状态，目标每250ms发布一次，最多每秒4次渲染；异常时每秒备用轮询，超过5秒没有服务状态标记陈旧。**250ms是本机发布节奏，不是从交易所到浏览器的延迟保证。**
- SDK解码后进入有界4096事件队列，后台处理。界面显示最近2048个处理样本的本机排队/处理 p50、p95、丢弃数。队列溢出、连接断开或解析失败会清空对应证据段，不能拼成连续数据。
- 连接失败自动按1、2、4…最多30秒重试；恢复时重新订阅。单个订阅失败明确展示，批量失败逐标的重试；持续权限失败不无限重试冒充成功。
- 约60秒变动达到±0.5%、展示盘口点差达到30bp时产生观察提醒，同类每标的60秒去重。阈值是未校准的启发式，**不是已验证盈利信号**。
- 新闻、实时期权Gamma和云端大模型尚未接入此实时链路。当前“推理”是本地确定性证据规则；不会编造新闻原因、做市商净Gamma或未来胜率。已交付的历史/Gamma工具仍独立使用。
- 源报价最多允许15秒旧、2秒超前；种子缓存与离线回放不进入实时价格证据。盘前/盘后/夜盘字段继续展示，但字段时间未核实时不用于短时收益推理。交易所节假日与半日市日历尚未核实。
- 录制的 `receivedAt`/`monotonicNs` 保留串行记录顺序；新增 `callbackReceivedAt`/`callbackMonotonicNs` 保存SDK解码后进入队列的时刻，不能称为交易所时刻。跨断线或溢出世代的积压事件标为 `discarded`，回放不纳入。

### 本轮验证

48项Python测试通过，包括时间门禁、跨标的窗口、回放、队列溢出、旧种子覆盖保护、模拟重连、HTTP/SSE与访问限制。JavaScript语法及DOM逻辑检查通过；本环境未能运行浏览器截图检查，也无法访问用户Mac上的OpenD，因此真实订阅权限、设备性能与端到端延迟尚未测得。

已回放此前的3,148条实际录制事件，SHA256/事件数/字节数一致，未发现本地完整性问题。录制发生在美东盘前，其常规时段旧报价被排除；0条报价进入实时价格窗口。保留的42条提醒都是展示盘口点差观察，不是价格方向或买卖信号。详见 `research/current_replay_v04.json`。这不验证供应商数据覆盖和策略收益。

离线诊断（不用安装SDK）：

```bash
python3 bridge.py --offline --no-record
python3 -m unittest discover -s tests -v
python3 research/replay_intelligence.py --events /实际路径/events.jsonl --output replay-report.json
```

以下为早期版本变更记录；当前观察池与行为以本节为准。

## v0.3.0 连续录制与研究验证

本轮研究审查见 `research/RESEARCH_REVIEW.md`。当前结论仍为尚未验证，没有实盘交易信号。

- 启动即自动录制原始回调至本文件夹的 `recordings/每次运行编号/`，不上传，不读账户。
- 页面显示已录事件、字节、队列和丢弃数，新增“下载研究数据（ZIP）”。
- 导出包含完整行前缀、元数据与SHA256，下载时录制可继续；不是全天数据完整性证明。
- 单次原始事件上限256MiB；达到上限会停止写入并显示错误，不自动删除旧录制。历史录制占用空间由用户管理。
- `--no-record` 可关闭本地录制；正常启动默认打开。
- `research/audit_data.py` 审计导出；`research/evaluate.py` 只评估预注册的完整逐日净收益。
- `protocol_draft.json` 刻意保持未冻结：没有成本、样本量、日历和已审计成交引擎时，评估器应阻断，不能给盈利结论。
- 现有快照审计见 `research/current_data_audit.json`，本轮多agent报告见 `research/RESEARCH_REVIEW.md`。

## v0.2.1 数据显示修正

- 用户实机报告已验证8项订阅成功，逐笔与K线有返回；不代表数据全市场覆盖。
- K线保留接口时间标签，不未经证实称为起点；新鲜度改按接收时间展示。
- 当全部成交方向为中性/未知时，CVD面板显示方向数据不足而非绘制零线；原始诊断数值保留。

## v0.2.0 新增

- AAOI / AAOX 各增加 TICKER、K_1M 订阅；逐笔与K线请求 Session.ALL，实际时段及权限以返回结果为准。
- 本段CVD、方向可分类比例、中性成交量、最近30笔、1分钟K线图。
- 逐笔只统计明确REALTIME、30秒内且不超前5秒的事件，以日期与序号去重。按接口方向统计，非全市场主动买卖证明。
- 成交类型仅接受 AUTO_MATCH、ODD_LOT、T、EXTENDED_TRADING_HOURS；其他类型统计为排除。未完整处理成交撤销与更正，不能作为精确全市场CVD。
- 启动、时段改变、检测到断线或10万笔去重上限后重新起算；曲线按分钟汇总，最近一分钟未完成。未检测到的数据缺口不能补全。
- 1分钟K线前复权，最多存240根，画最新日期最后60根；末根可能未收盘，缺失分钟不填补。
- 原始报价/盘口接收计数与逐笔/K线行数可从页面或诊断查看。启动页面增加当前阶段提示。
- 新增代码与模拟输入测试通过；真实逐笔和K线权限仍须在用户Mac上验证。

## v0.1.1 修复

- 根据实际四项订阅结果显示连接状态，修复全部成功时仍提示可能失败的问题。
- 支持含毫秒的报价时间，修复来源时间显示未知的问题。
- 盘前/盘后字段明确标注时间未核实；常规时段旧报价保留原始日期。
- 已用用户导出的诊断文件复核四项订阅成功；不代表行情覆盖或持续推送已完整验证。

## 这一步的目的

通过已登录的OpenD读取配置观察池的价格、K线与所选标的盘口。权限截图显示US Stocks LV3、API Port 11111；这不等于我们已收到行情。程序必须在已登录OpenD的那台Mac上运行。

## 启动

1. 保持Moomoo OpenD打开并登录，API端口保持11111，监听地址保持本机127.0.0.1。
2. 将本文件夹完整解压，不要单独移动Start.command。
3. 双击Start.command。首次运行会在本文件夹的.venv中安装官方moomoo-api及依赖，需要联网；不会修改系统Python包。
4. 浏览器会自动打开本机行情页。如果没有自动打开，复制终端输出的浏览器地址。
5. 页面会显示观察池与订阅结果；需要排查时下载“连接诊断”。

### Mac无法直接打开Start.command

打开“终端”，输入 `bash` 后按一次空格，把解压后的Start.command拖到终端窗口，再按回车。不要输入账号密码或交易密码。若系统安全提示无法确认文件，先查看本包源码；不要关闭系统安全保护。

### 缺少Python

启动器会提示。安装Python 3.12或3.13的官方macOS版本：https://www.python.org/downloads/macos/ ，再运行Start.command。本程序需要Python 3.9及以上的zoneinfo支持；moomoo SDK兼容性仍以实际安装结果为准。

### 停止

在运行窗口按Control+C，或关闭该终端窗口。无需退出moomoo交易客户端。

## 能做与不能做

- 使用OpenQuoteContext连接127.0.0.1:11111，按watchlist.json订阅报价/K线，以及deepSymbols的盘口/逐笔。
- 请求最多60档盘口；实际返回档数取决于权限、时段和接口。推送返回档数也可能与初始查询不同，以每次实际数据为准。
- 显示报价来源时间、盘口原始服务器时间与本机接收时间。不会把刚收到的旧缓存当作最新成交。
- 显示五档挂单量倾斜，不能据此识别真实吸收、冰山单或撤单意图。
- 没有交易上下文、下单、撤单、解锁交易、资金或持仓查询。
- 不收集密码，不向研究台或第三方上传行情。SDK自身日志由官方SDK管理。
- 本机网页仅监听127.0.0.1，使用随机访问路径，无跨域开放。
- 首次启动依赖安装需要联网；常规运行通过已登录的OpenD获取行情。
- 不会后台持续运行AI、抓取新闻、生成预测胜率或交易建议。

## 关于时段与价格

常规时段last_price与pre_price/after_price分开展示。不要把常规时段的旧值当作夜盘或盘前价格。只读测试不保证当前时段有推送。若市场尚未开盘或标的不活跃，可能看到旧数据、空盘口。记录了行情权限与端口不意味着所有交易场所全部覆盖。

盘口NORMAL类型的交易场所合并口径未独立核实；股票LV3也不等于全市场逐笔委托（MBO）数据。当前只解析价格档位与数量。

## 原有云端面板

https://orderflow-research-desk.cosmicmacaw16.chatgpt.site

云端页面无法直接读取你Mac的127.0.0.1。当前提供“下载面板快照”供手动导入；快照标记replay，盘口时间未经时区确认时留空，不伪造新鲜数据。持续同步将在本机读取成功后单独连接。不要将OpenD端口暴露到公网。

## 文件

- Start.command：本机启动器与隔离依赖安装
- requirements.txt：官方SDK版本
- bridge.py：只读连接、重连与本机HTTP/SSE页面
- runtime.py：队列、数据处理、性能采样与状态发布
- intelligence.py：时间门禁、对齐窗口、确定性证据与提醒
- watchlist.json：观察池、深度订阅、基准与同业配置
- core.py：价格、盘口与时间字段处理
- recorder.py：本地原始事件录制与导出
- research/：审计、统计评估、研究报告与未冻结协议
- flow.py：逐笔去重、分段CVD与K线合并
- dashboard.html：本机面板
- tests/：时间、盘口、订阅状态、CVD去重/分段/排除规则与K线更新测试

## 验证范围

交付前检查Python与JavaScript语法、核心解析与本机HTTP访问。测试环境没有你的OpenD登录会话，真实盘口推送、你的Mac依赖安装与浏览器显示仍需你启动后确认。

## 官方参考

- SDK安装：https://openapi.moomoo.com/moomoo-api-doc/en/quick/demo.html
- 实时逐笔：https://openapi.moomoo.com/moomoo-api-doc/en/quote/update-ticker.html
- 实时K线：https://openapi.moomoo.com/moomoo-api-doc/en/quote/update-kl.html
- 盘口：https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-order-book.html
- 报价字段：https://openapi.moomoo.com/moomoo-api-doc/en/quote/update-stock-quote.html
- 权限：https://openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html

## 离线研究命令（无需调用券商）

以下命令在本文件夹内运行。数据文件路径由用户选择，未通过数据门禁的录制不能作为正式策略证据。

```bash
python3 research/audit_data.py /你的路径/flowdesk-recording-运行编号.zip
python3 -m unittest discover -s tests -v
```

统计评估器只消费未来由审计过的执行引擎产生的每日净收益CSV。本版尚未实现该执行引擎。必需列：`date,candidate_net_return,baseline_net_return,candidate_trades,baseline_trades,costs_included,data_complete`；收益使用小数，例如0.001表示0.1%，包括全部预定零交易日。

```bash
python3 research/evaluate.py --protocol 冻结协议.json --protocol-sha256 外部预存的64位SHA256 --daily 每日净收益.csv --ledger 唯一次分析结果.json
```

请勿将草案的false改为true来绕过门禁。真实预注册需要先确定费用、成交模型、交易日历、目标效应与样本长度，并外部保存代码、协议哈希及时间。评估器只能校验声明及文件一致性，不能证明输入真实性、强制市场日历或防止有意更换ledger路径。`RESEARCH_EVIDENCE`仅是有条件统计结果，不是实盘采纳；费用压力、成交质量与风险审查仍需另外通过。
