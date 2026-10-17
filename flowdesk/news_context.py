#!/usr/bin/env python3
"""Verified as-of news research: no hindsight, no causal claims, no trading orders."""
from __future__ import annotations
import argparse,json,sys
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
from news_feed import NEWS_LOG, STATUS, DATA, read_log, clock
from multiscale_lab import usable_context

SECTORS={
 'OPTICAL':{'AAOI','LITE','COHR','CIEN'},
 'MEMORY':{'MU','SNDK','WDC','STX'},
 'AI_CHIPS':{'NVDA','AMD','AVGO','MRVL','MU'},
 'BENCHMARKS':{'SPY','QQQ','SMH','SOXX','IWM','XLK','XSD'},
}

def asof_news(path=NEWS_LOG,*,asof=None,max_age_minutes=120,top_n=30):
    asof=clock(asof or datetime.now(timezone.utc).isoformat())
    if not 1<=max_age_minutes<=10080:raise ValueError('INVALID_WINDOW')
    events=read_log(path)
    items=usable_context(events,asof.isoformat())
    recent=[];counts=Counter();subjects=Counter();topic_counts=Counter()
    for row in items['known']:
        v=row['value'];available=clock(row['available_at'])
        delta=(asof-available).total_seconds()/60
        if delta<0:raise ValueError('FUTURE_EVENT_ASOF')
        if delta>max_age_minutes:continue
        tags=v.get('topics',[]);ticks=v.get('tickers',[])
        for name,members in SECTORS.items():
            if any(x in members for x in ticks):subjects[name]+=1
        for tag in tags:topic_counts[tag]+=1
        counts[row['provider']]+=1
        recent.append({'sourceId':row['source_id'],'provider':row['provider'],
            'title':v.get('title'),'url':v.get('url'),'tickers':ticks,
            'topics':tags,'publishedAt':row['published_at'],'localAvailableAt':row['available_at'],
            'localAgeMinutes':round(delta,3),'revision':row['revision'],
            'firstObservedNotVendorFirstSeen':True})
    recent.sort(key=lambda x:(x['localAvailableAt'],x['sourceId']),reverse=True)
    return {'status':'OBSERVED_NEWS_RESEARCH_ONLY' if events else 'NO_GO_NO_NEWS_OBSERVATIONS',
            'asof':asof.isoformat(),'sourceRecords':len(events),'verifiedHashChain':True if events else None,
            'matchedArticles':len(recent),'recentArticles':recent[:top_n],
            'bySource':dict(counts),'bySectorMentions':dict(subjects),'byHeadlineTopic':dict(topic_counts),
            'discarded':items['discarded'],'providerAvailabilityAudited':False,
            'publicationTimeNotReceiptTime':True,'headlineTopicsNotCausalityOrSentiment':True,
            'executableNewsEdgeProven':False,'tradeSignal':False,'profitCertified':False,
            'warning':'Historical retrieval is known only when locally first received; news mentions do not establish causes or predictive power.'}



def minute_news_features(observations,events,*,max_rows=10000):
    """Causal news context for *finished* minute order-flow observations.

    A minute's decision time is its minute-end; news first locally observed later
    must not enter that minute's feature. This is research ONLY, not predictive alpha.
    """
    if len(observations)>max_rows:raise ValueError('TOO_MANY_MINUTES_FOR_CAUSAL_JOIN')
    versions=sorted(events,key=lambda r:(clock(r['first_seen_at']),r['key'],r['revision']))
    ordered=sorted(observations,key=lambda r:(clock(r['minuteUtc']),r.get('symbol','')))
    previous={};ptr=0;result=[]
    for row in ordered:
        decision=clock(row['minuteUtc'])+timedelta(minutes=1)
        while ptr<len(versions) and clock(versions[ptr]['first_seen_at'])<=decision:
            r=versions[ptr];ptr+=1
            # Refuse to treat a retrospective publication as known before receipt.
            if clock(r['published_at'])>clock(r['first_seen_at']):raise ValueError('BAD_NEWS_CLOCK_ORDER')
            old=previous.get(r['key'])
            if old is None or (clock(r['first_seen_at']),r['revision'])>(clock(old['first_seen_at']),old['revision']):
                previous[r['key']]=r
        sym=str(row.get('symbol','')).removeprefix('US.')
        sector=next((name for name,names in SECTORS.items() if sym in names and name!='BENCHMARKS'),None)
        windows={};latest=None
        for mins in (5,30,120):
            eligible=[a for a in previous.values() if 0<=(decision-clock(a['first_seen_at'])).total_seconds()<=mins*60]
            stock=[a for a in eligible if sym and sym in a['value'].get('tickers',[])]
            sector_hits=[a for a in eligible if sector and any(s in SECTORS[sector] for s in a['value'].get('tickers',[]))]
            windows[str(mins)+'m']={'marketArticles':len(eligible),'symbolMentions':len(stock),
                                    'sectorMentions':len(sector_hits)}
        result.append({'symbol':row.get('symbol'),'minuteUtc':row['minuteUtc'],
                       'decisionTimeUtc':decision.isoformat(), 'newsWindowCounts':windows,
                       'replayAvailabilityClock':'LOCAL_RECEIVED_ONLY',
                       'newsPredictivePowerVerified':False})
    return result

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--asof',help='ISO8601 aware time; defaults to now')
    p.add_argument('--log',type=Path,default=NEWS_LOG)
    p.add_argument('--max-age-minutes',type=int,default=120)
    p.add_argument('--out',type=Path,default=DATA/'news_research_report.json')
    p.add_argument('--microstructure-report',type=Path,help='Optional v2.6 multiscale report with minute observations')
    args=p.parse_args(argv)
    try:
        report=asof_news(args.log,asof=args.asof,max_age_minutes=args.max_age_minutes)
        if args.microstructure_report:
            raw=json.loads(args.microstructure_report.read_text(encoding='utf-8'))
            obs=raw.get('microstructure',{}).get('observations',[])
            report['minuteJoinedFeatures']=minute_news_features(obs,read_log(args.log))
            report['minuteFeatureWarning']='Market minute indicators and news as-of features are descriptive, not validated alpha.'
    except (ValueError,OSError) as exc:
        print(json.dumps({'status':'NO_GO_NEWS_INTEGRITY','error':str(exc)},ensure_ascii=False));return 2
    args.out.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.out.with_suffix('.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');tmp.replace(args.out)
    print(json.dumps({'status':report['status'],'matchedArticles':report['matchedArticles'],
                      'bySectorMentions':report['bySectorMentions'],'out':str(args.out)},ensure_ascii=False));return 0 if report['sourceRecords'] else 2

if __name__=='__main__':raise SystemExit(main())
