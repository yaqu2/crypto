#!/usr/bin/env python3
"""Audit EVERY recorded forward shadow run, including incomplete/corrupt ones.

Not a broker trade PnL evaluator. Refuses the common selection trap of looking
only at runs that produced winning models. All runs remain reported by ID.
"""
import argparse,json,statistics
from pathlib import Path
from collections import defaultdict
from zoneinfo import ZoneInfo
from forward_report import report
from source_crosscheck import crosscheck
from prospective import verify_chain,moment,RULES
from run_inventory import read_inventory

ET=ZoneInfo('America/New_York')


def audit_all(folder):
    directory=Path(folder)
    runs=sorted(directory.glob('*/forward_shadow'))
    inventory_error=None;missing=[];inventory_present=False
    try:
        registered,inventory_present=read_inventory(directory)
        shadow_ids={r.parent.name for r in runs}
        missing=[e['runId'] for e in registered if e['shadowExpected'] and e['runId'] not in shadow_ids]
    except (OSError,ValueError,KeyError,TypeError) as exc:
        inventory_error=str(exc)[:220]

    details=[];by_rule=defaultdict(list);days=set();symbols=set()
    for r in runs:
        item={'runId':r.parent.name}
        try:
            result=report(r)
            lineage=crosscheck(r)
            if lineage['status']!='MATCHED_LOCAL_CALLBACK_RECORDS':raise ValueError('NO_RAW_CALLBACK_EVIDENCE')
            item['rawBookCrosscheck']=lineage['bookTimesChecked']
            item.update({'status':'VALID_COMPLETE' if result['completeRun'] else 'INCOMPLETE_NO_GO',
                         'eventCount':result['chainEvents'],'countedExits':result['eventCounts'].get('PAPER_EXIT',0)})
            if result['completeRun']:
                plan,events=verify_chain(r)
                for x in events:
                    if x['type']=='PAPER_EXIT':
                        day=moment(x['entryAt']).astimezone(ET).date().isoformat()
                        by_rule[x['rule']].append((day,x));days.add(day);symbols.add(x['code'])
        except Exception as exc:
            item.update({'status':'CORRUPT_OR_UNAUDITABLE_NO_GO','reason':str(exc)[:200]})
        details.append(item)
    for rid in missing:
        details.append({'runId':rid,'status':'MISSING_SHADOW_DIRECTORY_NO_GO',
                        'reason':'RUN_INVENTORY_EXPECTS_SHADOW_BUT_DIRECTORY_MISSING'})
    if inventory_error:
        details.append({'runId':'__INVENTORY__','status':'INVENTORY_CORRUPT_NO_GO','reason':inventory_error})
    broken=any(r['status']!='VALID_COMPLETE' for r in details)
    table=[]
    for rule in RULES:
        o=by_rule[rule['id']];date_groups=defaultdict(list)
        for day,x in o:date_groups[day].append(x['netQuoteModelBps']['20'])
        by_day=[statistics.fmean(v) for v in date_groups.values()]
        table.append({'rule':rule['id'],'modelExits':len(o),'independentEtDays':len(date_groups),
                      'totalSymbols':len({x['code'] for _,x in o}),
                      'meanNet20BpsAcrossExits':round(statistics.fmean(x['netQuoteModelBps']['20'] for _,x in o),4) if o else None,
                      'meanEtDayClusterNet20Bps':round(statistics.fmean(by_day),4) if by_day else None,
                      'meanNet40BpsAcrossExits':round(statistics.fmean(x['netQuoteModelBps']['40'] for _,x in o),4) if o else None})
    return {'verdict':'NO_GO_LIVE_TRADING','runsFound':len(runs)+len(missing),'validCompleteRuns':sum(x['status']=='VALID_COMPLETE' for x in details),
            'incompleteOrCorrupt':sum(x['status']!='VALID_COMPLETE' for x in details),
            'uniqueEtDays':len(days),'uniqueSymbols':len(symbols),
            'allRunsAuditable':bool(runs) and not broken,
            'runInventoryPresent':inventory_present,'inventoryError':inventory_error,
            'minimumForwardCoverageMet':bool(len(days)>=20 and len(symbols)>=10 and not broken),
            'perRun':details,'perRule':table,'liveOrdersEnabled':False,'certifiedAlpha':False,
            'notice':'All run folders are enumerated, including corrupt/incomplete. Crossed quote model outcomes are not broker fills or realized account PnL; results may have cross-stock/time dependence and substantial selection bias.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--recordings',type=Path,default=Path(__file__).parent/'recordings')
    p.add_argument('--out',type=Path,default=Path(__file__).parent/'research'/'forward_audits'/'all_runs.json')
    a=p.parse_args();r=audit_all(a.recordings)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    content=json.dumps(r,indent=2,ensure_ascii=False)+'\n';a.out.write_text(content);print(content)

if __name__=='__main__':main()
