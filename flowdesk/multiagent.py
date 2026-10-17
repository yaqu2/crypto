"""Independent deterministic, read-only research reviewers for the original FlowDesk.

These are explicitly rule-based agents, NOT independently hosted LLMs; no predictions
or execution capabilities. All conclusions are evaluated on observed facts only.
"""
from datetime import datetime, timezone
import math

NAMES = ('行情完整性审查','市场范围审查','盘口微观结构','成交方向审查','跨标的背景审查','统计反证审查','执行成本审查','组合风控审查')


def _time(value):
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (TypeError,ValueError,OverflowError):return None


def _num(value):
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except (ValueError,TypeError):return None


def _check(agent, status, evidence, veto=False, **fields):
    return {'agent':agent,'status':status,'evidence':str(evidence),'veto':bool(veto),**fields}


def _fresh(value, now, seconds):
    t=_time(value)
    return t is not None and -2 <= (now-t).total_seconds() <= seconds


def evaluate_symbol(code, state, feed, intel=None, market=None, now=None):
    """All reviewers get the same immutable-at-publication snapshot, no order actions."""
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('timezone-aware time required')
    obj=(state or {}).get(code) or {}
    i=(intel or {}).get(code) or {}
    q=i.get('quality') or {}; b=obj.get('book') or {}; tape=obj.get('tape') or {}
    agents=[]

    fq=(feed or {}).get('status','NO_GO_CONNECTION')
    quote_ok=fq=='RECENT_RESEARCH_ONLY' and bool(q.get('quoteFresh')) and _fresh(q.get('sourceAsOf'),now,15)
    agents.append(_check(NAMES[0], 'OBSERVED' if quote_ok else 'VETO',
                          f'全局门禁={fq}；个股报价源时间新鲜={quote_ok}', not quote_ok))

    market_age=(now-_time((market or {}).get('completedAt'))).total_seconds() if _time((market or {}).get('completedAt')) else None
    market_ok=(bool(market and (
                   (market.get('dataGate')=='PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY' and market.get('fullListedUniverse'))
                   or (market.get('dataGate')=='PASS_SNAPSHOT_QUALITY_RESEARCH_ONLY' and market.get('fullCurrentStockUniverse'))
                 )) and market_age is not None and -5<=market_age<=600)
    agents.append(_check(NAMES[1],'OBSERVED' if market_ok else 'LIMITED',
                         '当前上市股票范围快照已通过完整性审计；不含OTC和历史退市股票' if market_ok else '尚无近期可信的范围完整快照；观察池≠全市场',False,
                         fullMarketSnapshotVerified=market_ok))

    bids=b.get('bids') or [];asks=b.get('asks') or []
    bb=_num(bids[0].get('price')) if bids and isinstance(bids[0],dict) else None
    ba=_num(asks[0].get('price')) if asks and isinstance(asks[0],dict) else None
    spread=_num(b.get('spreadBps'))
    book_ok=bool(bb and ba and ba>=bb and _fresh(b.get('receivedAt'),now,15) and
                 spread is not None and spread>=0 and (feed or {}).get('deepBookFreshness',{}).get(code,{}).get('callbackFresh',False))
    imbalance=_num(b.get('imbalance')) if book_ok else None
    if imbalance is not None and not -1<=imbalance<=1:book_ok=False;imbalance=None
    mid=(bb+ba)/2 if book_ok else None
    micro=None
    if book_ok:
        bid_size=_num(bids[0].get('size'));ask_size=_num(asks[0].get('size'))
        if bid_size is not None and ask_size is not None and bid_size>=0 and ask_size>=0 and bid_size+ask_size>0:
            micro=(ba*bid_size+bb*ask_size)/(bid_size+ask_size)
    agents.append(_check(NAMES[2],'OBSERVED' if book_ok else 'LIMITED',
                         '盘口仅为可见快照；缺少逐订单新增/取消和完整交易所时间戳' if book_ok else '缺少有效且近期盘口',
                         False, imbalance5=imbalance, microprice=micro, midpoint=mid,spreadBps=spread if book_ok else None,
                         exchangeTimestampVerified=False))

    count=int(tape.get('count') or 0)
    classified=_num(tape.get('classifiedFraction'))
    cvd=_num(tape.get('cvd'))
    tape_ok=bool(count>=30 and classified is not None and classified>=.75 and
                 _fresh(tape.get('asOf'),now,15) and cvd is not None)
    total=(_num(tape.get('buyVolume')) or 0)+(_num(tape.get('sellVolume')) or 0)
    pressure=(((_num(tape.get('buyVolume')) or 0)-(_num(tape.get('sellVolume')) or 0))/total) if tape_ok and total else None
    agents.append(_check(NAMES[3],'OBSERVED' if tape_ok else 'LIMITED',
                         '数据源逐笔分类不等于全市场主动订单' if tape_ok else f'仅计入当前段有效实时成交；样本={count} 分类比例={classified}',
                         False,tapePressure=pressure,classifiedFraction=classified))

    benchmark=i.get('benchmark') or {}
    peer=i.get('peers') or {}
    cross_ok=bool(benchmark.get('available') and peer.get('available') and peer.get('count',0)>=2)
    agents.append(_check(NAMES[4],'OBSERVED' if cross_ok else 'LIMITED',
                         '对齐基准+至少两只同业的相同60秒窗口' if cross_ok else '缺少对齐基准或足够同业；不能确认市场传导',False,
                         benchmarkExcessPct=benchmark.get('excessPct') if cross_ok else None))

    # No automatic profitability stamp: review of out-of-sample executions must be external and independent.
    agents.append(_check(NAMES[5],'VETO','缺少独立跨日跨股票样本外、预注册试验及多重检验校正的净收益证据',True,
                         validatedAlpha=False))
    overhead=8.0 # configurable research assumption: fee+latency+slippage, NOT broker-measured
    required_bps=spread+overhead if book_ok else None
    cost_ok=book_ok and spread is not None and spread<=20
    agents.append(_check(NAMES[6],'OBSERVED' if cost_ok else 'LIMITED',
                         '仅计算跨价差及假设附加成本，不等于真实成交；缺少市场冲击和券商成交报告' if book_ok else '缺少近期有效可见买卖价差',False,
                         indicativeRoundtripHurdleBps=round(required_bps,4) if required_bps is not None else None,
                         assumedExtraRoundtripBps=overhead))
    agents.append(_check(NAMES[7],'VETO','无经过独立审计的风险预算、实时持仓、成交和尾部风险；禁止自动下单',True,
                         liveOrderPermission=False))
    if quote_ok and book_ok and tape_ok and cross_ok:
        agree = (imbalance is not None and pressure is not None and abs(imbalance)>=.35
                 and abs(pressure)>=.25 and imbalance*pressure>0)
        hypothesis = ('供需压力观察：同向偏多' if imbalance>0 else '供需压力观察：同向偏空') if agree else None
    else:hypothesis=None
    return {'code':code,'status':'OBSERVATION_ONLY' if quote_ok else 'NO_GO_DATA',
            'hypothesis':hypothesis,'qualifiedForProfitClaim':False,'tradeEnabled':False,
            'hardVetoes':[a['agent'] for a in agents if a['veto']],
            'agents':agents,'sourceScope':'Moomoo watchlist push + optional independent current-market snapshot; not consolidated MBO',
            'asOf':now.isoformat()}


def evaluate_all(symbol_states,feed,intelligence=None,market=None,now=None):
    now=now or datetime.now(timezone.utc)
    market_report=market if isinstance(market,dict) else None
    by_code=(intelligence or {}).get('symbols') or {}
    results={code:evaluate_symbol(code,symbol_states,feed,by_code,market_report,now)
             for code in symbol_states}
    hypothesis_count=sum(bool(x['hypothesis']) for x in results.values())
    return {'asOf':now.isoformat(),'mode':'DETERMINISTIC_MULTI_AGENT_RESEARCH_ONLY',
            'roles':list(NAMES),'evaluatedWatchlistCount':len(results),'observationHypotheses':hypothesis_count,
            'independentLLMAgentsRunning':False,'fullMarketMBOAvailable':False,
            'liveOrdersEnabled':False,'verifiedAlphaCount':0,'symbols':results,
            'notice':'规则式多角色独立审查；不是八个已部署的独立大模型，也不是获利保证；对未覆盖股票不得推断。'}
