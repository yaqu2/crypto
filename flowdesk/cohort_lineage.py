"""Cross-check first raw run_start event against time-frozen watchlist activations.

Historical v0.9 static watchlists are labelled UNREGISTERED, not retroactively
claimed to be selected via the all-market scanner. Current local ledger is not
third-party immutable storage.
"""
import json,hashlib
from datetime import datetime
from pathlib import Path
from market_archive import canonical
from segment_store import verify_recording
from feed_guard import as_time


def _hash(value):return hashlib.sha256(canonical(value)).hexdigest()

def check_cohort_lineage(recording,project,verified=None):
    recording=Path(recording);project=Path(project)
    verified=verified or verify_recording(recording,strict=True)
    with verified['files'][0].open('rb') as src:
        first=json.loads(src.readline())
    if first.get('kind')!='run_start':
        return {'status':'NO_GO_RUN_START_MISSING','tradeEnabled':False}
    data=first.get('data') or {};started=as_time(first.get('receivedAt'))
    cfg=data.get('watchlist')
    if not isinstance(cfg,dict) or started is None:
        return {'status':'NO_GO_RUN_START_METADATA','tradeEnabled':False}
    digest=_hash(cfg)
    if data.get('watchlistSha256') and data['watchlistSha256']!=digest:
        return {'status':'NO_GO_RUN_START_WATCHLIST_HASH','tradeEnabled':False}
    ledger=project/'research'/'watchlist_activation.jsonl'
    if not ledger.exists():
        return {'status':'UNREGISTERED_STATIC_WATCHLIST_ONLY','watchlistSha256':digest,
                'tradeEnabled':False,'profitCertified':False}
    prior=[];prev='0'*64
    try:
        for index,line in enumerate(ledger.read_bytes().splitlines()):
            entry=json.loads(line)
            unsigned={k:v for k,v in entry.items() if k!='hash'}
            if entry['seq']!=index+1 or entry['prev']!=prev or _hash(unsigned)!=entry['hash']:
                raise ValueError('BROKEN_ACTIVATION_HISTORY')
            prev=entry['hash'];ts=as_time(entry['activatedAt'])
            if ts is None:raise ValueError('ACTIVATION_DATE_INVALID')
            if ts<=started:prior.append(entry)
    except (ValueError,KeyError,TypeError):
        return {'status':'NO_GO_ACTIVATION_LEDGER','tradeEnabled':False}
    if not prior:
        return {'status':'UNREGISTERED_BEFORE_ACTIVATION','watchlistSha256':digest,
                'tradeEnabled':False,'profitCertified':False}
    actual=prior[-1]
    status=('VERIFIED_LOCAL_COHORT_LINEAGE_RESEARCH_ONLY' if digest==actual['newConfigSha256']
            else 'NO_GO_COHORT_MISMATCH')
    return {'status':status,'activatedAt':actual['activatedAt'],
            'runStartedAt':started.isoformat(),'scanArchiveId':actual['scanArchiveId'],
            'watchlistSha256':digest,'registeredSha256':actual['newConfigSha256'],
            'tradeEnabled':False,'profitCertified':False}
