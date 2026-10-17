#!/usr/bin/env python3
"""Read-only pre-collection audit for FlowDesk.

No order APIs; no hidden trial PnL; no false claim of live data or verified alpha.
The only output mutation is an explicitly selected JSON report path.
"""
from __future__ import annotations
import argparse, ast, hashlib, json, os, re, socket, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = ('bridge.py','runtime.py','core.py','intelligence.py','flow.py',
            'recorder.py','feed_guard.py','prospective.py','trial_registry.py',
            'market_scan.py','market_archive.py','market_robustness.py',
            'watchlist.json','requirements.txt','Start.command','ScanMarket.command',
            'dashboard.html')
ORDER_ACTIONS = frozenset(('place_order','modify_order','cancel_order','unlock_trade','place_algo_order'))


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def parse_iso(value):
    try:
        t=datetime.fromisoformat(value.replace('Z','+00:00'))
        return t.astimezone(timezone.utc) if t.tzinfo else None
    except (ValueError,TypeError,AttributeError):return None


def check_sources(project):
    project=Path(project)
    missing=[s for s in REQUIRED if not (project/s).is_file()]
    source_hashes={}
    violations=[];unreadable=[]
    for path in sorted(project.glob('*.py')):
        try:
            blob=path.read_bytes();tree=ast.parse(blob,filename=path.name)
            source_hashes[path.name]=hashlib.sha256(blob).hexdigest()
        except (OSError,SyntaxError,UnicodeError) as exc:
            unreadable.append({'file':path.name,'error':type(exc).__name__});continue
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr in ORDER_ACTIONS:
                violations.append({'file':path.name,'api':node.func.attr,'line':node.lineno})
    okay=not missing and not violations and not unreadable
    return {'status':'PASS_LOCAL_SOURCE_SHAPE_ONLY' if okay else 'NO_GO_CODE_STRUCTURE',
            'missingFiles':missing,'orderActionCallSites':violations,
            'unparsedSources':unreadable,'sourceCount':len(source_hashes),
            'sourceHashesSha256':hashlib.sha256(json.dumps(source_hashes,sort_keys=True).encode()).hexdigest(),
            'note':'Local AST scan is not a proof of absence of dynamic or indirect order calls.'}


def check_config(project):
    try:
        from runtime import validate_config
        config=json.loads((Path(project)/'watchlist.json').read_text(encoding='utf-8'))
        validated=validate_config(config)
        return {'status':'PASS_CONFIG_ONLY','symbolCount':len(validated['symbols']),
                'deepSymbolCount':len(validated['deepSymbols']),
                'benchmark':validated['benchmark'],
                'configSha256':digest(Path(project)/'watchlist.json')}
    except (OSError,ValueError,TypeError,KeyError,ImportError) as exc:
        return {'status':'NO_GO_CONFIG','error':str(exc)[:220]}


def check_latest(project,now):
    root=Path(project)/'output'/'market';file=root/'latest_report.json'
    if not file.is_file():return {'status':'NO_GO_NO_CURRENT_MARKET_SCAN','ageSeconds':None}
    try:
        obj=json.loads(file.read_text(encoding='utf-8'))
        files={'us_stock_universe.csv':obj['universeSha256'],
               'latest_quotes.csv':obj['quotesSha256']}
        if obj.get('benchmarksSha256'):files['latest_benchmarks.csv']=obj['benchmarksSha256']
        mismatches=[name for name,expected in files.items()
                    if not (root/name).is_file() or digest(root/name)!=expected]
        if mismatches:return {'status':'NO_GO_CURRENT_SCAN_TAMPERED','mismatches':mismatches}
        completed=parse_iso(obj.get('completedAt'))
        if not completed:return {'status':'NO_GO_SCAN_TIME_MISSING'}
        seconds=(now-completed).total_seconds()
        if seconds< -5:return {'status':'NO_GO_SCAN_FUTURE_CLOCK','ageSeconds':round(seconds,1)}
        if seconds>180:return {'status':'NO_GO_SCAN_TOO_OLD','ageSeconds':round(seconds,1),
                                'sourceGate':obj.get('dataGate')}
        if obj.get('dataGate') not in ('PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY',
                                      'PASS_SNAPSHOT_QUALITY_RESEARCH_ONLY'):
            return {'status':'NO_GO_SCAN_COVERAGE','ageSeconds':round(seconds,1),
                    'sourceGate':obj.get('dataGate'),
                    'listedFreshUsableCount':obj.get('listedFreshUsableCount',0)}
        return {'status':'PASS_RECENT_RESEARCH_SNAPSHOT_ONLY','ageSeconds':round(seconds,1),
                'sourceGate':obj['dataGate'],'quotedCount':obj.get('snapshotReceivedCount',0),
                'pointInTimeHistoricalUniverse':False,'realOrderFlowConfirmed':False}
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        return {'status':'NO_GO_INVALID_CURRENT_SCAN','reason':str(exc)[:180]}


def check_archive(project):
    try:
        from market_archive import audit_archive
        result=audit_archive(Path(project)/'output'/'market')
        return {'status':result['status'],'verified':result.get('verified',0),
                'errors':result.get('errors',[])}
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        return {'status':'NO_GO_ARCHIVE_AUDIT_ERROR','reason':str(exc)[:160]}


def check_recordings(project,deep=False):
    root=Path(project)/'recordings'
    try:
        from run_inventory import read_inventory
        entries,present=read_inventory(root)
        actual={p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
        expected={r['runId'] for r in entries}
        missing=sorted(expected-actual);extra=sorted(actual-expected)
        if missing or extra:return {'status':'NO_GO_RUN_INVENTORY_MISMATCH',
                                    'registered':len(entries),'missingRuns':missing[:20],
                                    'unregisteredRuns':extra[:20]}
        if not present:return {'status':'NO_GO_NO_RUN_INVENTORY','registered':0}
        if not entries:return {'status':'NO_GO_NO_RECORDED_SESSIONS','registered':0}
        if not deep:return {'status':'UNVERIFIED_RAW_SEGMENTS_DEEP_AUDIT_REQUIRED',
                            'registered':len(entries),'note':'Inventory checked; raw segment contents not scanned.'}
        from audit_recordings import audit_all_recordings
        report=audit_all_recordings(root)
        return {'status':report['verdict'],'registered':len(entries),
                'failedRuns':[v for v in report.get('sessions',[]) if v.get('status')!='PASS_LOCAL_FILE_INTEGRITY'][:20],
                'note':'File integrity does not certify exchange coverage.'}
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        return {'status':'NO_GO_RUN_INVENTORY_CORRUPT','reason':str(exc)[:160]}


def check_trial(project,now):
    try:
        from trial_registry import status
        result=status(project,now=now)
        return {'status':result['status'],'trialId':result.get('trialId'),
                'frozenSourceValid':result['status']!='UNREGISTERED',
                'note':'Never inspects PnL; trial must be frozen before collecting future test data.'}
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        return {'status':'NO_GO_TRIAL_INTEGRITY','reason':str(exc)[:160]}


def check_sdk():
    try:
        from importlib.metadata import version,PackageNotFoundError
        installed=version('moomoo-api')
        want='10.11.7108'
        return {'status':'PASS_PINNED_SDK' if installed==want else 'NO_GO_SDK_VERSION',
                'installed':installed,'required':want}
    except Exception as exc:
        return {'status':'NO_GO_SDK_NOT_INSTALLED','reason':type(exc).__name__,
                'note':'Start.command installs the pinned package on the user Mac.'}


def check_opend(enabled):
    if not enabled:return {'status':'NOT_CHECKED_LOCAL_TCP','note':'No connection was attempted.'}
    try:
        with socket.create_connection(('127.0.0.1',11111),timeout=1):pass
        return {'status':'TCP_PORT_RESPONDS_ONLY','note':'Does NOT verify OpenD login, quote entitlement, or fresh data.'}
    except OSError as exc:
        return {'status':'NO_GO_OPEN_D_TCP','reason':type(exc).__name__}


def run_tests(project,enabled):
    if not enabled:return {'status':'NOT_RUN','reason':'Use --tests to execute full regression'}
    try:
        proc=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-q'],
                             cwd=project,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},
                             capture_output=True,text=True,timeout=180)
        last=(proc.stdout+'\n'+proc.stderr).strip()[-1600:]
        match=re.search(r'Ran\s+(\d+)\s+tests',last)
        return {'status':'PASS_REGRESSION_TESTS' if proc.returncode==0 and match else 'NO_GO_REGRESSION_TESTS',
                'testsRun':int(match.group(1)) if match else None,'summary':last,'exitCode':proc.returncode}
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {'status':'NO_GO_REGRESSION_TESTS','reason':type(exc).__name__}


def audit(project,*,now=None,tests=False,deep=False,opend=False):
    project=Path(project)
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('UTC_AWARE_TIME_REQUIRED')
    now=now.astimezone(timezone.utc)
    blocks={
        'source':check_sources(project),
        'configuration':check_config(project),
        'regression':run_tests(project,tests),
        'sdk':check_sdk(),
        'opendTcp':check_opend(opend),
        'marketSnapshot':check_latest(project,now),
        'marketArchive':check_archive(project),
        'rawRuns':check_recordings(project,deep),
        'frozenTrial':check_trial(project,now)
    }
    # Passing offline software tests must never be conflated with actual broker
    # account returns. The account has no live execution path in this project.
    software=blocks['source']['status']=='PASS_LOCAL_SOURCE_SHAPE_ONLY' and blocks['configuration']['status']=='PASS_CONFIG_ONLY' and blocks['regression']['status']=='PASS_REGRESSION_TESTS'
    collectionReady=(software and blocks['sdk']['status']=='PASS_PINNED_SDK' and
                     (not opend or blocks['opendTcp']['status']=='TCP_PORT_RESPONDS_ONLY') and
                     not blocks['frozenTrial']['status'].startswith('NO_GO'))
    confirmedNewFeed=False  # Requires full fresh feed and an independent live capture test, not TCP or a snapshot.
    return {'schema':1,'atUtc':now.isoformat(),'project':str(project),
            'product':'FlowDesk prelaunch engineering audit',
            'softwareRegressionGate':'PASS_OFFLINE_TESTS_ONLY' if software else 'NO_GO_OFFLINE_ENGINEERING',
            'canAttemptControlledCollection':('YES_ENGINEERING_ONLY' if collectionReady else
                 'NO_GO_FIX_ENGINEERING_FIRST' if not software else 'NO_GO_LOCAL_SDK_OR_OPEN_D'),
            'liveMoomooStreamCertified':confirmedNewFeed,
            'alphaCertified':False,'liveOrdersEnabled':False,
            'liveTradingVerdict':'NO_GO_NO_VERIFIED_ALPHA_OR_BROKER_EXECUTION',
            'checks':blocks,
            'nextGates':['Verify real OpenD rights and continuous live RTH quote/book/ticker coverage on Mac',
                         'Complete pre-registered untouched future trading sessions',
                         'Measure fill rates and real commissions/slippage/market impact',
                         'Pass portfolio risk limits and explicit live execution review'],
            'disclosure':'This local preflight cannot prove positive trading expectancy or guarantee stable profits.'}


def main():
    p=argparse.ArgumentParser(description='FlowDesk 上线前工程总验收；只读，不下单')
    p.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--out',type=Path,default=None)
    p.add_argument('--tests',action='store_true',help='运行完整回归测试')
    p.add_argument('--deep-audit',action='store_true',help='读取和校验录制文件每一段（大型数据可能耗时）')
    p.add_argument('--check-opend',action='store_true',help='仅检查 127.0.0.1:11111 TCP 端口')
    args=p.parse_args()
    result=audit(args.project,tests=args.tests,deep=args.deep_audit,opend=args.check_opend)
    dest=args.out or args.project/'research'/'prelaunch'/'preflight_report.json'
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp=dest.with_suffix(dest.suffix+'.tmp')
    temp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temp.replace(dest)
    print(json.dumps({'softwareRegressionGate':result['softwareRegressionGate'],
                      'canAttemptControlledCollection':result['canAttemptControlledCollection'],
                      'liveTradingVerdict':result['liveTradingVerdict'],
                      'checks':{k:v['status'] for k,v in result['checks'].items()},
                      'report':str(dest)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
