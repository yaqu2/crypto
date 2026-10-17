#!/usr/bin/env python3
"""Independent *counterfactual* capital-constrained shadow portfolio replay.

Input is the cryptographically verified ForwardStudy journal, NOT broker fills.
- No short is admitted without independent borrow evidence (unavailable here).
- Simultaneous research rules compete for one portfolio; first frozen-rule priority.
- Cannot silently dispose of censored positions or use missing exits as zero P&L.
- Does NOT infer exchange timestamp, execution, fair fill, true equity curve or alpha.
"""
from __future__ import annotations
import argparse, json, math
from collections import Counter,defaultdict
from datetime import timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from pathlib import Path
from prospective import RULES,verify_chain,moment

RULE_ORDER={r['id']:i for i,r in enumerate(RULES)}
COSTS=(8,20,40)
DEFAULTS={'accountUsd':100000.0,'maxGrossUsd':10000.0,'maxConcurrent':10,
          'maxSameSymbol':1,'maxPerTradeUsd':1200.0,'allowShort':False}


def _ts(item):
    t=item['event']
    stamp=t.get('at') or t.get('exitAt') or t.get('entryAt')
    dt=moment(stamp)
    if dt is None:raise ValueError('MISSING_OR_NAIVE_EVENT_TIME')
    return dt.timestamp()


def replay_portfolio(runs, settings=None):
    """Replay (run_id, verified_events) iterable; never allocate > one account budget.

    Long-only by construction: allowing paper shorts without borrow proof is unsupported.
    For identical timestamps, the *frozen* RULES order determines tie precedence.
    """
    runs=list(runs)  # Accept generators without silently losing run IDs on second traversal.
    cfg=dict(DEFAULTS)
    if settings:cfg.update(settings)
    if cfg['allowShort']:raise ValueError('SHORT_BORROW_EVIDENCE_UNAVAILABLE')
    if not (cfg['accountUsd']>0 and cfg['maxGrossUsd']>0 and
            cfg['maxGrossUsd']<=cfg['accountUsd'] and cfg['maxConcurrent']>=1 and
            cfg['maxSameSymbol']>=1 and cfg['maxPerTradeUsd']>0):
        raise ValueError('INVALID_PAPER_CAPITAL_BUDGET')
    timeline=[];signals={}
    for run_id,events in runs:
        for seq,event in enumerate(events):
            typ=event.get('type')
            if typ=='SIGNAL':
                key=(str(run_id),str(event.get('id')))
                if key in signals:raise ValueError('DUPLICATE_SIGNAL_ID')
                signals[key]=event
            if typ in ('PAPER_ENTRY','PAPER_EXIT','CENSORED','RUN_END'):
                timeline.append((_ts({'event':event}),int(RULE_ORDER.get(event.get('rule'),9999)),
                                 str(run_id),seq,event))
    # Chronological priority is fixed before observation; a simultaneous exit frees
    # capital only if it was already a pending position in the source evidence.
    timeline.sort(key=lambda z:(z[0],z[1],z[2],z[3]))
    open_positions={};all_seen=set();blocked=set();per_symbol=Counter()
    gross=0.;peak=0.;accepted=0;closed=0;censored=0
    counts=Counter();pnl={str(c):0. for c in COSTS};rule_pnl=defaultdict(lambda:{str(c):0. for c in COSTS})
    by_day=defaultdict(lambda:defaultdict(float))
    rule_by_day=defaultdict(lambda:defaultdict(lambda:defaultdict(float)))
    rule_count_by_day=defaultdict(lambda:defaultdict(int))
    realized_curve=[]
    have_runs={str(run_id) for run_id,_ in runs}
    previous_by_run={};last_event_time={}
    for stamp,order,run,seq,e in timeline:
        typ=e['type'];last_event_time[run]=max(stamp,last_event_time.get(run,stamp))
        key=(run,str(e.get('id')))
        if typ=='RUN_END':
            continue
        if typ=='PAPER_ENTRY':
            counts['submittedEntries']+=1
            if key in all_seen:raise ValueError('DUPLICATE_PAPER_ENTRY_ID')
            all_seen.add(key)
            rule=e.get('rule')
            if rule not in RULE_ORDER:raise ValueError('UNKNOWN_FROZEN_RULE')
            code=e.get('code')
            if not isinstance(code,str) or not code.startswith('US.'):
                raise ValueError('INVALID_SYMBOL')
            try:
                px=float(e['price']);qty=int(e['shadowShares']);side=int(e.get('direction',0))
            except (TypeError,ValueError,KeyError):
                # v07 PAPER_ENTRY does not carry direction: infer from entry bid/ask
                px=float(e['price']);qty=int(e['shadowShares']);side=0
            if qty<=0 or not math.isfinite(px) or px<=0:raise ValueError('INVALID_ENTRY_NOTIONAL')
            # Direction not in PAPER_ENTRY v07: infer from whether price is ask/bid;
            # crossed or one-tick ambiguous quotes -> FAIL CLOSED.
            bid=float(e['bid']);ask=float(e['ask'])
            if not (0<bid<ask and math.isfinite(bid) and math.isfinite(ask)):
                raise ValueError('INVALID_ENTRY_BOOK')
            if abs(px-ask)<1e-7 and abs(px-bid)>1e-7:side=1
            elif abs(px-bid)<1e-7 and abs(px-ask)>1e-7:side=-1
            else:raise ValueError('CANNOT_DETERMINE_POSITION_SIDE')
            notional=qty*px
            reason=None
            if side==-1:reason='BORROW_NOT_VERIFIED'
            elif notional>cfg['maxPerTradeUsd']+1e-8:reason='PER_TRADE_NOTIONAL_CAP'
            elif len(open_positions)>=cfg['maxConcurrent']:reason='MAX_CONCURRENT'
            elif per_symbol[code]>=cfg['maxSameSymbol']:reason='OVERLAPPING_SYMBOL_RULE'
            elif gross+notional>cfg['maxGrossUsd']+1e-8:reason='GROSS_CAPITAL_EXHAUSTED'
            signal=signals.get(key)
            if not signal:
                reason='MISSING_SOURCE_SIGNAL'
            else:
                when=moment(signal.get('at'))
                delay=stamp-when.timestamp() if when else -1
                if (signal.get('rule')!=rule or signal.get('code')!=code or
                        int(signal.get('direction',0))!=side or not 2<=delay<=15):
                    reason='SIGNAL_PROVENANCE_OR_CAUSAL_DELAY_INVALID'
            level=e.get('entryL1Size')
            if level is None or not math.isfinite(float(level)) or qty>float(level)*.1+1e-8:
                reason='UNVERIFIED_ENTRY_DEPTH'
            if reason:
                blocked.add(key);counts['blocked_'+reason]+=1
                continue
            open_positions[key]={'code':code,'rule':rule,'qty':qty,'price':px,
                                 'side':side,'notional':notional,'entryAt':stamp}
            gross+=notional;peak=max(peak,gross);per_symbol[code]+=1;accepted+=1
            counts['acceptedEntries']+=1
        elif typ=='PAPER_EXIT':
            if key in blocked:
                counts['blockedExitExcluded']+=1;continue
            pos=open_positions.pop(key,None)
            if pos is None:raise ValueError('ORPHAN_PAPER_EXIT')
            if pos['rule']!=e.get('rule') or pos['code']!=e.get('code'):
                raise ValueError('EXIT_MISMATCH')
            if int(e.get('shadowShares',-1))!=pos['qty'] or int(e.get('direction',0))!=pos['side']:
                raise ValueError('EXIT_SIZE_OR_SIDE_MISMATCH')
            entry_iso=moment(e.get('entryAt'))
            if entry_iso is None or abs(entry_iso.timestamp()-pos['entryAt'])>0.001:
                raise ValueError('EXIT_ENTRY_TIMESTAMP_MISMATCH')
            depth=e.get('exitL1Size')
            if depth is None or not math.isfinite(float(depth)) or pos['qty']>float(depth)*.1+1e-8:
                raise ValueError('EXIT_DEPTH_UNVERIFIED')
            exitpx=float(e['exitPrice'])
            quoted_side=e.get('exitBid') if pos['side']==1 else e.get('exitAsk')
            if quoted_side is None or abs(float(quoted_side)-exitpx)>1e-7:
                raise ValueError('EXIT_CROSS_SIDE_MISMATCH')
            if not math.isfinite(exitpx) or exitpx<=0 or stamp<pos['entryAt']:
                raise ValueError('INVALID_EXIT_PRICE_OR_TIME')
            # Recompute P&L; do not trust the precomputed paper outcome.
            crossed_bps=(exitpx/pos['price']-1)*10000*pos['side']
            if abs(crossed_bps-float(e['quotedCrossingBps']))>0.002:
                raise ValueError('RECORDED_PNL_DISAGREES_WITH_QUOTES')
            for c in COSTS:
                keycost=str(c)
                if abs((crossed_bps-c)-float(e['netQuoteModelBps'][keycost]))>.002:
                    raise ValueError('RECORDED_COST_PNL_DISAGREEMENT')
                dollars=pos['notional']*(crossed_bps-c)/10000
                pnl[keycost]+=dollars;rule_pnl[pos['rule']][keycost]+=dollars
                day=moment(e['exitAt']).astimezone(ET).date().isoformat()
                by_day[day][keycost]+=dollars
                rule_by_day[day][pos['rule']][keycost]+=dollars
            rule_count_by_day[day][pos['rule']]+=1
            gross-=pos['notional'];per_symbol[pos['code']]-=1
            closed+=1;counts['closedModelExits']+=1
            realized_curve.append({'at':e['exitAt'],'equityUsdClosedOnlyCost40':round(cfg['accountUsd']+pnl['40'],4)})
        elif typ=='CENSORED':
            if key in blocked:
                counts['blockedCensorExcluded']+=1;continue
            pos=open_positions.pop(key,None)
            if pos:
                gross-=pos['notional'];per_symbol[pos['code']]-=1
                censored+=1;counts['censoredAcceptedPositionsUnknownPnl']+=1
    dangling=[{'runId':run,'code':p['code'],'rule':p['rule'],'notionalUsd':round(p['notional'],2)}
              for (run,_),p in open_positions.items()]
    # An event stream ending without complete exit/censor provides UNKNOWN P&L.
    # Closed-trade P&L is descriptive, not an account equity curve (no intratrade MTM).
    total_pnl={k:round(v,4) for k,v in pnl.items()}
    net_basis_points={k:round(v/cfg['accountUsd']*10000,4) for k,v in pnl.items()}
    return {'verdict':'NO_GO_REAL_TRADING','mode':'COUNTERFACTUAL_CAPITAL_CONSTRAINED_LONG_ONLY',
            'accountUsd':cfg['accountUsd'],'maxGrossUsd':cfg['maxGrossUsd'],
            'maxSameSymbol':cfg['maxSameSymbol'],'maxConcurrent':cfg['maxConcurrent'],
            'maxPerTradeUsd':cfg['maxPerTradeUsd'],'runsReplayed':len(have_runs),
            'entryProposals':counts['submittedEntries'],'admittedLongModelEntries':accepted,
            'closedLongModelExits':closed,'censoredAdmittedPositions':censored,
            'stillOpenUnresolved':len(dangling),'unresolvedNotionalUsd':round(sum(p['notional'] for p in open_positions.values()),2),
            'maximumModelGrossExposureUsd':round(peak,2), 'modelRemainingGrossExposureUsd':round(gross,2),
            'blockedOrExcluded':{k:v for k,v in sorted(counts.items()) if k.startswith('blocked_')},
            'recomputedClosedOnlyModelPnlUsd':total_pnl,
            'closedOnlyAccountBasisPointContribution':net_basis_points,
            'ruleClosedOnlyPnlUsd':{r:{k:round(v,4) for k,v in values.items()} for r,values in sorted(rule_pnl.items())},
            'exitDayClosedOnlyPnlUsd':{d:{k:round(v,4) for k,v in vals.items()} for d,vals in sorted(by_day.items())},
            'ruleExitDayClosedOnlyPnlUsd':{d:{r:{c:round(v,4) for c,v in vals.items()}
                                              for r,vals in sorted(rules.items())}
                                           for d,rules in sorted(rule_by_day.items())},
            'ruleExitDayClosedOnlyCounts':{d:dict(sorted(rules.items())) for d,rules in sorted(rule_count_by_day.items())},
            'dayTimezone':'America/New_York',
            'diagnosticComplete':bool(have_runs and censored==0 and not dangling),
            'claimableAsRealPortfolioReturn':False,'borrowsVerified':False,'brokerFillsVerified':False,
            'actualUnrealizedPnlKnown':False,'markToMarketDrawdownKnown':False,
            'liveOrdersEnabled':False,
            'limitations':['假设全部可按盘口报价成交，实际无券商执行回报','不纳入未观测报价时的尾部损失；删失持仓的损益未知',
                           '相同标的多策略机会竞争同一账户资金，按冻结策略顺序排队','仅已闭合影子交易损益，绝非完整资金曲线或Sharpe']}


def audit_folder(recordings):
    root=Path(recordings)
    runs=sorted(root.glob('*/forward_shadow'))
    verified=[]
    for path in runs:
        plan,events=verify_chain(path)
        verified.append((path.parent.name,events))
    result=replay_portfolio(verified)
    from audit_forward_runs import audit_all
    independent=audit_all(root)
    result['allSourceRunsAuditable']=independent['allRunsAuditable']
    result['runInventoryPresent']=independent['runInventoryPresent']
    result['diagnosticComplete']=bool(result['diagnosticComplete'] and
                                      independent['allRunsAuditable'] and independent['runInventoryPresent'])
    return result


def main():
    p=argparse.ArgumentParser(description='资金与做空资格约束的历史报价影子交易审计，不下单')
    p.add_argument('--recordings',type=Path,default=Path(__file__).parent/'recordings')
    p.add_argument('--out',type=Path,default=Path(__file__).parent/'research'/'forward_audits'/'portfolio_stress.json')
    a=p.parse_args();result=audit_folder(a.recordings)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    tmp=a.out.with_suffix('.json.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');tmp.replace(a.out)
    print(json.dumps({k:result[k] for k in ('verdict','runsReplayed','entryProposals','admittedLongModelEntries','closedLongModelExits','censoredAdmittedPositions','recomputedClosedOnlyModelPnlUsd','diagnosticComplete')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
