#!/usr/bin/env python3
"""Independent audit of frozen, forward-only *quote model* outcomes.

Output never grants trading permission: Moomoo book is not proven consolidated NBBO,
quotes do not prove actual fills, and short-borrow/impact are not modelled.
"""
import argparse,json,hashlib,random,statistics
from collections import defaultdict
from pathlib import Path
from prospective import verify_chain,moment,RULES,COSTS,DELAY_SEC,MAX_WAIT_SEC,SHADOW_NOTIONAL_USD,MAX_L1_PARTICIPATION,MAX_BOOK_GAP_SEC,MAX_QUOTE_PROOF_AGE_SEC,sha_bytes
from zoneinfo import ZoneInfo
ET=ZoneInfo("America/New_York")


def report(root):
    p=Path(root);plan,events=verify_chain(p)
    version=plan.get('version')
    if (version not in (1,2,3) or plan['rules']!=list(RULES) or
        tuple(plan['extraRoundtripCostsBps'])!=COSTS or
        (version in (2,3) and (plan.get('shadowNotionalUsd')!=SHADOW_NOTIONAL_USD or
                         plan.get('maxVisibleL1Participation')!=MAX_L1_PARTICIPATION)) or
        (version==3 and (plan.get('maxBookSilenceSeconds')!=MAX_BOOK_GAP_SEC or
                         plan.get('maxVendorQuoteAgeDuringHoldSeconds')!=MAX_QUOTE_PROOF_AGE_SEC))):
        raise ValueError('FROZEN_RULES_OR_COSTS_DIFFER_FROM_ENGINE')
    engine=(Path(__file__).parent/'prospective.py' if version==3 else
            Path(__file__).parent/'legacy_v15'/'prospective.py' if version==2 else
            Path(__file__).parent/'legacy_v06'/'prospective.py')
    if plan['sourceSha256']!=sha_bytes(engine.read_bytes()):
        raise ValueError('ENGINE_SOURCE_CHANGED_FROM_FROZEN_PROTOCOL')
    known={x['id']:x for x in plan['rules']};lifecycle={};outcomes=[];counts=defaultdict(int)
    for event in events:
        t=event['type'];counts[t]+=1
        if t in ('RUN_END','BOOK_MINUTE'):continue
        ident=event.get('id')
        if t=='SIGNAL':
            if ident in lifecycle or event['rule'] not in known:raise ValueError('DUPLICATE_OR_UNREGISTERED_SIGNAL')
            lifecycle[ident]={'signal':event,'state':'PENDING'}
        elif t in ('PAPER_ENTRY','PAPER_EXIT','CANCELLED','CENSORED','EXPIRED'):
            if ident not in lifecycle:raise ValueError('ORPHAN_EVENT')
            x=lifecycle[ident];status=x['state'];rule=known[x['signal']['rule']]
            if event['code']!=x['signal']['code'] or event['rule']!=rule['id']:raise ValueError('CONTRADICTORY_SYMBOL_OR_RULE')
            if t=='PAPER_ENTRY':
                if status!='PENDING':raise ValueError('DUPLICATE_ENTRY')
                gap=(moment(event['at'])-moment(x['signal']['at'])).total_seconds()
                if not DELAY_SEC<=gap<=MAX_WAIT_SEC+1e-6:raise ValueError('CAUSAL_ENTRY_VIOLATION')
                if event['price'] != (event['ask'] if x['signal']['direction']==1 else event['bid']):
                    raise ValueError('NOT_CROSSING_ON_ENTRY')
                if version in (2,3):
                    shares=max(1,int(SHADOW_NOTIONAL_USD//event['price']))
                    top=event.get('entryL1Size',0)
                    if (event.get('shadowShares')!=shares or shares>MAX_L1_PARTICIPATION*top+1e-9):
                        raise ValueError('INVALID_ENTRY_VISIBLE_LIQUIDITY')
                x['entry']=event;x['state']='OPEN'
            elif t=='PAPER_EXIT':
                if status!='OPEN':raise ValueError('EXIT_WITHOUT_ENTRY')
                hold=(moment(event['exitAt'])-moment(x['entry']['at'])).total_seconds()
                if not rule['horizon']<=hold<=rule['horizon']+MAX_WAIT_SEC+1e-6:raise ValueError('NONCAUSAL_EXIT')
                if version in (2,3):
                    if (event.get('shadowShares')!=x['entry']['shadowShares'] or
                        event.get('exitL1Size',0)*MAX_L1_PARTICIPATION+1e-9<event['shadowShares']):
                        raise ValueError('INVALID_EXIT_VISIBLE_LIQUIDITY')
                    if event['exitPrice'] != (event['exitBid'] if event['direction']==1 else event['exitAsk']):
                        raise ValueError('NOT_CROSSING_ON_EXIT')
                if event['entryPrice']!=x['entry']['price'] or event['direction']!=x['signal']['direction']:
                    raise ValueError('PRICE_OR_DIRECTION_CHANGED')
                calc=(event['exitPrice']/event['entryPrice']-1)*10000*event['direction']
                if abs(calc-event['quotedCrossingBps'])>1e-4:raise ValueError('INCORRECT_PNL')
                for c in COSTS:
                    if abs(calc-c-event['netQuoteModelBps'][str(int(c))])>1e-4:raise ValueError('MISSING_TRADING_COST')
                x['state']='DONE';outcomes.append(event)
            else:
                if t=='CANCELLED' and status!='PENDING':raise ValueError('INVALID_CANCEL')
                if t=='EXPIRED' and status!='PENDING':raise ValueError('INVALID_EXPIRY')
                if t=='CENSORED' and status!='OPEN':raise ValueError('INVALID_CENSOR')
                x['state']='DONE'
    # A crash or incomplete journal is not silently treated as a successful run.
    complete=bool(events and events[-1].get('type')=='RUN_END' and all(v['state']=='DONE' for v in lifecycle.values()))
    rows=[]
    for rule in plan['rules']:
        selected=[r for r in outcomes if r['rule']==rule['id']]
        days=defaultdict(list)
        for x in selected:days[moment(x['entryAt']).astimezone(ET).date().isoformat()].append(x['netQuoteModelBps']['20'])
        clusters=[statistics.fmean(v) for v in days.values()]
        ci=None
        if len(clusters)>=20:
            rng=random.Random(518);boots=[]
            for i in range(1000):boots.append(statistics.fmean(rng.choice(clusters) for _ in clusters))
            boots.sort();ci=[round(boots[24],4),round(boots[974],4)]
        rows.append({'rule':rule['id'],'modelExits':len(selected),'distinctEtDates':len(days),
                     'symbols':len({x['code'] for x in selected}),
                     'net8MeanBps':round(statistics.fmean(x['netQuoteModelBps']['8'] for x in selected),4) if selected else None,
                     'net20MeanBps':round(statistics.fmean(x['netQuoteModelBps']['20'] for x in selected),4) if selected else None,
                     'net40MeanBps':round(statistics.fmean(x['netQuoteModelBps']['40'] for x in selected),4) if selected else None,
                     'dayClusterBootstrap95CI20Bps':ci})
    independent_days=len({moment(x['entryAt']).astimezone(ET).date().isoformat() for x in outcomes})
    symbol_count=len({x['code'] for x in outcomes})
    return {'verdict':'NO_GO_REAL_TRADING','journalValid':True,'completeRun':complete,
            'protocolHash':hashlib.sha256((p/'frozen_protocol.json').read_bytes()).hexdigest(),
            'frozenHypotheses':len(RULES),'chainEvents':len(events),'eventCounts':dict(counts),
            'independentEtDays':independent_days,'symbolsWithExits':symbol_count,
            'sampleFloorMet':bool(independent_days>=20 and symbol_count>=10 and len(outcomes)>=200),
            'results':rows,'paperFillEvidence':False,'marketDataSourceTimestampVerified':False,
            'source':'LOCAL_RECEIVED_MOOMOO_ORDERBOOK_SNAPSHOTS',
            'warning':'报价穿价模型不是券商成交；没有完整NBBO、真实执行、真实冲击、借券/融券；哪怕正净bps也不能认证Alpha。',
            'liveOrdersEnabled':False}


def main():
    p=argparse.ArgumentParser(description='Audit forward-only, hash-linked quote-crossing shadow outcomes')
    p.add_argument('--run-dir',type=Path,required=True);p.add_argument('--out',type=Path)
    a=p.parse_args();r=report(a.run_dir);content=json.dumps(r,ensure_ascii=False,indent=2)+'\n'
    if a.out:
        a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(content)
    print(content)

if __name__=='__main__':main()
