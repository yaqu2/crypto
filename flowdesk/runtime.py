"""Bounded local stream pipeline and coalesced, immutable JSON publication."""
import json, queue, threading, time
from pathlib import Path
from universe_agents import review as review_universe
from collections import deque
from datetime import datetime, timezone, timedelta
from feed_guard import evaluate
from core import clean_quote, clean_book, utcnow, snapshot
from flow import Tape, merge_bars
from intelligence import Intelligence
from multiagent import evaluate_all
from prospective import ForwardStudy
from feed_guard import as_time

DEFAULT_CONFIG = {
 'symbols':['US.SPY','US.QQQ','US.AAPL','US.MSFT','US.NVDA','US.AMD','US.AMZN','US.META','US.GOOGL','US.TSLA','US.AAOI','US.AAOX','US.LITE','US.COHR'],
 'deepSymbols':['US.AAOI','US.AAOX','US.NVDA','US.SPY'],
 'benchmark':'US.SPY',
 'peers':{'US.AAOI':['US.LITE','US.COHR'],'US.LITE':['US.AAOI','US.COHR'],'US.COHR':['US.AAOI','US.LITE'],'US.NVDA':['US.AMD'],'US.AMD':['US.NVDA']}}

def validate_config(c):
    import re
    symbols=c.get('symbols',[])
    if not isinstance(symbols,list) or not 1<=len(symbols)<=60 or len(set(symbols))!=len(symbols):raise ValueError('symbols requires 1–60 unique codes')
    if any(not isinstance(s,str) or not re.fullmatch(r'US\.[A-Z0-9.-]{1,15}',s) for s in symbols):raise ValueError('Invalid US symbol')
    if c.get('benchmark') not in symbols:raise ValueError('Benchmark must be subscribed')
    if not isinstance(c.get('deepSymbols'),list) or any(s not in symbols for s in c['deepSymbols']):raise ValueError('deepSymbols must be subscribed')
    if not isinstance(c.get('peers',{}),dict):raise ValueError('peers must be an object')
    for s, peers in c.get('peers',{}).items():
        if s not in symbols or not isinstance(peers,list) or any(p not in symbols for p in peers):raise ValueError('Peer symbols must be subscribed')
    return c

def stats(values):
    v=sorted(values)
    return {k:(round(v[min(len(v)-1,int((len(v)-1)*p))],3) if v else None) for k,p in [('p50',.5),('p95',.95),('max',1)]}

class Runtime:
    def __init__(self,config,recorder=None,queue_size=4096,forward_folder=None,trial_binding=None):
        self.config=validate_config(config);self.recorder=recorder
        self.trial_binding=trial_binding or {}
        self.lock=threading.RLock();self.stop=threading.Event();self.queue=queue.Queue(queue_size)
        self.symbols=config['symbols'];self.tapes={s:Tape() for s in self.symbols}
        self.engine=Intelligence(self.symbols,config['benchmark'],config.get('peers'))
        self.forward=ForwardStudy(forward_folder,config['deepSymbols'],recorder.run_id if recorder is not None else None) if forward_folder is not None else None
        self.state={'version':'2.1.0-robustness-red-team','mode':'live','startedAt':utcnow(),'connection':'正在连接本机OpenD','stage':'准备连接','heartbeatAt':None,'subscriptions':{},'errors':[],
                    'watchlist':config,'symbols':{s:{} for s in self.symbols}}
        self.counts={s:dict(quote=0,book=0,ticker=0,kline=0) for s in self.symbols}
        self.callbacks=0;self.dropped=0;self.processed=0;self.generation=0;self.last_callback=None
        self.feed_session_start=None;self.good_quotes={};self.good_books={};self.connection_verified=False;self.reconnect_history=deque(maxlen=20)
        self.ingest=deque(maxlen=2048);self.delay=deque(maxlen=2048)
        self.revision=0;self.published=b'{}';self.snapshot=b'{}';self.threads=[]
        self.market_last_check=0;self.market_audit=None;self.market_raw_verified=None
        self._last_forward_good=False
        self.publish()
    def record(self,kind,data,origin='local',**kwargs):
        if self.recorder:self.recorder.record(kind,data,origin,**kwargs)
    def error(self,message):
        with self.lock:self.state['errors']=(self.state['errors']+[{'at':utcnow(),'message':str(message)[:400]}])[-12:]
        self.record('error',{'message':str(message)[:400]})
    def reset(self,reason):
        with self.lock:
            self.generation+=1;self.engine.reset(reason);self.good_quotes.clear()
            self.feed_session_start=None;self.connection_verified=False;self.good_books.clear()
            for tape in self.tapes.values():tape.reset(reason)
            if self.forward:self.forward.reset(reason)
            self.record('stream_reset',{'reason':reason,'generation':self.generation})
    def enqueue(self,kind,data,origin='push'):
        if self.stop.is_set():return
        received=utcnow();mono=time.monotonic_ns()
        with self.lock:
            self.callbacks+=1;self.last_callback=received
            event=(kind,data,origin,received,mono,self.generation)
            try:self.queue.put_nowait(event)
            except queue.Full:
                self.dropped+=1
                # Reset once per overflow burst. Prior queued generations cannot enter inference.
                if not getattr(self,'overflow',False):self.reset('本机接收队列溢出；数据存在缺口');self.overflow=True
    def process(self,event):
        kind,data,origin,received,mono,generation=event
        start=time.monotonic_ns()
        rows=data.to_dict('records') if hasattr(data,'to_dict') else data
        with self.lock:
            self.record(kind,rows,origin if generation==self.generation else 'discarded',callback_received_at=received,callback_monotonic_ns=mono)
            self.delay.append(max(0,(start-mono)/1e6))
            if generation!=self.generation:return
            if kind=='book':rows=[rows]
            for row in rows:
                code=row.get('code')
                if code not in self.tapes:continue
                target=self.state['symbols'][code];self.counts[code][kind]+=1
                if kind in ('quote','book'):
                    cleaned=clean_quote(row) if kind=='quote' else clean_book(row)
                    cleaned['receivedAt']=received
                    if kind=='quote':cleaned['stockState']=row.get('stock_state')
                    old=target.get(kind)
                    accepted=getattr(self.engine,'ingest_'+kind)(code,cleaned,origin=origin)
                    if kind=='quote':
                        # Only source-time validated push quotes may refresh research state.
                        # Old, future, unordered or seed quotes may remain diagnostics but
                        # cannot replace the last accepted live quote or refresh its clock.
                        if origin=='push' and accepted:
                            target[kind]=cleaned
                            self.good_quotes[code]=cleaned['sourceAsOf']
                        elif origin=='seed' and old is None:
                            target[kind]=cleaned
                    else:
                        # Preserve a valid prior book when an invalid book arrives;
                        # the data gate and forward engine still see the bad callback.
                        if accepted and origin=='push':
                            target[kind]=cleaned
                            self.good_books[code]=cleaned.get('receivedAt')
                        elif origin=='seed' and old is None:
                            target[kind]=cleaned
                    if kind=='book' and self.forward is not None and code in self.config['deepSymbols']:
                        # A *fresh, accepted push quote on this same stock* is required.
                        # A recently polled seed quote is never evidence for entry.
                        now=as_time(received)
                        own=as_time(self.good_quotes.get(code))
                        anchor=as_time(self.good_quotes.get(self.config['benchmark']))
                        per_stock_fresh=bool(now and own and -2 <= (now-own).total_seconds() <= 5)
                        anchor_fresh=bool(now and anchor and -2 <= (now-anchor).total_seconds() <= 5)
                        allowed=(accepted and origin=='push' and per_stock_fresh and anchor_fresh and
                                 self.feed_quality()['status']=='RECENT_RESEARCH_ONLY' and
                                 self.recorder is not None and
                                 not self.forward.journal.error)
                        self.forward.on_book(code,cleaned,received,allowed=allowed,
                                             reason='NO_FRESH_PUSH_QUOTE_OR_SOURCE_CLOCK',origin=origin)
                elif kind=='ticker':
                    if origin=='push':self.tapes[code].add(row,received_at=received)
                elif kind=='kline':
                    target['candles']=merge_bars(target.get('candles',[]),[row],origin=='seed');target['candlesReceivedAt']=received
            self.processed+=1;self.ingest.append((time.monotonic_ns()-start)/1e6)
    def _worker(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:event=self.queue.get(timeout=.1)
            except queue.Empty:continue
            try:self.process(event)
            except Exception as exc:self.error('数据处理异常：'+str(exc));self.reset('数据解析失败；证据重置')
            finally:
                self.queue.task_done()
                with self.lock:
                    if self.queue.qsize()<self.queue.maxsize//2:self.overflow=False
    def publish(self):
        with self.lock:
            for code in self.symbols:
                self.state['symbols'][code]['tape']=self.tapes[code].export()
                self.state['symbols'][code]['receivedCounts']=dict(self.counts[code])
            self.state['recording']=self.recorder.status() if self.recorder else {'error':'录制未启用'}
            self.state['intelligence']=self.engine.export()
            self.state['feedQuality']=self.feed_quality()
            currently_good=self.state['feedQuality']['status']=='RECENT_RESEARCH_ONLY'
            if self.forward and self._last_forward_good and not currently_good:
                self.forward.reset('PUBLISHER_FEED_GATE_CLOSED')
            self._last_forward_good=currently_good
            # Independent fail-closed reviewers see the same published evidence.
            self.state['multiAgent']=evaluate_all(self.state['symbols'], self.state['feedQuality'], self.state['intelligence'], self.market_raw_verified)
            self.state['forwardResearch']=self.forward.summary() if self.forward else {
                'status':'DISABLED_NO_LIVE_JOURNAL','liveOrdersEnabled':False,'profitCertified':False}
            # A frozen trial cannot show rule-level PnL/recent winning exits during
            # collection; prospective outcomes remain journaled but blinded in UI/SSE.
            if self.trial_binding.get('trialId'):
                previous=self.state['forwardResearch']
                self.state['forwardResearch']={
                    'status':'BLINDED_FROZEN_FUTURE_TEST',
                    'hypotheses':previous.get('hypotheses'),
                    'counts':{k:v for k,v in previous.get('counts',{}).items()
                              if k in ('signals','gateRejected','bookRejected','expired','liquidityRejected')},
                    'journal':previous.get('journal'),
                    'recent':[],'perRule':{},'disabled':previous.get('disabled',False),
                    'explanation':'事前封存试验进行中：模型收益及逐笔出场结果不向实时看板公布。到期后一次性解封；仍非实盘收益。',
                    'liveOrdersEnabled':False,'profitCertified':False}
            self.state['marketCoverageAudit']=self.market_audit
            self.revision+=1
            self.state['performance']={'callbacks':self.callbacks,'ingestMs':stats(self.ingest),'queueDelayMs':stats(self.delay),'queuePending':self.queue.qsize(),'queueDropped':self.dropped,'processedEvents':self.processed,'lastCallbackAt':self.last_callback,'pushRevision':self.revision,'deliveryMode':'SSE latest state coalesced 250ms','serverAt':utcnow(),'uiTargetHz':4,'measurement':'Local queue and processing only; not exchange/network latency'}
            self.published=json.dumps(self.state,ensure_ascii=False,allow_nan=False).encode()
            self.snapshot=json.dumps(snapshot(self.state['symbols']),ensure_ascii=False,allow_nan=False).encode()
    def _market_refresh(self):
        # Once every 30s, verify both snapshots' bytes against the scanner manifest.
        # Do not publish unverified or stale full-market evidence to the agents.
        root=Path(__file__).resolve().parent/'output'/'market'
        try:
            audit=review_universe(root)
            report=json.loads((root/'latest_report.json').read_text())
            verified=report if audit['status'] in ('RESEARCH_WATCHLIST_ONLY','LISTED_RESEARCH_WATCHLIST_ONLY') else None
        except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
            audit={'status':'NO_GO_COVERAGE_OR_INTEGRITY','reason':str(exc)[:240],
                   'tradeEnabled':False,'predictionValidated':False}
            verified=None
        with self.lock:
            self.market_audit={k:v for k,v in audit.items() if k not in ('candidates','uncertifiedPreview')}
            self.market_raw_verified=verified

    def _publisher(self):
        while not self.stop.wait(.25):
            if time.monotonic()-self.market_last_check>=30:
                self.market_last_check=time.monotonic()
                self._market_refresh()
            try:
                with self.lock:
                    if self.forward:
                        self.forward.monitor(datetime.now(timezone.utc),self.good_quotes,self.config['benchmark'])
                self.publish()
            except Exception as exc:self.error('发布状态或持仓连续性监控失败：'+str(exc))
    def feed_quality(self):
        result=evaluate(self.config,self.state['subscriptions'],self.good_quotes,
                        self.feed_session_start, queue_dropped=self.dropped,
                        recording=self.recorder.status() if self.recorder else None,
                        connection_ok=self.connection_verified)
        now=datetime.now(timezone.utc)
        depth={}
        for code in self.config['deepSymbols']:
            dt=datetime.fromisoformat(self.good_books[code]) if code in self.good_books else None
            age=max(0,(now-dt).total_seconds()) if dt is not None else None
            depth[code]={'callbackFresh':age is not None and age<=60,
                         'ageSeconds':round(age,1) if age is not None else None,
                         'exchangeTimestampVerified':False}
        result['deepBookFreshness']=depth
        result['fullOrderFlowReady']=False # quote/book snapshots != complete exchange event stream
        return result

    def may_reconnect(self):
        # At most three data-staleness reconnects per hour, separated by >=180 seconds.
        now=datetime.now(timezone.utc)
        while self.reconnect_history and (now-self.reconnect_history[0]).total_seconds()>3600:
            self.reconnect_history.popleft()
        if len(self.reconnect_history)>=3:return False
        if self.reconnect_history and (now-self.reconnect_history[-1]).total_seconds()<180:return False
        self.reconnect_history.append(now)
        return True

    def start(self):
        self.threads=[threading.Thread(target=f,daemon=True) for f in (self._worker,self._publisher)]
        for t in self.threads:t.start()
    def close(self):
        self.stop.set()
        for t in self.threads:t.join(timeout=3)
        if any(t.is_alive() for t in self.threads):self.error('退出前接收队列未排空')
        if self.forward:self.forward.close()
