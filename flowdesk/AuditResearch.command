#!/bin/bash
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then echo '请先用 Start.command 安装环境'; exit 1; fi
if [ "${1:-}" != "" ]; then
  events="$1"
else
  events="$(find recordings -name events.jsonl -type f 2>/dev/null | sort | tail -n 1 || true)"
fi
if [ -z "$events" ] || [ ! -f "$events" ]; then
  echo '尚无录制文件。先启动 FlowDesk 进行行情采集，或传入 events.jsonl 的绝对路径。'
  exit 1
fi
.venv/bin/python research_study.py audit --events "$events"
echo '审计完成；NO_GO 表示样本还不足以证明净盈利。'
