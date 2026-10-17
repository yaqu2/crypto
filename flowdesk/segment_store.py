"""Strict, backward-compatible local raw-recording reader. No trade authorization.

v0.9: independently verify *every* segment's bytes, digest, event count,
contiguous sequence and run ID before allowing an audit to use the recording.
Legacy v0.8 single-file captures can still be opened without upgrading records.
"""
import hashlib
import json
import re
from pathlib import Path

SEGMENT_RE = re.compile(r'events(?:\.(\d{6}))?\.jsonl\Z')


def _segments_from_metadata(meta):
    if 'segments' in meta:
        rows=meta['segments']
        if not isinstance(rows,list) or not rows:
            raise ValueError('RAW_SEGMENTS_EMPTY')
        names=[row.get('filename') for row in rows]
        if names[0]!='events.jsonl' or names[1:]!=[f'events.{i:06d}.jsonl' for i in range(1,len(names))]:
            raise ValueError('RAW_SEGMENT_ORDER_INVALID')
        return rows,True
    return [{'filename':'events.jsonl','sha256':meta.get('eventsSha256'),
             'bytes':meta.get('bytesWritten'),'events':meta.get('eventsWritten')}],False


def verify_recording(folder,strict=True,verify_rows=True):
    """Verify on-disk run. v0.8 synthetic fixture legacy may omit optional counts.

    strict=True demands clean, closed metadata; auditors must use it for real runs.
    Returns paths only after *all* file and metadata checks passed.
    """
    folder=Path(folder)
    meta_path=folder/'metadata.json'
    if not meta_path.is_file():raise ValueError('RAW_METADATA_MISSING')
    meta=json.loads(meta_path.read_text(encoding='utf-8'))
    if meta.get('error') or meta.get('dropped',0)>0:
        raise ValueError('RAW_RECORDER_INTEGRITY_FAILURE')
    if strict and not meta.get('closed',False):
        raise ValueError('RAW_RECORDER_NOT_CLOSED')
    segments,modern=_segments_from_metadata(meta)
    declared={row['filename'] for row in segments}
    actual={p.name for p in folder.iterdir() if p.is_file() and SEGMENT_RE.fullmatch(p.name)}
    if actual!=declared:raise ValueError('RAW_SEGMENT_MISSING_OR_UNREGISTERED')
    total_bytes=total_rows=0;paths=[];last_seq=0
    for item in segments:
        name=item['filename']
        if not SEGMENT_RE.fullmatch(name):raise ValueError('RAW_SEGMENT_NAME_INVALID')
        path=folder/name
        if path.is_symlink() or not path.is_file():raise ValueError('RAW_SEGMENT_NOT_REGULAR_FILE')
        h=hashlib.sha256();size=count=0;first_seq=None;end_seq=None
        with path.open('rb') as src:
            for line in src:
                h.update(line);size+=len(line)
                if not line.endswith(b'\n'):raise ValueError('RAW_TRUNCATED_EVENT_LINE')
                if verify_rows:
                    obj=json.loads(line)
                    seq=obj.get('seq')
                    if modern or isinstance(seq,int):
                        if not isinstance(seq,int) or seq!=last_seq+1:
                            raise ValueError('RAW_NONCONTIGUOUS_SEQUENCE')
                        last_seq=seq
                    if meta.get('runId') and obj.get('runId')!=meta['runId']:
                        raise ValueError('RAW_WRONG_RUN_ID')
                if first_seq is None:first_seq=last_seq if verify_rows else None
                end_seq=last_seq if verify_rows else None
                count+=1
        if not item.get('sha256') or h.hexdigest()!=item['sha256']:
            raise ValueError('RAW_SEGMENT_SHA256_MISMATCH')
        if item.get('bytes') is not None and size!=item['bytes']:
            raise ValueError('RAW_SEGMENT_SIZE_MISMATCH')
        if item.get('events') is not None and count!=item['events']:
            raise ValueError('RAW_SEGMENT_EVENT_COUNT_MISMATCH')
        if modern and (item.get('firstSeq')!=first_seq or item.get('lastSeq')!=end_seq):
            raise ValueError('RAW_SEGMENT_SEQUENCE_RANGE_MISMATCH')
        total_bytes+=size;total_rows+=count;paths.append(path)
    if meta.get('bytesWritten') is not None and total_bytes!=meta['bytesWritten']:
        raise ValueError('RAW_TOTAL_SIZE_MISMATCH')
    if meta.get('eventsWritten') is not None and total_rows!=meta['eventsWritten']:
        raise ValueError('RAW_TOTAL_EVENT_COUNT_MISMATCH')
    if modern and meta.get('eventsSha256')!=segments[0]['sha256']:
        raise ValueError('RAW_FIRST_SEGMENT_HASH_MISMATCH')
    return {'files':paths,'events':total_rows,'bytes':total_bytes,'segmentCount':len(paths),
            'sha256':[row['sha256'] for row in segments],
            'status':'VERIFIED_LOCAL_RAW_RECORDING_NOT_PROVIDER_COMPLETENESS'}
