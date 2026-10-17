#!/usr/bin/env python3
"""Read-only forward observation panel from immutable archived market scans.

Not a backtest or live trading signal. No order book, no spread, no fill model,
no look-ahead: observations from scan N are evaluated only against a later scan.
The price-relative returns are descriptive market labels, NEVER net P&L.
"""
import argparse,csv,json,math
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from market_archive import audit_archive,sha
from market_scan import exchange_scope

ET=ZoneInfo('America/New_York')
BENCHMARK='US.SPY'
MIN_HORIZON_SECONDS=15*60
MAX_HORIZON_SECONDS=45*60
MAX_SOURCE_AGE_SECONDS=45
MAX_PAIR_SKEW_SECONDS=30
MIN_TURNOVER_DOLLARS=2_000_000
MIN_ABS_DAY_CHANGE_PCT=2.0
MAX_CANDIDATES_PER_SCAN=25


def read_csv(path):
    with Path(path).open(newline='',encoding='utf-8') as f:return list(csv.DictReader(f))


def as_utc(raw):
    try:
        value=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
        if value.tzinfo:return value.astimezone(timezone.utc)
    except (TypeError,ValueError,OverflowError):pass
    return None


def positive(value):
    try:
        f=float(value)
        if math.isfinite(f) and f>0:return f
    except (ValueError,TypeError):pass
    return None


def numeric(value):
    try:
        f=float(value)
        if math.isfinite(f):return f
    except (ValueError,TypeError):pass
    return None


def truth(v):return str(v).lower() in ('true','1','yes')


def load_scan(root,inventory_item):
    folder=Path(root)/'archive'/inventory_item['runId']
    report=json.loads((folder/'latest_report.json').read_text(encoding='utf-8'))
    completed=as_utc(report.get('completedAt'))
    if completed is None:raise ValueError('INVALID_SCAN_COMPLETION_TIME')
    quotes=read_csv(folder/'latest_quotes.csv')
    universe=read_csv(folder/'us_stock_universe.csv')
    if len({x['code'] for x in quotes})!=len(quotes) or len(quotes)!=report.get('snapshotReceivedCount'):
        raise ValueError('DUPLICATE_OR_INCORRECT_QUOTE_COUNT')
    if len({x['code'] for x in universe})!=len(universe) or len(universe)!=report.get('stockUniverseCount'):
        raise ValueError('DUPLICATE_OR_INCORRECT_UNIVERSE_COUNT')
    universe_codes={u['code'] for u in universe}
    if any(x['code'] not in universe_codes for x in quotes):
        raise ValueError('QUOTE_NOT_IN_UNIVERSE')
    if 'latest_benchmarks.csv' in inventory_item['files']:
        bench=read_csv(folder/'latest_benchmarks.csv')
        if len({x['code'] for x in bench})!=len(bench):raise ValueError('DUPLICATE_BENCHMARK')
        if {x['code'] for x in bench}-set(report.get('benchmarkRequested') or []):
            raise ValueError('BENCHMARK_NOT_REQUESTED')
    else:bench=[]
    return {'id':inventory_item['runId'],'time':completed,'report':report,
            'quotes':{x['code']:x for x in quotes},'universe':{x['code']:x for x in universe},
            'bench':{x['code']:x for x in bench}}


def valid_point(row,report_time):
    if not row or not truth(row.get('usable')):return None
    t=as_utc(row.get('sourceAsOf'))
    px=positive(row.get('lastPrice'))
    if t is None or px is None:return None
    age=(report_time-t).total_seconds()
    if not (-2<=age<=MAX_SOURCE_AGE_SECONDS):return None
    if t.astimezone(ET).date()!=report_time.astimezone(ET).date():return None
    return {'price':px,'at':t}


def paired_point(stock,benchmark,at):
    a,b=valid_point(stock,at),valid_point(benchmark,at)
    if a is None or b is None:return None
    if abs((a['at']-b['at']).total_seconds())>MAX_PAIR_SKEW_SECONDS:return None
    return a,b


def select_candidates(scan):
    """All selection uses only the origin scan, not any future scan."""
    report=scan['report']
    eligible=[];rejected={'UNLISTED_OR_UNKNOWN':0,'BAD_OR_OLD_QUOTE':0,'LOW_LIQUIDITY_OR_PRICE':0,'NO_EVENT':0}
    for code,stock in scan['universe'].items():
        if exchange_scope(stock.get('exchange'))!='US_PRIMARY_LISTED':
            rejected['UNLISTED_OR_UNKNOWN']+=1;continue
        row=scan['quotes'].get(code)
        if valid_point(row,scan['time']) is None:
            rejected['BAD_OR_OLD_QUOTE']+=1;continue
        price=positive(row.get('lastPrice'))
        turn=numeric(row.get('turnover'))
        pct=numeric(row.get('changePct'))
        if price is None or price<5 or price>5000 or turn is None or turn<MIN_TURNOVER_DOLLARS:
            rejected['LOW_LIQUIDITY_OR_PRICE']+=1;continue
        if pct is None or not (MIN_ABS_DAY_CHANGE_PCT<=abs(pct)<=50):
            rejected['NO_EVENT']+=1;continue
        eligible.append({'code':code,'atOriginChangePct':pct,'observedTurnover':turn,
                         'direction':'UP_EVENT' if pct>0 else 'DOWN_EVENT'})
    eligible.sort(key=lambda x:(-abs(x['atOriginChangePct']),-x['observedTurnover'],x['code']))
    return eligible[:MAX_CANDIDATES_PER_SCAN],rejected,len(eligible)


def _dates_match(a,b):return a.astimezone(ET).date()==b.astimezone(ET).date()


def evaluate_panel(root):
    root=Path(root)
    audit=audit_archive(root)
    if audit['status']!='PASS_LOCAL_ARCHIVE_INTEGRITY':
        return {'status':'NO_GO_ARCHIVE_INTEGRITY','archiveAudit':audit,'tradeEnabled':False,'alphaCertified':False}
    invpath=root/'archive'/'scan_inventory.jsonl'
    indexed=[]
    for raw in invpath.read_text(encoding='utf-8').splitlines():
        if raw.strip():
            item=json.loads(raw); item['files']=json.loads((root/'archive'/item['runId']/'archive_manifest.json').read_text())['filesSha256']
            indexed.append(item)
    scans=[load_scan(root,item) for item in indexed]
    scans.sort(key=lambda s:(s['time'],s['id']))
    outcomes=[];rejections={};all_selected=0;qualifying_days=set()
    for i,origin in enumerate(scans):
        candidates,counts,total=select_candidates(origin)
        all_selected+=len(candidates)
        for k,v in counts.items():rejections[k]=rejections.get(k,0)+v
        end_scan=next((s for s in scans[i+1:] if _dates_match(origin['time'],s['time']) and
                       MIN_HORIZON_SECONDS <= (s['time']-origin['time']).total_seconds() <= MAX_HORIZON_SECONDS),None)
        origin_spy=origin['bench'].get(BENCHMARK)
        for candidate in candidates:
            code=candidate['code']
            x={'originScan':origin['id'],'originCompletedAt':origin['time'].isoformat(),
               'code':code,'direction':candidate['direction'],
               'originChangePct':candidate['atOriginChangePct'],
               'originObservedTurnover':candidate['observedTurnover'],
               'outcomeStatus':None,'futureScan':None,'marketRelativePriceMoveBps':None,
               'NOT_Executable_Profit':True}
            p0=paired_point(origin['quotes'].get(code),origin_spy,origin['time'])
            if not p0:x['outcomeStatus']='NO_GO_ORIGIN_BENCHMARK_OR_CLOCK'
            elif not end_scan:x['outcomeStatus']='PENDING_NO_FUTURE_SNAPSHOT'
            elif not end_scan['report'].get('dataGate','').startswith('PASS_'):
                x['outcomeStatus']='NO_GO_FUTURE_MARKET_SCOPE'
            elif code not in end_scan['universe'] or exchange_scope(end_scan['universe'][code].get('exchange'))!='US_PRIMARY_LISTED':
                x['outcomeStatus']='MISSING_EXIT_UNIVERSE'
            else:
                p1=paired_point(end_scan['quotes'].get(code),end_scan['bench'].get(BENCHMARK),end_scan['time'])
                if not p1:x['outcomeStatus']='MISSING_OR_UNSYNC_EXIT_QUOTES'
                elif p1[0]['at']<=origin['time'] or p1[1]['at']<=origin['time']:
                    x['outcomeStatus']='LOOKAHEAD_OR_STALE_EXIT'
                else:
                    stock_bps=(p1[0]['price']/p0[0]['price']-1)*10000
                    spy_bps=(p1[1]['price']/p0[1]['price']-1)*10000
                    x['outcomeStatus']='OBSERVED_MARKET_RELATIVE_NOT_NET'
                    x['futureScan']=end_scan['id']
                    x['marketRelativePriceMoveBps']=round(stock_bps-spy_bps,4)
                    x['directionalPriceMoveBps']=round((stock_bps-spy_bps)*(1 if candidate['direction']=='UP_EVENT' else -1),4)
                    x['absolutePriceMoveBps']=round(stock_bps,4)
                    x['benchmarkPriceMoveBps']=round(spy_bps,4)
                    x['sourceClockSkewMaxSeconds']=MAX_PAIR_SKEW_SECONDS
                    qualifying_days.add(origin['time'].astimezone(ET).date().isoformat())
            outcomes.append(x)
    counts={}
    for r in outcomes:counts[r['outcomeStatus']]=counts.get(r['outcomeStatus'],0)+1
    return {'status':'NO_GO_NO_VALID_FORWARD_OBSERVATIONS' if not counts.get('OBSERVED_MARKET_RELATIVE_NOT_NET')
            else 'EXPLORATORY_MARKET_RELATIVE_OBSERVATIONS_ONLY',
            'archiveAudit':{'status':audit['status'],'verifiedScans':audit['verified']},
            'archiveCount':len(scans),'originCandidates':all_selected,'outcomeCounts':counts,
            'independentObservedETDays':len(qualifying_days),
            'originSelectionExclusions':rejections,'studyConfig':{
                'benchmark':BENCHMARK,'minFutureScanMinutes':MIN_HORIZON_SECONDS//60,
                'maxFutureScanMinutes':MAX_HORIZON_SECONDS//60,'maxSourceAgeSeconds':MAX_SOURCE_AGE_SECONDS,
                'maxQuotePairSkewSeconds':MAX_PAIR_SKEW_SECONDS,
                'minDollarTurnover':MIN_TURNOVER_DOLLARS,'minAbsoluteDailyChangePct':MIN_ABS_DAY_CHANGE_PCT,
                'maxEventCandidatesPerScan':MAX_CANDIDATES_PER_SCAN},
            'results':outcomes,'tradeEnabled':False,'alphaCertified':False,'profitMeasured':False,
            'notice':'Observed last-price moves relative to SPY are not executable return. No bid/ask, fill, fees, impacts or borrow. Selection uses current listed stock snapshots and is not PIT historical universe. Many scans on a date are dependent; no significance or profitability claim.'}


def main():
    p=argparse.ArgumentParser(description='从两次以上已归档市场扫描估计未来市场相对价格变化（只读）')
    p.add_argument('--market-dir',type=Path,default=Path(__file__).resolve().parent/'output'/'market')
    p.add_argument('--out',type=Path,default=None)
    args=p.parse_args()
    result=evaluate_panel(args.market_dir)
    dest=args.out or args.market_dir/'forward_panel_report.json'
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='results'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
