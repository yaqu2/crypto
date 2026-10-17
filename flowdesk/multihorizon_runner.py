#!/usr/bin/env python3
"""FlowDesk v2.6: three independent read-only chronology-aware horizon competitions.

Each horizon keeps its own purge/calibration/holdout test; these are not joint
consistent calibrated forecasts nor comparable real execution PnLs.
"""
import argparse, json, hashlib
from pathlib import Path
from sector_model_lab import run, DEFAULTS

# Pre-declared research thresholds, not tuned and not estimates of optimal levels.
TARGETS={1:(1.0,0.75),3:(2.5,2.0),5:(4.0,2.5)}

def research(root, output):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    horizons={}
    for h,(abs_threshold,excess_threshold) in TARGETS.items():
        cfg=dict(DEFAULTS,horizon=h,label_absolute_return_pct=abs_threshold,
                 label_excess_over_spy_pct=excess_threshold,
                 gap_days=max(5,h+1))
        dest=output/f'model_horizon_{h}d.json'
        result=run(Path(root),dest,cfg)
        horizons[str(h)]={'status':result['status'],'outerBrier':result.get('outerBrier'),
                          'baselineBrier':result.get('outerBaselineBrier'),
                          'completedFolds':result.get('completedFolds',0),
                          'researchReport':str(dest),'targetAbsolutePct':abs_threshold,
                          'targetExcessSpyPct':excess_threshold}
    statuses=[r['status'] for r in horizons.values()]
    status='EXPLORATORY_MULTIHORIZON_COMPARISON_ONLY' if all(x=='EXPLORATORY_WALK_FORWARD_COMPARISON_ONLY' for x in statuses) else 'NO_GO_ALL_HORIZONS_NOT_VALIDATED'
    summary={'version':'2.6.0','status':status,'horizons':horizons,'noTradingPermission':True,
             'profitCertified':False,'jointProbabilitiesCalibrated':False,
             'warning':'Independently fitted horizon probabilities need joint coherence checks; no guarantee of real execution profits.'}
    path=output/'multihorizon_model_report.json';tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8');tmp.replace(path)
    return summary

def main():
    parser=argparse.ArgumentParser(description='V2.6 independent 1/3/5-day model challenges')
    base=Path(__file__).resolve().parent
    parser.add_argument('--market-dir',type=Path,default=base/'output'/'market')
    parser.add_argument('--out-dir',type=Path,default=base/'output'/'research'/'v26_multihorizon')
    args=parser.parse_args();report=research(args.market_dir,args.out_dir)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='EXPLORATORY_MULTIHORIZON_COMPARISON_ONLY' else 2
if __name__=='__main__':raise SystemExit(main())
