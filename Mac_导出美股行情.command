#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'from importlib.metadata import version; assert version("moomoo-api")=="10.11.7108"' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --disable-pip-version-check -r flowdesk/requirements.txt
fi
end_date="$(.venv/bin/python -c 'from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo("America/New_York")).date().isoformat())')"
stamp="$(date +%Y%m%d-%H%M%S)"
output_path="$HOME/Desktop/FlowDesk_US_History_${stamp}.csv"
.venv/bin/python -m tools.moomoo_history \
  --codes AAPL,MSFT,NVDA,AMD,AMZN,META,GOOGL,TSLA,SPY,QQQ \
  --start 2023-01-01 --end "$end_date" --out "$output_path"
echo "只读历史行情已保存：$output_path"
echo "此文件只覆盖固定观察池，不是历史全市场股票池。"
