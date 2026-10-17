#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '请先运行 Start.command 安装本地 Python 环境。'
  exit 1
fi
.venv/bin/python market_quote_stress.py "$@"
echo '报价压力测试已写入 output/market/quote_execution_stress.json；此报告不是实际成交利润。'
