#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then echo '请先运行 Start.command 完成环境安装。'; exit 1; fi
.venv/bin/python audit_recordings.py
.venv/bin/python audit_daily_coverage.py
printf '逐交易日行情覆盖率审计完成；结果在 research/forward_audits/。\n'
