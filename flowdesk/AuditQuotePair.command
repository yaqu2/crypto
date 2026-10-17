#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
echo '检查最近一次录制的报价/盘口因果时间证据（仅只读，不下单）'
python3 - "$PWD" <<'PY'
from pathlib import Path
from datetime import datetime
import subprocess,sys
root=Path(sys.argv[1]);runs=[p for p in (root/'recordings').glob('*/metadata.json') if p.is_file()]
if not runs:
 print('没有录制目录；先启动 Start.command 采集完整的真实交易时段。')
 sys.exit(0)
last=max(runs,key=lambda p:p.stat().st_mtime).parent
print('检查：',last)
subprocess.run([sys.executable,str(root/'quote_pair_audit.py'),'--recording',str(last),
                '--out',str(root/'research'/'latest_quote_pair_audit.json')],check=True)
PY
