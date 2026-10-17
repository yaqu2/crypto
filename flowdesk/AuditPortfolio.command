#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi
"$py" portfolio_audit.py
printf '\n已写入 research/forward_audits/portfolio_stress.json；仅是受资金约束的假想报价审计，不能代替券商成交。\n'
