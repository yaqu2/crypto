# 真实行情采集的完整性记录

PaperDesk 的真实模式每隔约 90 秒尝试一次 OpenD 全市场扫描。每次尝试先写入 `data/paperdesk/scans/attempts/` 的 `STARTED` 事件，写入成功后才调用 `feed.scan()`。扫描返回后，程序先把完整或不完整的行情快照写入 `data/paperdesk/scans/`，再写 `SCAN_ARCHIVED` 事件，最后才让模拟账户处理该快照。演示模式不写真实采集记录。

`attempts/` 中每个事件为独立的 gzip JSON 文件。文件名末尾的 64 位十六进制值是**压缩后文件字节**的 SHA-256；临时文件同步后用硬链接发布，已有记录不会被覆盖。每个事件包含 `schemaVersion: 1`、`attemptId`、`startedAt`、`recordedAt` 和 `phase`。`attemptId` 是同一次采集共享的随机标识，两个时间都是带时区的 ISO 时间。

| 阶段 | 记录时机 | 额外字段 |
| --- | --- | --- |
| `STARTED` | 调用行情源前 | 无 |
| `SCAN_ARCHIVED` | 行情快照成功归档后、模拟账户处理前 | `scanFile`，仅为归档文件名 |
| `FEED_ERROR` | 行情源抛出异常 | `exceptionType` |
| `RECORD_ERROR` | 行情快照无法归档 | `exceptionType` |
| `ENGINE_ERROR` | 快照已归档，但模拟账户处理抛出异常 | `exceptionType` |

审计事件和面板错误信息只保留异常**类型**，不写异常消息、账号、密码、访问令牌或原始 SDK 响应。行情快照本身仍包含研究所需的股票与报价，不应公开上传含私有观察池或个人注释的改造版本。

研究评估必须把 `STARTED` 的独立 `attemptId` 作为采集尝试分母，并核对每份快照恰有一个同名 `SCAN_ARCHIVED.scanFile` 引用。`FEED_ERROR`、`RECORD_ERROR`、未终结的 `STARTED`、缺失或多余的扫描引用均会降低完整性；`ENGINE_ERROR` 表明执行与归档不一致，不能给出通过结论。校验失败的文件不能静默跳过。旧版只有成功扫描、没有 `attempts/` 的档案无法证明采集覆盖率。

若事件本身无法写盘，真实模式立即停用本进程后续的扫描与模拟交易，面板显示 `CAPTURE_AUDIT_ERROR`，终端只打印不含异常消息的告警。修复存储并重启后才可继续采集。**磁盘故障可能连失败事件也无法落盘**；这段不可见停机时间不能视作有效观测。研究结论还需核对预定交易时段的采集时间覆盖、进程启停和 OpenD 可用性，不能只依赖现存快照数量。

事件是采集与模拟流程的审计线索，不证明 OpenD 的行情覆盖了所有交易所，也不证明模拟成交可在市场真实执行。
