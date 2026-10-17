# 量化研究工作区

本仓库包含两条**只读研究**路径：

1. `flowdesk/`：从你提供的归档恢复的 FlowDesk v2.8 源码，保留原始研究工具、行情质量门禁和离线网页。实时行情需要用户本机的 Moomoo OpenD，本云环境仅验证离线功能。
2. `quantlab/`：独立的加密货币日线移动平均回测器。输入真实 UTC 日线 CSV 后，按前一日收盘信号在次日开盘模拟成交，计入双边手续费和滑点，并与买入持有比较。**没有自动下单功能。**

## 安装与运行

要求 Python 3.10–3.14。在仓库根目录运行：

```bash
python -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r flowdesk/requirements_ml.txt
.venv/bin/python -m unittest discover -s tests -v
(cd flowdesk && ../.venv/bin/python -m unittest discover -s tests -q)
```

离线 FlowDesk 网页（进程重启后需重新执行）：

```bash
cd flowdesk
../.venv/bin/python bridge.py --offline --no-browser --no-record
```

命令会打印本机地址。在云环境中可用本机 HTTP 请求验证 `/state`，不依赖 Moomoo 凭据。实时 OpenD 接入另外需要 Moomoo 官方 SDK、用户本机 OpenD 服务和相应行情权限；当前离线研究不需要这些条件。

## 加密货币日线回测

准备一份按 UTC 自然日连续排列的 CSV：

```csv
date,open,close
2026-01-01,100,101
2026-01-02,101,103
```

实际文件至少要有 `long + 2` 行，日期不能缺漏，价格须为正。然后运行：

```bash
.venv/bin/python -m quantlab path/to/BTC_USD_daily.csv --short 10 --long 30 --fee-bps 10 --slippage-bps 5 --out reports/btc.json
```

这是现货、单标的、满仓或空仓的研究模型。信号仅看前一日及更早的收盘价；交易假设在次日开盘执行。最终一日按收盘价清算。模型未包含盘口冲击、部分成交、资金费、税、托管或交易所故障；年化与回撤是输入样本下的模拟结果，不是已实现收益。请保留行情来源及时间戳。不要把测试中生成的合成行情当作真实回测。

## 已提供的历史行情

上传包内的 FlowDesk `events.jsonl` 已提取至忽略版本控制的 `data/recordings/20261012T092334Z-078d54b8/`，与归档所述 SHA-256 匹配：`681248ac2b8a2cc49954008ed44b40808c9a6a3a561523181a5e218475cd7b5c`。可重跑覆盖审计：

```bash
cd flowdesk
../.venv/bin/python session_coverage.py --recording ../data/recordings/20261012T092334Z-078d54b8 --diagnose-prefix --out ../data/coverage_audit.json
```

该录制是尚未关闭的前缀导出，仅观察到一个常规交易日，SPY 合格报价覆盖为 0%，**不能据此回算或认证交易收益**。要评估真实策略，还需要完整点时行情、真实可成交报价与成本，以及独立样本外期间。归档中的 usquant 日频原始源码、CryptoQuant 多数工程和完整成交账本未包含在本次上传中。
