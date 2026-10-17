#!/usr/bin/env python3
"""Audit ALL recorded raw sessions, including those with no profitable paper outcome."""
import argparse,json
from pathlib import Path
from run_inventory import read_inventory
from segment_store import verify_recording


def audit_all_recordings(root):
    root=Path(root)
    try:registered,present=read_inventory(root)
    except Exception as exc:
        return {'verdict':'NO_GO_RAW_INTEGRITY','inventoryError':str(exc),'sessions':[],
                'liveOrdersEnabled':False}
    expected={x['runId'] for x in registered}
    actual={p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
    report=[]
    for rid in sorted(expected|actual):
        item={'runId':rid}
        if rid not in expected:
            item.update(status='NO_GO_UNREGISTERED_RUN')
        elif rid not in actual:
            item.update(status='NO_GO_MISSING_RUN_DIRECTORY')
        else:
            try:
                result=verify_recording(root/rid,strict=True)
                item.update(status='PASS_LOCAL_FILE_INTEGRITY',segments=result['segmentCount'],
                            events=result['events'],bytes=result['bytes'])
            except Exception as exc:item.update(status='NO_GO_CORRUPT_OR_INCOMPLETE',reason=str(exc)[:260])
        report.append(item)
    good=bool(present and report and all(x['status']=='PASS_LOCAL_FILE_INTEGRITY' for x in report))
    return {'verdict':'PASS_LOCAL_FILE_INTEGRITY_ONLY' if good else 'NO_GO_RAW_INTEGRITY',
            'inventoryPresent':present,'sessionsTotal':len(report),'sessions':report,
            'marketFeedCompletenessProven':False,'liveOrdersEnabled':False,
            'notice':'Local hash and row sequence cannot certify exchange coverage, SIP/NBBO or trading profits.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--recordings',type=Path,default=Path(__file__).parent/'recordings')
    p.add_argument('--out',type=Path,default=Path(__file__).parent/'research'/'forward_audits'/'raw_integrity.json')
    a=p.parse_args();r=audit_all_recordings(a.recordings)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'verdict':r['verdict'],'sessionsTotal':r.get('sessionsTotal'),
                      'sessions':r.get('sessions')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
