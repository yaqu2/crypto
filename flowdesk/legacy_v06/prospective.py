"""Forward-only, immutable-protocol quote-crossing *shadow* experiments.

NO real orders or simulated broker fills. Receipt-time Moomoo book snapshots are
not exchange-timestamped consolidated NBBO. Outcomes are sensitivity studies.
No historical backfill; no parameter fitting, optimization, or strategy approvals.
"""
from __future__ import annotations
import hashlib, json, math, os, queue, threading
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, deque
from feed_guard import clock_state

RULES=(tuple(dict(id=f'{style}-L{level}-H{horizon}',style=style,feature='book_imbalance',level=level,horizon=horizon,threshold=0.35)
            for style in ('follow','revert') for level in (1,5) for horizon in (60,300)) +
       tuple(dict(id=f'{style}-OFI1-H{horizon}',style=style,feature='ofi_proxy',level=1,horizon=horizon,threshold=0.30)
            for style in ('follow','revert') for horizon in (60,300)))
COSTS=(8.0,20.0,40.0)    # round-trip EXTRA bps beyond crossed spread, unmeasured
DELAY_SEC=2.0
MAX_WAIT_SEC=15.0
MAX_SPREAD_BPS=30.0
COOLDOWN_SEC=10.0


def iso(t):return t.astimezone(timezone.utc).isoformat()

def moment(value):
    if isinstance(value,datetime):d=value
    else:
        try:d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        except (ValueError,TypeError):return None
    return d.astimezone(timezone.utc) if d.tzinfo else None


def sha_bytes(blob):return hashlib.sha256(blob).hexdigest()


def canonical(d):return json.dumps(d,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


class ChainJournal:
    """Bounded asynchronous append-only journal. On failure, evidence gate latches closed."""
    def __init__(self,root,symbols,raw_run_id=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=False)
        self.path=self.root/'shadow_events.jsonl'
        self.manifest=self.root/'frozen_protocol.json'
        src=Path(__file__)
        spec={'version':1,'createdAt':iso(datetime.now(timezone.utc)),'symbols':sorted(set(symbols)),
              'rules':list(RULES),'entryDelaySeconds':DELAY_SEC,'maxQuoteWaitSeconds':MAX_WAIT_SEC,
              'maxSpreadBps':MAX_SPREAD_BPS,'extraRoundtripCostsBps':COSTS,
              'sourceSha256':sha_bytes(src.read_bytes()),'sourceTime':'LOCAL_CALLBACK_RECEIPT_NOT_EXCHANGE_VERIFIED',
              'marketScope':'SUBSCRIBED_MOOMOO_BOOK_ONLY','rawRecorderRunId':raw_run_id, 'execution':'COUNTERFACTUAL_CROSSED_QUOTES_NOT_BROKER_FILLS',
              'liveOrdersEnabled':False,'certifiedAlpha':False}
        self.manifest.write_bytes(canonical(spec)+b'\n')
        (self.root/'protocol_seal.sha256').write_text(sha_bytes(self.manifest.read_bytes())+'\n')
        self.q=queue.Queue(maxsize=4096);self.lock=threading.Lock();self.stop=threading.Event()
        self.error=None;self.written=0;self.dropped=0
        self.worker=threading.Thread(target=self._writer,name='forward-journal',daemon=True);self.worker.start()

    def record(self,event):
        with self.lock:
            if self.error:return False
            try:self.q.put_nowait(dict(event));return True
            except queue.Full:
                self.error='SHADOW_JOURNAL_QUEUE_OVERFLOW';self.dropped+=1;return False

    def _writer(self):
        previous='0'*64;seq=0
        try:
            with self.path.open('xb') as f:
                while not self.stop.is_set() or not self.q.empty():
                    try:obj=self.q.get(timeout=.1)
                    except queue.Empty:continue
                    try:
                        seq+=1
                        row={'seq':seq,'previousHash':previous,'event':obj}
                        digest=sha_bytes(canonical(row));row['hash']=digest
                        f.write(canonical(row)+b'\n');f.flush();os.fsync(f.fileno())
                        previous=digest
                        with self.lock:self.written=seq
                    except Exception as e:
                        with self.lock:self.error='SHADOW_JOURNAL_WRITE_FAILURE: '+str(e)[:120]
                        break
                    finally:self.q.task_done()
        except Exception as e:
            with self.lock:self.error='SHADOW_JOURNAL_INIT_FAILURE: '+str(e)[:120]

    def status(self):
        with self.lock:
            return {'path':str(self.path),'written':self.written,'queued':self.q.qsize(),
                    'dropped':self.dropped,'error':self.error,'usable':self.error is None}

    def close(self):
        self.stop.set();self.worker.join(timeout=10)
        if self.worker.is_alive():
            with self.lock:self.error='SHADOW_JOURNAL_DID_NOT_FLUSH'


def verify_chain(root):
    root=Path(root);manifest=root/'frozen_protocol.json';seal=root/'protocol_seal.sha256'
    if not (manifest.exists() and seal.exists()) or sha_bytes(manifest.read_bytes())!=seal.read_text().strip():
        raise ValueError('PROTOCOL_SEAL_MISMATCH')
    plan=json.loads(manifest.read_text());prev='0'*64;count=0;events=[]
    with (root/'shadow_events.jsonl').open('rb') as stream:
        for raw in stream:
            row=json.loads(raw)
            if row['seq']!=count+1 or row['previousHash']!=prev:raise ValueError('BROKEN_HASH_CHAIN')
            unsigned={k:row[k] for k in ('seq','previousHash','event')}
            if sha_bytes(canonical(unsigned))!=row['hash']:raise ValueError('TAMPERED_EVENT')
            prev=row['hash'];count+=1;events.append(row['event'])
    return plan,events


def extract_book(b):
    try:
        bids=b.get('bids') or [];asks=b.get('asks') or []
        if len(bids)<5 or len(asks)<5:return None # real L5 required; no pretend depth
        bp=float(bids[0]['price']);ap=float(asks[0]['price'])
        sb=[float(x['size']) for x in bids[:5]];sa=[float(x['size']) for x in asks[:5]]
        if not 0<bp<ap or any(not math.isfinite(x) or x<0 for x in sb+sa):return None
        if any(not math.isfinite(x) or x<=0 for x in (bp,ap)) or sb[0]<=0 or sa[0]<=0:return None
        # prevent duplicate, unsorted, obviously fabricated books
        if any(float(bids[i]['price'])<=float(bids[i+1]['price']) for i in range(4)):return None
        if any(float(asks[i]['price'])>=float(asks[i+1]['price']) for i in range(4)):return None
        spread=(ap-bp)*20000/(ap+bp)
        if spread>MAX_SPREAD_BPS:return None
        im={}
        for level in (1,5):
            vol_b=sum(sb[:level]);vol_a=sum(sa[:level]);im[level]=(vol_b-vol_a)/(vol_b+vol_a) if vol_a+vol_b>0 else None
        return {'bid':bp,'ask':ap,'bidSize':sb[0],'askSize':sa[0],
                'spread':spread,'imbalance':im}
    except (ValueError,TypeError,IndexError,KeyError,OverflowError):return None


class ForwardStudy:
    def __init__(self,folder,symbols,raw_run_id=None):
        self.symbols=tuple(dict.fromkeys(symbols));self.journal=ChainJournal(folder,self.symbols,raw_run_id)
        self.positions={};self.last_seen={};self.last_session={};self.last_books={};self.counts=defaultdict(int)
        self.results=defaultdict(list);self.recent=deque(maxlen=12)
        self.closed=False;self.disabled=False

    def _record(self,ev):
        if self.disabled:return
        if not self.journal.record(ev):self.disabled=True

    def _clear(self,reason,at,code=None):
        for key,p in list(self.positions.items()):
            if code is not None and key[0]!=code:continue
            if p.get('entry') is not None:self.counts['censored']+=1
            else:self.counts['cancelled']+=1
            self._record({'type':'CENSORED' if p.get('entry') else 'CANCELLED',
                          'id':p['id'],'code':key[0],'rule':key[1],'at':iso(at),
                          'reason':reason,'signalAt':p['signalAt']})
            del self.positions[key]

    def reset(self,reason,at=None):
        at=moment(at) or datetime.now(timezone.utc)
        self._clear(reason,at)
        self.last_seen.clear();self.last_session.clear();self.last_books.clear()

    def on_book(self,code,book,at,allowed=False,reason='DATA_GATE',origin='push'):
        """Only events with measured callback receipt time may advance simulation.
        allowed must come from independent global quote and local recording gates.
        """
        if self.closed or self.disabled or code not in self.symbols:return
        t=moment(at)
        if t is None:return
        seconds=t.timestamp()
        prev=self.last_seen.get(code)
        if prev is not None and seconds<=prev:
            self.counts['outOfOrder']+=1;return
        self.last_seen[code]=seconds
        session=t.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()
        if code in self.last_session and self.last_session[code]!=session:
            self._clear('DATE_ROLLOVER',t,code);self.last_books.pop(code,None)
        self.last_session[code]=session
        if origin!='push' or not allowed or not clock_state(t)['regular'] or self.journal.error:
            self._clear(reason if allowed is False else 'NOT_VALID_REGULAR_LIVE_PUSH',t,code)
            self.last_books.pop(code,None);self.counts['gateRejected']+=1;return
        b=extract_book(book)
        if b is None:
            self._clear('INVALID_BOOK_OR_SPREAD',t,code)
            self.last_books.pop(code,None);self.counts['bookRejected']+=1;return
        # Causal OFI proxy from consecutive, *already received* L1 books only.
        # A change is not a certified executed trade / individual order cancel.
        ofi=None
        previous=self.last_books.get(code)
        if previous and 0 < seconds-previous[0] <= 5:
            old=previous[1]
            bflow=(b['bidSize'] if b['bid']>old['bid'] else
                   b['bidSize']-old['bidSize'] if b['bid']==old['bid'] else -old['bidSize'])
            aflow=(-old['askSize'] if b['ask']>old['ask'] else
                   old['askSize']-b['askSize'] if b['ask']==old['ask'] else b['askSize'])
            scale=max(1,(b['bidSize']+b['askSize']+old['bidSize']+old['askSize'])/2)
            ofi=max(-1.,min(1.,(bflow-aflow)/scale))
        self.last_books[code]=(seconds,b)
        for rule in RULES:
            key=(code,rule['id']);p=self.positions.get(key)
            if p:
                elapsed=seconds-p['signalSeconds']
                if p['entry'] is None:
                    if elapsed>MAX_WAIT_SEC:
                        self._record({'type':'EXPIRED','id':p['id'],'code':code,'rule':rule['id'],'at':iso(t),
                                      'reason':'NO_FUTURE_BOOK_WITHIN_ENTRY_WINDOW'})
                        self.counts['expired']+=1;del self.positions[key]
                    elif elapsed>=DELAY_SEC:
                        price=b['ask'] if p['direction']==1 else b['bid']
                        p['entry']={'at':iso(t),'seconds':seconds,'price':price,'bid':b['bid'],'ask':b['ask']}
                        self.counts['paperEntries']+=1
                        self._record({'type':'PAPER_ENTRY','id':p['id'],'code':code,'rule':rule['id'],
                                      'at':iso(t),'price':price,'bid':b['bid'],'ask':b['ask'],'delaySeconds':elapsed})
                else:
                    hold=seconds-p['entry']['seconds']
                    if hold>rule['horizon']+MAX_WAIT_SEC:
                        self.counts['censored']+=1
                        self._record({'type':'CENSORED','id':p['id'],'code':code,'rule':rule['id'],
                                      'at':iso(t),'reason':'NO_EXIT_BOOK_IN_TOLERANCE'})
                        del self.positions[key]
                    elif hold>=rule['horizon']:
                        entry=p['entry']['price'];exitprice=b['bid'] if p['direction']==1 else b['ask']
                        gross=(exitprice/entry-1)*10000*p['direction']
                        outcome={'type':'PAPER_EXIT','id':p['id'],'code':code,'rule':rule['id'],
                                 'signalAt':p['signalAt'],'entryAt':p['entry']['at'],'exitAt':iso(t),
                                 'direction':p['direction'],'entryPrice':entry,'exitPrice':exitprice,'exitBid':b['bid'],'exitAsk':b['ask'],
                                 'holdSeconds':hold,'quotedCrossingBps':round(gross,6),
                                 'extraCostsBps':list(COSTS),
                                 'netQuoteModelBps':{str(int(cost)):round(gross-cost,6) for cost in COSTS},
                                 'brokerFillVerified':False,'shortBorrowModelled':False,'sourceTimestampVerified':False}
                        self._record(outcome);self.counts['paperExits']+=1
                        self.results[rule['id']].append(outcome['netQuoteModelBps']['20'])
                        self.recent.appendleft(outcome)
                        del self.positions[key]
                continue
            feature=ofi if rule['feature']=='ofi_proxy' else b['imbalance'][rule['level']]
            if feature is None or abs(feature)<rule['threshold']:continue
            sign=1 if feature>0 else -1
            direction=sign if rule['style']=='follow' else -sign
            ident=f'{code}-{rule["id"]}-{t.timestamp_ns() if hasattr(t,"timestamp_ns") else int(seconds*1000000)}'
            p={'id':ident,'signalAt':iso(t),'signalSeconds':seconds,
               'direction':direction,'entry':None}
            self.positions[key]=p;self.counts['signals']+=1
            self._record({'type':'SIGNAL','id':ident,'code':code,'rule':rule['id'],'at':iso(t),
                          'feature':rule['feature'],'featureValue':round(feature,6),
                          'spreadBps':round(b['spread'],6),'bid':b['bid'],'ask':b['ask'],
                          'direction':direction,'assumption':'shadow crossed quotes; local receipt clock'})

    def summary(self):
        stats={}
        for name,values in self.results.items():
            stats[name]={'paperExits':len(values),'meanNet20Bps':round(sum(values)/len(values),4) if values else None}
        return {'mode':'FORWARD_ONLY_COUNTERFACTUAL_RESEARCH','status':'NO_GO_LIVE_ALPHA',
                'hypotheses':len(RULES),'openOrPending':len(self.positions),
                'counts':dict(self.counts),'perRule':stats,'recent':list(self.recent),
                'journal':self.journal.status(), 'disabled':self.disabled or bool(self.journal.error),
                'liveOrdersEnabled':False,'fillVerified':False,'profitCertified':False,
                'explanation':'真实未来盘口报价跨价差模型；非订单成交、没有借券/市场冲击/真实排队证据。'}

    def close(self):
        if self.closed:return
        self.reset('PROCESS_SHUTDOWN')
        self._record({'type':'RUN_END','at':iso(datetime.now(timezone.utc))})
        self.closed=True;self.journal.close()
