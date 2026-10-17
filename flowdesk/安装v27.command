#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ];then PY=.venv/bin/python;else PY=python3;fi
"$PY" install_v27.py "${1:-/Users/quyang/Desktop/FlowDesk}"
