#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ];then PY=.venv/bin/python;else PY=python3;fi
"$PY" multiscale_lab.py "$@"
echo '只读多尺度研究报告：output/research/multiscale_v26.json。此功能不产生实盘信号。'
