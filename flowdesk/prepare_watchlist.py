#!/usr/bin/env python3
"""Read-only dynamic-watchlist PROPOSAL, never changes subscribed watchlist.json."""
import argparse,json
from pathlib import Path
from universe_agents import review


def propose(market_dir, existing_config, capacity=36, deep_count=5):
    if not 3<=capacity<=60 or not 1<=deep_count<=10:raise ValueError('Invalid capacity')
    audit=review(market_dir)
    if audit['status'] not in ('RESEARCH_WATCHLIST_ONLY','LISTED_RESEARCH_WATCHLIST_ONLY'):
        return {'status':'NO_GO_COVERAGE','reason':'Current whole-market snapshot is incomplete/stale',
                'tradeEnabled':False,'symbols':[],'deepSymbols':[]}
    base=list(dict.fromkeys(['US.SPY','US.QQQ','US.IWM']))
    # Never guess membership from present snapshot as point-in-time historical universe.
    dynamic=[x['code'] for x in audit['candidates'] if x['code'] not in base]
    symbols=(base+dynamic)[:capacity]
    # These are attention proposals only; requires human check and restart to subscribe.
    deep=list(dict.fromkeys([base[0]]+dynamic[:max(0,deep_count-1)]))
    return {'status':'REVIEW_REQUIRED_NOT_APPLIED','symbols':symbols,'deepSymbols':deep,
            'benchmark':'US.SPY','peers':{},'tradeEnabled':False,
            'source':'Current US primary listed stocks (NYSE/Nasdaq/AMEX) batch snapshots; excludes unsupported OTC and unknown exchanges; not PIT or microstructure',
            'warning':'This is a proposal only. The original watchlist.json and running OpenD subscriptions were not modified.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--market-dir',type=Path,default=Path(__file__).parent/'output'/'market');p.add_argument('--capacity',type=int,default=36);p.add_argument('--deep-count',type=int,default=5)
    p.add_argument('--out',type=Path,default=None);a=p.parse_args()
    x=propose(a.market_dir,None,a.capacity,a.deep_count)
    target=a.out or a.market_dir/'proposed_watchlist.json'
    target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':x['status'],'candidateCount':len(x['symbols'])},ensure_ascii=False))

if __name__=='__main__':main()
