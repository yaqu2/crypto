"""Append-only local receipt of research run creation; detects missing run folders.

Local file tamper evidence is not trusted remote notarization. Run inventory is
NOT a source of market timestamps or proof of completeness.
"""
import hashlib,json,os
from pathlib import Path


def canonical(obj):
    return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


def digest(obj):return hashlib.sha256(canonical(obj)).hexdigest()


def inventory_path(folder):return Path(folder)/'run_inventory.jsonl'


def read_inventory(folder):
    path=inventory_path(folder)
    if not path.exists():return [],False
    prev='0'*64;entries=[];seen=set()
    with path.open('rb') as fh:
        for raw in fh:
            if not raw.endswith(b'\n'):raise ValueError('TRUNCATED_RUN_INVENTORY')
            obj=json.loads(raw)
            content={k:obj[k] for k in ('seq','prevHash','runId','startedAt','shadowExpected')}
            if content['prevHash']!=prev or content['seq']!=len(entries)+1 or digest(content)!=obj['hash']:
                raise ValueError('TAMPERED_RUN_INVENTORY')
            if content['runId'] in seen:raise ValueError('DUPLICATE_RUN_INVENTORY_ID')
            entries.append(content);seen.add(content['runId']);prev=obj['hash']
    return entries,True


def register_run(folder,run_id,started_at,shadow_expected):
    # Mac + Unix advisory lock to prevent two simultaneous writers colliding.
    import fcntl
    parent=Path(folder);parent.mkdir(parents=True,exist_ok=True)
    lock=parent/'.inventory.lock'
    with lock.open('a+b') as guard:
        fcntl.flock(guard.fileno(),fcntl.LOCK_EX)
        entries,_=read_inventory(parent)
        if any(e['runId']==run_id for e in entries):raise ValueError('DUPLICATE_RUN_ID')
        prev='0'*64
        if entries:
            with inventory_path(parent).open('rb') as f:
                last=list(f)[-1]
            prev=json.loads(last)['hash']
        obj={'seq':len(entries)+1,'prevHash':prev,'runId':run_id,
             'startedAt':started_at,'shadowExpected':bool(shadow_expected)}
        obj['hash']=digest(obj)
        with inventory_path(parent).open('ab') as f:
            f.write(canonical(obj)+b'\n');f.flush();os.fsync(f.fileno())
        fcntl.flock(guard.fileno(),fcntl.LOCK_UN)
