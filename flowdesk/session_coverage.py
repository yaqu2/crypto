#!/usr/bin/env python3
"""Calendar-aware, source-time-aware, *observational* OpenD feed coverage audit.

Separate valid quote-source minutes, book callback minutes, health heartbeat minutes.
This measures the actual sampled subset, not SIP, total market, or executable alpha.
Closed segmented runs can be eligible for engineering coverage gates; prefix exports
stay NO_GO even when their hash matches.
"""
import argparse,json,hashlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from segment_store import verify_recording
from feed_guard import as_time, standard_market_day
from core import quote_time

ET=ZoneInfo('America/New_York')

def _minute(t):
    e=t.astimezone(ET)
    valid,end=standard_market_day(e.date())
    minute=e.hour*60+e.minute
    return (e.date().isoformat(), minute-570, (end*60-570)) if valid and 570<=minute<end*60 else None

def _rows(data):
    if isinstance(data,list):return [x for x in data if isinstance(x,dict)]
    return [data] if isinstance(data,dict) else []

def _valid_book(row):
    try:
        b=row['Bid'];a=row['Ask']
        return bool(b and a and float(b[0][0])>0 and float(a[0][0])>=float(b[0][0])
                    and float(b[0][1])>=0 and float(a[0][1])>=0)
    except (KeyError,ValueError,TypeError,IndexError):return False

def longest_missing_run(present, expected):
    """Largest consecutive absent minute streak, including open and close."""
    longest=current=0
    for minute in range(expected):
        if minute in present:current=0
        else:
            current+=1;longest=max(longest,current)
    return longest


def audit_recording_coverage(folder, *, benchmark='US.SPY', strict=True):
    checked=verify_recording(folder,strict=strict)
    meta=json.loads((Path(folder)/'metadata.json').read_text(encoding='utf-8'))
    # Keys: ET date, value: minute indices, never infer absent messages were zero trades.
    days=defaultdict(lambda:{'quotes':defaultdict(set),'books':defaultdict(set),
                             'ticks':defaultdict(set),'health':set(),'healthy':set(),
                             'invalidQuote':0,'staleQuote':0,'invalidBook':0,
                             'validQuoteEvents':0,'validBookEvents':0,'vendorTickEvents':0,
                             'expected':None})
    observed=0
    for path in checked['files']:
        with path.open('rb') as src:
            for line in src:
                x=json.loads(line)
                # Only actual feed observations create an ET trading day.
                # A run_start/run_stop/error written on the machine's current day
                # must not fabricate a second session in historical replay.
                kind=x.get('kind')
                if kind not in ('health','quote','book','ticker'):continue
                callback=as_time(x.get('callbackReceivedAt') or x.get('receivedAt'))
                if callback is None:continue
                slot=_minute(callback)
                if slot is None:continue
                day,index,expected=slot
                d=days[day];d['expected']=expected;observed+=1
                origin=x.get('origin')
                if kind=='health':
                    data=x.get('data') or {}
                    if isinstance(data,dict):
                        d['health'].add(index)
                        if data.get('connected') is True and data.get('feedStatus')=='RECENT_RESEARCH_ONLY':
                            d['healthy'].add(index)
                    continue
                if origin!='push':continue
                for r in _rows(x.get('data')):
                    code=r.get('code')
                    if not isinstance(code,str) or not code.startswith('US.'):continue
                    if kind=='quote':
                        src_time=as_time(quote_time(r.get('data_date'),r.get('data_time')))
                        age=(callback-src_time).total_seconds() if src_time else None
                        if age is None or age < -2 or age>20 or _minute(src_time)!=slot:
                            d['staleQuote']+=1;continue
                        try:
                            price=float(r['last_price'])
                            if price<=0 or not (price< float('inf')):raise ValueError('bad price')
                        except (KeyError,ValueError,TypeError,OverflowError):
                            d['invalidQuote']+=1;continue
                        d['quotes'][code].add(index);d['validQuoteEvents']+=1
                    elif kind=='book':
                        if not _valid_book(r):d['invalidBook']+=1;continue
                        d['books'][code].add(index);d['validBookEvents']+=1
                    elif kind=='ticker':
                        d['ticks'][code].add(index);d['vendorTickEvents']+=1
    report=[]
    for day in sorted(days):
        d=days[day];n=d['expected']
        # Heartbeats are the best available running-time proxy, not an exchange feed proof.
        first=min(d['health']) if d['health'] else None
        last=max(d['health']) if d['health'] else None
        full=bool(first is not None and first<=1 and last is not None and last>=n-2)
        base_minutes=len(d['quotes'].get(benchmark,set()))
        ratio=round(base_minutes/n,5) if n else 0
        max_quote_gap=longest_missing_run(d['quotes'].get(benchmark,set()),n)
        max_healthy_gap=longest_missing_run(d['healthy'],n)
        monitored={code:{'sourceVerifiedQuoteMinutes':len(minutes),
                          'quoteMinuteCoverage':round(len(minutes)/n,5)}
                   for code,minutes in sorted(d['quotes'].items())}
        book={code:{'callbackBookMinutes':len(minutes),
                    'callbackMinuteCoverage':round(len(minutes)/n,5),
                    'exchangeTimestampVerified':False}
              for code,minutes in sorted(d['books'].items())}
        health=len(d['health'])
        # Gate only for the benchmark; quiet-stock book sparsity alone is not vendor failure.
        gate=('RESEARCH_FEED_COVERAGE_DAY_ONLY' if full and ratio>=.95 and
              len(d['healthy'])>=int(.90*n) and max_quote_gap<=2 and
              max_healthy_gap<=2 else 'NO_GO_SESSION_COVERAGE')
        report.append({'marketDateET':day,'expectedRegularMinutes':n,
                       'heartbeatMinutes':health,'healthyFeedHeartbeatMinutes':len(d['healthy']),
                       'observedNearOpenAndClose':full,
                       'benchmark':benchmark,'benchmarkSourceVerifiedQuoteMinutes':base_minutes,
                       'benchmarkQuoteMinuteCoverage':ratio,
                       'maxConsecutiveMissingBenchmarkQuoteMinutes':max_quote_gap,
                       'maxConsecutiveUnhealthyFeedMinutes':max_healthy_gap,
                       'maxGapThresholdMinutes':2,
                       'quotesBySymbol':monitored,
                       'bookCallbacksBySymbol':book,
                       'validQuoteEvents':d['validQuoteEvents'],'validBookEvents':d['validBookEvents'],
                       'vendorTickerEventsNotFullyReconciled':d['vendorTickEvents'],
                       'staleOrOutOfOrderQuotes':d['staleQuote'],
                       'invalidQuotes':d['invalidQuote'],'invalidBooks':d['invalidBook'],
                       'status':gate})
    complete=bool(meta.get('closed') is True)
    good=sum(x['status']=='RESEARCH_FEED_COVERAGE_DAY_ONLY' for x in report)
    return {'status':('RESEARCH_FEED_COVERAGE_DAYS_ONLY' if complete and good>=30 else
                      'NO_GO_PREFIX_EXPORT' if not complete else 'NO_GO_INDEPENDENT_SESSION_COVERAGE'),
            'runId':meta.get('runId'),'rawIntegrity':checked['status'],
            'closedRun':complete,'tradingDaysObserved':len(report),
            'independentQualifiedDays':good,'requiredQualifiedDays':30,
            'sourceTimeVerifiedQuoteMinutesNotNBBO':True,
            'bookCoverageIsCallbackTimeNotExchangeCompleteness':True,
            'tradeEnabled':False,'profitCertified':False,
            'notProofOf':'SIP completeness, all-US-stock coverage, MBO, alpha, execution or future returns',
            'days':report}

def main():
    p=argparse.ArgumentParser(description='审计全分段日盘行情连续性；不认证收益')
    p.add_argument('--recording',type=Path,required=True)
    p.add_argument('--benchmark',default='US.SPY')
    p.add_argument('--diagnose-prefix',action='store_true',help='允许审查旧前缀但绝不将其认证为完整运行')
    p.add_argument('--out',type=Path,default=None)
    a=p.parse_args()
    result=audit_recording_coverage(a.recording,benchmark=a.benchmark,strict=not a.diagnose_prefix)
    if a.out:
        a.out.parent.mkdir(parents=True,exist_ok=True)
        a.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'tradingDaysObserved':result['tradingDaysObserved'],
                      'qualifiedDays':result['independentQualifiedDays'],
                      'summary':[{'day':d['marketDateET'],'SPYQuoteCoverage':d['benchmarkQuoteMinuteCoverage'],
                                  'healthMinutes':d['heartbeatMinutes'],'bookEvents':d['validBookEvents'],
                                  'status':d['status']} for d in result['days']]},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
