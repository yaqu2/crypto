#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '请先运行 Start.command 创建项目虚拟环境。'
  exit 1
fi
.venv/bin/python market_robustness.py "$@"
echo '稳健性反证报告：output/market/market_robustness_report.json（仅研究，不授权实盘）。'
