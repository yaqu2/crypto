"""Continuous, bounded, rotating local callback recorder; strictly read-only.

v0.9 retains events.jsonl as segment zero for old programs and adds
consecutively numbered segments, each independently SHA-256 verified.
A disk, size, or queue error is terminal for research, never concealed.
"""
import io,json,math,queue,threading,time,zipfile,uuid,hashlib,platform
from run_inventory import register_run
from importlib.metadata import version,PackageNotFoundError
from datetime import datetime,timezone
from pathlib import Path


def timestamp():return datetime.now(timezone.utc).isoformat()

def safe(value):
    if isinstance(value,dict):return {str(k):(str(v) if str(k)=='sequence' and v is not None else safe(v)) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [safe(v) for v in value]
    if isinstance(value,float):return value if math.isfinite(value) else None
    if value is None or isinstance(value,(str,int,bool)):return value
    if hasattr(value,'item'):return safe(value.item())
    return str(value)

class Recorder:
    def __init__(self,directory,max_bytes=8*1024**3,queue_size=5000,symbols=None,
                 app_version='0.9.0-segmented-evidence',config=None,shadow_expected=False,
                 segment_bytes=64*1024**2,max_export_bytes=256*1024**2,trial_binding=None):
        if not (0 < segment_bytes <= max_bytes):raise ValueError('INVALID_SEGMENT_LIMIT')
        self.run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        self.folder=Path(directory)/self.run_id;self.folder.mkdir(parents=True,exist_ok=False)
        self.max_bytes=max_bytes;self.segment_bytes=segment_bytes;self.max_export_bytes=max_export_bytes
        self.q=queue.Queue(maxsize=queue_size)
        self.guard=threading.Lock();self.disk=threading.Lock();self.stop=threading.Event()
        self.sequence=0;self.written=0;self.size=0;self.dropped=0;self.error=None;self.last=None
        self.started=timestamp();self.closed=False;self.shadow_expected=bool(shadow_expected)
        trial_binding=trial_binding or {}
        self.trial_id=trial_binding.get('trialId')
        self.trial_seal=trial_binding.get('trialSealSha256')
        self._index=0;self._sealed=[];self._current_size=0;self._current_count=0
        self._current_first=None;self._current_last=None;self._hash=hashlib.sha256()
        self.path=self.folder/'events.jsonl';self.file=self.path.open('xb')
        try:
            register_run(Path(directory),self.run_id,self.started,self.shadow_expected)
        except Exception:
            self.file.close();raise
        try:sdk=version('moomoo-api')
        except PackageNotFoundError:sdk='not-installed'
        self.worker=threading.Thread(target=self._write,daemon=True);self.worker.start()
        self.record('run_start',{'version':app_version,'sdkVersion':sdk,'pythonVersion':platform.python_version(),
                    'openDVersion':'unknown','shadowExpected':self.shadow_expected,'symbols':symbols or [],'watchlist':config,
                    'trialId':self.trial_id,'trialSealSha256':self.trial_seal,
                    'watchlistSha256':hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest() if config is not None else None,
                    'scope':'quote-only; no account data','timestamps':'receive order; not exchange order',
                    'clock':'UTC wall clock and monotonic_ns','session_request':'ALL',
                    'quote_coverage':'OpenD NORMAL venue coverage unverified'},origin='local')

    def _name(self,index):return 'events.jsonl' if index==0 else f'events.{index:06d}.jsonl'

    def record(self,kind,data,origin='push',callback_received_at=None,callback_monotonic_ns=None):
        with self.guard:
            if self.closed:return
            self.sequence+=1
            if self.error:
                self.dropped+=1;return
            row={'schemaVersion':1,'runId':self.run_id,'seq':self.sequence,'receivedAt':timestamp(),
                 'monotonicNs':time.monotonic_ns(),'kind':kind,'origin':origin,'data':safe(data)}
            if callback_received_at is not None:
                row['callbackReceivedAt']=callback_received_at;row['callbackMonotonicNs']=callback_monotonic_ns
            try:self.q.put_nowait(row)
            except queue.Full:self.dropped+=1

    def _current_metadata(self):
        return {'filename':self._name(self._index),'bytes':self._current_size,
                'events':self._current_count,'firstSeq':self._current_first,'lastSeq':self._current_last,
                'sha256':self._hash.hexdigest()}

    def _seal_and_rotate(self):
        # Called exclusively with self.disk held by writer; existing file is immutable after close.
        self.file.flush();self.file.close()
        self._sealed.append(self._current_metadata())
        self._index+=1;self.path=self.folder/self._name(self._index)
        self.file=self.path.open('xb');self._hash=hashlib.sha256()
        self._current_size=0;self._current_count=0;self._current_first=None;self._current_last=None

    def _write(self):
        while not self.stop.is_set() or not self.q.empty():
            try:row=self.q.get(timeout=.2)
            except queue.Empty:continue
            try:
                blob=(json.dumps(row,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n').encode()
                with self.disk:
                    if self.size+len(blob)>self.max_bytes:raise OSError('研究录制达到全运行容量上限；停止录制并关闭研究门禁')
                    if len(blob)>self.segment_bytes:raise OSError('单条行情消息超过分段容量上限')
                    if self._current_count and self._current_size+len(blob)>self.segment_bytes:
                        self._seal_and_rotate()
                    self.file.write(blob);self.file.flush()
                    self._hash.update(blob)
                    self._current_size+=len(blob);self._current_count+=1
                    if self._current_first is None:self._current_first=row['seq']
                    self._current_last=row['seq']
                    self.size+=len(blob);self.written+=1;self.last=row['receivedAt']
            except Exception as exc:
                with self.guard:self.error=str(exc);self.dropped+=1
            finally:self.q.task_done()

    def status(self):
        with self.guard:
            return {'runId':self.run_id,'startedAt':self.started,'shadowExpected':self.shadow_expected,
                    'trialId':self.trial_id,'trialSealSha256':self.trial_seal,
                    'eventsWritten':self.written,'bytesWritten':self.size,'segmentCount':self._index+1,
                    'maxRunBytes':self.max_bytes,'segmentLimitBytes':self.segment_bytes,
                    'queuePending':self.q.qsize(),'dropped':self.dropped,'error':self.error,
                    'lastWrittenAt':self.last,'closed':self.closed,
                    'integrity':'INCOMPLETE' if self.error or self.dropped else 'NO_LOCAL_DROP_DETECTED',
                    'note':'No local drop detected does not prove provider completeness or historical market coverage.'}

    def export(self):
        # ZIP only for bounded *complete-line prefix*; large captures must be copied from Mac storage.
        with self.disk:
            if not self.file.closed:self.file.flush()
            parts=[dict(x) for x in self._sealed]+[self._current_metadata()]
            if sum(x['bytes'] for x in parts)>self.max_export_bytes:
                raise ValueError('EXPORT_TOO_LARGE_USE_LOCAL_RECORDINGS_DIRECTORY')
        meta=self.status();meta['exportedBytes']=sum(x['bytes'] for x in parts)
        meta['exportedEvents']=sum(x['events'] for x in parts)
        meta['exportedAt']=timestamp();meta['exportType']='MULTI_SEGMENT_PREFIX_SNAPSHOT_NOT_CLOSED_RUN'
        memory=io.BytesIO();out=[]
        with zipfile.ZipFile(memory,'w',zipfile.ZIP_DEFLATED) as z:
            for part in parts:
                digest=hashlib.sha256();remaining=part['bytes']
                with (self.folder/part['filename']).open('rb') as src,z.open(part['filename'],'w') as dst:
                    while remaining:
                        blob=src.read(min(1<<20,remaining))
                        if not blob:raise OSError('RAW_EXPORT_TRUNCATED')
                        digest.update(blob);dst.write(blob);remaining-=len(blob)
                if digest.hexdigest()!=part['sha256']:
                    raise OSError('RAW_EXPORT_SEGMENT_CHANGED_DURING_COPY')
                out.append(part)
            meta['segments']=out;meta['eventsSha256']=out[0]['sha256']
            z.writestr('metadata.json',json.dumps(meta,ensure_ascii=False,indent=2))
        return memory.getvalue()

    def close(self):
        self.record('run_stop',{'at':timestamp()},origin='local')
        with self.guard:self.closed=True
        self.stop.set();self.worker.join(timeout=8)
        with self.disk:
            if self.worker.is_alive():
                with self.guard:self.error='录制队列未在退出前写完；录制不完整'
            else:
                if not self.file.closed:
                    self.file.flush();self.file.close()
        meta=self.status()
        if not self.worker.is_alive():
            parts=[dict(x) for x in self._sealed]+[self._current_metadata()]
            meta['segments']=parts;meta['eventsSha256']=parts[0]['sha256']
            meta['recordingSchema']='MULTI_SEGMENT_V1'
        (self.folder/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
