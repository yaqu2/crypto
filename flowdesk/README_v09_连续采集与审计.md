# FlowDesk v0.9 · 原项目连续分段行情证据增强

本版本在用户上传的 **FlowDesk v0.4 原工程及 v0.8 集成工程**上继续修改；不是独立项目或新策略。保留 `bridge.py` / `runtime.py` / `dashboard.html`、Moomoo OpenD、全市场批量扫描、多 Agent、前瞻影子实验、组合资金与统计审计。**只读研究；无券商下单接口、无自动实盘授权。**

## 关键修复

之前 `recorder.py` **全运行上限仅为 256 MiB**：达到上限就停止录制。长期监控时，页面还可以继续运行，却不能积累完整订单流研究样本。

现在将本机原始行情事件分段追加：

- 第 1 段继续叫 `events.jsonl`，后续 `events.000001.jsonl`、`events.000002.jsonl` ...；**默认 64 MiB/段**，单次启动的整个运行目录最多 **8 GiB**。两者是预设可控上限，不代表不限量连续录制。写盘错误、队列溢出、全运行超限仍直接 `NO_GO_INTEGRITY`。
- 录制运行结束后 `metadata.json` 包含全部分段的名称、字节数、事件数、序号范围和 SHA-256；`run_inventory.jsonl` 继续登记每次启动。
- 新增 `segment_store.py`：逐段检查缺失、重复、序号断裂、文件长度、事件数、哈希及运行编号。`source_crosscheck.py` 现会在 **所有分段**中核对前瞻模拟用过的原始盘口，而非只检查 `events.jsonl`。
- 新增 `AuditRecorder.command` 和 `audit_recordings.py`：一次检查**所有已登记的原始录制目录**，防止只挑盈利实验、删除异常时间段；审计输出到 `research/forward_audits/raw_integrity.json`。`AuditAlpha.command`、组合及哈希审计继续保留。
- 浏览器仍能下载不超过 **256 MiB 原始大小**的 ZIP 前缀；超过上限会返回 HTTP 413，并告知必须从 Mac 本机的 `recordings/运行编号/` 复制**整个文件夹**，避免浏览器把巨大文件放进内存。**正在运行中的导出是前缀快照（`closed:false`），不是完整录制。**
- 加入对未正常结束录制、篡改后续分段、缺失登记运行目录、没有后续真实报价就声称模型退出等风险的专门拦截。历史 v0.8 单文件录制保持可验证；旧导出的 `closed:false` 只承认文件哈希正确，不能认证全天完整。

## Mac 上怎么用

1. 先备份现有 FlowDesk 文件夹和 `recordings/`。解压**完整的**新项目文件夹，**不要覆盖、删除旧录制**，也不要同时启动两个程序争抢订阅。
2. 先打开已登录的 Moomoo OpenD；在项目文件夹双击 `Start.command`。
3. 正常交易时段持续运行，网页「本机录制与导出」将显示事件数、MiB、**分段数**和丢弃数。关注 `feedQuality`：OpenD 登录成功不等于行情活跃。
4. 结束时在终端 `Control+C` 正常退出，确保分段目录产生 `metadata.json`，其中 `closed:true`、`dropped:0`、`error:null`。
5. 双击 **`AuditRecorder.command`** 审计全部本机原始录制，再运行 `AuditForward.command`、`AuditPortfolio.command`、`AuditAlpha.command`。若任一原始录制有破损，不要把对应影子交易报告宣传为经过验证的收益。
6. 扫描全美股当前目录仍用 `ScanMarket.command`。这仅发现动态股票候选，不等于拿到了数千只股票的同步 Level 2 或逐订单 MBO 数据。

离线测试：

```bash
python3 -m unittest discover -s tests -q
python3 bridge.py --offline --no-record --no-browser
python3 audit_recordings.py --recordings recordings
```

## 什么不能宣称

- 没有直接连接用户 Mac 上的 OpenD 做全天实机验证，没有取得新交易日真实盘口数据，也没有实际券商成交或借券信息。
- 旧导出 `events.jsonl`（64,420 条、145,918,509 字节）已核实 SHA-256、JSON 行数及序号连续性，但原始 `metadata.json` 明确为 `closed:false` 和 `exportType: prefix snapshot; recording continues`。**因此只能说导出前缀完整，不能声称整次运行在当时无缺口地完成。**
- Moomoo 可见订单簿不等于全市场逐订单 MBO；哈希链不等于独立公证；本地软件自动化测试不证明 Alpha、稳定盈利或实盘成交可能性。
- 单运行 **8 GiB 上限**可能被高流量采集耗尽，应监控磁盘，定期停止并正常结束运行、保留原数据。发生任何丢弃/容量上限错误立即拒绝研究认证。

下一真正决定盈利能力的工作依旧是连续的多交易日、跨股票有效市场数据，预注册未来样本，真实成交与费用校准。任何报价模型净收益都只是一种研究假设。
