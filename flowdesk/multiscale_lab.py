#!/usr/bin/env python3
"""FlowDesk v2.6: read-only multiscale research evidence and path-label lab.

Never interprets L2 snapshot increments as identified exchange order messages.
Never calls order placement, downloads external sources or certifies alpha.
All as-of comparisons use recorded availability clocks, not revised future values.
"""
from __future__ import annotations
import argparse, hashlib, json, math
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo('America/New_York')
HORIZONS = (1, 3, 5)
REGULAR_MINUTES = 390
BENCHMARKS = ('US.SPY', 'US.QQQ', 'US.SMH', 'US.IWM', 'US.XLK', 'US.XSD')


def stamp(value):
    if not isinstance(value, str): raise ValueError('CLOCK_MISSING')
    try: dt = datetime.fromisoformat(value.replace('Z','+00:00'))
    except (ValueError,OverflowError): raise ValueError('CLOCK_INVALID') from None
    if dt.tzinfo is None: raise ValueError('CLOCK_TIMEZONE_REQUIRED')
    return dt.astimezone(UTC)


def positive(v):
    try:
        n=float(v)
        return n if math.isfinite(n) and n>0 else None
    except (TypeError,ValueError,OverflowError): return None


def nonnegative(v):
    try:
        n=float(v)
        return n if math.isfinite(n) and n>=0 else None
    except (TypeError,ValueError,OverflowError): return None


def usable_context(items, decision_at, *, allowed_lag_seconds=None):
    """Pick the latest revision actually recorded and received by decision_at.

    Every item must have all three timestamps and an explicit provider.
    Causal clock: max(original public release, provider first-seen, local receipt).
    A daily FRED observation without intraday first availability is never auto-promoted.
    """
    decision=stamp(decision_at); latest={}; discarded=Counter()
    for item in items:
        try:
            key=(str(item['type']),str(item['key']))
            if not key[0] or not key[1] or not item.get('provider'): raise ValueError('BAD_CONTEXT_ID')
            public=stamp(item['published_at']); first=stamp(item['first_seen_at']); received=stamp(item['received_at'])
            if public > first or first > received: raise ValueError('IMPOSSIBLE_PUBLICATION_ORDER')
            available=max(public,first,received)
            if available>decision: discarded['NOT_KNOWN_YET']+=1;continue
            if allowed_lag_seconds is not None and (decision-available).total_seconds()>allowed_lag_seconds:
                discarded['STALE_CONTEXT']+=1;continue
            value=item['value']; float(value) if isinstance(value,(float,int)) else None
            if isinstance(value,float) and not math.isfinite(value):raise ValueError('NONFINITE_VALUE')
            old=latest.get(key)
            if old is None or (available, int(item.get('revision',0))) > (stamp(old['available_at']),old['revision']):
                latest[key]={'type':key[0],'key':key[1],'value':value,'provider':item['provider'],
                             'available_at':available.isoformat(),'published_at':public.isoformat(),
                             'revision':int(item.get('revision',0)), 'source_id':item.get('source_id')}
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            discarded['INVALID_CONTEXT']+=1
    return {'known':list(sorted(latest.values(),key=lambda x:(x['type'],x['key']))), 'discarded':dict(discarded),
            'tradeSignal':False}


def _best(levels):
    try:
        if not levels: return None
        first=levels[0]
        if isinstance(first,dict): p,q=first['price'],first['volume']
        else:p,q=first[:2]
        p=positive(p); q=nonnegative(q)
        return (p,q) if p is not None and q is not None else None
    except (ValueError,KeyError,TypeError,IndexError):return None


def normalized_book(raw):
    bid=_best(raw.get('Bid') or raw.get('bid'))
    ask=_best(raw.get('Ask') or raw.get('ask'))
    if not bid or not ask or bid[0]>=ask[0]: return None
    return {'code':raw.get('code'), 'bid':bid[0], 'bidSize':bid[1],
            'ask':ask[0], 'askSize':ask[1],
            # Moomoo US callbacks in historical recordings have empty venue source clocks.
            'exchangeEventClockVerified':False}


def pair_ofi(a,b):
    """Top of book OFI (Cont et al.), and display-size increase *proxies*.
    Comparing successive snapshots does not distinguish cancellation vs execution;
    missing exchange message sequence prevents true order-add/cancel attribution.
    """
    if not a or not b or a.get('code')!=b.get('code'):return None
    bid=(b['bidSize'] if b['bid']>=a['bid'] else 0)-(a['bidSize'] if b['bid']<=a['bid'] else 0)
    ask=(b['askSize'] if b['ask']<=a['ask'] else 0)-(a['askSize'] if b['ask']>=a['ask'] else 0)
    bid_inc=max(0,b['bidSize']-a['bidSize']) if a['bid']==b['bid'] else 0
    ask_inc=max(0,b['askSize']-a['askSize']) if a['ask']==b['ask'] else 0
    return {'ofi_top_proxy':bid-ask,'bid_size_increase_proxy':bid_inc,
            'ask_size_increase_proxy':ask_inc,'true_order_additions_known':False}


def minute_observations(records, *, max_callback_lag_seconds=5, max_gap_seconds=20):
    """Process callback-ordered Moomoo event JSONL, no future updates/seed/cache.
    All event times are LOCAL receipt clocks; these are diagnostic proxies only.
    Do not equate absence of local drops with exchange-wide order flow coverage.
    """
    if max_gap_seconds<=0 or max_callback_lag_seconds<=0:raise ValueError('BAD_GAP_LIMIT')
    minutes={}; completed_bars={}; last_book={}; last_seq_by_run={}; seen_ticks=set(); excluded=Counter()
    for event in records:
        kind=event.get('kind')
        if kind not in ('book','ticker','kline'):continue
        if event.get('origin')!='push': excluded['NOT_LIVE_PUSH']+=1;continue
        try:
            recv=stamp(event['callbackReceivedAt'] if 'callbackReceivedAt' in event else event['receivedAt'])
            written=stamp(event['receivedAt'])
            if abs((written-recv).total_seconds())>max_callback_lag_seconds:
                excluded['LOCAL_RECORDER_LAG']+=1;continue
            seq=event.get('seq'); run=event.get('runId')
            if seq is not None and run is not None:
                old=last_seq_by_run.get(run)
                if old is not None and int(seq)<=old:excluded['NONMONOTONIC_RECORDER']+=1;continue
                last_seq_by_run[run]=int(seq)
        except (ValueError,KeyError,TypeError):excluded['BAD_CALLBACK_CLOCK']+=1;continue
        payload=event.get('data')
        entries=payload if isinstance(payload,list) else [payload]
        for raw in entries:
            if not isinstance(raw,dict) or not raw.get('code'):excluded['BAD_RAW_EVENT']+=1;continue
            code=raw['code']; minute=recv.replace(second=0,microsecond=0).isoformat()
            key=(code,minute)
            if key not in minutes:minutes[key]={'symbol':code,'minuteUtc':minute,'bookUpdates':0,'ofiTopProxy':0.0,
                                                'displayedBidIncreaseProxy':0.0,'displayedAskIncreaseProxy':0.0,
                                                'tradeVolumeKnownDirection':0.0,'signedTradeVolume':0.0,
                                                'unknownDirectionVolume':0.0,'ticks':0,'lastMid':None,
                                                'meanSpreadBps':None,'_spread':[],'_gap':False}
            b=minutes[key]
            if kind=='book':
                book=normalized_book(raw)
                if not book:excluded['INVALID_BOOK']+=1;continue
                old=last_book.get(code)
                if old:
                    elapsed=(recv-old['at']).total_seconds()
                    if elapsed<=0:excluded['OUT_OF_ORDER_BOOK']+=1;continue
                    if elapsed>max_gap_seconds:
                        b['_gap']=True;excluded['BOOK_STREAM_GAP']+=1
                    else:
                        delta=pair_ofi(old['book'],book)
                        b['ofiTopProxy']+=delta['ofi_top_proxy']
                        b['displayedBidIncreaseProxy']+=delta['bid_size_increase_proxy']
                        b['displayedAskIncreaseProxy']+=delta['ask_size_increase_proxy']
                last_book[code]={'at':recv,'book':book}
                b['bookUpdates']+=1;b['lastMid']=(book['bid']+book['ask'])/2
                b['_spread'].append(2e4*(book['ask']-book['bid'])/(book['ask']+book['bid']))
            elif kind=='ticker':
                if raw.get('push_data_type')=='CACHE':excluded['CACHED_TICK']+=1;continue
                price=positive(raw.get('price')); size=positive(raw.get('volume'))
                if price is None or size is None:excluded['INVALID_TICK']+=1;continue
                # ticker time may be provider ET exchange event timestamp, but
                # callback arrival is the conservative signal-availability clock.
                direction=str(raw.get('ticker_direction','')).upper()
                dedupe=(event.get('runId'),code,str(raw.get('sequence')))
                if raw.get('sequence') is not None and dedupe in seen_ticks:
                    excluded['DUPLICATE_TICK']+=1;continue
                if raw.get('sequence') is not None:seen_ticks.add(dedupe)
                b['ticks']+=1
                if direction in ('BUY','SELL'):
                    b['tradeVolumeKnownDirection']+=size
                    b['signedTradeVolume']+=size*(1 if direction=='BUY' else -1)
                else:b['unknownDirectionVolume']+=size
            else:
                # Moomoo time_key is ET wall-clock; 1-minute bar starts at time_key.
                # Intraminute callbacks are *partial* and never treated as final.
                try:
                    time_key=str(raw['time_key'])
                    bar_start=datetime.strptime(time_key,'%Y-%m-%d %H:%M:%S').replace(tzinfo=ET)
                    bar_end=(bar_start+timedelta(minutes=1)).astimezone(UTC)
                    local=bar_start.astimezone(ET)
                    if not ((local.hour>9 or (local.hour==9 and local.minute>=30)) and local.hour<16):
                        excluded['BAR_NOT_REGULAR_SESSION']+=1;continue
                    if recv<bar_end:
                        excluded['PARTIAL_BAR_NOT_USED']+=1;continue
                    if (recv-bar_end).total_seconds()>180:
                        excluded['STALE_BAR_FIRST_AVAILABLE_LATE']+=1;continue
                    o=positive(raw.get('open'));h=positive(raw.get('high'))
                    l=positive(raw.get('low'));c=positive(raw.get('close'))
                    vol=nonnegative(raw.get('volume'))
                    if None in (o,h,l,c,vol) or h<max(o,c,l) or l>min(o,c,h):
                        excluded['BAD_MINUTE_OHLCV']+=1;continue
                    ident=(code,bar_start.date().isoformat(),bar_start.isoformat())
                    if ident in completed_bars:
                        excluded['LATER_BAR_REVISION_NOT_BACKFILLED']+=1;continue
                    completed_bars[ident]={'symbol':code,'startUtc':bar_start.astimezone(UTC).isoformat(),
                        'barEndUtc':bar_end.isoformat(),'firstAvailableUtc':recv.isoformat(),
                        'open':o,'high':h,'low':l,'close':c,'volume':vol,
                        'finalBarByTimeHeuristicOnly':True,'brokerVerifiedClose':False}
                except (ValueError,KeyError,TypeError):excluded['BAD_MINUTE_BAR_TIMESTAMP']+=1
    out=[]
    for b in minutes.values():
        if b['bookUpdates']==0 and b['ticks']==0:continue
        b['meanSpreadBps']=round(sum(b['_spread'])/len(b['_spread']),4) if b['_spread'] else None
        b['ofiTopProxy']=round(b['ofiTopProxy'],4)
        b['displayedBidIncreaseProxy']=round(b['displayedBidIncreaseProxy'],4)
        b['displayedAskIncreaseProxy']=round(b['displayedAskIncreaseProxy'],4)
        b['tapeImbalance']=round(b['signedTradeVolume']/b['tradeVolumeKnownDirection'],6) if b['tradeVolumeKnownDirection']>0 else None
        b['status']='GAP_OR_UNVERIFIED_L2_PROVENANCE' if b.pop('_gap') else 'ARRIVAL_CLOCK_PROXY_ONLY'
        del b['_spread']
        b['exchangeOrderEventsObserved']=False
        b['executableSignal']=False
        out.append(b)
    return {'status':'EXPLORATORY_RECEIVE_TIME_MICROSTRUCTURE_ONLY' if out or completed_bars else 'NO_GO_NO_ELIGIBLE_EVENTS',
            'observations':sorted(out,key=lambda x:(x['minuteUtc'],x['symbol'])),
            'completedMinuteBars':sorted(completed_bars.values(),key=lambda x:(x['startUtc'],x['symbol'])),
            'excluded':dict(excluded),'tradeSignal':False,'profitCertified':False,
            'warning':'Arrival-time L2 snapshots do not identify individual adds/cancels or exchange event sequence.'}


def path_targets(sessions, *, horizons=HORIZONS, max_calendar_gap=5):
    """Horizon-aware target construction from *externally verified* completed session prices.
    Rows represent one asset; no same-day intraday bar is treated as official close.
    Returns only matured labels, and explicitly tracks unfinished labels.
    """
    horizons=tuple(sorted(set(int(h) for h in horizons)))
    if not horizons or any(h<1 or h>20 for h in horizons):raise ValueError('BAD_HORIZON')
    dates=[s.get('day') for s in sessions]
    if dates!=sorted(set(dates)):raise ValueError('SESSION_DATES_NOT_STRICTLY_INCREASING')
    out=[]; pending=[]; discarded=Counter()
    for i,entry in enumerate(sessions):
        symbol=entry.get('symbol','US.UNKNOWN')
        # Require independently verified regular-session close or a precisely declared sampling contract.
        # For daily outputs mixing symbols, caller must group by symbol before calling.
        p=positive(entry.get('close'))
        if not p or entry.get('close_verified') is not True:
            discarded['UNVERIFIED_CLOSE']+=1;continue
        for h in horizons:
            if i+h>=len(sessions):pending.append({'day':entry['day'],'symbol':symbol,'horizon':h,'reason':'NOT_YET_OBSERVED'});continue
            block=sessions[i:i+h+1]
            if any(b.get('symbol','US.UNKNOWN')!=symbol or b.get('close_verified') is not True or positive(b.get('close')) is None for b in block):
                discarded['INCOMPLETE_FUTURE_CLOSE']+=1;continue
            try:
                dd=[datetime.fromisoformat(b['day']).date() for b in block]
                if any(not 0<(dd[k]-dd[k-1]).days<=max_calendar_gap for k in range(1,len(dd))):
                    discarded['GAP_OR_HOLIDAY_NOT_RECONCILED']+=1;continue
            except (ValueError, TypeError):discarded['BAD_SESSION_DATE']+=1;continue
            prices=[float(b['close']) for b in block]
            future=prices[-1]/prices[0]-1
            path=[v/prices[0]-1 for v in prices]
            running_peak=prices[0];mdd=0.0
            for v in prices[1:]:running_peak=max(running_peak,v);mdd=min(mdd,v/running_peak-1)
            out.append({'day':entry['day'],'symbol':symbol,'horizon':h,'labelEndDay':block[-1]['day'],
                        'futureReturn':round(future,9),'maxDrawdownFromObservedCloses':round(mdd,9),
                        'worstCloseFromEntry':round(min(path),9),'bestCloseFromEntry':round(max(path),9),
                        'positive':int(future>0),'rebound5pct':int(future>=.05),
                        'futurePricesUsedInFeatures':False,'intraDayPathKnown':False,
                        'brokerFillEvidence':False})
    return {'status':'LABELED_RESEARCH_ONLY' if out else 'NO_GO_NO_MATURED_LABELS',
            'labels':out,'pending':pending,'excluded':dict(discarded),'tradeSignal':False}


def intraday_path_targets(bars, *, horizons=(1, 5, 15), barrier_pct=.02):
    """Observed minute OHLC ranges; returns barrier ORDER as unknown if ambiguous.

    Closed-bar observations only; does not infer executable entry or broker fill.
    For one equity and ET date, accepted minute bars must be consecutive.
    A missing minute blocks its horizon, rather than interpolating future prices.
    """
    horizons=tuple(sorted(set(int(h) for h in horizons)))
    if not horizons or any(h<1 or h>60 for h in horizons) or not 0<barrier_pct<1:
        raise ValueError('INVALID_INTRADAY_HORIZON')
    sorted_bars=sorted(bars,key=lambda b:(b['symbol'],b['startUtc']))
    grouped=defaultdict(list)
    for b in sorted_bars:
        if b.get('finalBarByTimeHeuristicOnly') is not True:continue
        grouped[(b['symbol'],stamp(b['startUtc']).astimezone(ET).date().isoformat())].append(b)
    labels=[];unmatured=[];discarded=Counter()
    for (symbol,day),series in grouped.items():
        for i,entry in enumerate(series):
            current=positive(entry.get('close'))
            if current is None:discarded['INVALID_ENTRY_CLOSE']+=1;continue
            try:available=stamp(entry['firstAvailableUtc'])
            except (KeyError,ValueError):discarded['NO_ENTRY_RECEIPT_TIME']+=1;continue
            # Never use high/low from a minute that started before the decision
            # became available: even 2 seconds of hindsight would leak returns.
            eligible=[b for b in series[i+1:] if stamp(b['startUtc'])>=available]
            if eligible and (stamp(eligible[0]['startUtc'])-available).total_seconds()>120:
                discarded['DELAYED_OR_MISSING_FUTURE_BAR']+=1;continue
            for h in horizons:
                if len(eligible)<h:
                    unmatured.append({'symbol':symbol,'minuteUtc':entry['startUtc'],'horizonMinutes':h})
                    continue
                block=eligible[:h]
                clock=stamp(block[0]['startUtc']);gap=False
                for b in block[1:]:
                    if stamp(b['startUtc'])!=clock+timedelta(minutes=1):gap=True;break
                    clock=stamp(b['startUtc'])
                if gap:discarded['MISSING_MINUTE_BAR']+=1;continue
                if any(positive(b.get('high')) is None or positive(b.get('low')) is None or positive(b.get('close')) is None for b in block):
                    discarded['INVALID_FUTURE_BAR']+=1;continue
                best=max(b['high'] for b in block)/current-1
                worst=min(b['low'] for b in block)/current-1
                close_ret=block[-1]['close']/current-1
                hit_up=best>=barrier_pct;hit_down=worst<=-barrier_pct
                labels.append({'symbol':symbol,'entryMinuteUtc':entry['startUtc'],
                    'featureAvailableUtc':available.isoformat(),
                    'firstFutureFullMinuteUtc':block[0]['startUtc'],
                    'horizonMinutes':h,'labelEndUtc':block[-1]['barEndUtc'],
                    'closeReturn':round(close_ret,8),'observedMFE':round(best,8),
                    'observedMAE':round(worst,8),'bothBarriersTouched':bool(hit_up and hit_down),
                    'barrierHitOrder':('UNKNOWN_BOTH_DURING_WINDOW' if hit_up and hit_down else
                         'UPSIDE_ONLY' if hit_up else 'DOWNSIDE_ONLY' if hit_down else 'NEITHER'),
                    'fillAssumed':False,'highLowOrderWithinBarKnown':False,
                    'profitCertified':False})
    return {'status':'EXPLORATORY_INTRADAY_PATH_ONLY' if labels else 'NO_GO_NO_MATURED_INTRADAY_PATH',
            'labels':labels,'pending':unmatured,'discarded':dict(discarded),
            'warning':'Future highs/lows do not establish quote executability or same-bar price-hit order.'}


def broad_regime(history, *, date_cutoff=None):
    """Point-in-time trailing drawdown, volatility and direction; not smoothed HMM."""
    records=sorted(history,key=lambda x:x['day'])
    if date_cutoff:records=[r for r in records if r['day']<=date_cutoff]
    if len(records)<61 or any(positive(x.get('close')) is None or x.get('close_verified') is not True for x in records[-61:]):
        return {'status':'NO_GO_NOT_ENOUGH_VERIFIED_HISTORY','regime':None}
    values=[float(x['close']) for x in records[-61:]]
    ret=[values[i]/values[i-1]-1 for i in range(1,len(values))]
    roll_peak=max(values[-60:]); drawdown=values[-1]/roll_peak-1
    ret20=values[-1]/values[-21]-1
    variance=sum((r-sum(ret)/len(ret))**2 for r in ret)/len(ret)
    vol=math.sqrt(variance)*math.sqrt(252)
    # Example non-optimized thresholds; must be frozen before prospective use.
    regime='PANIC_WATCH' if vol>.45 and drawdown<-.12 else ('BEAR_PRESSURE' if ret20<-.07 and drawdown<-.12 else ('RECOVERY_WATCH' if ret20>.05 and drawdown<-.10 else 'OTHER'))
    return {'status':'RULE_BASED_CONTEMPORANEOUS_ONLY','regime':regime,'trailing20Return':round(ret20,6),
            'trailingDrawdown60':round(drawdown,6),'annualizedVol60':round(vol,6),
            'notFutureRegime':True,'noSmoothedHiddenStates':True}


def cli():
    p=argparse.ArgumentParser(description='Read-only v2.6 multiscale evidence; no model predictions or orders')
    p.add_argument('--events-file',type=Path, help='Raw FlowDesk recorder event JSONL (complete run preferred)')
    p.add_argument('--context-file',type=Path, help='Externally supplied PIT records with publication/receipt timestamps')
    p.add_argument('--asof',help='UTC ISO time for as-of external context')
    p.add_argument('--daily-file',type=Path,help='Verified daily closes JSONL for ONE symbol')
    p.add_argument('--out',type=Path,default=Path(__file__).resolve().parent/'output'/'research'/'multiscale_v26.json')
    p.add_argument('--max-events',type=int,default=500000)
    args=p.parse_args()
    report={'version':'2.6-research-only','status':'NO_GO_NO_DATA','profitCertified':False,
            'realOrdersEnabled':False,'tradeSignal':False,'sourceCoverage':{},
            'capabilities':['minute receive-time proxies','PIT macro/news joins','1/3/5d close-to-close risk labels'],
            'nonCapabilities':['no complete MBO reconstruction','no verified predictive alpha','no actual fills']}
    if args.events_file:
        if args.max_events<1:raise SystemExit('INVALID_MAX_EVENTS')
        counter=Counter()
        seen_end=[False]
        digest=hashlib.sha256()
        def stream():
            with args.events_file.open('rb') as f:
                for raw in f:
                    if counter['readEvents']>=args.max_events:
                        counter['truncatedAtMaxEvents']=1;break
                    digest.update(raw)
                    counter['readEvents']+=1
                    try:obj=json.loads(raw)
                    except (ValueError,UnicodeDecodeError):counter['badJson']+=1;continue
                    if obj.get('kind')=='run_stop':seen_end[0]=True
                    yield obj
        report['microstructure']=minute_observations(stream())
        report['intradayPathTargets']=intraday_path_targets(report['microstructure']['completedMinuteBars'])
        report['sourceCoverage'].update(counter)
        report['sourceCoverage']['sha256ReadPrefix']=digest.hexdigest()
        report['sourceCoverage']['sawRunStop']=seen_end[0]
        report['sourceCoverage']['fileIntegrityVerified']=False
        report['sourceCoverage']['warning']='A raw JSONL SHA256 does not replace the recorder segment manifest integrity audit'
        report['status']='EXPLORATORY_RECEIVE_TIME_ONLY'
        if not seen_end[0] or counter['truncatedAtMaxEvents']:
            report['sourceCoverage']['sourceCompleteness']='INCOMPLETE_OR_TRUNCATED_NEVER_CERTIFY'
            report['status']='NO_GO_INCOMPLETE_SOURCE_RESEARCH_ONLY'
    if args.context_file:
        if not args.asof:raise SystemExit('CONTEXT_REQUIRES_ASOF')
        rows=[json.loads(line) for line in args.context_file.read_text(encoding='utf-8').splitlines() if line.strip()]
        report['asofContext']=usable_context(rows,args.asof)
        report['status']='EXPLORATORY_ASOF_CONTEXT_ONLY' if report['status']=='NO_GO_NO_DATA' else report['status']
    if args.daily_file:
        rows=[json.loads(line) for line in args.daily_file.read_text(encoding='utf-8').splitlines() if line.strip()]
        report['pathTargets']=path_targets(rows)
        if report['status']!='NO_GO_INCOMPLETE_SOURCE_RESEARCH_ONLY':
            report['status']='EXPLORATORY_MULTISCALE_DATA_ONLY'
    args.out.parent.mkdir(parents=True,exist_ok=True)
    temp=args.out.with_suffix('.tmp');temp.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8');temp.replace(args.out)
    print(json.dumps({k:v for k,v in report.items() if k not in ('microstructure','pathTargets')},ensure_ascii=False,indent=2))
    return 2 if report['status'].startswith('NO_GO') else 0

if __name__=='__main__':raise SystemExit(cli())
