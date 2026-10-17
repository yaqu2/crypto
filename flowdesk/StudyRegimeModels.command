#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ];then PY=.venv/bin/python;else PY=python3;fi
"$PY" sector_model_lab.py "$@"
echo '只读模型实验完成。报告 output/research/sector_model_selection.json；不能代表实盘盈利或交易许可。'
