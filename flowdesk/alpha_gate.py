#!/usr/bin/env python3
"""Independent, multi-hypothesis order-flow research quality review.

NO live trade approval. Both crossed quotes and this day-block sign-flip test are
exploratory: venue coverage, timestamp provenance, borrow, true executions,
publication bias, holdout and selection process are NOT certified.

Unit of inference is a New York *eligible observed day*, never an individual
quote or an independent stock-tick. All 12 hypotheses are resampled together.
"""
from __future__ import annotations
import argparse, json, math, random, statistics
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
from audit_forward_runs import audit_all
from prospective import RULES, verify_chain, moment
from portfolio_audit import replay_portfolio
from execution_path_audit import audit_all as audit_execution_paths

ET=ZoneInfo('America/New_York')
MIN_BOOK_MINUTES=120     # At least two separate stocks need this many valid book minutes/day
MIN_SYMBOLS_PER_DAY=2
MIN_INDEPENDENT_DAYS=30
MIN_DISTINCT_EXIT_SYMBOLS=10
MIN_MODEL_EXITS=400
MAX_INCOMPLETE_FRACTION=.05
MAX_RULE_DAY_SHARE=.35
TEST_DRAWS=2000
COSTS=('8','20','40')


def day_et(value):
    dt=moment(value)
    if dt is None:raise ValueError('INVALID_AWARE_EVENT_TIME')
    return dt.astimezone(ET).date().isoformat()


def t_stat(v):
    if len(v)<2:return 0.0
    mu=statistics.fmean(v)
    sd=statistics.stdev(v)
    # Constant positive returns are not evidence of a stochastic distribution.
    return mu/(sd/math.sqrt(len(v))) if sd>1e-10 else 0.0


def joint_day_signflip(panel,seed=20261014,draws=TEST_DRAWS):
    """Exploratory max-T across rules using synchronized day signs.

    Assumes approx sign-symmetric centered day residuals, generally untestable.
    Does not guarantee strict familywise control under real market dependence.
    """
    keys=list(panel)
    if not keys:return {}
    n=len(panel[keys[0]])
    if n<MIN_INDEPENDENT_DAYS or any(len(panel[k])!=n for k in keys):return {}
    obs={k:t_stat(panel[k]) for k in keys}
    centered={k:[x-statistics.fmean(panel[k]) for x in panel[k]] for k in keys}
    rnd=random.Random(seed)
    exceed={k:0 for k in keys}
    for _ in range(draws):
        signs=[1 if rnd.getrandbits(1) else -1 for _ in range(n)]
        max_null=max(t_stat([a*b for a,b in zip(centered[k],signs)]) for k in keys)
        for k in keys:
            if max_null>=obs[k]-1e-12:exceed[k]+=1
    return {k:{'observedDayT':round(obs[k],4),
               'approxFamilyAdjustedOneSidedP':round((exceed[k]+1)/(draws+1),5),
               'draws':draws,
               'method':'EXPLORATORY_SYNCHRONIZED_DAY_SIGN_FLIP_MAX_T_NOT_CERTIFIED'}
            for k in keys}


def study_record(recordings):
    root=Path(recordings)
    independent=audit_all(root)
    all_runs=sorted(root.glob('*/forward_shadow'))
    # Fail closed as soon as a run cannot be verified, but still return full diagnostics.
    minutes=defaultdict(set)  # (ET date, ticker) -> unique observed book-minute IDs
    exits=[];entries=[];unresolved=defaultdict(int);raw_bound=True;all_days=set()
    portfolio_runs=[]
    if independent['allRunsAuditable']:
        for run in all_runs:
            plan,events=verify_chain(run)
            portfolio_runs.append((run.parent.name,events))
            metadata=run.parent/'metadata.json'
            try:
                meta=json.loads(metadata.read_text())
                raw_bound=raw_bound and bool(meta.get('eventsSha256')) and meta.get('closed') is not False
            except (ValueError,OSError):raw_bound=False
            for e in events:
                typ=e['type']
                if typ=='BOOK_MINUTE':
                    d=day_et(e['at']);all_days.add(d)
                    minute=e['etMinute']
                    if not minute.startswith(d+' '):raise ValueError('BOOK_MINUTE_WRONG_SESSION_DATE')
                    minutes[(d,e['code'])].add(minute)
                elif typ=='PAPER_ENTRY':entries.append(e)
                elif typ=='PAPER_EXIT':exits.append(e)
                elif typ in ('EXPIRED','CANCELLED','CENSORED'):
                    unresolved[typ]+=1
    portfolio=replay_portfolio(portfolio_runs)
    # Additional INDEPENDENT path evidence, beyond endpoint price crosschecks.
    # Every run is included; one uncertified completed path fails research promotion.
    execution_paths=audit_execution_paths(root) if independent['allRunsAuditable'] else {
        'verdict':'NO_GO_REAL_TRADING', 'pathEvidenceAllComplete':False,
        'runsFound':len(all_runs), 'runResults':[], 'reason':'INDEPENDENT_RUN_AUDIT_FAILED'}
    # Portfolio P&L is never broker P&L. An unfinished shadow model means UNKNOWN P&L.
    portfolio_valid=bool(independent['allRunsAuditable'] and independent['runInventoryPresent']
                         and portfolio['diagnosticComplete'] and portfolio['closedLongModelExits']>0 and execution_paths['pathEvidenceAllComplete'])
    per_day=defaultdict(dict)
    for (d,s),m in minutes.items():per_day[d][s]=len(m)
    eligible_days=sorted(d for d,table in per_day.items()
                         if sum(n>=MIN_BOOK_MINUTES for n in table.values())>=MIN_SYMBOLS_PER_DAY)
    qualified=set(eligible_days)
    qexits=[e for e in exits if day_et(e['entryAt']) in qualified]
    exit_symbols={e['code'] for e in qexits}
    by_rule_day=defaultdict(lambda:defaultdict(list))
    by_rule_total=defaultdict(list)
    for e in qexits:
        if not all(k in e.get('netQuoteModelBps',{}) for k in COSTS):raise ValueError('MISSING_COST_SCENARIOS')
        d=day_et(e['entryAt'])
        by_rule_day[e['rule']][d].append(e)
        by_rule_total[e['rule']].append(e)
    # Total consecutive valid day windows are crucial; no single day of thousands
    # of callbacks should count as statistical replication.
    eligible_n=len(eligible_days)
    samples=len(qexits)
    exited_ids={x['id'] for x in exits}
    censored=sum(e['id'] not in exited_ids for e in entries)
    frac=censored/max(1,len(entries))
    global_floor=(eligible_n>=MIN_INDEPENDENT_DAYS and len(exit_symbols)>=MIN_DISTINCT_EXIT_SYMBOLS
                  and samples>=MIN_MODEL_EXITS and frac<=MAX_INCOMPLETE_FRACTION
                  and independent['allRunsAuditable'] and independent['runInventoryPresent'] and raw_bound
                  and execution_paths['pathEvidenceAllComplete'])
    panel={};per_rule=[]
    for rule in RULES:
        name=rule['id']
        records=by_rule_total[name];groups=by_rule_day[name]
        # Zero means a rule had no *recorded model exit* on an eligible day;
        # this is a day-level diagnostic, NOT a book-level backtest PnL.
        series={k:[statistics.fmean(e['netQuoteModelBps'][k] for e in groups[d]) if groups.get(d) else 0.0
                   for d in eligible_days] for k in COSTS}
        panel[name]=series['40']
        shares_by_day=[len(groups[d]) for d in eligible_days]
        dominance=max(shares_by_day)/sum(shares_by_day) if sum(shares_by_day) else None
        active_days=sum(x>0 for x in shares_by_day)
        long_count=sum(e.get('direction')==1 for e in records)
        per_rule.append({'rule':name,'exitsOnEligibleDays':len(records),'activeEtDays':active_days,
                         'symbols':len({e['code'] for e in records}),
                         'longModelExits':long_count,'shortNotBorrowVerifiedExits':len(records)-long_count,
                         'largestDayExitFraction':round(dominance,4) if dominance is not None else None,
                         'meanDayNormalizedBps':{k:round(statistics.fmean(series[k]),4) if series[k] else None
                                                 for k in COSTS},
                         'cost40PositiveAcrossDayProxy':bool(series['40'] and statistics.fmean(series['40'])>0),
                         'dayConcentrationPass':bool(dominance is not None and dominance<=MAX_RULE_DAY_SHARE)})
    tests=joint_day_signflip(panel) if global_floor else {}
    candidates=[]
    for row in per_rule:
        t=tests.get(row['rule'])
        # Do not advance a rule based only on unborrowable shorts or positions
        # conflicting with a single capital-limited account.
        capital_pnl=portfolio['ruleClosedOnlyPnlUsd'].get(row['rule'],{}).get('40',0)
        if portfolio_valid and capital_pnl>0 and t and t['approxFamilyAdjustedOneSidedP']<.05 and row['dayConcentrationPass'] and \
           row['activeEtDays']>=20 and row['cost40PositiveAcrossDayProxy']:
            candidates.append(row['rule'])
        row['exploratoryAdjustedTest']=t
    reasons=[]
    if not all_runs:reasons.append('NO_FORWARD_RUNS')
    if not independent['allRunsAuditable']:reasons.append('INCOMPLETE_OR_UNAUDITABLE_RUNS')
    if not raw_bound:reasons.append('RAW_FINAL_SHA256_NOT_PRESENT_FOR_EVERY_RUN')
    if not independent['runInventoryPresent']:reasons.append('RUN_INVENTORY_ABSENT_OR_NOT_CREATED_BY_THIS_VERSION')
    if eligible_n<MIN_INDEPENDENT_DAYS:reasons.append('TOO_FEW_QUALIFIED_INDEPENDENT_DAYS')
    if len(exit_symbols)<MIN_DISTINCT_EXIT_SYMBOLS:reasons.append('TOO_FEW_DISTINCT_SYMBOLS')
    if samples<MIN_MODEL_EXITS:reasons.append('TOO_FEW_QUOTE_MODEL_EXITS')
    if frac>MAX_INCOMPLETE_FRACTION:reasons.append('TOO_MANY_UNRESOLVED_PAPER_ENTRIES')
    if not portfolio_valid:reasons.append('PORTFOLIO_CAPITAL_BORROW_OR_CENSORSHIP_NOT_CLEARED')
    if not execution_paths['pathEvidenceAllComplete']:reasons.append('UNVERIFIED_INTERMEDIATE_TRADE_PATHS_OR_RAW_QUOTES')
    if not candidates:reasons.append('NO_STATISTICAL_RESEARCH_CANDIDATE')
    return {'verdict':'NO_GO_REAL_TRADING',
            'researchStage':'EXPLORATORY_FAMILY_ADJUSTED_CANDIDATES_ONLY' if candidates else 'NO_GO_INDEPENDENT_EVIDENCE',
            'paperAlphaResearchCandidates':candidates, 'notBrokerExecuted':True,
            'liveOrdersEnabled':False,'certifiedProfitability':False,
            'runAudit':{'found':independent['runsFound'],'validComplete':independent['validCompleteRuns'],
                        'incompleteOrCorrupt':independent['incompleteOrCorrupt'],
                        'allAuditable':independent['allRunsAuditable'],
                        'inventoryPresent':independent['runInventoryPresent'],
                        'allRawRunHashesPresent':raw_bound},
            'bookMinutesObservedOnEtDays':len(minutes),'daysWithAnyValidBookMinute':len(all_days),
            'eligibleFullerEtDays':eligible_n,'minObservedMinutesPerSymbol':MIN_BOOK_MINUTES,
            'minSymbolsPerEligibleDay':MIN_SYMBOLS_PER_DAY,
            'eligibleDays':eligible_days,
            'modelExitsOnEligibleDays':samples, 'distinctExitSymbols':len(exit_symbols),
            'paperEntriesAllRuns':len(entries),'unfinishedPaperEntryFraction':round(frac,4),
            'rejectedOrCensoredEvents':dict(unresolved),
            'exitsOnIneligibleDays':len(exits)-len(qexits),
            'hypothesesTested':len(RULES),'multipleTesting':'JOINT_DAY_MAX_T_EXPLORATORY',
            'capitalConstrainedPortfolioStress':portfolio,'portfolioStressDiagnosticComplete':portfolio_valid,
            'executionPathEvidence':execution_paths,
            'reasons':reasons,'perRule':per_rule,
            'limitations':['同步日期聚类降低了逐笔伪独立问题，但不证明独立交易日具有平稳同分布特性',
                           '中心化符号翻转依赖近似对称分布假设；p值探索性、非严格Alpha认证',
                           '没有逐笔真实成交回报、借券资格、盘口完整覆盖、冲击及排队模型',
                           '盘中信号重叠、选择股票池、缺失/删失与多次查看结果仍可能扭曲估计',
                           '全部出场报价必须通过持仓期间的独立逐笔盘口与基准报价路径审计',
                           '需要未来冻结测试集与真实执行审计才可评估交易资格']}


def main():
    p=argparse.ArgumentParser(description='全运行、日聚类、联合假设审计；永远不会解锁实盘')
    p.add_argument('--recordings',type=Path,default=Path(__file__).parent/'recordings')
    p.add_argument('--out',type=Path,default=Path(__file__).parent/'research'/'forward_audits'/'alpha_gate.json')
    a=p.parse_args()
    report=study_record(a.recordings)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('verdict','researchStage','eligibleFullerEtDays','modelExitsOnEligibleDays','reasons')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
