#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'from importlib.metadata import version; assert version("moomoo-api")=="10.11.7108"' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --disable-pip-version-check -r flowdesk/requirements.txt
fi
echo "请先启动并登录本机 Moomoo OpenD，确认地址为 127.0.0.1:11111。"
echo "浏览器会打开本地模拟交易面板。全部成交仅记在本机 data/paperdesk/live.json。"
.venv/bin/python -m paperdesk
