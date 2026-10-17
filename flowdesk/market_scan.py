#!/usr/bin/env python3
"""Moomoo batch US-stock scanner: one-off, read-only, no bulk subscriptions.

Actual universe != PIT historical universe. Best effort live snapshot != SIP or MBO.
Exports plain CSV and JSON with explicit coverage; never claims predictive alpha.
"""
import argparse,csv,hashlib,json,math,time,re
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from pathlib import Path
from core import quote_time
from feed_guard import clock_state,as_time


def rows(data):
    if hasattr(data,'to_dict'):
        return data.to_dict('records')
    if isinstance(data,list):return data
    raise ValueError('unexpected SDK result format')


def get_universe(ctx, market, stock_type, ok):
    ret,data=ctx.get_stock_basicinfo(market,stock_type)
    if ret!=ok:raise RuntimeError('stock basic info failed: '+str(data)[:220])
    found={}
    for row in rows(data):
        code=str(row.get('code',''))
        if code.startswith('US.') and len(code)<32 and not bool(row.get('delisting',False)):
            found[code]={'code':code,'name':str(row.get('name',''))[:160],
                         'exchange':str(row.get('exchange_type',''))[:80],
                         'securityType':str(row.get('stock_type',''))[:40]}
    return [found[k] for k in sorted(found)]


# Moomoo ExchType: US_NYSE=4, US_Nasdaq=5, US_Pink=6, US_AMEX=7.
# The SDK may stringify enums with a module/type prefix. Unclassified venues
# are kept in the audit denominator, but never silently counted as listed.
LISTED_EXCHANGES = {'4','5','7','US_NYSE','US_NASDAQ','US_AMEX','NYSE','NASDAQ','AMEX'}
OTC_EXCHANGES = {'6','US_PINK','PINK','OTC','OTCQX','OTCQB','OTCMKTS','OTC_MARKET'}

def exchange_scope(value):
    raw=str(value or '').strip().upper().split('.')[-1].replace(' ','_').replace('-','_')
    if raw in LISTED_EXCHANGES: return 'US_PRIMARY_LISTED'
    if raw in OTC_EXCHANGES or 'PINK' in raw or raw.startswith('OTC'): return 'US_OTC'
    return 'UNKNOWN_EXCHANGE'

class SnapshotPacer:
    """One API call at a time; cap retries and respect 60/30s documentation."""
    def __init__(self,ctx,interval,max_calls):
        self.ctx=ctx;self.interval=interval;self.max_calls=max_calls
        self.last=None;self.calls=0
    def request(self,codes):
        if self.calls>=self.max_calls: raise RuntimeError('SNAPSHOT_REQUEST_BUDGET_EXCEEDED')
        if self.last is not None:
            remaining=self.interval-(time.monotonic()-self.last)
            if remaining>0:time.sleep(remaining)
        self.last=time.monotonic();self.calls+=1
        return self.ctx.get_market_snapshot(codes)

def fetch_batch(pacer,codes,ok,at,errors,denials,max_recovery=12):
    """Recover nonproblematic symbols without hammering Moomoo on failures.

    A specifically named inaccessible symbol is removed from this request only,
    logged in denials, and never called 'covered'. For opaque errors, split
    cautiously, with bounded extra requests. Network/rate-limit errors fail
    closed rather than recursively retrying.
    """
    stack=[list(codes)];collected=[];extra=0
    while stack:
        subset=stack.pop()
        if not subset: continue
        try: ret,data=pacer.request(subset)
        except Exception as exc:
            errors.append({'start':at,'count':len(subset),'reason':str(exc)[:200]});break
        if ret==ok:
            valid=set(subset)
            try: got=rows(data)
            except Exception as exc:
                errors.append({'start':at,'count':len(subset),'reason':'BAD_SNAPSHOT_RESPONSE: '+str(exc)[:170]});continue
            collected.extend(normalize_snapshot(row,datetime.now(timezone.utc)) for row in got if str(row.get('code','')) in valid)
            continue
        reason=str(data)[:200]
        # A global quota or transport failure should never trigger a retry storm.
        if any(x in reason.lower() for x in ('frequency','too many','rate limit','timeout','not connected','disconnect')):
            errors.append({'start':at,'count':len(subset),'reason':reason});break
        match=re.search(r'\bfor\s+([A-Za-z0-9.\-]+)',reason)
        token=match.group(1).rstrip('.').upper() if match else ''
        bad=next((code for code in subset if code.upper()=='US.'+token or code.upper()==token),None)
        if bad and extra<max_recovery:
            denials.append({'code':bad,'reason':reason});extra+=1
            remain=[x for x in subset if x!=bad]
            if remain:stack.append(remain)
        elif len(subset)>1 and extra+2<=max_recovery:
            mid=len(subset)//2;extra+=2
            stack.append(subset[mid:]);stack.append(subset[:mid])
        else:
            errors.append({'start':at,'count':len(subset),'reason':reason})
    return collected


def number(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None


def normalize_snapshot(row, now):
    """Separate accessible quote, clock freshness and usable research evidence.

    An OpenD snapshot delivered at 08:37 is NOT a fresh *regular-session*
    strategy observation. The same snapshot still proves quote entitlement.
    """
    code=str(row.get('code',''))
    price=number(row.get('last_price'))
    pre=number(row.get('prev_close_price',row.get('prev_close')))
    stamp=str(row.get('update_time') or '')
    source=as_time(quote_time(None,stamp))
    clock=clock_state(now)
    age=(now-source).total_seconds() if source else None
    time_ok=bool(source and age is not None and -5<=age<=180 and
                 source.astimezone(ZoneInfo('America/New_York')).date().isoformat()==clock['marketDate'])
    fresh=bool(clock['regular'] and time_ok)
    stopped=bool(row.get('suspension',False)) or str(row.get('sec_status','')).upper() in ('HALT','HALTED','SUSPENDED','DELISTED')
    if not clock['regular']: reason='OUTSIDE_REGULAR'
    elif not source:reason='MISSING_SOURCE_TIME'
    elif age is not None and age < -5:reason='FUTURE_SOURCE_CLOCK'
    elif not time_ok:reason='STALE_SOURCE_TIME'
    elif stopped:reason='TRADING_SUSPENDED'
    elif price is None or price<=0:reason='INVALID_LAST_PRICE'
    elif pre is None or pre<=0:reason='INVALID_PREVIOUS_CLOSE'
    else:reason='USABLE_REGULAR'
    usable=reason=='USABLE_REGULAR'
    # OpenD snapshot exposes bid/ask and displayed top-of-book sizes. These are
    # NOT a consolidated NBBO, guaranteed fills or full exchange MBO.
    bid=number(row.get('bid_price'));ask=number(row.get('ask_price'))
    bidvol=number(row.get('bid_vol'));askvol=number(row.get('ask_vol'))
    bbo_valid=bool(usable and bid is not None and ask is not None and 0<bid<ask
                   and bidvol is not None and askvol is not None and bidvol>0 and askvol>0)
    premkt=number(row.get('pre_price'))
    return {'code':code,'sourceAsOf':source.isoformat() if source else None,
            'ageSeconds':round(age,1) if age is not None else None,
            'lastPrice':price,'previousClose':pre,'volume':number(row.get('volume')),
            'turnover':number(row.get('turnover')),'fresh':fresh,'usable':usable,
            'diagnostic':reason,'prePriceUnverified':premkt if premkt and premkt>0 else None,
            'changePct':round((price/pre-1)*100,4) if usable else None,
            'bidPrice':bid,'askPrice':ask,'bidVol':bidvol,'askVol':askvol,
            'bboValid':bbo_valid,
            'spreadBps':round((ask-bid)/((ask+bid)/2)*10000,4) if bbo_valid else None}


def summarize(universe,quotes,started,ended,batch_errors=None,requested_codes=None,denials=None,limit=False):
    batch_errors=batch_errors or [];denials=denials or []
    present={r['code']:r for r in quotes}
    from collections import Counter
    diagnostics=dict(sorted(Counter(r.get('diagnostic','UNSPECIFIED') for r in quotes).items()))
    elig=len(universe);received=len(present)
    listed={x['code'] for x in universe if exchange_scope(x.get('exchange'))=='US_PRIMARY_LISTED'}
    otc={x['code'] for x in universe if exchange_scope(x.get('exchange'))=='US_OTC'}
    unknown={x['code'] for x in universe if exchange_scope(x.get('exchange'))=='UNKNOWN_EXCHANGE'}
    validated=[r for r in present.values() if r['usable']]
    up=sum(r['changePct']>0 for r in validated)
    down=sum(r['changePct']<0 for r in validated)
    flat=len(validated)-up-down
    # Backward-compatible local scanner tests that used synthetic entries without
    # an exchange code. This does NOT certify a listed market in this case.
    full=bool(elig>=2000 and received==elig and not batch_errors and not denials and not limit)
    listed_present=len(set(present)&listed)
    exchange_histogram=dict(sorted(Counter(str(x.get('exchange') or '<missing>') for x in universe).items(), key=lambda x:(-x[1],x[0]))[:15])
    listed_usable=sum(x['usable'] for code,x in present.items() if code in listed)
    listed_full=bool(listed and len(listed)>=2000 and listed_present==len(listed)
                     and not batch_errors and not denials and not limit)
    listed_pass=bool(listed_full and listed_usable/len(listed)>=.8)
    legacy_pass=bool(full and len(validated)/elig>=.8 and not otc and
                     (not unknown or requested_codes is None))
    data_gate=('PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY' if listed_pass else
               'PASS_SNAPSHOT_QUALITY_RESEARCH_ONLY' if legacy_pass else 'NO_GO_COVERAGE')
    return {'mode':'US_CURRENT_STOCKS_SNAPSHOT_RESEARCH_ONLY','startedAt':started.isoformat(),
            'completedAt':ended.isoformat(),'stockUniverseCount':elig,
            'scanScope':'US_PRIMARY_LISTED' if listed else 'UNKNOWN_EXCHANGE_ONLY',
            'listedUniverseCount':len(listed),'otcExcludedCount':len(otc),
            'unknownExchangeCount':len(unknown),'listedSnapshotCount':listed_present,
            'listedMissingSnapshotCount':len(listed)-listed_present,
            'listedFreshUsableCount':listed_usable,
            'listedFreshUsableCoverage':round(listed_usable/len(listed),5) if listed else 0,
            'fullListedUniverse':listed_full,
            'snapshotReceivedCount':received,'missingSnapshotCount':max(0,elig-received),
            'freshUsableCount':len(validated),'freshUsableCoverage':round(len(validated)/elig,5) if elig else 0,
            'breadth':{'advanced':up,'declined':down,'unchanged':flat,'denominator':len(validated),
                       'advancedFraction':round(up/len(validated),4) if validated else None},
            'quoteDiagnosticCounts':diagnostics,'exchangeExamples':exchange_histogram,
            'snapshotAccessConfirmed':bool(received),
            'premarketPriceFieldCount':sum(bool(r.get('prePriceUnverified')) for r in quotes),
            'batchErrors':batch_errors[:20],'batchErrorCount':len(batch_errors),
            'quoteDenials':denials[:30],'quoteDenialCount':len(denials),
            'fullCurrentStockUniverse':full,
            'dataGate':data_gate,
            'tradeEnabled':False,'pointInTimeHistoricUniverse':False,
            'notIncluded':'OTC and unknown-exchange securities from primary listed certification; historical delisted membership; options; cross-venue MBO; full market trades; verified executions',
            'marketClock':clock_state(ended)}


def write_csv(path,items,keys):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=keys,extrasaction='ignore');writer.writeheader();writer.writerows(items)
    tmp.replace(path)


def run(ctx,market,stock_type,ok,dest, limit=None,interval=1.1,max_requests=100,benchmark_codes=()):
    if interval<0.55:raise ValueError('snapshot batch interval too short; keep >=0.55 sec')
    if max_requests<1:raise ValueError('max_requests must be positive')
    started=datetime.now(timezone.utc)
    universe=get_universe(ctx,market,stock_type,ok)
    if not universe:raise RuntimeError('Empty US stock universe; refusing to infer market breadth')
    listed=[x for x in universe if exchange_scope(x.get('exchange'))=='US_PRIMARY_LISTED']
    # Old/sdk-mocked environments may omit exchange_type. Probe UNKNOWN only if
    # not one single primary-listing venue could be identified; cannot certify.
    requested=listed if listed else [x for x in universe if exchange_scope(x.get('exchange'))=='UNKNOWN_EXCHANGE']
    if limit:requested=requested[:limit]
    if not requested:raise RuntimeError('NO_SUPPORTED_PRIMARY_EXCHANGE_FOUND')
    collected=[];errors=[];denials=[]
    pacer=SnapshotPacer(ctx,interval,max_requests)
    for pos in range(0,len(requested),400):
        subset=[x['code'] for x in requested[pos:pos+400]]
        result=fetch_batch(pacer,subset,ok,pos,errors,denials)
        collected.extend(result)
        if pacer.calls>=max_requests and pos+400<len(requested):
            errors.append({'start':pos,'count':len(requested)-pos,'reason':'SNAPSHOT_REQUEST_BUDGET_EXCEEDED'})
            break
    # Benchmark ETFs are fetched independently. SecurityType.STOCK does not
    # establish their inclusion in the stock directory. They are NEVER mixed
    # into the stock-universe numerator, denominator, or stock candidate list.
    benchmarks=[];benchmark_error=None
    benchmarks_requested=list(dict.fromkeys(benchmark_codes))
    if benchmarks_requested:
        if any(not re.fullmatch(r'US\.[A-Z0-9.-]{1,15}',x) for x in benchmarks_requested):
            raise ValueError('INVALID_BENCHMARK_CODE')
        try:
            ret,data=pacer.request(benchmarks_requested)
            if ret!=ok:benchmark_error=str(data)[:220]
            else:
                valid=set(benchmarks_requested)
                benchmarks=[normalize_snapshot(x,datetime.now(timezone.utc)) for x in rows(data)
                            if str(x.get('code','')) in valid]
        except Exception as exc:benchmark_error=str(exc)[:220]
    ended=datetime.now(timezone.utc)
    summary=summarize(universe,collected,started,ended,errors,requested_codes=[x['code'] for x in requested],denials=denials,limit=bool(limit))
    summary['benchmarkRequested']=benchmarks_requested
    summary['benchmarkReceivedCount']=len({x['code'] for x in benchmarks})
    summary['benchmarkFailure']=benchmark_error
    summary['benchmarkDataIsNotMBO']=True
    summary['snapshotRequestCount']=pacer.calls
    summary['failureDiagnosis']=('SNAPSHOT_API_FAILED' if not collected and (errors or denials) else
        'NO_PRIMARY_LISTING_CLASSIFIED' if not listed else
        'OUTSIDE_REGULAR_SESSION' if not clock_state(ended)['regular'] else
        'LISTED_COVERAGE_INCOMPLETE' if summary['listedMissingSnapshotCount'] else
        'REGULAR_QUOTES_STALE_OR_INVALID' if not summary['listedFreshUsableCount'] else
        'FRESH_LISTED_SNAPSHOT_NOT_ALPHA')
    summary['requestedListedCount']=len(requested)
    if limit:summary['limitation']='Explicit partial scan (--max-symbols); never full-market certified'
    if not listed:summary['limitation']='Missing/unrecognised exchange_type; only an unknown-venue diagnostic probe, never listed-market certified'
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    write_csv(dest/'us_stock_universe.csv',universe,['code','name','exchange','securityType'])
    cols=['code','sourceAsOf','ageSeconds','lastPrice','previousClose','volume','turnover','fresh','usable','diagnostic','prePriceUnverified','changePct','bidPrice','askPrice','bidVol','askVol','bboValid','spreadBps']
    write_csv(dest/'latest_quotes.csv',collected,cols)
    if benchmarks_requested:
        write_csv(dest/'latest_benchmarks.csv',benchmarks,cols)
        summary['benchmarksSha256']=hashlib.sha256((dest/'latest_benchmarks.csv').read_bytes()).hexdigest()
        summary['benchmarkCoverage']=round(summary['benchmarkReceivedCount']/len(benchmarks_requested),5)
    summary['universeSha256']=hashlib.sha256((dest/'us_stock_universe.csv').read_bytes()).hexdigest()
    summary['quotesSha256']=hashlib.sha256((dest/'latest_quotes.csv').read_bytes()).hexdigest()
    tmp=dest/'latest_report.json.tmp'
    tmp.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.replace(dest/'latest_report.json')
    from market_archive import register_scan
    archive_info=register_scan(dest)
    return {**summary,'archive':archive_info}


def main():
    p=argparse.ArgumentParser(description='全美股静态目录 + 当次批量快照（不订阅，不交易）')
    p.add_argument('--out',type=Path,default=Path(__file__).resolve().parent/'output'/'market')
    p.add_argument('--max-symbols',type=int,default=None,help='仅用于小批量权限检查，绝不认证全市场')
    p.add_argument('--interval',type=float,default=1.1)
    p.add_argument('--max-requests',type=int,default=100,help='API calls hard cap, including error recovery')
    args=p.parse_args()
    if args.max_symbols is not None and args.max_symbols<1:p.error('--max-symbols must be positive')
    from moomoo import OpenQuoteContext,Market,SecurityType,RET_OK
    ctx=OpenQuoteContext(host='127.0.0.1',port=11111)
    try:
        result=run(ctx,Market.US,SecurityType.STOCK,RET_OK,args.out,args.max_symbols,args.interval,args.max_requests,
                   benchmark_codes=('US.SPY','US.QQQ','US.IWM'))
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:ctx.close()

if __name__=='__main__':main()
