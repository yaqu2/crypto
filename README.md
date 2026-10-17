# 美股量化研究工作区

本仓库以用户提供的研究归档为依据，提供两条**只读研究**路径：

1. `flowdesk/`：从归档恢复的 FlowDesk v2.8 工程，可运行离线网页、历史行情完整性及覆盖审计。实时 Moomoo OpenD 连接需用户本机服务、账号和行情权限，本云环境只验证离线部分。
2. `quantlab/`：新建的美股 60 个交易日动量、Top-50 日频组合基线。它并非原 usquant 源码的复刻；归档未包含完整原引擎和对应原始回测。目标波动率、集中度和原策略的其他细节尚无足够材料确认，因此此处只实现可复算的基础动量规则。

不包含自动下单功能。研究结果是价格和成本假设下的模拟账户路径，不是经纪商成交或盈利认证。

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
