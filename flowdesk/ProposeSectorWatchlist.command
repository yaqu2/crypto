#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ];then PY=.venv/bin/python;else PY=python3;fi
"$PY" sector_watchlist_proposal.py "$@"
echo '提议已输出，尚未修改 watchlist.json 或任何订阅。'
