#!/bin/bash
set -eu
cd "$(dirname "$0")"
trap 'echo; echo "按回车关闭。"; read -r answer' EXIT
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi
"$py" live_readiness.py
printf '\n结论已保存至 research/readiness/live_readiness.json。所有实盘订单仍然禁用。\n'
