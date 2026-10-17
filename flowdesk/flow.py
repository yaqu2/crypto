"""Read-only stream analytics; no execution signals or broker operations."""
from collections import Counter, deque
from datetime import datetime
from core import number, quote_time, utcnow, ET

def session_key(asof):
    d=datetime.fromisoformat(asof).astimezone(ET)
    minute=d.hour*60+d.minute
    name='盘前' if 240<=minute<570 else '常规' if 570<=minute<960 else '盘后' if 960<=minute<1200 else '夜盘'
    return d.date().isoformat()+' '+name

class Tape:
    # Only these ordinary/extended-session conditions enter the displayed CVD.
    ACCEPT={'AUTO_MATCH','ODD_LOT','T','EXTENDED_TRADING_HOURS'}
    def __init__(self):
        self.reset('本次程序启动后起算')

    def reset(self,reason):
        self.seen=set(); self.recent=deque(maxlen=30); self.points={}
        self.buy=self.sell=self.neutral=0; self.count=0
        self.first=None; self.last=None; self.session=None
        self.reason=reason; self.excluded=Counter(); self.received=None

    def add(self,row,received_at=None):
        # Historical replay must inject the ORIGINAL recorded receive timestamp.
        now=received_at or utcnow(); self.received=now
        origin=str(row.get('push_data_type','N/A'))
        if origin=='BYDISCONN':
            self.reset('接口报告断线补推；重新起算，不补造缺失成交')
            self.received=now; self.excluded['断线补推']+=1; return
        if origin!='REALTIME': self.excluded['缓存或未知来源']+=1; return
        raw=str(row.get('time',''))
        # Some documentation uses a colon before milliseconds.
        if len(raw)>19 and raw[19]==':': raw=raw[:19]+'.'+raw[20:]
        asof=quote_time(None,raw)
        price=number(row.get('price'));volume=number(row.get('volume'))
        seq=row.get('sequence')
        if not asof or price is None or price<=0 or volume is None or volume<=0 or seq is None:
            self.excluded['字段不完整']+=1; return
        seq=str(seq)
        if seq in ['nan','None','0','0.0','']: self.excluded['无有效序号']+=1; return
        # Replayed or old events cannot establish a fresh live segment.
        age=(datetime.fromisoformat(now)-datetime.fromisoformat(asof)).total_seconds()
        if age>30 or age < -5: self.excluded['事件过旧或时钟异常']+=1; return
        session=session_key(asof)
        if self.session and session!=self.session:
            if self.last and asof<self.last: self.excluded['旧时段迟到成交']+=1; return
            self.reset('交易时段改变；CVD重新起算')
        if len(self.seen)>=100000: self.reset('达到10万笔去重上限；新段重新起算')
        self.session=session; self.received=now
        key=(raw[:10],seq)
        if key in self.seen: self.excluded['重复推送']+=1; return
        self.seen.add(key)
        kind=str(row.get('type','UNKNOWN'));direction=str(row.get('ticker_direction','NEUTRAL'))
        accepted=kind in self.ACCEPT
        tick={'time':raw,'asOf':asof,'price':price,'volume':volume,'direction':direction,'type':kind,'included':accepted,'sequence':seq}
        self.recent.appendleft(tick)
        if not accepted: self.excluded['非白名单成交类型']+=1; return
        if self.first is None or asof<self.first:self.first=asof
        if self.last is None or asof>self.last:self.last=asof
        delta=volume if direction=='BUY' else -volume if direction=='SELL' else 0
        if direction=='BUY':self.buy+=volume
        elif direction=='SELL':self.sell+=volume
        else:self.neutral+=volume
        self.count+=1
        minute=asof[:16];self.points[minute]=self.points.get(minute,0)+delta

    def export(self):
        total=self.buy+self.sell+self.neutral
        running=0;points=[]
        for minute,delta in sorted(self.points.items()):
            running+=delta;points.append({'time':minute+'Z','value':running})
        return {'cvd':self.buy-self.sell if self.count else None,'buyVolume':self.buy,'sellVolume':self.sell,
                'neutralVolume':self.neutral,'classifiedFraction':(self.buy+self.sell)/total if total else None,
                'count':self.count,'from':self.first,'asOf':self.last,'session':self.session,'receivedAt':self.received,
                'reason':self.reason,'excluded':dict(self.excluded),'points':points[-240:],'recent':list(self.recent)}

def merge_bars(existing,rows,seed=False):
    bars={b['time']:b for b in existing}
    for row in rows:
        if str(row.get('k_type','K_1M'))!='K_1M':continue
        raw=str(row.get('time_key',''));asof=quote_time(None,raw)
        vals={k:number(row.get(k)) for k in ['open','high','low','close','volume']}
        if not asof or any(v is None for v in vals.values()):continue
        if min(vals[k] for k in ['open','high','low','close'])<=0 or vals['volume']<0:continue
        if not (vals['low']<=min(vals['open'],vals['close'])<=max(vals['open'],vals['close'])<=vals['high']):continue
        if seed and asof in bars:continue
        bars[asof]={'time':asof,'sourceTime':raw,**vals}
    return sorted(bars.values(),key=lambda b:b['time'])[-240:]
