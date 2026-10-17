#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'from importlib.metadata import version; assert version("moomoo-api")=="10.11.7108"' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --disable-pip-version-check -r flowdesk/requirements.txt
fi
stamp="$(date +%Y%m%d-%H%M%S)"
output_path="$HOME/Desktop/FlowDesk_SPY_Option_OI_${stamp}.json"
.venv/bin/python -m tools.moomoo_option_snapshot --underlying SPY --days 14 --out "$output_path"
echo "只读期权未平仓量快照已保存：$output_path"
echo "该快照不预测价格方向，也不能证明期权墙有吸引作用。"
