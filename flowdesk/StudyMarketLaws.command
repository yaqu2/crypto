#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '请先运行 Start.command 安装本地 Python 环境。';exit 1
fi
.venv/bin/python market_law_lab.py "$@"
echo '市场规律探索报告：output/market/market_law_report.json；仅探索性研究，不授权实盘。'
