#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '请先运行 Start.command 以安装本机 Python 环境。'
  exit 1
fi
.venv/bin/python market_forward_panel.py "$@"
echo '跨次扫描研究结果：output/market/forward_panel_report.json。仅用于研究，不是净收益和交易信号。'
