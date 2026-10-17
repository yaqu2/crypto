#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi
"$py" chronological_validation.py
printf '\n按交易日先选策略、后隔离验证的报告已写入 research/forward_audits/chronological_validation.json\n'
printf '警告：历史日期划分不是未接触过的独立未来样本；永不授权下单。\n'
