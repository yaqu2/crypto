#!/usr/bin/env python3
"""Causal three-scan *quote crossing stress*, NOT trading execution or P&L.

Signal: scan A; distinct later entry quote: scan B (2--300 seconds later);
exit: first archived scan C 15--45 minutes after A. All scans must match
calendar/session, stock/benchmark source times and the chain inventory. We
never skip a failed earliest entry/exit scan to manufacture a winner.

Quoted best levels are optional, venue/entitlement dependent, not SIP-NBBO,
not executable guarantees, and may be stale even when the API call succeeded.
Only long UP_EVENT observations are modelled; short sales are unverified.
"""
import argparse,json,math
from pathlib import Path
from datetime import datetime
from collections import Counter
from market_archive import audit_archive
from market_forward_panel import (load_scan,select_candidates,valid_point,paired_point,
                                  _dates_match,as_utc,BENCHMARK,ET)
from feed_guard import clock_state

MIN_ENTRY_DELAY_SECONDS=2
MAX_ENTRY_DELAY_SECONDS=300
MIN_EXIT_DELAY_SECONDS=900
MAX_EXIT_DELAY_SECONDS=2700
MAX_BBO_SPREAD_BPS=80
MODEL_NOTIONAL_USD=1000
MAX_TOP_LEVEL_PARTICIPATION=.10
COST_BPS=(8,20,40)


def bbo_point(row,completed):
    """No synthetic spread or depth substitution; bad/missing quotes fail closed."""
    if valid_point(row,completed) is None:return None
    if str(row.get('bboValid','')).lower() not in ('true','1','yes'):return None
    try:
        bid=float(row['bidPrice']);ask=float(row['askPrice'])
        bv=float(row['bidVol']);av=float(row['askVol'])
        if any(not math.isfinite(n) for n in (bid,ask,bv,av)):return None
        if not (0<bid<ask and bv>0 and av>0):return None
        spread=(ask-bid)/((bid+ask)/2)*10000
        if spread>MAX_BBO_SPREAD_BPS:return None
        t=as_utc(row.get('sourceAsOf'))
        return dict(bid=bid,ask=ask,bidVol=bv,askVol=av,spreadBps=round(spread,4),at=t)
    except (ValueError,TypeError,KeyError):return None


def report_coverage(scan):
    """Cannot call a restricted/OTC-contaminated subset a complete universe."""
    r=scan['report']
    denominator=r.get('listedUniverseCount');numerator=r.get('listedSnapshotCount')
    if not isinstance(denominator,int) or denominator<=0 or not isinstance(numerator,int):return 0.
    return min(1.,max(0.,numerator/denominator))


def first_in_window(scans,index,start,end,same_day):
    return next((s for s in scans[index+1:] if _dates_match(same_day,s['time']) and
                 start <= (s['time']-same_day).total_seconds() <= end),None)


def evaluate_quote_stress(root):
    root=Path(root);audit=audit_archive(root)
    basic={'tradeEnabled':False,'alphaCertified':False,'realizedProfitVerified':False,
           'method':'three-scans: signal, later entry ask, future exit bid',
           'limitations':'Quotes are not NBBO/MBO, not verified executions; no borrow, fills, order placement or market-impact estimate.',
           'assumptions':{'entryDelaySecondsMin':MIN_ENTRY_DELAY_SECONDS,
                          'entryDelaySecondsMax':MAX_ENTRY_DELAY_SECONDS,
                          'exitAfterSignalSecondsMin':MIN_EXIT_DELAY_SECONDS,
                          'exitAfterSignalSecondsMax':MAX_EXIT_DELAY_SECONDS,
                          'shadowNotionalUSD':MODEL_NOTIONAL_USD,
                          'maxDisplayedLevelFraction':MAX_TOP_LEVEL_PARTICIPATION,
                          'maxSpreadBps':MAX_BBO_SPREAD_BPS,
                          'additionalRoundTripCostBps':list(COST_BPS),
                          'minListedQuoteCoverage':.90}}
    if audit['status']!='PASS_LOCAL_ARCHIVE_INTEGRITY':
        return {**basic,'status':'NO_GO_ARCHIVE_INTEGRITY','archiveAudit':audit,'results':[]}
    try:
        inventory=root/'archive'/'scan_inventory.jsonl'
        items=[json.loads(line) for line in inventory.read_text().splitlines() if line.strip()]
        scans=[]
        for item in items:
            mf=json.loads((root/'archive'/item['runId']/'archive_manifest.json').read_text())
            scans.append(load_scan(root,{**item,'files':mf['filesSha256']}))
        scans.sort(key=lambda s:(s['time'],s['id']))
    except (ValueError,KeyError,TypeError,OSError,IndexError) as e:
        return {**basic,'status':'NO_GO_SCAN_FORMAT','error':str(e)[:200],'results':[]}
    results=[];days=set()
    for idx,origin in enumerate(scans):
        candidates,_,_=select_candidates(origin)
        entry=first_in_window(scans,idx,MIN_ENTRY_DELAY_SECONDS,MAX_ENTRY_DELAY_SECONDS,origin['time'])
        exit_=first_in_window(scans,idx,MIN_EXIT_DELAY_SECONDS,MAX_EXIT_DELAY_SECONDS,origin['time'])
        for candidate in candidates:
            code=candidate['code']
            row={'code':code,'originScan':origin['id'],'originAt':origin['time'].isoformat(),
                 'signalDirection':candidate['direction'],'originChangePct':candidate['atOriginChangePct'],
                 'originObservedTurnover':candidate['observedTurnover'],
                 'originBenchmarkChangePct':origin['bench'].get(BENCHMARK,{}).get('changePct'),
                 'entryScan':entry['id'] if entry else None,
                 'exitScan':exit_['id'] if exit_ else None,'status':None,
                 'quoteCrossingPnLByCostUSD':None,'executionVerified':False,
                 'spyReturnBpsDuringHold':None,'quoteExcessOverSpyBps40':None}
            days.add(origin['time'].astimezone(ET).date().isoformat())
            origin_pair=paired_point(origin['quotes'].get(code),origin['bench'].get(BENCHMARK),origin['time'])
            if not clock_state(origin['time'])['regular'] or not origin_pair:
                row['status']='NO_GO_ORIGIN_SOURCE_OR_BENCHMARK'
            elif candidate['direction']!='UP_EVENT':
                row['status']='NO_GO_SHORT_BORROW_UNVERIFIED'
            elif entry is None:
                row['status']='PENDING_NO_DELAYED_ENTRY_SCAN'
            elif exit_ is None:
                row['status']='PENDING_NO_FUTURE_EXIT_SCAN'
            elif any(not clock_state(s['time'])['regular'] for s in (entry,exit_)):
                row['status']='NO_GO_NOT_REGULAR_SESSION'
            elif min(report_coverage(s) for s in (origin,entry,exit_))<.90:
                row['status']='NO_GO_MARKET_QUOTE_COVERAGE'
            elif any(code not in s['universe'] or s['universe'][code].get('exchange') is None for s in (entry,exit_)):
                row['status']='NO_GO_EXIT_OR_ENTRY_UNIVERSE'
            elif (not paired_point(entry['quotes'].get(code),entry['bench'].get(BENCHMARK),entry['time']) or
                  not paired_point(exit_['quotes'].get(code),exit_['bench'].get(BENCHMARK),exit_['time'])):
                row['status']='NO_GO_TARGET_SPY_CLOCK_OR_SOURCE'
            else:
                pb=bbo_point(entry['quotes'].get(code),entry['time'])
                pe=bbo_point(exit_['quotes'].get(code),exit_['time'])
                source_orig=origin_pair[0]['at'];source_entry=as_utc(entry['quotes'][code].get('sourceAsOf'))
                source_exit=as_utc(exit_['quotes'][code].get('sourceAsOf'))
                bench_orig=origin_pair[1]['at']
                bench_entry=as_utc(entry['bench'][BENCHMARK].get('sourceAsOf'))
                bench_exit=as_utc(exit_['bench'][BENCHMARK].get('sourceAsOf'))
                if not pb or not pe:
                    row['status']='NO_GO_MISSING_OR_WIDE_BBO'
                elif not (source_entry>origin['time'] and bench_entry>origin['time'] and
                          source_exit>entry['time'] and bench_exit>entry['time'] and
                          origin['time']>=source_orig and origin['time']>=bench_orig and
                          source_entry<=entry['time'] and bench_entry<=entry['time'] and
                          source_exit<=exit_['time'] and bench_exit<=exit_['time']):
                    row['status']='NO_GO_CAUSAL_SOURCE_ORDER'
                else:
                    shares=math.floor(MODEL_NOTIONAL_USD/pb['ask'])
                    if shares<1:
                        row['status']='NO_GO_BELOW_ONE_SHARE_NOTIONAL'
                    elif shares>pb['askVol']*MAX_TOP_LEVEL_PARTICIPATION or shares>pe['bidVol']*MAX_TOP_LEVEL_PARTICIPATION:
                        row['status']='NO_GO_DISPLAYED_DEPTH_LIMIT'
                    else:
                        # Assume full displayed top quote access as a favorable upper-bound scenario;
                        # without broker fills this is NOT an attainable P&L guarantee.
                        notional=shares*pb['ask']
                        gross=(pe['bid']-pb['ask'])*shares
                        # Benchmark-adjusted *quoted scenario*, NOT hedged real P&L.
                        spy_entry=paired_point(entry['quotes'].get(code),entry['bench'].get(BENCHMARK),entry['time'])[1]['price']
                        spy_exit=paired_point(exit_['quotes'].get(code),exit_['bench'].get(BENCHMARK),exit_['time'])[1]['price']
                        spy_bps=(spy_exit/spy_entry-1)*10000
                        quoted_net_bps=(gross-notional*40/10000)/notional*10000
                        row.update(spyReturnBpsDuringHold=round(spy_bps,6),
                                   quoteExcessOverSpyBps40=round(quoted_net_bps-spy_bps,6))
                        row.update(status='EXPLORATORY_QUOTE_CROSSING_ONLY',shares=shares,
                                   entryAsk=pb['ask'],exitBid=pe['bid'],
                                   entrySpreadBps=pb['spreadBps'],exitSpreadBps=pe['spreadBps'],
                                   displayedEntryAskVol=pb['askVol'],displayedExitBidVol=pe['bidVol'],
                                   entryNotionalUSD=round(notional,2),
                                   quoteCrossingBeforeExtraCostsUSD=round(gross,4),
                                   quoteCrossingPnLByCostUSD={str(bps):round(gross-notional*bps/10000,4)
                                                               for bps in COST_BPS},
                                   sourceClockSkewLimitSeconds=30,
                                   fillProbabilityMeasured=False)
            results.append(row)
    c=dict(Counter(x['status'] for x in results))
    successful=c.get('EXPLORATORY_QUOTE_CROSSING_ONLY',0)
    return {**basic,'status':'EXPLORATORY_UNFILLED_QUOTE_STRESS_ONLY' if successful
            else 'NO_GO_NO_CAUSAL_QUOTE_OBSERVATIONS',
            'archiveAudit':{'status':audit['status'],'scansVerified':audit['verified']},
            'archivedScanCount':len(scans),'originCandidateCount':len(results),
            'originObservedETDays':len(days),'observationsWithQuoteStress':successful,
            'outcomeCounts':c,'results':results,
            'notice':'No actual broker order or confirmed fill. These are venue/entitlement-dependent displayed-quote scenarios; never claim verified net profit. Missing observations are counted in outcomeCounts.'}


def main():
    p=argparse.ArgumentParser(description='仅从事前扫描与独立未来扫描计算报价穿价压力，非实盘收益')
    p.add_argument('--market-dir',type=Path,default=Path(__file__).resolve().parent/'output'/'market')
    p.add_argument('--out',type=Path,default=None)
    args=p.parse_args();result=evaluate_quote_stress(args.market_dir)
    out=args.out or args.market_dir/'quote_execution_stress.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='results'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
