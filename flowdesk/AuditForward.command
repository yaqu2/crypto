#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi
"$py" audit_forward_runs.py
printf '\n前瞻实验审计写入：research/forward_audits/all_runs.json\n'
printf '本报告属于未来报价跨价差敏感性分析，不是实盘成交记录或获利认证。\n'
