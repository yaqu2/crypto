#!/bin/bash
set -eu
cd "$(dirname "$0")"
trap 'echo; echo "按回车关闭。"; read -r answer' EXIT
printf '\nFlowDesk：正式未来研究冻结（不是购买或交易指令）\n请先关闭正在运行的 Start.command。\n\n'
python3 trial_registry.py freeze "$@"
echo '\n冻结成功后再启动 Start.command，未来60个交易日必须保持记录。'
