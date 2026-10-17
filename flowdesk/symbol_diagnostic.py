"""Read-only, bounded symbol evidence extraction from the active FlowDesk state.

No OpenD calls, no order access, no generated trading recommendation.
"""
import re
from datetime import datetime,timezone

def _age(iso, now):
    try:
        t=datetime.fromisoformat(str(iso).replace('Z','+00:00'))
        return round((now-t.astimezone(timezone.utc)).total_seconds(),2) if t.tzinfo else None
    except (ValueError,TypeError,OverflowError,AttributeError):return None

def diagnose(state,code,now=None):
    if not isinstance(code,str) or not re.fullmatch(r'US\.[A-Z0-9.-]{1,15}',code):
        return {'status':'BAD_SYMBOL','tradeEnabled':False}
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('timezone-aware now required')
    now=now.astimezone(timezone.utc)
    instruments=state.get('symbols') or {}
    if code not in instruments:
        return {'status':'NO_GO_NOT_WATCHED','code':code,'tradeEnabled':False,
                'reason':'不在当前观察池；需先完成扫描、审核观察池，并在下次启动应用'}
    item=instruments.get(code) or {}
    q=item.get('quote') or {};b=item.get('book') or {};t=item.get('tape') or {}
    feed=state.get('feedQuality') or {}
    agent=((state.get('multiAgent') or {}).get('symbols') or {}).get(code) or {}
    checks=agent.get('agents') or []
    quote_age=_age(q.get('sourceAsOf'),now)
    book_age=_age(b.get('receivedAt'),now)
    tape_age=_age(t.get('asOf'),now)
    feed_ok=feed.get('status')=='RECENT_RESEARCH_ONLY'
    quote_ok=quote_age is not None and -2<=quote_age<=15
    book_ok=book_age is not None and -2<=book_age<=15 and b.get('spreadBps') is not None
    if not feed_ok:status='NO_GO_GLOBAL_FEED'
    elif not quote_ok:status='NO_GO_SYMBOL_SOURCE_QUOTE'
    elif not book_ok:status='NO_GO_SYMBOL_ORDER_BOOK'
    elif not agent or agent.get('status')!='OBSERVATION_ONLY':status='NO_GO_AGENT_EVIDENCE'
    else:status='OBSERVATION_ONLY_NOT_TRADE_PERMISSION'
    return {'schemaVersion':1,'mode':'FLOWDESK_LOCAL_READ_ONLY_DIAGNOSTIC',
            'code':code,'asOf':now.isoformat(),'status':status,
            'feedStatus':feed.get('status','UNKNOWN'),
            'modelHypothesis':agent.get('hypothesis') if status=='OBSERVATION_ONLY_NOT_TRADE_PERMISSION' else None,
            'agentStatus':agent.get('status'),'agentVetoes':agent.get('hardVetoes',[]),
            'agents':[{'name':x.get('agent'),'status':x.get('status'),'veto':x.get('veto'),'evidence':x.get('evidence')} for x in checks],
            'quote':{'sourceAsOf':q.get('sourceAsOf'),'sourceAgeSeconds':quote_age,
                     'sourceTimestampVerifiedFresh':quote_ok,'lastPrice':q.get('last'),'originNotIndependentlyVerified':True},
            'book':{'receivedAt':b.get('receivedAt'),'receivedAgeSeconds':book_age,
                    'bids':len(b.get('bids') or []),'asks':len(b.get('asks') or []),
                    'spreadBps':b.get('spreadBps'),'imbalance5':b.get('imbalance'),
                    'exchangeEventTimeVerified':False},
            'tape':{'lastAsOf':t.get('asOf'),'ageSeconds':tape_age,
                    'acceptedTradesInSegment':t.get('count',0),'classifiedFraction':t.get('classifiedFraction')},
            'subscriptionStatuses':{k:v for k,v in (state.get('subscriptions') or {}).items() if k.startswith(code+'/')},
            'tradeEnabled':False,'profitCertified':False,
            'explanation':'观察性模型证据，非经过样本外验证的买卖信号；没有券商成交校准或交易授权。'}
