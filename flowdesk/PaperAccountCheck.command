#!/bin/bash
set -eu
cd "$(dirname "$0")"
trap 'echo; echo "按回车关闭。"; read -r answer' EXIT
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi
"$py" paper_probe.py
printf '\n只读检查完成，未提交任何订单，也不会打印账户号或账户余额。\n'
