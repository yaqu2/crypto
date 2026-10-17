"""Reconcile every synthetic quote-model event to raw callback records.

Proves *local data lineage*, not exchange authenticity or executable NBBO.
"""
import hashlib,json
from pathlib import Path
from prospective import verify_chain
from segment_store import verify_recording


def sha_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def crosscheck(run_folder,raw_path=None):
    run=Path(run_folder);plan,events=verify_chain(run)
    raw=Path(raw_path) if raw_path else run.parent/'events.jsonl'
    if plan.get('rawRecorderRunId') and plan['rawRecorderRunId']!=run.parent.name:
        raise ValueError('WRONG_RECORDER_RUN_ID')
    if raw_path is None and (run.parent/'metadata.json').exists():
        metadata_probe=json.loads((run.parent/'metadata.json').read_text())
        if metadata_probe.get('closed') is False or 'prefix' in str(metadata_probe.get('exportType','')).lower():
            raise ValueError('RAW_RECORDER_PREFIX_NOT_CLOSED')
        verified=verify_recording(run.parent,strict='segments' in metadata_probe)
        raw_files=verified['files']
    else:
        raw_files=[raw]
    if any(not p.is_file() for p in raw_files):raise ValueError('RAW_EVENTS_MISSING')
    # no receive-time / source-side reconstruction without the original callback timestamp
    expected={}
    for event in events:
        kind=event['type']
        if kind not in ('SIGNAL','PAPER_ENTRY','PAPER_EXIT','BOOK_MINUTE'):continue
        stamp=event['at'] if kind!='PAPER_EXIT' else event['exitAt']
        key=(event['code'],stamp)
        expected.setdefault(key,[]).append(event)
    found=set();seen_rows=0
    for raw_file in raw_files:
        with raw_file.open(encoding='utf-8') as f:
            for line in f:
                if not line.strip():continue
                obj=json.loads(line)
                if obj.get('kind')!='book' or obj.get('origin')!='push':continue
                code=(obj.get('data') or {}).get('code')
                stamp=obj.get('callbackReceivedAt')  # never silently use later recorder timestamp
                key=(code,stamp)
                if key not in expected:continue
                d=obj['data'];b=d.get('Bid') or [];a=d.get('Ask') or []
                if not b or not a:continue
                try:
                    bp=max(float(level[0]) for level in b)
                    ap=min(float(level[0]) for level in a)
                except (ValueError,TypeError,IndexError):continue
                for ev in expected[key]:
                    eb=ev.get('exitBid') if ev['type']=='PAPER_EXIT' else ev.get('bid')
                    ea=ev.get('exitAsk') if ev['type']=='PAPER_EXIT' else ev.get('ask')
                    if eb is not None and ea is not None and abs(bp-eb)<1e-7 and abs(ap-ea)<1e-7:
                        found.add((key,ev.get('id',ev.get('etMinute')),ev['type']))
                seen_rows+=1
    missing=[(k,e.get('id',e.get('etMinute')),e['type']) for k,events_at_k in expected.items() for e in events_at_k
             if (k,e.get('id',e.get('etMinute')),e['type']) not in found]
    meta=run.parent/'metadata.json';metadata={}
    if meta.exists():
        metadata=json.loads(meta.read_text())
        if metadata.get('error') or metadata.get('dropped',0)>0:
            raise ValueError('RAW_RECORDER_INTEGRITY_FAILURE')
    raw_hash=sha_file(raw_files[0])
    claimed=metadata.get('eventsSha256')
    if claimed and raw_hash!=claimed:raise ValueError('RAW_SHA256_MISMATCH')
    if missing:raise ValueError(f'MISSING_OR_DIFFERENT_RAW_CALLBACK_BOOK: {len(missing)}/{sum(map(len,expected.values()))}')
    # If there are zero model events, lineage cannot be inferred as sound market coverage.
    return {'status':'MATCHED_LOCAL_CALLBACK_RECORDS' if expected else 'NO_EVENT_TO_CROSSCHECK',
            'bookTimesChecked':len(expected),'paperEventsMatched':len(found),'rawDigestSha256':raw_hash,
            'rawManifestChecksumMatched':bool(metadata.get('eventsSha256')),
            'rawSegmentsChecked':len(raw_files),
            'proof':'LOCAL_FILE_LINEAGE_ONLY_NOT_EXCHANGE_TIME_OR_EXECUTED_FILL',
            'liveOrdersEnabled':False}
