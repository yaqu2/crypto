#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
echo "即将打开合成行情演示面板：不需要 OpenD，不代表真实市场或交易结果。"
echo "演示账户与真实行情模拟账户分别保存；所有买卖均为模拟。"
.venv/bin/python -m paperdesk --demo
