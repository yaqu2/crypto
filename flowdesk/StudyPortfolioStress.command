#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
"$PY" market_portfolio_stress.py "$@"
echo '结果：output/market/market_portfolio_stress.json。注意：只读报价与资金占用压力，不是实盘收益。'
