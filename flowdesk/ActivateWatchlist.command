#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then echo '请先运行 Start.command 安装环境，然后停止 FlowDesk 再激活观察池。'; exit 1; fi
.venv/bin/python activate_watchlist.py "$@"
echo '观察池已写入 watchlist.json；下次启动 Start.command 生效。此操作不下单。'
