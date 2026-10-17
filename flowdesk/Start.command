#!/bin/bash
set -eu
cd "$(dirname "$0")"
trap 'echo; echo "如有错误，请把最后几行发给我。按回车关闭。"; read -r answer' EXIT
printf '\nFlowDesk：只读行情连接\n首次启动需联网安装官方Python SDK。不会下单。\n\n'
flow_python=''
for candidate in python3.12 python3.11 python3.10 python3.13 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,15) else 1)' >/dev/null 2>&1; then
    flow_python="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$flow_python" ]; then
  echo '没有找到可用的Python 3.9–3.14。请从 https://www.python.org/downloads/macos/ 安装Python 3.12或3.13后再运行。'
  exit 1
fi
if [ ! -x .venv/bin/python ]; then "$flow_python" -m venv .venv; fi
if ! .venv/bin/python -c 'import moomoo; from importlib.metadata import version; assert version("moomoo-api")=="10.11.7108"' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
fi
.venv/bin/python bridge.py
