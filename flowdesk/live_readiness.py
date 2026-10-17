#!/usr/bin/env python3
"""Fail-closed pre-live evidence dossier for original FlowDesk.

No broker/order access. A positive quote-model trial NEVER certifies real-money
performance. This tool distinguishes prerequisite progress from an authorization
or promise that a strategy will work live.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def _sha(raw:bytes)->str:return hashlib.sha256(raw).hexdigest()


def _load_probe(path,now):
    if not path.is_file():return {'status':'MISSING','reason':'PAPER_PROBE_NOT_RUN'}
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        stamp=datetime.fromisoformat(data['inspectedAt'].replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError('NAIVE_PAPER_PROBE_TIME')
        age=(now-stamp).total_seconds()
        if age < -300 or age > 72*3600:
            return {'status':'STALE','reason':'PAPER_ACCOUNT_PROBE_OVER_72H_OR_IN_FUTURE'}
        if data.get('status')=='PAPER_ACCOUNT_DISCOVERED_NOT_EXECUTION_TESTED' and \
           data.get('paperStockAccountCount',0)>0 and data.get('orderApiUsed') is False:
            return {'status':'PAPER_ACCOUNT_ACCESS_ONLY','ageHours':round(max(0,age)/3600,1)}
        return {'status':'NO_GO','reason':'US_STOCK_SIMULATE_ACCOUNT_NOT_VERIFIED'}
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        return {'status':'NO_GO','reason':'INVALID_PAPER_PROBE','errorType':type(exc).__name__}


def _source_audit(project,*,recording=None):
    if recording is None:
        # Avoid trusting an arbitrary cached JSON / passing an unverified old run.
        return {'status':'MISSING','qualifiedDays':0,'reason':'NO_CLOSED_RECORDING_SELECTED'}
    try:
        from session_coverage import audit_recording_coverage
        r=audit_recording_coverage(recording,strict=True)
        return {'status':r['status'],'qualifiedDays':r['independentQualifiedDays'],
                'observedDays':r['tradingDaysObserved'], 'closed':r['closedRun'],
                'rawIntegrity':r['rawIntegrity'], 'provenance':'VERIFIED_LOCAL_RECORDING_ONLY'}
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        return {'status':'NO_GO_INVALID_RAW_EVIDENCE','qualifiedDays':0,
                'reason':str(exc)[:140]}


def _trial_evidence(project,now):
    # First ask the *blinded* trial status. Never read an interim trade PnL.
    import trial_registry as tr
    try:
        state=tr.status(project,now=now)
        out={'status':state['status'],'registered':state.get('trialCount',state.get('registeredTrials',0))}
        if state['status']=='UNREGISTERED':return out
        trial_id=state['trialId']
        if state['status']!='READY_FOR_ONE_SHOT_FINAL':return out
        folder=Path(project)/'research'/'sealed_trials'/trial_id
        report_path=folder/'final_report.json';seal_path=folder/'final_report.sha256'
        if not (report_path.is_file() and seal_path.is_file() and (folder/'finalization.claim').is_file()):
            return {'status':'WAITING_FOR_OFFICIAL_ONE_SHOT_FINALIZATION','registered':True}
        blob=report_path.read_bytes()
        if _sha(blob)!=seal_path.read_text(encoding='utf-8').strip():
            return {'status':'NO_GO_FINAL_REPORT_TAMPERED'}
        report=json.loads(blob)
        plan,protocol_hash=tr._verify_trial(project,trial_id)
        if report.get('trialId')!=trial_id or report.get('protocolSha256')!=protocol_hash:
            return {'status':'NO_GO_FINAL_REPORT_WRONG_TRIAL_OR_SEAL'}
        positive=(report.get('status')=='PROMISING_COUNTERFACTUAL_QUOTE_MODEL_ONLY' and
                  report.get('quoteModelNotBrokerFills') is True and
                  report.get('brokerFillVerified') is False and
                  report.get('tradeEnabled') is False and
                  report.get('profitCertified') is False and
                  report.get('qualifiedSessions')==report.get('scheduledSessions')==len(plan['calendarSessions']) and
                  report.get('capitalAudit',{}).get('diagnosticComplete') is True and
                  report.get('capitalAudit',{}).get('censoredAdmittedPositions')==0 and
                  isinstance(report.get('test',{}).get('40',{}).get('exploratoryBlockBootstrapMeanCi95'),list) and
                  report['test']['40']['exploratoryBlockBootstrapMeanCi95'][0]>0)
        return {'status':('POSITIVE_QUOTE_MODEL_REQUIRES_BROKER_VERIFICATION' if positive else 'NO_GO_FUTURE_TRIAL'),
                'officialFinalStatus':report.get('status'),'trialId':trial_id,
                'scheduledSessions':len(plan['calendarSessions']),
                'qualifiedSessions':report.get('qualifiedSessions',0),
                'brokerFillVerified':False,'localProtocolOnly':True,
                'sealedReportSha256':_sha(blob)}
    except (OSError,ValueError,TypeError,KeyError,RuntimeError,json.JSONDecodeError) as exc:
        return {'status':'NO_GO_TRIAL_INTEGRITY','reason':str(exc)[:160]}


def assess(project, *, recording=None, paper_probe=None, now=None):
    project=Path(project)
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('UTC_AWARE_TIME_REQUIRED')
    now=now.astimezone(timezone.utc)
    raw=_source_audit(project,recording=recording)
    trial=_trial_evidence(project,now)
    paper=_load_probe(Path(paper_probe) if paper_probe else project/'research'/'readiness'/'paper_probe.json',now)
    evidence={'closedFeedEvidence':raw,'preregisteredForwardTrial':trial,
              'paperAccountAccess':paper,
              'paperOrderLifecycle':'NOT_EXECUTED_OR_RECONCILED',
              'realBrokerFills':'NOT_PROVIDED_OR_VERIFIED',
              'riskAndMarketAccess':{'readOnlyCollector':True,'noRealOrderPath':True,
                                     'shortBorrowProof':'MISSING',
                                     'brokerReconciliation':'MISSING'}}
    blockers=[]
    # The finalized pre-registered trial independently checks the entire set of
    # scheduled, closed recording runs. A one-run diagnostic can cover only one day.
    qualified_by_trial=(trial['status']=='POSITIVE_QUOTE_MODEL_REQUIRES_BROKER_VERIFICATION' and
                        trial.get('qualifiedSessions',0)>=30)
    qualified_by_raw=(raw['status']=='RESEARCH_FEED_COVERAGE_DAYS_ONLY' and raw.get('qualifiedDays',0)>=30)
    if not (qualified_by_trial or qualified_by_raw):
        blockers.append('NO_30_INDEPENDENT_QUALIFIED_CLOSED_FEED_DAYS')
    if trial['status']!='POSITIVE_QUOTE_MODEL_REQUIRES_BROKER_VERIFICATION':
        blockers.append('NO_POSITIVE_PRE_REGISTERED_INDEPENDENT_QUOTE_MODEL')
    if paper['status']!='PAPER_ACCOUNT_ACCESS_ONLY':
        blockers.append('NO_VERIFIED_US_PAPER_ACCOUNT_CONNECTIVITY')
    blockers.extend(['PAPER_ORDER_AND_CANCEL_LIFECYCLE_NOT_TESTED',
                     'NO_AUTHENTICATED_BROKER_EXECUTION_SLIPPAGE_AND_FEES',
                     'NO_INTEGRATED_PRETRADE_LIMITS_OR_BROKER_KILL_SWITCH'])
    return {'version':'1.3.0','asOfUtc':now.isoformat(),
            'verdict':'NO_GO_LIVE_REAL_MONEY',
            'currentStage':('COUNTERFACTUAL_RESEARCH_CANDIDATE' if
                            trial['status']=='POSITIVE_QUOTE_MODEL_REQUIRES_BROKER_VERIFICATION'
                            else 'RESEARCH_EVIDENCE_INCOMPLETE'),
            'evidence':evidence,'blockers':blockers,
            'milestoneOrder':['Verify full RTH feed on Mac',
                              'Collect ex-ante sealed future sessions',
                              'Run paper-only broker order/cancel dry-run under separately reviewed module',
                              'Audit real venue-specific fees, slippage, partial fills and risk controls',
                              'Human-led tiny-notional live calibration only if all gates pass'],
            'hasPlaceOrderApi':False,'liveOrdersEnabled':False,'profitCertified':False,
            'noGuaranteeOfAnyLiveTradingDate':True,
            'warning':'No unverified counterfactual quote result permits real-money trading.'}


def main():
    ap=argparse.ArgumentParser(description='从原始证据出具实盘前验收报告：绝不下单')
    ap.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    ap.add_argument('--recording',type=Path,default=None,
                    help='需选择一份已结束且有完整分段的原始录制目录；前缀导出不算')
    ap.add_argument('--paper-probe',type=Path,default=None)
    ap.add_argument('--out',type=Path,default=None)
    args=ap.parse_args()
    r=assess(args.project,recording=args.recording,paper_probe=args.paper_probe)
    out=args.out or args.project/'research'/'readiness'/'live_readiness.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(r,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(r,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
