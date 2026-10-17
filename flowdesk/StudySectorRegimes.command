#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ];then PY=.venv/bin/python;else PY=python3;fi
"$PY" sector_regime_lab.py "$@"
echo '已生成只读观察报告 output/research/sector_regimes.json；不是预测信号、模拟成交或盈利认证。'
