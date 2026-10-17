#!/bin/bash
set -eu
cd "$(dirname "$0")"
trap 'echo; echo "按回车关闭。"; read -r answer' EXIT
printf '只允许预先冻结的未来样本在观察期结束后解封一次。\n'
python3 trial_registry.py finalize --recordings recordings
