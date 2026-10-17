#!/usr/bin/env python3
"""Red-team audit of v2.0's *observational* quote-crossing market-law samples.

This deliberately does not fit a new strategy or select a best trade. It starts
from v2.0's first eligible signal per ET day and ticker and reports conditions
under which an apparent edge disappears. Incomplete quotes are NOT real losses:
missing-data stress is labelled a counterfactual assumption, never broker P&L.
"""
from __future__ import annotations
import argparse,collections,json,math,random
from datetime import datetime,timedelta
from pathlib import Path
from market_quote_stress import evaluate_quote_stress
from market_law_lab import prepare_events,_week,_mean

MISSING_BPS=(0,-50,-150,-300)
EXTRA_BPS=(0,20,60)
MIN_WEEKS=8
MIN_DAYS=30
MIN_SYMBOLS=20
MIN_COMPLETION=.95


def _finite(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError,OverflowError):return None


def _day_means(rows,field):
    daily=collections.defaultdict(list)
    for r in rows:
        v=_finite(r.get(field))
        if v is not None:daily[r['day']].append(v)
    return {d:_mean(vals) for d,vals in sorted(daily.items())}


def _week_leave_one_out(day_means):
    blocks=collections.defaultdict(list)
    for day,v in day_means.items():blocks[_week(day)].append(v)
    weekly={w:_mean(vals) for w,vals in sorted(blocks.items())}
    all_values=list(weekly.values())
    omitted={w:round(_mean([x for k,x in weekly.items() if k!=w]),6)
             for w in weekly} if len(weekly)>1 else {}
    return {'weekBlocks':len(weekly),
            'weeklyEqualWeightMeanBps':round(_mean(all_values),6) if all_values else None,
            'leaveOneWeekOutMinBps':round(min(omitted.values()),6) if omitted else None,
            'leaveOneWeekOutMaxBps':round(max(omitted.values()),6) if omitted else None,
            'leaveOneWeekOutMeansBps':omitted,
            'worstObservedWeekBps':round(min(all_values),6) if all_values else None}


def _bootstrap_week_ci(day_means,seed=20261015,trials=4000):
    """Week-block bootstrap CI, *exploratory* and not valid under arbitrary drift."""
    blocks=collections.defaultdict(list)
    for day,v in day_means.items():blocks[_week(day)].append(v)
    if len(day_means)<MIN_DAYS or len(blocks)<MIN_WEEKS:return None
    values=[_mean(vals) for _,vals in sorted(blocks.items())]
    rng=random.Random(seed);n=len(values)
    means=sorted(sum(values[rng.randrange(n)] for _ in range(n))/n for _ in range(trials))
    return [round(means[int(.025*(trials-1))],6),round(means[int(.975*(trials-1))],6)]


def analyze_rows(stress_rows,archive_status='PASS_LOCAL_ARCHIVE_INTEGRITY',verified_scans=0):
    events=prepare_events(stress_rows)
    observed=[e for e in events if e.get('netQuoteScenarioBps') is not None]
    missing=[e for e in events if e.get('netQuoteScenarioBps') is None]
    observed_days={e['day'] for e in observed};symbols={e['code'] for e in observed}
    observed_fraction=len(observed)/len(events) if events else 0.
    # The same-day mark-to-market on contemporaneous SPY does not describe an
    # executed short ETF hedge. Keep both absolute and relative measures apart.
    abs_daily=_day_means(observed,'netQuoteScenarioBps')
    relative_daily=_day_means(observed,'quoteExcessOverSpyBps40')
    base=_mean(list(abs_daily.values()))
    week_stats=_week_leave_one_out(abs_daily)
    all_days=sorted({e['day'] for e in events})

    scenario_by_missing={}
    for replacement in MISSING_BPS:
        date_rows=collections.defaultdict(list)
        for e in events:
            v=_finite(e.get('netQuoteScenarioBps'))
            date_rows[e['day']].append(v if v is not None else replacement)
        daily={d:_mean(v) for d,v in date_rows.items()}
        scenario_by_missing[str(replacement)]={
            'assumedMissingQuoteReturnBps':replacement,
            'equalETDayMeanBps':round(_mean(list(daily.values())),6) if daily else None,
            'isCounterfactualImputationNotARealizedLoss':True}

    # A completed opportunity's quote scenario changes by precisely the extra
    # round-trip cost; no optimization or re-ranking across cost scenarios.
    cost_stress={str(additional):round(base-additional,6) if base is not None else None
                 for additional in EXTRA_BPS}
    regimes=collections.defaultdict(list)
    for e in observed:
        chg=_finite(e.get('benchmarkDayChangePct'))
        group='UNKNOWN_SPY_REGIME' if chg is None else ('SPY_DOWN' if chg<0 else 'SPY_UP_OR_FLAT')
        regimes[group].append(e)
    regime_report={}
    for regime,subset in sorted(regimes.items()):
        reg_daily=_day_means(subset,'netQuoteScenarioBps')
        regime_report[regime]={
            'observations':len(subset),'ETDays':len(reg_daily),
            'meanOfETDayMeansAbsoluteQuoteBps':round(_mean(list(reg_daily.values())),6) if reg_daily else None}

    # Diagnostic concentration: share of the positive *observed* day contribution,
    # not portfolio exposure or a performance attribution from filled orders.
    positive_days=[(d,v) for d,v in abs_daily.items() if v>0]
    positive_total=sum(v for _,v in positive_days)
    share=max((v for _,v in positive_days),default=0)/positive_total if positive_total>0 else None
    complete_dates=len(observed_days);weeks=len({_week(x) for x in observed_days})
    strong_enough=(archive_status=='PASS_LOCAL_ARCHIVE_INTEGRITY' and
                   complete_dates>=MIN_DAYS and weeks>=MIN_WEEKS and
                   len(symbols)>=MIN_SYMBOLS and len(observed)>=100 and
                   observed_fraction>=MIN_COMPLETION)
    return {
        'status':'EXPLORATORY_ROBUSTNESS_DIAGNOSTICS_ONLY' if strong_enough else 'NO_GO_INSUFFICIENT_EVIDENCE',
        'study':'FlowDesk fixed-rule observational quote robustness red team',
        'inputArchiveStatus':archive_status,'verifiedScans':verified_scans,
        'population':{'firstEligibleSignals':len(events),'observedQuoteScenarios':len(observed),
                      'unresolvedOrRejected':len(missing),'observedFraction':round(observed_fraction,6),
                      'allOriginETDays':len(all_days),'observedETDays':complete_dates,
                      'observedETWeeks':weeks,'observedDistinctSymbols':len(symbols),
                      'missingReasons':dict(sorted(collections.Counter(e.get('status') for e in missing).items()))},
        'profitVsRelativeMarket':{
            'absoluteQuotedNet40BpsDailyEqualMean':round(base,6) if base is not None else None,
            'spyRelativeQuotedNet40BpsDailyEqualMean':round(_mean(list(relative_daily.values())),6) if relative_daily else None,
            'absoluteMinusRelativeNotAnInvestableHedge':True,
            'observedPositiveETDayFraction':round(sum(v>0 for v in abs_daily.values())/len(abs_daily),6) if abs_daily else None,
            'largestPositiveDayShareOfAllPositiveDayMeans':round(share,6) if share is not None else None},
        'influence':week_stats,
        'weekBlockBootstrap95PctIntervalBpsExploratory':_bootstrap_week_ci(abs_daily) if strong_enough else None,
        'extraCostOnObservedQuoteScenarioBps':cost_stress,
        'missingOutcomeSensitivity':scenario_by_missing,
        'regimesDescriptiveOnly':regime_report,
        'sampleCriteriaPassed':strong_enough,
        'positiveQuotedOutcomeRobustnessConfirmed':False,
        'alphaCertified':False,'tradeEnabled':False,'brokerFillsObserved':False,
        'warnings':[
            'Observed quote scenarios are hypothetical crossings; not confirmed market orders or fill probabilities.',
            'Missing outcome imputation is an adverse scenario, not a measured trade P&L.',
            'Equal-day and equal-week weighting do not model capital allocations, overlapping positions or executions.',
            'Even a positive SPY-relative result can coincide with a negative absolute account return.',
            'Week bootstrap intervals are exploratory; structural breaks, selection and data snooping can invalidate them.',
            'Current-directory survivors are not a point-in-time historical market universe.',
            'This module never tests an untouched future period and never authorizes live trading.']}


def run(root):
    raw=evaluate_quote_stress(Path(root))
    audit=raw.get('archiveAudit') or {}
    status=audit.get('status',raw.get('status'))
    report=analyze_rows(raw.get('results') or [],status,audit.get('scansVerified',0))
    report['quoteStressStatus']=raw.get('status')
    if raw.get('status','').startswith('NO_GO_ARCHIVE'):
        report['status']='NO_GO_ARCHIVE_INTEGRITY'
    return report


def main():
    ap=argparse.ArgumentParser(description='市场规律稳健性反证/缺失成本敏感性；非交易系统')
    ap.add_argument('--market-dir',type=Path,default=Path(__file__).resolve().parent/'output'/'market')
    ap.add_argument('--out',type=Path)
    args=ap.parse_args();r=run(args.market_dir)
    out=args.out or args.market_dir/'market_robustness_report.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp=out.with_suffix(out.suffix+'.tmp')
    tmp.write_text(json.dumps(r,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(out)
    print(json.dumps(r,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
