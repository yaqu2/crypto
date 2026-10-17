#!/bin/bash
set -eu
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo '尚未初始化本机Python环境。先运行 Start.command 安装环境；不需要开启正式试验。'
  PY=python3
fi
"$PY" prelaunch_audit.py --project . --tests --check-opend "$@"
printf '\n总验收报告：research/prelaunch/preflight_report.json\n注意：本工具不会下单；PASS_OFFLINE_TESTS_ONLY 不代表交易有盈利优势。\n'
