#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then py=.venv/bin/python; else py=python3; fi
"$py" audit_recordings.py
printf '\n原始录制多段审计已写入 research/forward_audits/raw_integrity.json\n'
printf 'PASS仅说明本地文件完整，绝不证明行情全覆盖或已实现盈利。\n'
