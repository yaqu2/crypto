#!/usr/bin/env python3
"""Independent causal benchmark+stock quote pairing for captured L2 snapshots.

Only proves *locally observed callback causality*, not NBBO, exchange timestamps,
complete market coverage or executable transactions. Never places orders.
"""
import argparse, hashlib, json
from collections import Counter, defaultdict
from datetime import timezone
from zoneinfo import ZoneInfo

ET=ZoneInfo('America/New_York')
from pathlib import Path
from core import quote_time
from feed_guard import as_time, clock_state
from segment_store import verify_recording

MAX_QUOTE_AGE_FOR_BOOK=5.0
MAX_PROVIDER_LAG_AT_QUOTE=15.0
MAX_CLOCK_LEAD=2.0


def _paths(source, allow_prefix):
    src=Path(source)
    if src.is_dir():
        v=verify_recording(src,strict=not allow_prefix)
        meta=json.loads((src/'metadata.json').read_text())
        return v['files'],v['status'],bool(meta.get('closed')),v['events']
    if not src.is_file() or src.name!='events.jsonl':
        raise ValueError('EXPECTED_RUN_FOLDER_OR_EVENTS_JSONL')
    mf=src.parent/'metadata.json'
    if not mf.is_file():raise ValueError('RAW_METADATA_REQUIRED')
    meta=json.loads(mf.read_text())
    if meta.get('error') or meta.get('dropped',0):raise ValueError('RAW_INTEGRITY_ERROR')
    if not allow_prefix and not meta.get('closed'):raise ValueError('RAW_PREFIX_NOT_CLOSED')
    h=hashlib.sha256();size=count=0
    with src.open('rb') as f:
        for line in f:
            if not line.endswith(b'\n'):raise ValueError('TRUNCATED_RAW_LINE')
            h.update(line);size+=len(line);count+=1
    if h.hexdigest()!=meta.get('eventsSha256') or size!=meta.get('exportedBytes',meta.get('bytesWritten')) or count!=meta.get('exportedEvents',meta.get('eventsWritten')):
        raise ValueError('RAW_PREFIX_HASH_OR_LENGTH_MISMATCH')
    return [src],'VERIFIED_LOCAL_FILE_PREFIX_NOT_VENDOR_COMPLETENESS',bool(meta.get('closed')),count


def audit_pairing(source, *, benchmark='US.SPY', allow_prefix=False, max_gap=MAX_QUOTE_AGE_FOR_BOOK):
    if not 0<max_gap<=15:raise ValueError('MAX_GAP_OUT_OF_BOUNDS')
    files,local_integrity,closed,declared=_paths(source,allow_prefix)
    last_quote={}  # successful source quotes, ordered by actual callback/recorder processing
    counts=Counter();by_symbol=defaultdict(Counter);days=set();reason=Counter()
    latest_callback=None
    for path in files:
        with path.open('rb') as f:
            for raw in f:
                row=json.loads(raw)
                kind=row.get('kind');origin=row.get('origin')
                if origin!='push' or kind not in ('quote','book'):continue
                callback=as_time(row.get('callbackReceivedAt') or row.get('receivedAt'))
                if callback is None:
                    counts['missingCallbackTime']+=1;continue
                if latest_callback and callback<latest_callback:
                    # Never use late callbacks to repair earlier reconstructed evidence.
                    counts['outOfOrderCallback']+=1
                    continue
                latest_callback=callback
                regular=clock_state(callback)
                if not regular['regular']:continue
                day=regular['marketDate'];days.add(day)
                data=row.get('data')
                items=data if isinstance(data,list) else [data] if isinstance(data,dict) else []
                for item in items:
                    if not isinstance(item,dict):continue
                    code=item.get('code')
                    if not isinstance(code,str) or not code.startswith('US.'):continue
                    if kind=='quote':
                        counts['quotePushRows']+=1
                        source_time=as_time(quote_time(item.get('data_date'),item.get('data_time')))
                        lag=(callback-source_time).total_seconds() if source_time else None
                        try:price=float(item['last_price'])
                        except (KeyError,ValueError,TypeError):price=0
                        if source_time is None or source_time.astimezone(ET).date()!=callback.astimezone(ET).date() or lag is None or lag < -MAX_CLOCK_LEAD or lag>MAX_PROVIDER_LAG_AT_QUOTE or not 0<price<float('inf'):
                            counts['invalidOrStaleQuotes']+=1;continue
                        prev=last_quote.get(code)
                        if prev and source_time<=prev['source']:
                            counts['repeatedOrRegressiveQuotes']+=1;continue
                        last_quote[code]={'received':callback,'source':source_time}
                        counts['sourceVerifiedQuotePushRows']+=1
                    else:
                        counts['regularBookPushEvents']+=1
                        by_symbol[code]['regularBookPushEvents']+=1
                        try:
                            bid=item['Bid'];ask=item['Ask']
                            valid=bool(bid and ask and 0<float(bid[0][0])<float(ask[0][0]) and float(bid[0][1])>0 and float(ask[0][1])>0)
                        except (ValueError,TypeError,IndexError,KeyError):valid=False
                        if not valid:
                            reason['invalidBook']+=1;continue
                        counts['validRegularBooks']+=1
                        own=last_quote.get(code);base=last_quote.get(benchmark)
                        def fresh(q):
                            if not q:return False
                            receive_age=(callback-q['received']).total_seconds()
                            source_age=(callback-q['source']).total_seconds()
                            return 0<=receive_age<=max_gap and -MAX_CLOCK_LEAD<=source_age<=max_gap
                        if not fresh(own):
                            reason['noCausallyPriorFreshOwnQuote']+=1;continue
                        if not fresh(base):
                            reason['noCausallyPriorFreshBenchmarkQuote']+=1;continue
                        counts['pairedCandidateBookCallbacks']+=1
                        by_symbol[code]['pairedCandidateBookCallbacks']+=1
    return {'status':'NO_GO_TRADE_OR_ALPHA_CERTIFICATION',
            'mode':'READ_ONLY_QUOTE_PROVENANCE_AUDIT','verifiedLocalFileIntegrity':local_integrity,
            'closedRun':closed,'totalRawEvents':declared,'daysWithAnyRegularPush':sorted(days),
            'benchmark':benchmark,'maxPairAgeSeconds':max_gap,
            'counts':dict(counts),'rejectionReasons':dict(reason),
            'perStock':{key:dict(v) for key,v in sorted(by_symbol.items())},
            'readyForForwardProfitEvaluation':False,'liveOrdersEnabled':False,
            'warning':'Causal local quote/book callbacks only; NO stock-exchange quote auth, consolidated NBBO, live fill or alpha proof'}


def main():
    p=argparse.ArgumentParser(description='真实报价源时钟与盘口回调因果配对审计（只读）')
    p.add_argument('--recording',required=True,type=Path)
    p.add_argument('--benchmark',default='US.SPY')
    p.add_argument('--diagnose-prefix',action='store_true')
    p.add_argument('--out',type=Path)
    a=p.parse_args()
    result=audit_pairing(a.recording,benchmark=a.benchmark,allow_prefix=a.diagnose_prefix)
    if a.out:
        a.out.parent.mkdir(parents=True,exist_ok=True)
        a.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('status','closedRun','counts','rejectionReasons','perStock')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
