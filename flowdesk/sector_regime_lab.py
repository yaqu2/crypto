#!/usr/bin/env python3
"""Read-only causal *observations* of synchronized sector selling/rebounds.

This module deliberately does NOT train on future outcomes, mark a swing bottom,
produce trade advice, reconstruct PIT index membership, or infer broker fills.
Only hash-verified, regular-session, approximately time-of-day aligned archives
can be used. Sector membership is a declared exploratory user basket, not a
verified GICS or past point-in-time industry classification.
"""
from __future__ import annotations
import argparse, csv, json, math
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo
from market_archive import audit_archive

ET = ZoneInfo('America/New_York')
SECTORS = {
    'OPTICAL_NETWORKING_EXPLORATORY': ('US.AAOI', 'US.LITE', 'US.COHR', 'US.CIEN'),
    'MEMORY_AND_STORAGE_EXPLORATORY': ('US.MU', 'US.SNDK', 'US.WDC', 'US.STX'),
}
DEFAULT_TARGET = '15:30'  # same US local clock time each ET market day


def utc_time(s):
    try:
        v = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if v.tzinfo is None: return None
        return v.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError): return None


def positive(x):
    try:
        v = float(x)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError, OverflowError): return None


def _closest_clock_minutes(dt, target):
    local = dt.astimezone(ET)
    return abs((local.hour * 60 + local.minute + local.second / 60) - target)


def load_sessions(root, *, clock='15:30', tolerance_minutes=30):
    """Load one archived scan per ET day; fixed local clock target, not best PnL.

    Fail CLOSED if the inventory is not intact. Never fall back to unverified
    latest_quotes.csv when a matching archived source is absent.
    """
    hh, mm = (int(x) for x in clock.split(':'))
    if not (0 <= hh <= 23 and 0 <= mm <= 59): raise ValueError('INVALID_SESSION_TARGET')
    if not (0 < tolerance_minutes <= 90): raise ValueError('INVALID_CLOCK_TOLERANCE')
    target = hh * 60 + mm
    market = Path(root)
    audit = audit_archive(market)
    if audit['status'] != 'PASS_LOCAL_ARCHIVE_INTEGRITY':
        return [], {'status': 'NO_GO_ARCHIVE_INTEGRITY', 'audit': audit}
    inventory = market / 'archive' / 'scan_inventory.jsonl'
    chosen = {}
    if not inventory.exists(): return [], {'status':'NO_GO_ARCHIVE_EMPTY','audit':audit}
    for line in inventory.read_text(encoding='utf-8').splitlines():
        item = json.loads(line)
        folder = market / 'archive' / item['runId']
        report = json.loads((folder / 'latest_report.json').read_text(encoding='utf-8'))
        if report.get('dataGate') not in ('PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY', 'PASS_SNAPSHOT_QUALITY_RESEARCH_ONLY'):
            continue
        scan_at = utc_time(report.get('completedAt'))
        if scan_at is None: continue
        gap = _closest_clock_minutes(scan_at, target)
        if gap > tolerance_minutes: continue
        day = scan_at.astimezone(ET).date().isoformat()
        # deterministic selection that cannot depend on observed prices or future PnL
        candidate = (gap, scan_at.isoformat(), item['runId'])
        if day not in chosen or candidate < chosen[day][0]:
            chosen[day] = (candidate, folder, scan_at)
    sessions=[]
    desired = set(sum((list(v) for v in SECTORS.values()), []))
    for day, (_, folder, scan_at) in sorted(chosen.items()):
        prices={};bad_quotes=0
        with (folder/'latest_quotes.csv').open(newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                code = row.get('code')
                if code not in desired:continue
                stamp = utc_time(row.get('sourceAsOf'))
                val = positive(row.get('lastPrice'))
                if not (str(row.get('usable')).strip().lower()=='true' and val and stamp
                        and stamp <= scan_at
                        and 0 <= (scan_at-stamp).total_seconds() <= 180
                        and stamp.astimezone(ET).date().isoformat() == day):
                    bad_quotes += 1
                    continue
                prices[code]=val
        bench=None
        path=folder/'latest_benchmarks.csv'
        if path.exists():
            with path.open(newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    if row.get('code')!='US.SPY':continue
                    stamp=utc_time(row.get('sourceAsOf'))
                    v=positive(row.get('lastPrice'))
                    if str(row.get('usable')).strip().lower()=='true' and v and stamp and stamp <= scan_at \
                            and 0 <= (scan_at-stamp).total_seconds() <= 180 \
                            and stamp.astimezone(ET).date().isoformat()==day:
                        bench=v
        sessions.append({'day':day, 'scanAt':scan_at.isoformat(), 'prices':prices,
                         'spy':bench, 'sourceArchiveId':folder.name,'rejectedQuotes':bad_quotes})
    return sessions, {'status':'PASS_LOCAL_ARCHIVE_INTEGRITY', 'audit':audit,
                      'candidateDays':len(chosen),'clockTargetET':clock,
                      'clockToleranceMinutes':tolerance_minutes}


def evaluate_sessions(sessions, *, window=6, min_basket_coverage=0.75, sectors=None):
    """Observational features only, as of the final input session.

    Returns a NO-GO instead of silently excluding missing basket observations.
    A 6-session window represents five trading intervals, not five calendar days.
    """
    sectors=sectors or SECTORS
    if window < 3: raise ValueError('WINDOW_MUST_EXCEED_2')
    if not (0 < min_basket_coverage <= 1):raise ValueError('BAD_COVERAGE_THRESHOLD')
    dates=[x.get('day') for x in sessions]
    if len(dates)!=len(set(dates)) or dates!=sorted(dates):
        return {'status':'NO_GO_BAD_SESSION_SEQUENCE','tradeSignal':False, 'sectorResults':{}}
    if len(sessions) < window:
        return {'status':'NO_GO_INSUFFICIENT_ALIGNED_SESSIONS','requiredSessions':window,
                'observedSessions':len(sessions),'tradeSignal':False,'sectorResults':{}}
    s=sessions[-window:]
    if any(positive(x.get('spy')) is None for x in s):
        return {'status':'NO_GO_MISSING_BENCHMARK_IN_WINDOW', 'requiredSessions':window,
                'tradeSignal':False,'sectorResults':{}}
    spy_first=float(s[0]['spy']);spy_last=float(s[-1]['spy'])
    spy_total=(spy_last/spy_first-1)*100
    spy_last_session=(spy_last/float(s[-2]['spy'])-1)*100
    results={}
    for sector,codes in sectors.items():
        if not codes or len(set(codes))!=len(codes):raise ValueError('INVALID_SECTOR_BASKET')
        observed=[code for code in codes if all(positive(r.get('prices',{}).get(code)) is not None for r in s)]
        absent=[code for code in codes if code not in observed]
        coverage=len(observed)/len(codes)
        if len(observed)<3 or coverage<min_basket_coverage:
            results[sector]={'status':'NO_GO_BASKET_COVERAGE', 'basketSize':len(codes),
                             'completeSeriesCount':len(observed),'coverage':round(coverage,4),
                             'missingSeries':absent,'phase':'UNKNOWN'}
            continue
        total=[(s[-1]['prices'][k]/s[0]['prices'][k]-1)*100 for k in observed]
        last=[(s[-1]['prices'][k]/s[-2]['prices'][k]-1)*100 for k in observed]
        prior=[(s[-2]['prices'][k]/s[0]['prices'][k]-1)*100 for k in observed]
        all_median=median(total);last_median=median(last);prior_median=median(prior)
        breadth20=sum(x<=-20 for x in total)/len(total)
        breadth10=sum(x<=-10 for x in total)/len(total)
        rel=all_median-spy_total
        # Pure rulebook flags. Calibration requires independent, sealed training data.
        if prior_median<=-10 and last_median>=2 and last_median-spy_last_session>=1:
            phase='REBOUND_WATCH_NOT_BUY_SIGNAL'
        elif all_median<=-10 and breadth10>=.6 and rel<=-5:
            phase='SYNCHRONIZED_SELL_OFF_OBSERVED'
        else:
            phase='NO_PREDEFINED_PHASE_RULE_TRIGGERED'
        results[sector]={'status':'OBSERVATION_ONLY','phase':phase,
            'basketSize':len(codes),'completeSeriesCount':len(observed),'coverage':round(coverage,4),
            'observedCodes':observed,'missingSeries':absent,
            'medianFiveIntervalReturnPct':round(all_median,4),
            'medianLastIntervalReturnPct':round(last_median,4),
            'medianPriorFourIntervalReturnPct':round(prior_median,4),
            'spyFiveIntervalReturnPct':round(spy_total,4),
            'relativeToSpyPct':round(rel,4),
            'fractionDown10Pct':round(breadth10,4),
            'fractionDown20Pct':round(breadth20,4),
            'lastIntervalRelativeToSpyPct':round(last_median-spy_last_session,4)}
    return {'status':'EXPLORATORY_SECTOR_STATE_ONLY','tradeSignal':False,
            'asOfArchiveId':s[-1].get('sourceArchiveId'),
            'periodFrom':s[0]['day'],'periodThrough':s[-1]['day'],
            'sessionCount':window,'benchmark':'US.SPY',
            'sectorResults':results,'priceObservationsAreNotExecutions':True,
            'learningStatus':'NOT_TRAINED_NO_SEALED_FORWARD_LABELS',
            'caveats':['Static present-day user basket, not historical point-in-time universe',
                       'Price sampling near same ET clock time, not official closes',
                       'Detected phase is contemporaneous and NOT a prediction',
                       'No future labels, actual executions, or profit certification']}


def main():
    p=argparse.ArgumentParser(description='Hash-verified optical/memory sector-regime observation only; never sends orders')
    p.add_argument('--market-dir',type=Path, default=Path(__file__).resolve().parent/'output'/'market')
    p.add_argument('--target-et', default=DEFAULT_TARGET)
    p.add_argument('--tolerance-minutes',type=float,default=30)
    p.add_argument('--out',type=Path,default=Path(__file__).resolve().parent/'output'/'research'/'sector_regimes.json')
    args=p.parse_args()
    try:
        sessions, evidence=load_sessions(args.market_dir,clock=args.target_et,tolerance_minutes=args.tolerance_minutes)
        report=evaluate_sessions(sessions)
        report['dataEvidence']=evidence
        if evidence['status']!='PASS_LOCAL_ARCHIVE_INTEGRITY':
            report['status']='NO_GO_ARCHIVE_INTEGRITY'
        report['generatedAt']=datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        report={'status':'NO_GO_SECTOR_REGIME_INTERNAL_ERROR','reason':str(exc)[:300], 'tradeSignal':False}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.out.with_suffix(args.out.suffix+'.tmp')
    tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(args.out)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='EXPLORATORY_SECTOR_STATE_ONLY' else 2

if __name__=='__main__':raise SystemExit(main())
