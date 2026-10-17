#!/usr/bin/env python3
"""Independent, fail-closed *local evidence* audit for a shadow fill's entire path.

No reconstruction of consolidated NBBO, queue position, exchange time or broker fills.
A quote at entry and exit cannot establish a continuous or executable path.
Conservative absence-of-update thresholds are RESEARCH ASSUMPTIONS, not market truths.
This module never issues trading permissions or modifies the frozen shadow engine.
"""
from __future__ import annotations
import argparse
import bisect
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from core import quote_time
from prospective import moment, verify_chain
from forward_report import report as forward_report
from segment_store import verify_recording
from source_crosscheck import crosscheck

MAX_BOOK_GAP_SECONDS=15.0
MAX_SOURCE_QUOTE_AGE_SECONDS=5.0
FUTURE_QUOTE_TOLERANCE_SECONDS=2.0


def _at(text):
    dt=moment(text)
    return dt.timestamp() if dt is not None else None


def _best_l1(data):
    try:
        bids=data.get('Bid') or []
        asks=data.get('Ask') or []
        # 5 actual levels on each side, sorted exactly as used by the model.
        if len(bids)<5 or len(asks)<5:return None
        bp=float(bids[0][0]);ap=float(asks[0][0])
        if not all(math.isfinite(float(x[0])) and math.isfinite(float(x[1])) and float(x[1])>=0 for x in bids[:5]+asks[:5]):return None
        if not 0<bp<ap:return None
        if any(float(bids[i][0])<=float(bids[i+1][0]) for i in range(4)):return None
        if any(float(asks[i][0])>=float(asks[i+1][0]) for i in range(4)):return None
        return bp,ap
    except (ValueError,TypeError,KeyError,IndexError,OverflowError):return None


def _quote_ok(series,ts,age=MAX_SOURCE_QUOTE_AGE_SECONDS):
    """Both quote push receipt AND quote's provider source clock must be causal."""
    if not series:return False
    i=bisect.bisect_right(series,(ts,float('inf')))-1
    if i<0:return False
    receive,src=series[i]
    return receive<=ts and -FUTURE_QUOTE_TOLERANCE_SECONDS<=receive-src<=30 and -FUTURE_QUOTE_TOLERANCE_SECONDS<=ts-src<=age


def audit_run(run_folder,max_book_gap_seconds=MAX_BOOK_GAP_SECONDS):
    run=Path(run_folder)
    if run.name!='forward_shadow':run=run/'forward_shadow'
    if max_book_gap_seconds<=0:raise ValueError('INVALID_BOOK_GAP_THRESHOLD')
    # The original frozen engine and evidence chain remain byte-for-byte unchanged.
    plan,events=verify_chain(run)
    validation=forward_report(run)
    if not validation['completeRun']:raise ValueError('INCOMPLETE_SHADOW_JOURNAL')
    raw=verify_recording(run.parent,strict=True)
    lineage=crosscheck(run)
    if lineage['status']!='MATCHED_LOCAL_CALLBACK_RECORDS':raise ValueError('RAW_CALLBACKS_NOT_MATCHED')
    exits=[e for e in events if e['type']=='PAPER_EXIT']
    raw_books=defaultdict(list)
    quotes=defaultdict(list)
    feed_faults=[];benchmark=None;raw_book_rows=0;raw_quote_rows=0;inverted_clocks=0
    last_clock=None
    for path in raw['files']:
        with path.open(encoding='utf-8') as f:
            for line in f:
                row=json.loads(line)
                kind=row.get('kind');origin=row.get('origin')
                callback=_at(row.get('callbackReceivedAt'))
                if kind=='run_start':
                    config=(row.get('data') or {}).get('watchlist') or {}
                    declared=config.get('benchmark')
                    if isinstance(declared,str):
                        if benchmark and benchmark!=declared:raise ValueError('BENCHMARK_CHANGED_WITHIN_RUN')
                        benchmark=declared
                if kind in ('health','stream_reset'):
                    if kind=='stream_reset' or ((row.get('data') or {}).get('connected') is False):
                        fault_at=_at(row.get('receivedAt'))
                        if fault_at is not None:feed_faults.append(fault_at)
                if origin!='push' or kind not in ('quote','book') or callback is None:continue
                if last_clock is not None and callback<last_clock-1e-6:inverted_clocks+=1
                last_clock=callback
                payload=row.get('data')
                if kind=='book':
                    if not isinstance(payload,dict):continue
                    code=payload.get('code');level=_best_l1(payload)
                    if not code:continue
                    raw_book_rows+=1
                    # Invalid books are kept (None) so they disqualify a path.
                    raw_books[code].append((callback,level))
                else:
                    batch=payload if isinstance(payload,list) else [payload]
                    for q in batch:
                        if not isinstance(q,dict) or not q.get('code'):continue
                        provider=quote_time(q.get('data_date'),q.get('data_time'))
                        src=_at(provider)
                        if src is not None and math.isfinite(src):
                            quotes[q['code']].append((callback,src));raw_quote_rows+=1
    # Reject source-clock rollback, not silently sort events to manufacture chronology.
    for items in list(raw_books.values())+list(quotes.values()):
        if any(items[i][0]<items[i-1][0]-1e-6 for i in range(1,len(items))):inverted_clocks+=1
    faults=sorted(feed_faults)
    reason_counts=Counter();verified=0;sample_failures=[];max_adverse=None;worst_path_gap=0.
    for e in exits:
        failures=[];code=e['code'];signal=_at(e.get('signalAt'));entry=_at(e.get('entryAt'));end=_at(e.get('exitAt'))
        if signal is None or entry is None or end is None or not signal<entry<end:
            failures.append('INVALID_LIFECYCLE_CLOCK')
        else:
            rows=raw_books.get(code,[])
            times=[x[0] for x in rows]
            lo=bisect.bisect_left(times,signal-0.000001)
            hi=bisect.bisect_right(times,end+0.000001)
            path=rows[lo:hi]
            if not benchmark:failures.append('NO_DECLARED_BENCHMARK')
            if inverted_clocks:failures.append('RECEIPT_CLOCK_ROLLBACK')
            if any(signal<=x<=end for x in faults):failures.append('FEED_RESET_INSIDE_TRADE')
            if not path or any(not any(abs(x[0]-checkpoint)<=0.001 for x in path) for checkpoint in (signal,entry,end)):
                failures.append('SIGNAL_ENTRY_EXIT_NOT_IN_RAW_BOOK_STREAM')
            if path:
                gaps=[path[i+1][0]-path[i][0] for i in range(len(path)-1)]
                largest=max(gaps,default=0.)
                worst_path_gap=max(worst_path_gap,largest)
                if largest>max_book_gap_seconds+1e-6:failures.append('UNOBSERVED_INTERMEDIATE_BOOK_GAP')
                if any(x[1] is None for x in path):failures.append('INVALID_INTERMEDIATE_BOOK')
                if not all(_quote_ok(quotes.get(code,[]),x[0]) for x in path):
                    failures.append('TARGET_QUOTE_NOT_CONTINUOUSLY_PROVEN')
                if benchmark and not all(_quote_ok(quotes.get(benchmark,[]),x[0]) for x in path):
                    failures.append('BENCHMARK_QUOTE_NOT_CONTINUOUSLY_PROVEN')
                if all(x[1] is not None for x in path):
                    price=float(e.get('entryPrice',0));direction=e.get('direction')
                    if price>0 and direction in (-1,1):
                        # Worst *observed* adverse move at crossed exit quote;
                        # not a true intrabar maximum and not executable PnL.
                        excursions=[((q[0] if direction==1 else q[1])/price-1)*10000*direction for t,q in path if t>=entry]
                        if excursions:
                            adverse=min(excursions)
                            max_adverse=adverse if max_adverse is None else min(max_adverse,adverse)
        if failures:
            reason_counts.update(set(failures))
            if len(sample_failures)<40:sample_failures.append({'id':e.get('id'),'code':code,'reasons':sorted(set(failures))})
        else:verified+=1
    if not exits:reason_counts['NO_COMPLETED_PAPER_EXITS']+=1
    if inverted_clocks and not exits:reason_counts['RECEIPT_CLOCK_ROLLBACK']+=1
    ok=bool(exits) and verified==len(exits) and not inverted_clocks
    return {'status':'PATH_EVIDENCE_COMPLETE_NOT_FILL_VERIFIED' if ok else 'NO_GO_PATH_EVIDENCE',
            'runId':run.parent.name,'modelExits':len(exits),'pathVerifiedExits':verified,
            'pathUnverifiedExits':len(exits)-verified,'failureCounts':dict(reason_counts),
            'firstFailures':sample_failures,'benchmarkFromRawRunStart':benchmark,
            'rawBookCallbacks':raw_book_rows,'rawQuoteRowsWithSourceTime':raw_quote_rows,
            'maxObservedBookGapSeconds':round(worst_path_gap,3),
            'worstObservedAdverseBps':round(max_adverse,4) if max_adverse is not None else None,
            'receiptClockRollbacks':inverted_clocks,'rawSegmentCount':raw['segmentCount'],
            'maxPermittedUnobservedGapSeconds':max_book_gap_seconds,
            'rawIntegrityChecked':True,'rawPriceEndpointsCrosschecked':True,
            'source':'MOOMOO_LOCAL_CALLBACKS_NO_EXCHANGE_TIMESTAMP_CERTIFICATION',
            'liveOrdersEnabled':False,'brokerFillsVerified':False,'certifiedAlpha':False,
            'limitation':'收到持续快照不证明期间每一笔订单事件完整；无交易所时间、NBBO、真实成交或借券证据。静默缺少盘口推送也可能只是安静行情，故此门槛保守。'}


def audit_all(folder,max_book_gap_seconds=MAX_BOOK_GAP_SECONDS):
    base=Path(folder);runs=sorted(base.glob('*/forward_shadow'))
    rows=[]
    for run in runs:
        try:rows.append(audit_run(run,max_book_gap_seconds=max_book_gap_seconds))
        except Exception as exc:
            rows.append({'status':'NO_GO_UNAUDITABLE_PATH','runId':run.parent.name,
                         'reason':str(exc)[:250],'modelExits':None,'pathVerifiedExits':0,
                         'liveOrdersEnabled':False,'certifiedAlpha':False})
    verified=all(r['status']=='PATH_EVIDENCE_COMPLETE_NOT_FILL_VERIFIED' for r in rows)
    return {'verdict':'NO_GO_REAL_TRADING','pathEvidenceAllComplete':bool(rows) and verified,
            'runsFound':len(rows),'runsWithVerifiedPaths':sum(r['status']=='PATH_EVIDENCE_COMPLETE_NOT_FILL_VERIFIED' for r in rows),
            'runResults':rows,'liveOrdersEnabled':False,'brokerFillsVerified':False,'certifiedAlpha':False}


def main():
    p=argparse.ArgumentParser(description='持仓全过程原始回调/因果报价审计；绝不启用实盘')
    p.add_argument('--recordings',type=Path,default=Path(__file__).parent/'recordings')
    p.add_argument('--run-dir',type=Path)
    p.add_argument('--max-gap-seconds',type=float,default=MAX_BOOK_GAP_SECONDS)
    p.add_argument('--out',type=Path,default=Path(__file__).parent/'research'/'forward_audits'/'execution_paths.json')
    a=p.parse_args()
    if a.max_gap_seconds<=0: p.error('--max-gap-seconds must be positive')
    if a.run_dir:
        try:result=audit_run(a.run_dir,max_book_gap_seconds=a.max_gap_seconds)
        except Exception as exc:result={'status':'NO_GO_UNAUDITABLE_PATH','reason':str(exc)[:250],'liveOrdersEnabled':False}
    else:result=audit_all(a.recordings,a.max_gap_seconds)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
