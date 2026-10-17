#!/usr/bin/env python3
"""Explicit ex-ante, locally sealed prospective research trial. NO order API.

The freeze occurs before the first TEST ET session, pins the experiment code and
one primary hypothesis. A local hash chain deters accidental edits, but is NOT
remote notarization, tamper-proof time, an exchange clock, or broker-fill evidence.
No PnL is revealed during the test window. Finalization is one shot.
"""
from __future__ import annotations
import argparse, fcntl, hashlib, json, math, os, statistics, uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from prospective import RULES, COSTS, sha_bytes, canonical, verify_chain, moment
from feed_guard import standard_market_day
from run_inventory import read_inventory

ET=ZoneInfo('America/New_York')
FILES=tuple(sorted(x.name for x in Path(__file__).resolve().parent.glob('*.py')))

DEFAULT_SESSIONS=60
MIN_QUALIFIED_SHARE=.90
MIN_ACTIVE_DAYS=15
MIN_CLOSED_EXITS=20


def _utc(value=None):
    x=value or datetime.now(timezone.utc)
    if not isinstance(x,datetime) or x.tzinfo is None:raise ValueError('UTC_AWARE_CLOCK_REQUIRED')
    return x.astimezone(timezone.utc)


def _sessions(start,n):
    days=[];d=start
    while len(days)<n:
        if standard_market_day(d)[0]:days.append(d.isoformat())
        d+=timedelta(days=1)
    return days


def _registry_root(project):return Path(project)/'research'/'sealed_trials'


def _sha(path):return sha_bytes(Path(path).read_bytes())


def _fingerprints(project):
    project=Path(project)
    return {f:_sha(project/f) for f in FILES}


def _read_registry(project):
    root=_registry_root(project);r=root/'registry.jsonl';entries=[];prev='0'*64
    if not r.exists():return [],False
    with r.open('rb') as stream:
        for raw in stream:
            if not raw.endswith(b'\n'):raise ValueError('TRUNCATED_TRIAL_REGISTRY')
            obj=json.loads(raw)
            unsigned={k:obj[k] for k in ('seq','prevHash','trialId','frozenAt','protocolSha256')}
            if (unsigned['seq']!=len(entries)+1 or unsigned['prevHash']!=prev
                    or sha_bytes(canonical(unsigned))!=obj.get('hash')):
                raise ValueError('TAMPERED_TRIAL_REGISTRY')
            entries.append(unsigned);prev=obj['hash']
    if len({e['trialId'] for e in entries})!=len(entries):raise ValueError('DUPLICATE_TRIAL_ID')
    return entries,True


def _trial(project,trial_id):
    if not isinstance(trial_id,str) or not trial_id or '/' in trial_id or '\\' in trial_id or '..' in trial_id:
        raise ValueError('INVALID_TRIAL_ID')
    root=_registry_root(project)/trial_id
    file=root/'protocol.json';seal=root/'protocol.sha256'
    if not (file.is_file() and seal.is_file()):raise ValueError('TRIAL_PROTOCOL_MISSING')
    blob=file.read_bytes();digest=sha_bytes(blob)
    if digest!=seal.read_text().strip():raise ValueError('TRIAL_PROTOCOL_TAMPERED')
    plan=json.loads(blob)
    if plan.get('trialId')!=trial_id or plan.get('protocolSchema')!=1:raise ValueError('TRIAL_SCHEMA_MISMATCH')
    return plan,digest


def _verify_trial(project,trial_id):
    entries,present=_read_registry(project)
    if not present:raise ValueError('TRIAL_REGISTRY_MISSING')
    matching=[e for e in entries if e['trialId']==trial_id]
    if len(matching)!=1:raise ValueError('UNREGISTERED_TRIAL')
    plan,digest=_trial(project,trial_id)
    row=matching[0]
    if digest!=row['protocolSha256'] or plan['frozenAt']!=row['frozenAt']:
        raise ValueError('TRIAL_REGISTRY_PROTOCOL_MISMATCH')
    if plan['sourceFingerprints']!=_fingerprints(project):raise ValueError('FROZEN_RESEARCH_CODE_CHANGED')
    if plan['hypotheses']!=list(RULES) or plan['costsBps']!=list(COSTS):
        raise ValueError('FROZEN_RESEARCH_CONSTANTS_CHANGED')
    if plan['calendarSessions']!=_sessions(date.fromisoformat(plan['startEt']),len(plan['calendarSessions'])):
        raise ValueError('TRIAL_SESSION_SCHEDULE_CHANGED')
    return plan,digest


def _freeze_under_live_lock(project, *, now=None, start=None, sessions=DEFAULT_SESSIONS,
           rule=None, minimum_fraction=MIN_QUALIFIED_SHARE):
    """One registered trial; no training on future data, no PnL inspection."""
    project=Path(project);t=_utc(now)
    if sessions<30 or sessions>250:raise ValueError('SESSION_COUNT_30_TO_250')
    if not .90<=minimum_fraction<=1.:raise ValueError('QUALIFIED_FRACTION_MUST_BE_90_TO_100_PCT')
    chosen=rule or RULES[0]['id']
    if chosen not in {r['id'] for r in RULES}:raise ValueError('RULE_NOT_PRE_REGISTERED')
    today=t.astimezone(ET).date()
    begin=date.fromisoformat(start) if isinstance(start,str) else start
    if begin is None:begin=today+timedelta(days=1)
    if begin<=today:raise ValueError('TRIAL_START_MUST_BE_IN_FUTURE_ET_DAY')
    if not standard_market_day(begin)[0]:
        if start is not None:raise ValueError('TRIAL_START_NOT_TRADING_DAY')
        while not standard_market_day(begin)[0]:begin+=timedelta(days=1)
    schedule=_sessions(begin,sessions)
    root=_registry_root(project);root.mkdir(parents=True,exist_ok=True)
    with (root/'.registry.lock').open('a+b') as guard:
        fcntl.flock(guard.fileno(),fcntl.LOCK_EX)
        entries,_=_read_registry(project)
        # Enforce a single unfinished trial to avoid shopping simultaneous protocols.
        for item in entries:
            prev,_=_trial(project,item['trialId'])
            if date.fromisoformat(prev['endEt'])>=today:
                raise ValueError('EXISTING_ACTIVE_TRIAL_MUST_FINISH_BEFORE_NEW_FREEZE')
        trial_id='trial-'+t.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        protocol={'protocolSchema':1,'trialId':trial_id,'frozenAt':t.isoformat(),
                  'startEt':schedule[0],'endEt':schedule[-1],'calendarSessions':schedule,
                  'calendarRule':'STANDARD_US_EQUITY_RULES_NOT_VERIFIED_FOR_SPECIAL_CLOSURES',
                  'primaryRule':chosen,'hypotheses':list(RULES),'costsBps':list(COSTS),
                  'primaryCostBps':40,'minimumQualifiedSessions':math.ceil(sessions*minimum_fraction),
                  'minimumClosedLongExits':MIN_CLOSED_EXITS,'minimumActiveEtDays':MIN_ACTIVE_DAYS,
                  'sourceFingerprints':_fingerprints(project),
                  'scope':'CURRENT_MOOMOO_SUBSCRIBED_EQUITIES_NOT_FULL_MARKET_TAQ',
                  'execution':'COUNTERFACTUAL_ORDER_BOOK_QUOTES_ONLY',
                  'trialPolicy':'ONE_PRIMARY_HYPOTHESIS_NO_INTERIM_PNL_NO_RULE_CHANGES',
                  'independentNotary':False,'liveOrdersEnabled':False,'profitCertified':False}
        path=root/trial_id;path.mkdir(exist_ok=False)
        blob=canonical(protocol)+b'\n';digest=sha_bytes(blob)
        (path/'protocol.json').write_bytes(blob)
        (path/'protocol.sha256').write_text(digest+'\n')
        previous='0'*64
        if entries:
            previous=json.loads((root/'registry.jsonl').read_text().splitlines()[-1])['hash']
        row={'seq':len(entries)+1,'prevHash':previous,'trialId':trial_id,
             'frozenAt':protocol['frozenAt'],'protocolSha256':digest}
        row['hash']=sha_bytes(canonical(row))
        with (root/'registry.jsonl').open('ab') as f:
            f.write(canonical(row)+b'\n');f.flush();os.fsync(f.fileno())
        (root/'active_trial.json').write_bytes(canonical({'trialId':trial_id,'sha256':digest})+b'\n')
        return {'status':'FROZEN_EX_ANTE_LOCAL_ONLY','trialId':trial_id,'protocolSha256':digest,
                'primaryRule':chosen,'startEt':schedule[0],'endEt':schedule[-1],
                'scheduledSessions':len(schedule),'minimumQualifiedSessions':protocol['minimumQualifiedSessions'],
                'liveOrdersEnabled':False,'profitCertified':False}


def freeze(project, *, now=None, start=None, sessions=DEFAULT_SESSIONS,
           rule=None, minimum_fraction=MIN_QUALIFIED_SHARE):
    """Refuse freezing while an existing collector is running without trial binding."""
    project=Path(project)
    with (project/'.flowdesk_live.lock').open('a+b') as guard:
        try:fcntl.flock(guard.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ValueError('LIVE_COLLECTOR_ACTIVE_STOP_BEFORE_FREEZE') from e
        return _freeze_under_live_lock(project,now=now,start=start,sessions=sessions,
                                       rule=rule,minimum_fraction=minimum_fraction)


def active_binding(project,*,now=None):
    """Called before creating a LIVE Recorder. Missing protocol is never an OOS trial."""
    active=_registry_root(project)/'active_trial.json'
    if not active.exists():
        entries,_=_read_registry(project)
        if entries:
            last,_=_trial(project,entries[-1]['trialId'])
            if _utc(now).astimezone(ET).date()<=date.fromisoformat(last['endEt']):
                raise ValueError('ACTIVE_TRIAL_FILE_REMOVED_DURING_WINDOW')
        return {'status':'NO_PRE_REGISTERED_TRIAL','trialId':None,'trialSealSha256':None}
    obj=json.loads(active.read_text())
    plan,seal=_verify_trial(project,obj['trialId'])
    if obj.get('sha256')!=seal:raise ValueError('ACTIVE_TRIAL_SEAL_MISMATCH')
    if _utc(now).astimezone(ET).date()>date.fromisoformat(plan['endEt']):
        return {'status':'TRIAL_WINDOW_ENDED','trialId':None,'trialSealSha256':None}
    return {'status':'PREREGISTERED_LOCAL_TRIAL_ONLY','trialId':plan['trialId'],
            'trialSealSha256':seal,'startsEt':plan['startEt'],'endsEt':plan['endEt']}


def _window_closed(plan, now):
    clock=_utc(now).astimezone(ET)
    last=date.fromisoformat(plan['endEt'])
    if clock.date()>last:return True
    if clock.date()<last:return False
    session,close_h=standard_market_day(last)
    return bool(session and clock.hour>=close_h+1)


def status(project,*,now=None):
    """Blinded status before trial end: never reads shadow PnL."""
    entries,present=_read_registry(project)
    if not present:return {'status':'UNREGISTERED','trialCount':0,'liveOrdersEnabled':False}
    last=entries[-1];plan,digest=_verify_trial(project,last['trialId'])
    return {'status':'READY_FOR_ONE_SHOT_FINAL' if _window_closed(plan,now) else 'BLINDED_FUTURE_TEST_IN_PROGRESS',
            'trialId':plan['trialId'],'startEt':plan['startEt'],'endEt':plan['endEt'],
            'primaryRule':plan['primaryRule'],'registeredTrials':len(entries),
            'protocolSha256':digest,'performanceHiddenBeforeClose':True,
            'finalized':(_registry_root(project)/last['trialId']/'final_report.json').exists(),
            'liveOrdersEnabled':False,'profitCertified':False}


def evaluate_trial(project, record_root, trial_id, *,now=None):
    """Full source audit; NO execution authorization even after positive outcome."""
    from session_coverage import audit_recording_coverage
    from source_crosscheck import crosscheck
    from portfolio_audit import replay_portfolio
    from chronological_validation import block_bootstrap_mean_ci
    root=Path(record_root);plan,seal=_verify_trial(project,trial_id)
    if not _window_closed(plan,now):raise ValueError('TRIAL_END_NOT_REACHED_NO_PEEK')
    registered,inventory_present=read_inventory(root)
    scheduled=set(plan['calendarSessions']);starts={};run_errors=[];coverage={d:[] for d in scheduled};forward=[]
    if not inventory_present:run_errors.append('RAW_RUN_INVENTORY_MISSING')
    # Inspect every registered run, including missing, corrupt, unbound or incomplete runs.
    for entry in registered:
        rid=entry['runId'];run_dir=root/rid
        try:
            started=moment(entry['startedAt'])
            if started is None:raise ValueError('NAIVE_RUN_START')
            starts[rid]=started
            if not run_dir.exists():raise ValueError('REGISTERED_RUN_FOLDER_MISSING')
            metadata=json.loads((run_dir/'metadata.json').read_text())
            if metadata.get('trialId')!=trial_id:
                # All live runs which start during the window must be labelled.
                if started.astimezone(ET).date().isoformat() in scheduled:
                    raise ValueError('UNBOUND_RUN_DURING_FROZEN_WINDOW')
                continue
            if metadata.get('trialSealSha256')!=seal:raise ValueError('TRIAL_BINDING_TAMPERED')
            if metadata.get('closed') is not True:raise ValueError('UNFINISHED_RUN')
            if started<=moment(plan['frozenAt']):raise ValueError('RUN_PRECEDES_TRIAL_FREEZE')
            daily=audit_recording_coverage(run_dir)
            from prospective import verify_chain
            trial_plan,events=verify_chain(run_dir/'forward_shadow')
            if trial_plan.get('sourceSha256')!=plan['sourceFingerprints']['prospective.py']:
                raise ValueError('SOURCE_CODE_DIFFERENT_FROM_FROZEN_PROTOCOL')
            if crosscheck(run_dir/'forward_shadow')['status']!='MATCHED_LOCAL_CALLBACK_RECORDS':
                raise ValueError('SHADOW_NOT_BOUND_TO_RAW_BOOK')
            for day in daily['days']:
                d=day['marketDateET']
                if d in scheduled:coverage[d].append({'runId':rid,'status':day['status']})
            selected=[]
            for ev in events:
                stamp=ev.get('at') or ev.get('exitAt') or ev.get('entryAt')
                t=moment(stamp) if stamp else None
                if t and t.astimezone(ET).date().isoformat() in scheduled:
                    if ev.get('rule')==plan['primaryRule'] or ev.get('type')=='RUN_END':
                        selected.append(ev)
            forward.append((rid,selected))
        except Exception as exc:
            run_errors.append(f'{rid}: {str(exc)[:180]}')
    absent=[d for d,records in coverage.items() if not records]
    ambiguous=[d for d,records in coverage.items() if len(records)>1]
    qualified=[d for d,recs in coverage.items() if len(recs)==1 and recs[0]['status']=='RESEARCH_FEED_COVERAGE_DAY_ONLY']
    qualified.sort()
    incomplete=[d for d in plan['calendarSessions'] if d not in qualified]
    result={'status':'NO_GO_FUTURE_EVIDENCE','trialId':trial_id,'protocolSha256':seal,
            'startEt':plan['startEt'],'endEt':plan['endEt'], 'scheduledSessions':len(scheduled),
            'qualifiedSessions':len(qualified),'requiredQualifiedSessions':plan['minimumQualifiedSessions'],
            'missingSessions':absent,'ambiguousSessions':ambiguous,'nonQualifiedSessions':incomplete,
            'runErrors':run_errors,'selectedRuleFrozenBeforeTest':plan['primaryRule'],
            'singlePrimaryRuleCapitalReplay':True,'allScheduledDaysRequiredForAdvancement':True,
            'noInterimPnl':True,'localSealOnlyNoIndependentTimestampAuthority':True,
            'quoteModelNotBrokerFills':True,'brokerFillVerified':False,
            'tradeEnabled':False,'profitCertified':False,'test':None}
    # Even one missing/poor session can conceal adverse outcomes; no positive
    # research advancement when ANY scheduled session is not auditable.
    if run_errors or ambiguous or len(qualified)!=len(scheduled) or not forward:
        return result
    portfolio=replay_portfolio(forward)
    result['capitalAudit']={'diagnosticComplete':portfolio['diagnosticComplete'],
                            'closedLongModelExits':portfolio['closedLongModelExits'],
                            'censoredAdmittedPositions':portfolio['censoredAdmittedPositions'],
                            'accountUsd':portfolio['accountUsd']}
    if not portfolio['diagnosticComplete']:return result
    rule=plan['primaryRule'];account=portfolio['accountUsd'];cells=portfolio['ruleExitDayClosedOnlyPnlUsd']
    counts=portfolio['ruleExitDayClosedOnlyCounts']
    active=sum(counts.get(d,{}).get(rule,0)>0 for d in qualified)
    exits=sum(counts.get(d,{}).get(rule,0) for d in qualified)
    result['primaryActivity']={'activeDays':active,'closedModelExits':exits}
    if active<plan['minimumActiveEtDays'] or exits<plan['minimumClosedLongExits']:
        return result
    scenarios={}
    for cost in ('8','20','40'):
        values=[float(cells.get(d,{}).get(rule,{}).get(cost,0.0))/account*10000 for d in plan['calendarSessions']]
        ci=block_bootstrap_mean_ci(values,draws=1500)
        scenarios[cost]={'averageScheduledDayAccountContributionBps':round(statistics.fmean(values),8),
                         'totalScheduledPeriodAccountContributionBps':round(sum(values),8),
                         'exploratoryBlockBootstrapMeanCi95':ci,
                         'positiveScheduledDays':sum(v>0 for v in values)}
    result['test']=scenarios
    # Cumulative *realized closed-only* drawdown, not an intratrade MTM drawdown.
    proxy=[float(cells.get(d,{}).get(rule,{}).get('40',0.0)) for d in plan['calendarSessions']]
    cumulative=peak=0.0;maxdd=0.0
    for value in proxy:
        cumulative+=value;peak=max(peak,cumulative);maxdd=max(maxdd,peak-cumulative)
    result['riskDescriptive']={'closedOnlyMaxDrawdownUsd40':round(maxdd,4),
                               'worstDailyClosedOnlyPnlUsd40':round(min(proxy),4),
                               'largestPositiveDayPnlUsd40':round(max(proxy),4),
                               'unrealizedAndTailRiskNotKnown':True}
    result['status']=('PROMISING_COUNTERFACTUAL_QUOTE_MODEL_ONLY' if scenarios['40']['exploratoryBlockBootstrapMeanCi95']
                      and scenarios['40']['exploratoryBlockBootstrapMeanCi95'][0]>0 else 'NO_GO_COST40_OR_UNCERTAINTY')
    return result


def finalize(project,recordings,*,now=None,trial_id=None):
    root=_registry_root(project);entries,_=_read_registry(project)
    if not entries:raise ValueError('NO_REGISTERED_TRIAL')
    trial_id=trial_id or entries[-1]['trialId']
    plan,_=_verify_trial(project,trial_id)
    if not _window_closed(plan,now):raise ValueError('TRIAL_NOT_YET_CLOSED_BLINDING_ENFORCED')
    folder=root/trial_id;claim=folder/'finalization.claim'
    fd=os.open(claim,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:
        f.write(_utc(now).isoformat()+'\n');f.flush();os.fsync(f.fileno())
    try:
        report=evaluate_trial(project,recordings,trial_id,now=now)
    except Exception as exc:
        report={'status':'NO_GO_FINAL_AUDIT_ERROR','reason':str(exc)[:250],
                'trialId':trial_id,'tradeEnabled':False,'profitCertified':False}
    path=folder/'final_report.json';tmp=folder/'final_report.tmp'
    tmp.write_bytes(canonical(report)+b'\n');os.replace(tmp,path)
    (folder/'final_report.sha256').write_text(_sha(path)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description='正式未来样本注册与一次性审计（只读，无交易）')
    sub=p.add_subparsers(dest='action',required=True)
    f=sub.add_parser('freeze');f.add_argument('--start');f.add_argument('--sessions',type=int,default=60)
    f.add_argument('--rule',choices=[r['id'] for r in RULES],default=RULES[0]['id'])
    sub.add_parser('status')
    fin=sub.add_parser('finalize');fin.add_argument('--recordings',type=Path)
    a=p.parse_args();project=Path(__file__).resolve().parent
    try:
        if a.action=='freeze':out=freeze(project,start=a.start,sessions=a.sessions,rule=a.rule)
        elif a.action=='status':out=status(project)
        else:out=finalize(project,a.recordings or project/'recordings')
    except (ValueError,OSError,KeyError,TypeError,RuntimeError) as e:
        out={'status':'NO_GO_TRIAL_OPERATION','error':str(e),'tradeEnabled':False,'profitCertified':False}
        print(json.dumps(out,ensure_ascii=False,indent=2));raise SystemExit(2)
    print(json.dumps(out,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
