#!/usr/bin/env python3
"""Explicit, local, auditable next-restart watchlist activation.

Never touches a running OpenD session, subscribes, trades, or decides alpha.
Rejects stale/tampered scans and tampered candidate proposals. Persist audit
lineage before the next launch; checks are local, not third-party attestation.
"""
import argparse,fcntl,hashlib,json,os,tempfile
from datetime import datetime,timezone
from pathlib import Path
from market_archive import audit_archive, sha as file_sha, canonical
from prepare_watchlist import propose
from runtime import validate_config


def _hash(x):return hashlib.sha256(canonical(x)).hexdigest()

def audit_activations(project):
    root=Path(project);log=root/'research'/'watchlist_activation.jsonl'
    if not log.exists():return {'status':'NO_ACTIVATION_HISTORY','activations':0,'tradeEnabled':False}
    prev='0'*64;n=0;errors=[]
    for line in log.read_bytes().splitlines():
        try:
            x=json.loads(line);unsigned={k:v for k,v in x.items() if k!='hash'}
            if x['seq']!=n+1 or x['prev']!=prev or _hash(unsigned)!=x['hash']:
                raise ValueError('BROKEN_ACTIVATION_CHAIN')
            n+=1;prev=x['hash']
        except (KeyError,ValueError,TypeError) as exc:
            errors.append(str(exc));break
    if n and not errors:
        try:
            current=json.loads((root/'watchlist.json').read_text())
            if _hash(current)!=x['newConfigSha256']:
                errors.append('WATCHLIST_DIFFERS_FROM_LAST_REGISTERED_ACTIVATION')
        except (FileNotFoundError,json.JSONDecodeError) as exc:errors.append(str(exc))
    return {'status':'PASS_LOCAL_ACTIVATION_LOG' if n and not errors else 'NO_GO_ACTIVATION_INTEGRITY',
            'activations':n,'errors':errors,'tradeEnabled':False}

def activate(project,market, *, approve=False, capacity=36,deep_count=5,now=None):
    if not approve:raise PermissionError('EXPLICIT_OPERATOR_APPROVAL_REQUIRED')
    root=Path(project);market=Path(market)
    root.mkdir(parents=True,exist_ok=True)
    with (root/'.flowdesk_live.lock').open('a+b') as fd:
        try:fcntl.flock(fd.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('FLOWDESK_IS_RUNNING_RESTART_REQUIRED')
        try:
            current=json.loads((root/'watchlist.json').read_text(encoding='utf-8'))
            validate_config(current)
            old=audit_activations(root)
            if old['status']=='NO_GO_ACTIVATION_INTEGRITY':raise RuntimeError('ACTIVATION_HISTORY_NOT_TRUSTED')
            if audit_archive(market)['status']!='PASS_LOCAL_ARCHIVE_INTEGRITY':
                raise RuntimeError('MARKET_SCAN_ARCHIVE_NOT_VERIFIED')
            candidate=propose(market,current,capacity,deep_count)
            if candidate.get('status')!='REVIEW_REQUIRED_NOT_APPLIED':
                raise RuntimeError('MARKET_PROPOSAL_UNQUALIFIED_OR_STALE')
            planned=json.loads((market/'proposed_watchlist.json').read_text(encoding='utf-8'))
            # The proposal must be byte-content-equivalent in relevant normalized fields,
            # not an arbitrary handwritten list with an old source name.
            for key in ('status','symbols','deepSymbols','benchmark','peers'):
                if planned.get(key)!=candidate.get(key):raise RuntimeError('PROPOSAL_CHANGED_OR_STALE')
            # Ensure last archived scan matches the exact files used for ranking.
            lines=[json.loads(x) for x in (market/'archive'/'scan_inventory.jsonl').read_text().splitlines() if x.strip()]
            if not lines:raise RuntimeError('NO_ARCHIVED_MARKET_SCAN')
            latest=lines[-1];manifest=json.loads((market/'archive'/latest['runId']/'archive_manifest.json').read_text())
            for name in ('us_stock_universe.csv','latest_quotes.csv','latest_report.json'):
                if manifest['filesSha256'][name]!=file_sha(market/name):
                    raise RuntimeError('MARKET_SCAN_CHANGED_AFTER_ARCHIVE')
            new={'symbols':candidate['symbols'],'deepSymbols':candidate['deepSymbols'],
                 'benchmark':candidate['benchmark'],'peers':candidate['peers']}
            validate_config(new)
            if not all(s in new['symbols'] for s in ('US.SPY','US.QQQ','US.IWM')):
                raise RuntimeError('BASELINE_MARKETS_MISSING')
            # Do not backdate the activation. The model cannot use data before the
            # new collector starts and verifies its subscriptions.
            ts=(now or datetime.now(timezone.utc))
            if ts.tzinfo is None:raise ValueError('timezone-aware activation time required')
            stamp=ts.isoformat()
            log=root/'research'/'watchlist_activation.jsonl';log.parent.mkdir(parents=True,exist_ok=True)
            old_lines=log.read_bytes().splitlines() if log.exists() else []
            last=json.loads(old_lines[-1]) if old_lines else None
            entry={'seq':len(old_lines)+1,'prev':last['hash'] if last else '0'*64,
                   'activatedAt':stamp,'effective':'NEXT_BRIDGE_START_ONLY',
                   'scanArchiveId':latest['runId'], 'scanManifestSha256':latest['manifestSha256'],
                   'proposalSha256':file_sha(market/'proposed_watchlist.json'),
                   'oldConfigSha256':_hash(current),'newConfigSha256':_hash(new),
                   'symbols':new['symbols'],'deepSymbols':new['deepSymbols'],
                   'profitCertified':False,'tradeEnabled':False}
            entry['hash']=_hash(entry)
            # Write next-run config atomically. The activation journal logs EVERY
            # approved change, including repeated proposals. Guarded by OS lock.
            backup=root/f'watchlist.before-activation-{entry["seq"]:06d}.json'
            backup.write_bytes((root/'watchlist.json').read_bytes())
            tmp=root/'.watchlist.pending.tmp'
            try:
                with tmp.open('wb') as out:
                    out.write(json.dumps(new,ensure_ascii=False,indent=2).encode()+b'\n');out.flush();os.fsync(out.fileno())
                os.replace(tmp,root/'watchlist.json')
                with log.open('ab') as out:
                    out.write(canonical(entry)+b'\n');out.flush();os.fsync(out.fileno())
            except Exception:
                tmp.unlink(missing_ok=True)
                # Fail closed: the unlogged configuration must not be silently used.
                # Restore from the exact backup as best effort, then surface failure.
                os.replace(backup,root/'watchlist.json')
                raise
            return {'status':'ACTIVATED_NEXT_RESTART_ONLY','activatedAt':stamp,
                    'activationNumber':entry['seq'],'scanArchiveId':latest['runId'],
                    'symbols':new['symbols'],'deepSymbols':new['deepSymbols'],
                    'tradeEnabled':False,'profitCertified':False}
        finally:fcntl.flock(fd.fileno(),fcntl.LOCK_UN)

def main():
    p=argparse.ArgumentParser(description='人工确认之后的下次启动观察池切换，不实时改OpenD订阅')
    p.add_argument('--market-dir',type=Path,default=Path(__file__).parent/'output'/'market')
    p.add_argument('--project-dir',type=Path,default=Path(__file__).parent)
    p.add_argument('--capacity',type=int,default=36);p.add_argument('--deep-count',type=int,default=5)
    p.add_argument('--approve',action='store_true',help='已人工审查并明确授权替换下次启动观察池')
    p.add_argument('--audit',action='store_true')
    a=p.parse_args()
    if a.audit:print(json.dumps(audit_activations(a.project_dir),ensure_ascii=False,indent=2));return
    if not a.approve:
        answer=input('请先确认候选文件和风险。输入 APPLY 才会修改下次启动 watchlist.json：')
        if answer!='APPLY':raise SystemExit('未修改。')
    print(json.dumps(activate(a.project_dir,a.market_dir,approve=True,capacity=a.capacity,deep_count=a.deep_count),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
