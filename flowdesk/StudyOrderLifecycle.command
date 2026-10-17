#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ];then PY=.venv/bin/python;else PY=python3;fi
"$PY" order_lifecycle_lab.py "$@"
echo '合成订单演练已输出 output/rehearsals/order_lifecycle_report.json；不是券商成交、真实净值或策略认证。'
