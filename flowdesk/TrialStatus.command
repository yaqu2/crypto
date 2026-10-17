#!/bin/bash
set -eu
cd "$(dirname "$0")"
trap 'echo; echo "按回车关闭。"; read -r answer' EXIT
python3 trial_registry.py status
