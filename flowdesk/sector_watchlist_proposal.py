#!/usr/bin/env python3
"""Read-only ETF/sector universe PROPOSAL; never changes watchlist or subscriptions."""
import argparse,json
from pathlib import Path
from runtime import validate_config

SECTOR_ETFS=('US.SMH','US.SOXX','US.XLK','US.XSD','US.IWM')
SECTOR_PEERS=('US.CIEN','US.MU','US.SNDK','US.WDC','US.STX')

def propose(base):
    validate_config(base)
    original=list(base['symbols'])
    symbols=list(dict.fromkeys(original+list(SECTOR_ETFS)+list(SECTOR_PEERS)))
    if len(symbols)>60:raise ValueError('SUBSCRIPTION_SYMBOL_CAP_EXCEEDED')
    config={'symbols':symbols,'deepSymbols':list(base['deepSymbols']),
            'benchmark':base['benchmark'],'peers':dict(base.get('peers',{}))}
    validate_config(config)
    return {'status':'REVIEW_ONLY_NOT_ACTIVATED','proposedConfig':config,
            'newSymbols':[s for s in symbols if s not in original],
            'qualityRequirements':['venue subscription permission','source timestamps','actual feed freshness','user manual approval'],
            'tradeSignal':False,'liveSubscriptionsChanged':False,
            'warning':'This does NOT change original watchlist.json, does not verify quote entitlements or auto-activate next run.'}

def main():
    ap=argparse.ArgumentParser(description='Propose QQQ/SMH/sector ETF watchlist, no activation')
    base=Path(__file__).resolve().parent
    ap.add_argument('--watchlist',type=Path,default=base/'watchlist.json')
    ap.add_argument('--out',type=Path,default=base/'output'/'research'/'sector_watchlist_proposal_v26.json')
    args=ap.parse_args()
    result=propose(json.loads(args.watchlist.read_text(encoding='utf-8')))
    args.out.parent.mkdir(exist_ok=True,parents=True)
    tmp=args.out.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');tmp.replace(args.out)
    print(json.dumps({'status':result['status'],'newSymbols':result['newSymbols']},ensure_ascii=False,indent=2))
    return 0
if __name__=='__main__':raise SystemExit(main())
