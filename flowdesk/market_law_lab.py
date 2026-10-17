#!/usr/bin/env python3
"""Read-only, falsifiable US stock *patterns* research from archived FlowDesk scans.

No strategy is certified here.  This is an observational price/quote investigation,
not an order, an NBBO reconstruction, an executable backtest or a sealed holdout.

The three *fixed, overlapping* hypotheses are:
H1: mean long 40bp quote-crossing scenario after positive large day move > 0;
H2: >=5% event beats 2%-5% event within the SAME ET day;
H3: >=$10m turnover event beats <$10m event within the SAME ET day.
All tests use one first event per (ET date, symbol) and aggregate by ET day, then
Monday-start ET-week clusters.  Exploratory one-sided week sign-flip p-values
and Holm corrections are shown ONLY if minimum sample criteria are met.
"""
from __future__ import annotations
import argparse,collections,json,math,random
from datetime import datetime,timedelta
from pathlib import Path
from market_quote_stress import evaluate_quote_stress
from market_forward_panel import ET,as_utc

COST_BPS='40'
REQUIRED_WEEKS=8
REQUIRED_DAYS=30
REQUIRED_SYMBOLS=20
REQUIRED_RECORDS=100
REQUIRED_COMPLETION=.95
HYPOTHESES=(
    ('H1_up_event_quote_crossing_positive','positive_up_event'),
    ('H2_stronger_up_event_outperforms','strength_contrast'),
    ('H3_higher_turnover_outperforms','turnover_contrast'),
)


def _week(date_str):
    d=datetime.fromisoformat(date_str).date()
    return (d-timedelta(days=d.weekday())).isoformat()


def _mean(values):
    return sum(values)/len(values) if values else None


def _holm(pvalues):
    """Family-wise multiplicity adjustment, monotone step down."""
    if not pvalues:return {}
    ranked=sorted(pvalues.items(),key=lambda x:(x[1],x[0]))
    output={};previous=0.
    for i,(name,p) in enumerate(ranked):
        previous=max(previous,min(1.,(len(ranked)-i)*p))
        output[name]=round(previous,6)
    return output


def _signflip_p(weekly_values):
    """One-sided block sign-flip sensitivity test, NOT a causal/null proof.

    Exact enumeration for <=16 independent *weeks*, deterministic Monte Carlo
    with +1 correction for larger samples.  Requires week-block exchangeability;
    observed structural regime shifts can violate that assumption.
    """
    n=len(weekly_values)
    if n<REQUIRED_WEEKS:return None
    obs=sum(weekly_values)/n
    if n<=16:
        sims=1<<n
        extreme=0
        for mask in range(sims):
            value=sum(v if (mask>>i)&1 else -v for i,v in enumerate(weekly_values))/n
            if value>=obs-1e-12:extreme+=1
        return extreme/sims
    # Use a fixed seed; prevent global random-state / run-to-run result changes.
    rng=random.Random(20261015)
    trials=20000
    extreme=0
    for _ in range(trials):
        v=sum(x*(1 if rng.getrandbits(1) else -1) for x in weekly_values)/n
        if v>=obs-1e-12:extreme+=1
    return (extreme+1)/(trials+1)


def _candidate(row):
    """Only origin-time data can define eligibility or the hypothesis group."""
    try:
        t=as_utc(row['originAt'])
        change=float(row['originChangePct'])
        turn=float(row['originObservedTurnover'])
        if t is None or not all(map(math.isfinite,(change,turn))) or turn<0:
            return None
        if row.get('signalDirection')!='UP_EVENT' or change<2:
            return None
        return dict(code=str(row['code']), day=t.astimezone(ET).date().isoformat(),
                    firstAt=t.isoformat(), changePct=change,turnoverUSD=turn,
                    status=row.get('status'),pnl=row.get('quoteCrossingPnLByCostUSD'),
                    notional=row.get('entryNotionalUSD'),
                    relativeQuote=row.get('quoteExcessOverSpyBps40'),
                    spyReturn=row.get('spyReturnBpsDuringHold'),
                    benchmarkDayChangePct=row.get('originBenchmarkChangePct'),
                    sourceScan=row.get('originScan'))
    except (TypeError,ValueError,KeyError,OverflowError):return None


def prepare_events(stress_rows):
    """Do NOT cherry-pick later good signals from the same symbol/day."""
    seen=set();events=[]
    first=sorted(stress_rows,key=lambda x:(str(x.get('originAt','')),str(x.get('originScan','')),str(x.get('code',''))))
    for row in first:
        event=_candidate(row)
        if event is None:continue
        key=(event['day'],event['code'])
        if key in seen:continue
        seen.add(key)
        if event['status']=='EXPLORATORY_QUOTE_CROSSING_ONLY':
            try:
                pnl=float((event['pnl'] or {})[COST_BPS]);notional=float(event['notional'])
                relative=float(event['relativeQuote']);spy=float(event['spyReturn'])
                if not all(map(math.isfinite,(pnl,notional,relative,spy))) or notional<=0:
                    raise ValueError('bad notional or PNL')
                # Never trust a supplied positive relative-alpha figure without
                # recomputing its constituent *already costed* quote scenario.
                if abs((10000*pnl/notional-spy)-relative)>0.025:
                    raise ValueError('relative result inconsistent with raw quote scenario')
                event['netQuoteScenarioBps']=round(10000*pnl/notional,6)
                event['quoteExcessOverSpyBps40']=round(relative,6)
            except (ValueError,TypeError,KeyError):
                event['status']='NO_GO_INVALID_SCENARIO_AMOUNTS'
                event['netQuoteScenarioBps']=None
                event['quoteExcessOverSpyBps40']=None
        else:
            event['netQuoteScenarioBps']=None
            event['quoteExcessOverSpyBps40']=None
        events.append(event)
    return events


def _daily_hypothesis(daily_rows,kind):
    if kind=='positive_up_event':
        return _mean([r['quoteExcessOverSpyBps40'] for r in daily_rows])
    if kind=='strength_contrast':
        hi=[r['quoteExcessOverSpyBps40'] for r in daily_rows if r['changePct']>=5]
        lo=[r['quoteExcessOverSpyBps40'] for r in daily_rows if 2<=r['changePct']<5]
    elif kind=='turnover_contrast':
        hi=[r['quoteExcessOverSpyBps40'] for r in daily_rows if r['turnoverUSD']>=10_000_000]
        lo=[r['quoteExcessOverSpyBps40'] for r in daily_rows if r['turnoverUSD']<10_000_000]
    else:raise ValueError('UNKNOWN_HYPOTHESIS')
    if not hi or not lo:return None
    return _mean(hi)-_mean(lo)


def analyze_stress_rows(stress_rows,archive_status='PASS_LOCAL_ARCHIVE_INTEGRITY',verified_scans=0):
    """Independent pure-data unit for adversarial fixtures; no access to future labels in selection."""
    events=prepare_events(stress_rows)
    all_days={x['day'] for x in events}
    successes=[x for x in events if x['quoteExcessOverSpyBps40'] is not None]
    success_days={x['day'] for x in successes}
    symbols={x['code'] for x in successes}
    coverage=len(successes)/len(events) if events else 0.
    reasons=dict(sorted(collections.Counter(x['status'] for x in events).items()))
    group=collections.defaultdict(list)
    for r in successes:group[r['day']].append(r)
    regimes=collections.defaultdict(list)
    for r in successes:
        try:
            change=float(r['benchmarkDayChangePct'])
            if not math.isfinite(change):raise ValueError('bad SPY regime')
            regime='SPY_UP_OR_FLAT' if change>=0 else 'SPY_DOWN'
        except (TypeError,ValueError,KeyError):regime='UNKNOWN_SPY_REGIME'
        regimes[regime].append(r['quoteExcessOverSpyBps40'])
    results={};raw_p={}
    for name,kind in HYPOTHESES:
        daily={day:_daily_hypothesis(rows,kind) for day,rows in sorted(group.items())}
        daily={d:round(v,8) for d,v in daily.items() if v is not None}
        weekly=collections.defaultdict(list)
        for d,v in daily.items():weekly[_week(d)].append(v)
        block_values=[_mean(vals) for week,vals in sorted(weekly.items())]
        enough=(len(daily)>=REQUIRED_DAYS and len(weekly)>=REQUIRED_WEEKS and
                len(symbols)>=REQUIRED_SYMBOLS and len(successes)>=REQUIRED_RECORDS and
                coverage>=REQUIRED_COMPLETION and archive_status=='PASS_LOCAL_ARCHIVE_INTEGRITY')
        p=_signflip_p(block_values) if enough else None
        if p is not None:raw_p[name]=p
        results[name]={
            'hypothesis':kind,'metric':'40bp quote crossing after subtracting contemporaneous SPY last-price move',
            'direction':'positive',
            'dailyDifferenceBps':_mean(list(daily.values())),
            'daysWithPairedEvidence':len(daily),
            'weekBlocks':len(weekly),
            'weeklyMeansBps':[round(v,6) for v in block_values],
            'exploratorySignFlipP':round(p,6) if p is not None else None,
            'holmAdjustedP':None,
            'isPreregisteredHoldout':False,
            'sampleCriteriaPassed':enough,
            'interpretation':'quote scenario / matched observational contrast, not executable alpha'
        }
    for name,adjusted in _holm(raw_p).items():results[name]['holmAdjustedP']=adjusted
    all_samples_ok=(len(success_days)>=REQUIRED_DAYS and len({_week(x) for x in success_days})>=REQUIRED_WEEKS
                    and len(symbols)>=REQUIRED_SYMBOLS and len(successes)>=REQUIRED_RECORDS
                    and coverage>=REQUIRED_COMPLETION)
    return {'status':('EXPLORATORY_PATTERNS_NOT_CERTIFIED' if all_samples_ok and archive_status=='PASS_LOCAL_ARCHIVE_INTEGRITY'
                      else 'NO_GO_INSUFFICIENT_INDEPENDENT_EVIDENCE'),
            'study':'FlowDesk fixed-hypothesis observational market regularity lab',
            'archiveStatus':archive_status,'verifiedScanCount':verified_scans,
            'population':{'firstUpEventPerETDaySymbol':len(events),
                          'quoteScenariosObserved':len(successes),
                          'censoredOrRejected':len(events)-len(successes),
                          'observedFraction':round(coverage,5),
                          'originETDays':len(all_days),'observedETDays':len(success_days),
                          'observedSymbols':len(symbols),
                          'failureReasons':reasons},
            'rules':{'costBps':40,'firstSignalOnlyPerSymbolDay':True,
                     'maxHorizonMinutes':45,'minETDays':REQUIRED_DAYS,
                     'minDistinctSymbols':REQUIRED_SYMBOLS,'minEvents':REQUIRED_RECORDS,
                     'minWeekBlocks':REQUIRED_WEEKS,'minOutcomeFraction':REQUIRED_COMPLETION,
                     'pValueMethod':'one-sided week-block sign flips; Holm correction across 3 fixed tests',
                     'hypotheses':[x[0] for x in HYPOTHESES]},
            'regimeDescriptiveOnly':{name:{'count':len(values),'meanExcessBps40':round(_mean(values),6)}
                                      for name,values in sorted(regimes.items())},
            'hypotheses':results,'sampleRows':events,
            'tradeEnabled':False,'alphaCertified':False,'realizedProfitVerified':False,
            'limitations':['not a sealed future holdout; these are exploratory, overlapping patterns',
                           'subtracting SPY changes does not create a frictionless hedge, and is not hedged portfolio P&L',
                           'not NBBO, MBO, broker fills, borrow, or genuine round-trip account net P&L',
                           'week sign-flip null requires exchangeable symmetric weekly innovations',
                           'trading costs are fixed stresses, not broker fees or realized impact',
                           'OTC/unlisted/unknown universe and historical delisted PIT membership not proven',
                           'survivorship, scan selection, correlated opportunity and data-missingness bias remain']}


def run(root):
    stress=evaluate_quote_stress(Path(root))
    result=analyze_stress_rows(stress.get('results') or [],
            stress.get('archiveAudit',{}).get('status',stress['status']),
            stress.get('archiveAudit',{}).get('scansVerified',0))
    result['quoteStressStatus']=stress['status']
    if stress['status'].startswith('NO_GO_ARCHIVE'):
        result['status']='NO_GO_ARCHIVE_INTEGRITY'
    return result


def main():
    p=argparse.ArgumentParser(description='全市场规律探索性反证，非盈利认证与交易信号')
    p.add_argument('--market-dir',type=Path,default=Path(__file__).resolve().parent/'output'/'market')
    p.add_argument('--out',type=Path,default=None)
    args=p.parse_args()
    report=run(args.market_dir)
    out=args.out or args.market_dir/'market_law_report.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp=out.with_suffix(out.suffix+'.tmp')
    tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(out)
    print(json.dumps({k:v for k,v in report.items() if k not in ('sampleRows',)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
