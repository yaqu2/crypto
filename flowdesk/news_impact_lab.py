#!/usr/bin/env python3
"""FlowDesk v2.8: read-only, point-in-time NEWS -> future minute price event study.

This measures observational price response; it is NOT causality, fills, or net Alpha.
Use only audited local first-receipt timestamps, NEVER historical vendor publication as
availability; future labels require distinct *completed* minute observations.
"""
from __future__ import annotations
import argparse, hashlib, json, math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from news_feed import NEWS_LOG, read_log, clock

ROOT=Path(__file__).resolve().parent
ET=ZoneInfo('America/New_York')
HORIZONS=(1,5,15)
SECTOR_BENCHMARK={'AAOI':'SMH','LITE':'SMH','COHR':'SMH','CIEN':'SMH',
                  'MU':'SMH','SNDK':'SMH','WDC':'SMH','STX':'SMH',
                  'NVDA':'SMH','AMD':'SMH','AVGO':'SMH','MRVL':'SMH'}
BENCHMARKS={'SPY','QQQ','SMH','SOXX','XLK','IWM','XSD'}


def _symbol(v):return str(v or '').upper().removeprefix('US.')


def _finite_positive(v):
    try:
        x=float(v)
        return x if math.isfinite(x) and x>0 else None
    except (ValueError,TypeError,OverflowError):return None


def _index_bars(bars, *, asof):
    """Require observation availability <= study cutoff, no duplicate/revised bar selection."""
    index={};rejected=Counter();duplicate=set()
    for b in bars:
        try:
            if b.get('finalBarByTimeHeuristicOnly') is not True:
                rejected['NOT_CLOSED_MINUTE']+=1;continue
            sym=_symbol(b['symbol'])
            start=clock(b['startUtc']);end=clock(b['barEndUtc']);receipt=clock(b['firstAvailableUtc'])
            close=_finite_positive(b.get('close'))
            if not sym or close is None or end!=start+timedelta(minutes=1) or receipt<end:
                rejected['INVALID_OR_PARTIAL_MINUTE_BAR']+=1;continue
            if receipt>asof:
                rejected['UNAVAILABLE_AT_STUDY_CUTOFF']+=1;continue
            if start.second or start.microsecond:
                rejected['UNALIGNED_BAR']+=1;continue
            key=(sym,start)
            if key in index:
                duplicate.add(key);rejected['DUPLICATE_MINUTE_REVISION']+=1
            else:index[key]={'close':close,'start':start,'end':end,'available':receipt,'raw':b}
        except (KeyError,TypeError,ValueError,OverflowError):rejected['BAD_BAR_CLOCK']+=1
    for key in duplicate:index.pop(key,None)
    return index,rejected


def _last_pre(index,sym,event_time,max_lag=timedelta(minutes=3)):
    options=[b for (s,_),b in index.items() if s==sym and b['end']<=event_time
             and b['available']<=event_time and event_time-b['end']<=max_lag]
    return max(options,key=lambda r:r['end']) if options else None


def _after(index,sym,start,h,asof):
    """All h consecutive future full bars, each with first receipt by cutoff."""
    block=[]
    for n in range(h):
        bar=index.get((sym,start+timedelta(minutes=n)))
        if bar is None:return None
        if bar['available']>asof:raise ValueError('FUTURE_BAR_NOT_YET_RECEIVED')
        block.append(bar)
    return block


def _ret(a,b):return round(b/a-1,9)


def _flow_context(micro_obs,sym,decision,cutoff):
    """Optional descriptive received-minute OFI proxies, NO implied event causality.

    preEvent features only use minutes fully ended before local news receipt.
    postEvent minutes stay OUTCOME diagnostics and never enter signal features.
    Missing observed minutes are unknown, not zero flow.
    """
    before=[];after=[]
    for row in micro_obs or []:
        try:
            if _symbol(row.get('symbol'))!=sym or row.get('status')!='ARRIVAL_CLOCK_PROXY_ONLY':continue
            minute=clock(row['minuteUtc']);end=minute+timedelta(minutes=1)
            ofi=float(row.get('ofiTopProxy'))
            if not math.isfinite(ofi):continue
            if decision-timedelta(minutes=5)<=end<=decision:before.append(ofi)
            if decision<=minute and end<=cutoff and minute<decision+timedelta(minutes=6):after.append(ofi)
        except (KeyError,TypeError,ValueError,OverflowError):continue
    return {'availableAtDecision':{'observedCompleteMinutes':len(before),
                                  'sumTopOfiProxy':round(sum(before),6) if before else None},
            'futureOutcomeDiagnosticsNeverModelFeature':{'observedCompleteMinutes':len(after),
                                                        'sumTopOfiProxy':round(sum(after),6) if after else None},
            'snapshotSizeChangeIsNotTrueExchangeOrderAddCancel':True}


def study(news,bars,*,asof, micro_obs=None, horizons=HORIZONS, cooldown_minutes=60,
          require_sector=True, source_coverage=None):
    asof=clock(asof)
    if not horizons or any(not isinstance(h,int) or h<1 or h>60 for h in horizons):raise ValueError('INVALID_HORIZONS')
    if cooldown_minutes<1 or cooldown_minutes>390:raise ValueError('BAD_COOLDOWN')
    source_coverage=source_coverage or {}
    if source_coverage.get('sourceCompleteness')=='INCOMPLETE_OR_TRUNCATED_NEVER_CERTIFY':
        return {'status':'NO_GO_INCOMPLETE_MARKET_RECORDING','profitCertified':False,'tradeSignal':False}
    idx,bad=_index_bars(bars,asof=asof)
    latest_initial={};discard=Counter();groups=defaultdict(list)
    for r in news:
        try:
            if r['type']!='news':continue
            # News log is presumed verified by read_log() at CLI. Revision >0 is NOT a new signal.
            if int(r['revision'])!=0:discard['REVISED_VERSION_NOT_NEW_EVENT']+=1;continue
            at=clock(r['first_seen_at']);published=clock(r['published_at']);received=clock(r['received_at'])
            if not published<=at<=received:raise ValueError('NONCAUSAL_NEWS_CLOCK')
            if at>asof:discard['NOT_OBSERVED_BY_ASOF']+=1;continue
            key=str(r['key'])
            if key in latest_initial:
                discard['DUPLICATE_FIRST_NEWS_VERSION']+=1
                raise ValueError('DUPLICATE_FIRST_NEWS_VERSION')
            latest_initial[key]=r
        except (KeyError,TypeError,ValueError) as exc:
            if str(exc)=='DUPLICATE_FIRST_NEWS_VERSION':raise
            raise ValueError('INVALID_NEWS_EVENT') from None
    # Signals are determined using news metadata only, before examining any future bar.
    candidates=[];seen_last={}
    for r in sorted(latest_initial.values(),key=lambda x:(clock(x['received_at']),x['key'])):
        decision=clock(r['received_at']);dt=decision.astimezone(ET)
        if dt.weekday()>4 or not (time(9,30)<=dt.time()<time(15,45)):
            discard['OUTSIDE_REGULAR_EVENT_WINDOW']+=1;continue
        tickers=sorted(set(_symbol(x) for x in r['value'].get('tickers',[])))
        if not tickers:discard['UNTAGGED_NO_SYMBOL']+=1
        for sym in tickers:
            if sym in BENCHMARKS:discard['BENCHMARK_IS_NOT_TRADE_TARGET']+=1;continue
            if not sym or not sym.replace('.','').replace('-','').isalnum():discard['INVALID_SYMBOL']+=1;continue
            bucket=(sym,dt.date().isoformat())
            if bucket in seen_last and (decision-seen_last[bucket]).total_seconds()<cooldown_minutes*60:
                discard['OVERLAPPING_EVENT_FIRST_ONLY']+=1;continue
            seen_last[bucket]=decision
            candidates.append({'newsKey':r['key'],'symbol':sym,'decision':decision,
              'localNewsReceipt':decision.isoformat(),'newsPublished':r['published_at'],
              'topics':r['value'].get('topics',[]), 'sectorProxy':SECTOR_BENCHMARK.get(sym)})
    results=[];pending=[]
    for c in candidates:
        sym=c['symbol'];decision=c['decision'];sector=c['sectorProxy']
        # Need a close *already locally received at the decision*. No same-minute hindsight.
        required=['SPY']+([sector] if sector and sector!='SPY' else [])
        if require_sector and not sector:
            discard['NO_PREDECLARED_SECTOR_PROXY']+=1;continue
        pre={s:_last_pre(idx,s,decision) for s in [sym]+required}
        if any(x is None for x in pre.values()):
            discard['NO_ASOF_PRE_NEWS_REFERENCE']+=1;continue
        ceil=decision.replace(second=0,microsecond=0)
        if ceil<decision:ceil+=timedelta(minutes=1)
        for h in sorted(set(horizons)):
            end=ceil+timedelta(minutes=h)
            item={'newsKey':c['newsKey'],'symbol':sym,'newsReceiptUtc':decision.isoformat(),
                  'firstFutureMinuteUtc':ceil.isoformat(),'horizonMinutes':h,
                  'lastFutureBarEndUtc':end.isoformat(),'sectorProxy':sector,
                  'proxyIsNotCausalIndustryControl':True,'newsHeadlineNotSentiment':True,
                  'quoteExecutionVerified':False,'brokerFillVerified':False}
            if asof<end+timedelta(seconds=1):
                pending.append({**item,'reason':'FUTURE_HORIZON_NOT_FINISHED'});continue
            blocks={s:_after(idx,s,ceil,h,asof) for s in pre}
            if any(v is None for v in blocks.values()):
                # Data could still arrive slightly late; after 3 minutes classify as missing.
                if asof<end+timedelta(minutes=3):
                    pending.append({**item,'reason':'FUTURE_BARS_NOT_ALL_OBSERVED_YET'})
                else:discard['MISSING_SYNCHRONIZED_FUTURE_BARS']+=1
                continue
            values={s:_ret(pre[s]['close'],blocks[s][-1]['close']) for s in pre}
            own=values[sym]; market=values['SPY']; peer=values.get(sector)
            result={**item,'preEventReferenceUtc':pre[sym]['end'].isoformat(),
                    'preEventClose':pre[sym]['close'],'futureClose':blocks[sym][-1]['close'],
                    'stockObservedReturn':own,'spyObservedReturn':market,
                    'excessVsSPY':round(own-market,9),
                    'sectorObservedReturn':peer,'excessVsSectorProxy':round(own-peer,9) if peer is not None else None,
                    'worstObservedMinuteLowFromReference':None,
                    'firstSeenClockWasLocalReceipt':True,'priceResponseNotCausality':True,
                    'actualRoundTripCostsKnown':False,'netExecutablePnl':None,'profitCertified':False,
                    'orderflowProxyContext':_flow_context(micro_obs,sym,decision,min(end,asof))}
            # High/low only for diagnostics (never label quote fill or intra-bar barrier order).
            lows=[_finite_positive(b['raw'].get('low')) for b in blocks[sym]]
            highs=[_finite_positive(b['raw'].get('high')) for b in blocks[sym]]
            if all(p is not None for p in lows+highs):
                result['worstObservedMinuteLowFromReference']=round(min(lows)/pre[sym]['close']-1,9)
                result['bestObservedMinuteHighFromReference']=round(max(highs)/pre[sym]['close']-1,9)
                result['barrierOrderKnown']=False
            results.append(result)
    # Equal weight EACH EVENT-DAY for diagnostic summaries, not trade independent day evidence.
    by_h={};dates=set()
    for h in sorted(set(horizons)):
        matched=[r for r in results if r['horizonMinutes']==h]
        buckets=defaultdict(list)
        for r in matched:
            day=clock(r['newsReceiptUtc']).astimezone(ET).date().isoformat();dates.add(day)
            buckets[day].append(r['excessVsSPY'])
        means=[sum(arr)/len(arr) for arr in buckets.values()]
        by_h[str(h)]={'maturedEvents':len(matched),'independentEventDays':len(buckets),
                     'dayEqualWeightedMeanExcessVsSPY':round(sum(means)/len(means),9) if means else None,
                     'isExploratoryDescriptiveOnly':True}
    return {'status':'EXPLORATORY_OBSERVED_EVENT_RESPONSE_ONLY' if results else 'NO_GO_INSUFFICIENT_MATCHED_EVENTS',
            'version':'2.8','studyAsOfUtc':asof.isoformat(),'newsInitialEvents':len(latest_initial),
            'selectedNewsSymbolEvents':len(candidates),'maturedMatchedOutcomes':len(results),
            'unmaturedCount':len(pending),'excludedReasons':dict(discard),
            'barExcludedReasons':dict(bad),'byHorizon':by_h,
            'results':results,'pending':pending,
            'pricesAreCompletedMinuteHeuristicsNotBrokerVerified':True,
            'sourceCompletenessAudited':bool(source_coverage) and not bool(source_coverage.get('fileIntegrityVerified') is False),
            'marketResponseDoesNotEstablishEventCausality':True,
            'predefinedCostNotSameAsActualSlippage':True,
            'tradeSignal':False,'profitCertified':False,
            'warning':'PIT descriptive event study only; contemporaneous ETFs cannot establish causality, and minute closes do not establish fills.'}


def replay_source(events_file,report):
    """Recompute minute bars from the whole raw JSONL; report alone is NOT proof.

    This detects local changes between a v2.6 report and the raw source, not
    missing exchange messages or missing quote subscriptions.
    """
    from multiscale_lab import minute_observations
    src=report.get('sourceCoverage',{})
    if not src.get('sawRunStop') or src.get('truncatedAtMaxEvents') or src.get('sourceCompleteness')=='INCOMPLETE_OR_TRUNCATED_NEVER_CERTIFY':
        raise ValueError('RAW_RECORDING_NOT_CLOSED_OR_TRUNCATED')
    digest=hashlib.sha256();n=0;bad=0;last_kind=None
    def iter_rows():
        nonlocal n,bad,last_kind
        with Path(events_file).open('rb') as f:
            for raw in f:
                digest.update(raw);n+=1
                try:obj=json.loads(raw)
                except (UnicodeError,ValueError):
                    bad+=1;raise ValueError('MALFORMED_RAW_EVENT') from None
                last_kind=obj.get('kind')
                yield obj
    rebuild=minute_observations(iter_rows())
    if bad or n!=src.get('readEvents') or digest.hexdigest()!=src.get('sha256ReadPrefix') or last_kind!='run_stop':
        raise ValueError('RAW_RECORDING_REPORT_MISMATCH_OR_NOT_CLOSED')
    original=report.get('microstructure',{})
    for field in ('completedMinuteBars','observations','excluded'):
        if rebuild.get(field)!=original.get(field):raise ValueError('MICROSTRUCTURE_REPLAY_MISMATCH_'+field)
    return {'status':'LOCAL_SOURCE_REPLAY_VERIFIED_ONLY','readEvents':n,'sha256':digest.hexdigest(),
            'exchangeSequenceIntegrityProven':False,'segmentManifestAudited':False}


def cli(argv=None):
    p=argparse.ArgumentParser(description='FlowDesk v2.8 news/price event response; research only')
    p.add_argument('--news-log',type=Path,default=NEWS_LOG)
    p.add_argument('--multiscale-report',type=Path,default=ROOT/'output/research/multiscale_v26.json')
    p.add_argument('--events-file',type=Path,help='Full original JSONL required to independently replay v2.6 minute bars')
    p.add_argument('--asof',required=True,help='Explicit aware ISO8601 cutoff: NEVER default to historical publication time')
    p.add_argument('--out',type=Path,default=ROOT/'output/research/news_impact_v28.json')
    args=p.parse_args(argv)
    try:
        news=read_log(args.news_log)
        source=json.loads(args.multiscale_report.read_text(encoding='utf-8'))
        if not isinstance(source,dict):raise ValueError('BAD_REPORT')
        independent_replay=replay_source(args.events_file,source) if args.events_file else None
        study_result=study(news,source.get('microstructure',{}).get('completedMinuteBars',[]),
              asof=args.asof,micro_obs=source.get('microstructure',{}).get('observations',[]),
              source_coverage=source.get('sourceCoverage',{}))
        study_result['newsLogSha256']=hashlib.sha256(args.news_log.read_bytes()).hexdigest() if args.news_log.exists() else None
        study_result['marketReportSha256']=hashlib.sha256(args.multiscale_report.read_bytes()).hexdigest()
        study_result['independentLocalReplay']=independent_replay
        if independent_replay is None:
            study_result['status']='NO_GO_RAW_RECORDING_REPLAY_REQUIRED'
            study_result['sourceCompletenessAudited']=False
        else:
            # Local replay is not exchange-wide proof or a broker execution receipt.
            study_result['sourceCompletenessAudited']=True
            study_result['sourceCompletenessMeaning']='LOCAL_FULL_RAW_FILE_REPLAY_ONLY_NOT_EXCHANGE_DELIVERY'
            study_result['profitCertified']=False
    except (OSError,TypeError,ValueError,KeyError,json.JSONDecodeError) as e:
        print(json.dumps({'status':'NO_GO_NEWS_EVENT_STUDY','reason':str(e)},ensure_ascii=False));return 2
    args.out.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.out.with_suffix('.tmp')
    tmp.write_text(json.dumps(study_result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(args.out)
    print(json.dumps({'status':study_result['status'],'selected':study_result.get('selectedNewsSymbolEvents'),
                      'matured':study_result.get('maturedMatchedOutcomes'),
                      'out':str(args.out)},ensure_ascii=False))
    return 2 if study_result['status'].startswith('NO_GO') else 0

if __name__=='__main__':raise SystemExit(cli())
