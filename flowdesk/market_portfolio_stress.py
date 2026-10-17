#!/usr/bin/env python3
"""Strictly observational capital/time priority replay from verified three-scan data.

Never sends an order.  Displayed BBO is NOT a fill, NBBO, an executable quote,
or real account equity. The earliest daily symbol event and the earliest eligible
entry/exit *scan* are fixed regardless of later outcomes. An admitted entry whose
exit cannot be independently verified remains censored and continues to reserve
capital -- it is never assumed to have liquidated.
"""
from __future__ import annotations
import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from market_archive import audit_archive
from market_forward_panel import load_scan, select_candidates, paired_point, as_utc, BENCHMARK, ET, _dates_match
from market_quote_stress import (first_in_window, report_coverage, bbo_point,
    MIN_ENTRY_DELAY_SECONDS, MAX_ENTRY_DELAY_SECONDS,
    MIN_EXIT_DELAY_SECONDS, MAX_EXIT_DELAY_SECONDS, MODEL_NOTIONAL_USD,
    MAX_TOP_LEVEL_PARTICIPATION)
from market_scan import exchange_scope
from feed_guard import clock_state

COST_BPS = (8, 20, 40, 80, 120)

@dataclass(frozen=True)
class Limits:
    account_usd: float = 100000.0
    gross_cap_usd: float = 5000.0
    concurrent_cap: int = 5
    per_signal_cap_usd: float = MODEL_NOTIONAL_USD
    daily_entry_cap_usd: float = 5000.0
    modeled_daily_loss_pause_usd: float = 100.0
    allow_short: bool = False

    def verify(self):
        floats=(self.account_usd,self.gross_cap_usd,self.per_signal_cap_usd,
                self.daily_entry_cap_usd,self.modeled_daily_loss_pause_usd)
        if (any(type(n) not in (int,float) or not math.isfinite(n) or n<=0 for n in floats)
            or type(self.concurrent_cap) is not int or not 1<=self.concurrent_cap<=100
            or self.gross_cap_usd>self.account_usd
            or self.per_signal_cap_usd>self.gross_cap_usd
            or self.daily_entry_cap_usd>self.account_usd or self.allow_short):
            raise ValueError('INVALID_OR_UNVERIFIED_LONG_ONLY_RISK_LIMITS')
        return self


def _date(scan):
    return scan['time'].astimezone(ET).date().isoformat()


def _inside(scan):
    return bool(clock_state(scan['time'])['regular'])


def _source_ok(scan,code,after=None):
    pair=paired_point(scan['quotes'].get(code), scan['bench'].get(BENCHMARK), scan['time'])
    if not pair:return False
    # Both stock and benchmark actually existed at the scan's completion.
    for v in pair:
        if v['at']>scan['time']:
            return False
        if after is not None and v['at']<=after:
            return False
    return True


def prepare_intents(scans, limits):
    """All *entry admission* fields use A or B only, never C.

    First candidate for symbol/day is locked before checking entry feasibility;
    no skipping a weak first event to select a later winner.
    """
    limits.verify()
    scans=sorted(scans,key=lambda s:(s['time'],s['id']))
    seen=set();counts=Counter();intents=[]
    for i,origin in enumerate(scans):
        candidates,_,_=select_candidates(origin)
        for c in candidates:
            code=c['code'];key=(_date(origin),code)
            if key in seen:
                counts['DUPLICATE_SAME_SYMBOL_DAY_NOT_RESELECTED']+=1;continue
            seen.add(key);counts['firstDailySignals']+=1
            if c['direction']!='UP_EVENT':
                counts['NO_GO_SHORT_BORROW_EVIDENCE']+=1;continue
            if not _inside(origin) or report_coverage(origin)<.90 or not _source_ok(origin,code):
                counts['NO_GO_ORIGIN_PROVENANCE_OR_SCOPE']+=1;continue
            entry=first_in_window(scans,i,MIN_ENTRY_DELAY_SECONDS,MAX_ENTRY_DELAY_SECONDS,origin['time'])
            if entry is None:
                counts['PENDING_NO_DELAYED_ENTRY']+=1;continue
            if (not _inside(entry) or report_coverage(entry)<.90 or
                exchange_scope(entry['universe'].get(code,{}).get('exchange'))!='US_PRIMARY_LISTED' or
                not _source_ok(entry,code,origin['time'])):
                counts['NO_GO_ENTRY_PROVENANCE_OR_COVERAGE']+=1;continue
            bbo=bbo_point(entry['quotes'].get(code),entry['time'])
            if bbo is None:
                counts['NO_GO_ENTRY_BBO']+=1;continue
            # Exact intended $1k BBO-crossing shadow size, not a probabilistic fill.
            shares=math.floor(min(limits.per_signal_cap_usd,MODEL_NOTIONAL_USD)/bbo['ask'])
            if shares<1:
                counts['NO_GO_BELOW_ONE_SHARE']+=1;continue
            if shares>bbo['askVol']*MAX_TOP_LEVEL_PARTICIPATION:
                counts['NO_GO_ENTRY_DISPLAYED_DEPTH']+=1;continue
            future=first_in_window(scans,i,MIN_EXIT_DELAY_SECONDS,MAX_EXIT_DELAY_SECONDS,origin['time'])
            intents.append({'day':key[0],'code':code,'origin':origin,'entry':entry,'future':future,
                            'shares':shares,'ask':bbo['ask'],'entryDepth':bbo['askVol'],
                            'notional':shares*bbo['ask'],'key':(key[0],code)})
            counts['qualifiedEntryIntents']+=1
    # Equal-clock independent entries are prioritised deterministically by the
    # pre-declared origin clock, then symbol, NEVER by their future profitability.
    intents.sort(key=lambda x:(x['entry']['time'],x['origin']['time'],x['code']))
    return intents,counts


def _exit_result(intent):
    """Only called when the simulated clock reaches future scan C."""
    s=intent['future'];e=intent['entry'];code=intent['code']
    if s is None:return None,'CENSORED_NO_FUTURE_EXIT_SCAN'
    if (not _inside(s) or report_coverage(s)<.90 or
        exchange_scope(s['universe'].get(code,{}).get('exchange'))!='US_PRIMARY_LISTED' or
        not _source_ok(s,code,e['time'])):
        return None,'CENSORED_EXIT_PROVENANCE_OR_COVERAGE'
    bbo=bbo_point(s['quotes'].get(code),s['time'])
    if bbo is None:return None,'CENSORED_MISSING_OR_WIDE_EXIT_BBO'
    if intent['shares']>bbo['bidVol']*MAX_TOP_LEVEL_PARTICIPATION:
        return None,'CENSORED_INSUFFICIENT_EXIT_DISPLAYED_DEPTH'
    entry_spy=paired_point(e['quotes'].get(code),e['bench'].get(BENCHMARK),e['time'])[1]['price']
    exit_spy=paired_point(s['quotes'].get(code),s['bench'].get(BENCHMARK),s['time'])[1]['price']
    return {'exitBid':bbo['bid'],'exitSourceAt':s['quotes'][code]['sourceAsOf'],
            'spyReturnBps':(exit_spy/entry_spy-1)*10000},'VERIFIED_FUTURE_DISPLAYED_QUOTE_ONLY'


def replay_scans(scans, limits=Limits()):
    """Replays only the scan-derived intents in chronological order.

    Future is not consulted when *admitting* positions. Unknown exits hold the
    capital slot indefinitely; no invented sale, stop, intratrade MTM or equity.
    """
    limits.verify();scans=sorted(scans,key=lambda s:(s['time'],s['id']))
    intents,counts=prepare_intents(scans,limits)
    # At any identical timestamp, entries are attempted before exits. This is
    # a conservative tie break: no capital can be recycled prematurely.
    timeline=[]
    for num,intent in enumerate(intents):
        timeline.append((intent['entry']['time'],0,num,'entry',intent))
        if intent['future'] is not None:
            timeline.append((intent['future']['time'],1,num,'exit',intent))
    timeline.sort(key=lambda x:(x[0],x[1],x[2]))
    admitted={};open_pos={};gross=0.0;peak=0.0;closed=0;unknown=0
    daily_opened=defaultdict(float);daily_observed_loss=defaultdict(float)
    dollars={str(c):0.0 for c in COST_BPS};daily_pnl=defaultdict(lambda:defaultdict(float))
    observations=[]
    for when,_,idx,kind,it in timeline:
        if kind=='entry':
            reason=None
            if len(open_pos)>=limits.concurrent_cap:reason='CAP_MAX_CONCURRENT'
            elif gross+it['notional']>limits.gross_cap_usd+1e-8:reason='CAP_GROSS_EXHAUSTED'
            elif daily_opened[it['day']]+it['notional']>limits.daily_entry_cap_usd+1e-8:
                reason='CAP_DAILY_ENTRY_EXHAUSTED'
            elif daily_observed_loss[it['day']]>=limits.modeled_daily_loss_pause_usd:
                reason='PAUSE_MODELED_CLOSED_LOSS_ONLY'
            if reason:
                counts[reason]+=1
                observations.append({'day':it['day'],'code':it['code'],
                                     'originScan':it['origin']['id'],'entryScan':it['entry']['id'],
                                     'status':reason,'hypotheticalOrderSent':False})
                continue
            gross+=it['notional'];peak=max(peak,gross)
            daily_opened[it['day']]+=it['notional'];open_pos[idx]=it
            admitted[idx]=True;counts['ADMITTED_HYPOTHETICAL_BBO_CROSS']+=1
        else:
            if idx not in admitted:continue
            pos=open_pos[idx];quote,status=_exit_result(pos)
            if quote is None:
                counts[status]+=1;unknown+=1
                observations.append({'day':it['day'],'code':it['code'],
                                     'originScan':it['origin']['id'],'entryScan':it['entry']['id'],
                                     'futureScan':it['future']['id'],'status':status,
                                     'reservedUnknownUsd':round(pos['notional'],4),
                                     'hypotheticalOrderSent':False})
                continue
            gross-=pos['notional'];del open_pos[idx];closed+=1
            gross_dollars=(quote['exitBid']-pos['ask'])*pos['shares']
            scenarios={}
            for c in COST_BPS:
                net=gross_dollars-pos['notional']*c/10000
                scenarios[str(c)]=round(net,6)
                dollars[str(c)]+=net
                daily_pnl[it['day']][str(c)]+=net
            daily_observed_loss[it['day']]=max(0.,-daily_pnl[it['day']]['40'])
            counts['CLOSED_DISPLAYED_QUOTE_SCENARIO_ONLY']+=1
            observations.append({'day':it['day'],'code':it['code'],
                                 'originScan':it['origin']['id'],'entryScan':it['entry']['id'],
                                 'futureScan':it['future']['id'],'status':status,
                                 'shares':pos['shares'],'entryAsk':pos['ask'],
                                 'exitBid':quote['exitBid'], 'notionalUsd':round(pos['notional'],4),
                                 'quoteNetByExtraCostUSD':scenarios,
                                 'spyReturnBps':round(quote['spyReturnBps'],4),
                                 'hypotheticalOrderSent':False,'confirmedFill':False})
    # Accepted with no scheduled exit NEVER become a zero-profit trade or a
    # cash-free position. Their unverified capital remains encumbered.
    for idx,pos in open_pos.items():
        if pos['future'] is None:
            counts['CENSORED_NO_FUTURE_EXIT_SCAN']+=1;unknown+=1
            observations.append({'day':pos['day'],'code':pos['code'],
                                 'originScan':pos['origin']['id'],
                                 'entryScan':pos['entry']['id'],'futureScan':None,
                                 'status':'CENSORED_NO_FUTURE_EXIT_SCAN',
                                 'reservedUnknownUsd':round(pos['notional'],4),
                                 'hypotheticalOrderSent':False})
    # Summary of CLOSED cases, NOT the P&L of a feasible live portfolio. If any
    # quote was censored, portfolio-wide net P&L remains indeterminate.
    summary={'status':'EXPLORATORY_CAPITAL_CONSTRAINED_QUOTES_ONLY' if closed else
                       'NO_GO_NO_CLOSED_OBSERVATIONS',
        'model':'first symbol/day, first causal BBO entry, first causal future exit; long only',
        'config':asdict(limits),'limits':{'BBOParticipation':MAX_TOP_LEVEL_PARTICIPATION,
                                      'additionalRoundTripCostBps':list(COST_BPS)},
        'sourceScanCount':len(scans),'firstDailySignals':counts['firstDailySignals'],
        'admitted':len(admitted),'closedQuoteCases':closed,'censoredOrUnclosed':unknown,
        'peakModeledGrossUsd':round(peak,4),'unresolvedReservedCapitalUsd':round(gross,4),
        'accountProfitKnown':False,'wholePortfolioPnlKnown':False,
        'closedOnlyQuotePnLByCostUsd':{k:round(v,4) for k,v in dollars.items()},
        'closedOnlyWorstDayCost40Usd':round(min((v['40'] for v in daily_pnl.values()),default=0),4),
        'observedLossPauseIsNotIntratradeStop':True,
        'unknownCapitalShockUsd':{str(int(p*100))+'pct':round(gross*p,4)
                                  for p in (.1,.2,1.)},
        'outcomeCounts':dict(counts),'observations':observations,
        'tradeEnabled':False,'actualOrdersSent':0,'confirmedFills':0,'alphaCertified':False,
        'warning':'Admission assumes a hypothetical ask fill. Displayed L1 is not NBBO or a broker fill. No actual intratrade MTM, no force-exit or guaranteed stop. Censored capital stays reserved. Closed-only quote PnL is not account equity or broker profit.'}
    return summary


def evaluate_portfolio(market_root,limits=Limits()):
    limits.verify();root=Path(market_root)
    verified=audit_archive(root)
    if verified['status']!='PASS_LOCAL_ARCHIVE_INTEGRITY':
        return {'status':'NO_GO_ARCHIVE_INTEGRITY','archiveAudit':verified,
                'tradeEnabled':False,'alphaCertified':False,'actualOrdersSent':0}
    try:
        inv=root/'archive'/'scan_inventory.jsonl'
        scans=[]
        for line in inv.read_text(encoding='utf-8').splitlines():
            if not line.strip():continue
            item=json.loads(line)
            manifest=json.loads((root/'archive'/item['runId']/'archive_manifest.json').read_text())
            scans.append(load_scan(root,{**item,'files':manifest['filesSha256']}))
        return {**replay_scans(scans,limits),'archiveAudit':{'status':verified['status'],
                                                         'verifiedScans':verified['verified']}}
    except (OSError,ValueError,KeyError,TypeError,IndexError,OverflowError,ZeroDivisionError) as e:
        return {'status':'NO_GO_SOURCE_FORMAT_OR_REPLAY','error':str(e)[:240],
                'tradeEnabled':False,'alphaCertified':False,'actualOrdersSent':0}


def main():
    ap=argparse.ArgumentParser(description='只读组合资金和报价交易情景时间序列审计，不下单')
    ap.add_argument('--market-dir',type=Path,default=Path(__file__).resolve().parent/'output'/'market')
    ap.add_argument('--out',type=Path)
    args=ap.parse_args()
    report=evaluate_portfolio(args.market_dir)
    path=args.out or args.market_dir/'market_portfolio_stress.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='observations'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
