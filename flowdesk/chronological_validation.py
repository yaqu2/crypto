#!/usr/bin/env python3
"""Fail-closed, date-ordered *descriptive* audit of capital-constrained shadow outcomes.

No real broker fills, no independent untouched holdout, no trading authorization.
Selection only uses the earliest eligible NY trading dates. The embargo prevents
same-day leakage across selection/test boundaries. Every truly qualified day is
included in the test denominator, even when no model exit happened.
"""
from __future__ import annotations
import argparse
import json
import math
import random
import statistics
from collections import Counter
from datetime import date
from pathlib import Path
from audit_daily_coverage import audit_every_run
from audit_forward_runs import audit_all
from portfolio_audit import audit_folder
from prospective import RULES

COST_SCENARIOS=('8','20','40')
MIN_DAYS=60
EMBARGO_DAYS=2
MIN_TRAIN_EXITS=20
MIN_TRAIN_ACTIVE_DAYS=10
MIN_TEST_DAYS=20
DRAWS=1500


def split_eligible_dates(dates, min_days=MIN_DAYS, embargo=EMBARGO_DAYS):
    """Partition distinct, sorted NY dates; never balance on number of trades."""
    days=sorted(set(dates))
    if len(days)!=len(dates):raise ValueError('DUPLICATE_ELIGIBLE_DATE')
    if any(date.fromisoformat(x).isoformat()!=x for x in days):raise ValueError('INVALID_MARKET_DATE')
    if len(days)<min_days:
        return {'train':[],'embargo':[],'test':[],'ready':False,'needed':min_days,
                'observed':len(days)}
    cutoff=int(len(days)*.6)
    return {'train':days[:cutoff], 'embargo':days[cutoff:cutoff+embargo],
            'test':days[cutoff+embargo:], 'ready':len(days[cutoff+embargo:])>=MIN_TEST_DAYS,
            'needed':min_days,'observed':len(days)}


def block_bootstrap_mean_ci(values, *, seed=20261014, block=5, draws=DRAWS):
    """Overlapping moving-block resampling. Exploratory, not an exact coverage CI."""
    if len(values)<MIN_TEST_DAYS or any(not math.isfinite(v) for v in values):return None
    # Zero dispersion is a degenerate empirical distribution, not evidence of certainty.
    if max(values)-min(values)<1e-12:return None
    rng=random.Random(seed)
    k=min(block,len(values)); starts=range(len(values)-k+1)
    estimates=[]
    for _ in range(draws):
        sample=[]
        while len(sample)<len(values):
            i=rng.choice(starts)
            sample.extend(values[i:i+k])
        estimates.append(statistics.fmean(sample[:len(values)]))
    estimates.sort()
    return [round(estimates[int(.025*(draws-1))],8),
            round(estimates[int(.975*(draws-1))],8)]


def _zero_padded_day_pnl(portfolio,rule,dates,cost):
    cells=portfolio.get('ruleExitDayClosedOnlyPnlUsd',{})
    equity=float(portfolio['accountUsd'])
    if not equity>0:raise ValueError('INVALID_EQUITY')
    # Dollars of model PnL per 10000 units of *fixed account*; NOT a live return.
    return [float(cells.get(d,{}).get(rule,{}).get(str(cost),0.0))/equity*10000
            for d in dates]


def _count(portfolio,rule,days):
    cells=portfolio.get('ruleExitDayClosedOnlyCounts',{})
    return sum(int(cells.get(d,{}).get(rule,0)) for d in days)


def select_and_score(portfolio, dates, *, blocked_dates=(), minimum=MIN_DAYS):
    """Purely chronological comparison, useful in tests with synthetic PnL.

    Candidate selected using training dates only. Evaluation never re-selects the
    winner or adjusts its rule based on test results. Existing observed test data
    are still *not sealed*, hence this is a diagnostic, not proof of OOS alpha.
    """
    banned=set(blocked_dates)
    usable=[d for d in sorted(set(dates)) if d not in banned]
    split=split_eligible_dates(usable,min_days=minimum)
    report={'status':'NO_GO_INSUFFICIENT_INDEPENDENT_DAYS',
            'selectionProcedure':'FIXED_FIRST_60PCT_ELIGIBLE_ET_DAYS_TRAIN_NEXT_2_DAYS_EMBARGO_REMAINING_TEST',
            'dateSplit':split,'excludedDuplicatedOrAmbiguousDates':sorted(banned),
            'trainOnlySelectedRule':None,'testResults':None,
            'noSealedUntouchedHoldout':True,'quoteModelNotBrokerFilled':True,
            'liveOrdersEnabled':False,'profitCertified':False}
    if not split['ready']:return report
    trains=split['train'];tests=split['test']
    train=[]
    for rule in RULES:
        rid=rule['id'];series=_zero_padded_day_pnl(portfolio,rid,trains,'40')
        exits=_count(portfolio,rid,trains)
        active=sum(portfolio.get('ruleExitDayClosedOnlyCounts',{}).get(d,{}).get(rid,0)>0 for d in trains)
        mean=statistics.fmean(series)
        train.append({'rule':rid,'meanDailyAccountProxyBps40':round(mean,8),
                      'activeEtDays':active,'capitalAdmittedClosedExits':exits,
                      'selectionEligible':exits>=MIN_TRAIN_EXITS and active>=MIN_TRAIN_ACTIVE_DAYS and mean>0})
    report['trainingCandidates']=train
    selected=[x for x in train if x['selectionEligible']]
    if not selected:
        report['status']='NO_GO_NO_TRAIN_WINNER_WITH_SUFFICIENT_DAYS'
        return report
    chosen=sorted(selected,key=lambda x:(-x['meanDailyAccountProxyBps40'],x['rule']))[0]['rule']
    report['trainOnlySelectedRule']=chosen
    scenarios={}
    for cost in COST_SCENARIOS:
        values=_zero_padded_day_pnl(portfolio,chosen,tests,cost)
        ci=block_bootstrap_mean_ci(values)
        positive=sum(v>0 for v in values)
        max_dd=0.0;capital=0.;peak=0.
        for v in values:
            capital+=v;peak=max(peak,capital)
            max_dd=max(max_dd,peak-capital)
        scenarios[cost]={'testDaysIncludingZeroActivity':len(tests),
                         'modelClosedExits':_count(portfolio,chosen,tests),
                         'positiveDailyContributionDates':positive,
                         'meanDailyAccountProxyBps':round(statistics.fmean(values),8),
                         'totalAccountProxyBps':round(sum(values),8),
                         'movingBlock5DayBootstrapMeanCi95':ci,
                         'closedOnlyMaxDrawdownProxyBps':round(max_dd,8),
                         'lowerBoundAboveZero':bool(ci is not None and ci[0]>0)}
    report['testResults']=scenarios
    # Even a positive result never unlocks trading or claims genuine untouched OOS.
    report['status']='EXPLORATORY_HISTORICAL_TIME_SPLIT_POSITIVE' if scenarios['40']['lowerBoundAboveZero'] else 'NO_GO_TEST_COST40_NOT_ROBUST'
    report['limitations']=[
        'These same forward logs existed before this test was designed; test partition was NOT sequestered ex ante.',
        'Selection is restricted to first 60% of eligible dates, but this alone does not eliminate prior specification search.',
        'All 12 frozen rules compete; training-only selection avoids direct test selection but repeated reports still create peeking risk.',
        'Daily values include zero-exit days and use fixed initial shadow equity, not a mark-to-market brokerage account.',
        'Missing exits, inferred depth, borrow availability, off-book fills, exchange quote provenance, market impact are unverified.',
        'Block bootstrap CI is an exploratory dependence sensitivity, not a certified inferential coverage statement.'
    ]
    return report


def report_recordings(recordings):
    recordings=Path(recordings)
    coverage=audit_every_run(recordings)
    forward=audit_all(recordings)
    portfolio=audit_folder(recordings)
    status={'verdict':'NO_GO_REAL_TRADING','tradeEnabled':False,'profitCertified':False,
            'coverageVerdict':coverage['status'],
            'rawIntegrity':coverage['rawIntegrityVerdict'],
            'forwardRunsAllAuditable':forward['allRunsAuditable'],
            'forwardInventoryPresent':forward['runInventoryPresent'],
            'portfolioDiagnosticsComplete':portfolio['diagnosticComplete']}
    # Do not count two independent runs on a date as two independent market days.
    days=Counter()
    for run in coverage['runs']:
        if run.get('status') in ('NO_GO_RAW_INTEGRITY','NO_GO_COVERAGE_AUDIT_FAILURE'):continue
        for d in run.get('days',[]):
            if d['status']=='RESEARCH_FEED_COVERAGE_DAY_ONLY':days[d['marketDateET']]+=1
    ambiguous=[d for d,n in days.items() if n>1]
    # A distinct ET day is the unit of replication.
    study=select_and_score(portfolio,[d for d,n in sorted(days.items()) if n==1],
                           blocked_dates=ambiguous)
    status['historicalDateComparison']=study
    status['rawEligibleEtDates']=len(days)
    status['ambiguousEtDates']=ambiguous
    if not (coverage['rawIntegrityVerdict']=='PASS_LOCAL_FILE_INTEGRITY_ONLY' and
            forward['allRunsAuditable'] and forward['runInventoryPresent'] and
            portfolio['diagnosticComplete']):
        study['status']='NO_GO_SOURCE_OR_CAPITAL_AUDIT'
        study['trainOnlySelectedRule']=None
        study['testResults']=None
    return status


def main():
    p=argparse.ArgumentParser(description='资金约束后按美东日期顺序比较冻结策略；永不解锁实盘')
    p.add_argument('--recordings',type=Path,default=Path(__file__).parent/'recordings')
    p.add_argument('--out',type=Path,default=Path(__file__).parent/'research'/'forward_audits'/'chronological_validation.json')
    args=p.parse_args()
    r=report_recordings(args.recordings)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.out.with_suffix('.json.tmp');tmp.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
    tmp.replace(args.out)
    s=r['historicalDateComparison']
    print(json.dumps({'verdict':r['verdict'],'sourceAudit':s['status'],
                      'qualifiedDays':s['dateSplit']['observed'],
                      'trainingOnlyWinner':s['trainOnlySelectedRule'],
                      'NOT_REAL_OOS_OR_BROKER_PROFIT':True},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
