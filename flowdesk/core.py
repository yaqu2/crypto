"""Data normalization only. No broker trading APIs."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math

ET = ZoneInfo('America/New_York')
def utcnow():
    return datetime.now(timezone.utc).isoformat()

def number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None

def quote_time(date,time):
    try:
        text=str(time)
        if len(text)>2 and text[2]==':': text=str(date)+'T'+text
        d=datetime.fromisoformat(text)
        if d.tzinfo is None: d=d.replace(tzinfo=ET)
        return d.astimezone(timezone.utc).isoformat()
    except (ValueError,TypeError):
        return None

def connection_status(responded, logged, subscriptions, expected=4):
    if not responded: return 'OpenD连接异常'
    if logged is not None and not logged: return 'OpenD行情未登录'
    if any(v!='成功' for v in subscriptions.values()): return 'OpenD已响应 · 存在订阅失败'
    if len(subscriptions)<expected: return 'OpenD已响应 · 等待订阅完成'
    return 'OpenD已连接 · 全部订阅成功'

def clean_quote(row):
    return {'last':number(row.get('last_price')), 'pre':number(row.get('pre_price')), 'after':number(row.get('after_price')),
            'overnight':number(row.get('overnight_price')), 'volume':number(row.get('volume')),
            'sourceDate':str(row.get('data_date','')), 'sourceTime':str(row.get('data_time','')),
            'sourceAsOf':quote_time(row.get('data_date'),row.get('data_time')), 'receivedAt':utcnow()}

def levels(rows):
    output=[]
    for row in rows or []:
        if not isinstance(row,(tuple,list)) or len(row)<2: continue
        p,v=number(row[0]),number(row[1])
        if p is None or v is None or p<=0 or v<0: continue
        output.append({'price':p,'size':v})
    return output[:60]

def clean_book(data):
    bids=sorted(levels(data.get('Bid')),key=lambda x:x['price'],reverse=True)
    asks=sorted(levels(data.get('Ask')),key=lambda x:x['price'])
    b=sum(x['size'] for x in bids[:5]); a=sum(x['size'] for x in asks[:5])
    spread=None
    if bids and asks and asks[0]['price']>=bids[0]['price']:
        spread=(asks[0]['price']-bids[0]['price'])/((asks[0]['price']+bids[0]['price'])/2)*10000
    return {'bids':bids,'asks':asks,'type':str(data.get('order_book_type','未提供')),
            'sourceBidTime':str(data.get('svr_recv_time_bid','')),'sourceAskTime':str(data.get('svr_recv_time_ask','')),
            'receivedAt':utcnow(),'imbalance':(b-a)/(b+a) if b+a else None,'spreadBps':spread}

def snapshot(rows):
    # A one-off export is replay, never a fresh signal. Raw book timestamps are preserved
    # but not reinterpreted as exchange timestamps when timezone provenance is unknown.
    out={'source':'Moomoo OpenD / coverage and timestamps require verification','mode':'replay','asOf':utcnow(),'symbols':[]}
    for code,r in rows.items():
        q=r.get('quote') or {}; b=r.get('book') or {}; bids=b.get('bids') or [];asks=b.get('asks') or []
        x={'ticker':code.split('.',1)[-1],'candles':[],'timeframe':'not-collected'}
        if bids and asks:
            x['quote']={'bid':bids[0]['price'],'ask':asks[0]['price'],'asOf':None}
            x['depth']={'venue':'OpenD '+b.get('type','unknown')+'; coverage not independently verified','asOf':None,'bids':bids,'asks':asks}
        x['diagnostics']={'quote':q,'bookReceivedAt':b.get('receivedAt'),'sourceBidTime':b.get('sourceBidTime'),'sourceAskTime':b.get('sourceAskTime')}
        out['symbols'].append(x)
    return out
