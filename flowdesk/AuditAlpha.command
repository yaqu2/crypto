#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi
"$py" execution_path_audit.py
"$py" portfolio_audit.py
"$py" alpha_gate.py
"$py" chronological_validation.py
printf '\n全部运行与多策略统计检查写入 research/forward_audits/alpha_gate.json\n'
printf '该检查绝不授权真实下单；仅评价报价模型与数据缺口。\n'
