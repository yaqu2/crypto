#!/usr/bin/env python3
"""Audit every registered recording's full-session quote provenance.

A run may pass byte integrity yet fail full-market-minute coverage; both are needed
before research even begins. Missing/tampered runs invalidate the aggregate.
"""
import argparse,json
from pathlib import Path
from audit_recordings import audit_all_recordings
from session_coverage import audit_recording_coverage
from cohort_lineage import check_cohort_lineage


def audit_every_run(root,benchmark='US.SPY'):
    root=Path(root)
    raw=audit_all_recordings(root)
    output=[];qualified=set()
    for item in raw['sessions']:
        row={'runId':item['runId']}
        if item['status']!='PASS_LOCAL_FILE_INTEGRITY':
            row.update(status='NO_GO_RAW_INTEGRITY',reason=item.get('reason',item['status']))
        else:
            try:
                report=audit_recording_coverage(root/item['runId'],benchmark=benchmark)
                lineage=check_cohort_lineage(root/item['runId'],root.parent)
                row.update(status=report['status'],days=report['days'],watchlistLineage=lineage)
                qualified.update(d['marketDateET'] for d in report['days']
                                 if d['status']=='RESEARCH_FEED_COVERAGE_DAY_ONLY')
            except Exception as exc:
                row.update(status='NO_GO_COVERAGE_AUDIT_FAILURE',reason=str(exc)[:250])
        output.append(row)
    success=(raw['verdict']=='PASS_LOCAL_FILE_INTEGRITY_ONLY' and len(qualified)>=30
             and all(x['status'] not in ('NO_GO_RAW_INTEGRITY','NO_GO_COVERAGE_AUDIT_FAILURE') for x in output))
    return {'status':'RESEARCH_FEED_SAMPLE_DAYS_ONLY' if success else 'NO_GO_FEED_EVIDENCE',
            'rawIntegrityVerdict':raw['verdict'],'distinctQualifiedDates':len(qualified),
            'requiredQualifiedDates':30,'benchmark':benchmark,'runs':output,
            'profitCertified':False,'tradeEnabled':False,
            'caution':'Even a pass is data coverage only, not an alpha or execution certificate.'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--recordings',type=Path,default=Path(__file__).parent/'recordings')
    p.add_argument('--benchmark',default='US.SPY')
    p.add_argument('--out',type=Path,default=Path(__file__).parent/'research'/'forward_audits'/'session_coverage.json')
    a=p.parse_args();r=audit_every_run(a.recordings,a.benchmark)
    a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':r['status'],'rawIntegrity':r['rawIntegrityVerdict'],
                      'distinctQualifiedDates':r['distinctQualifiedDates'],
                      'runStatuses':[{'runId':x['runId'],'status':x['status']} for x in r['runs']]},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
