#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '请先双击 Start.command 完成环境安装，再运行本脚本。'
  exit 1
fi
.venv/bin/python market_scan.py "$@"
.venv/bin/python market_archive.py
.venv/bin/python universe_agents.py || echo "候选研究未认证；请检查报告与快照哈希"
.venv/bin/python prepare_watchlist.py || echo "动态观察池未满足数据门槛"
echo '完成。结果位于 output/market/。如显示 NO_GO，请检查数据权限或报价新鲜度。'
