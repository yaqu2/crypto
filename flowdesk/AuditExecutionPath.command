#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi
"$py" execution_path_audit.py --recordings recordings --out research/forward_audits/execution_paths.json
printf '\n每笔模拟成交均要求完整的原始盘口路径、个股与SPY源报价。此审计不授权实盘。\n'
