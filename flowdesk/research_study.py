#!/usr/bin/env python3
"""Strict, read-only, two-step offline order-book event study.

We do NOT have exchange timestamps, true order events, queue position, or filled
orders. Receipt-time observations and crossed top-of-book paper fills are ONLY a
sensitivity model, never a verified PnL/backtest/alpha certification.
"""
import argparse,bisect,hashlib,json,math,statistics,os
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
from feed_guard import standard_market_day,ET

HORIZONS=(60,300)
LEVELS=(1,5)
STYLES=('follow','revert')
CANDIDATES=[dict(level=level,horizon=horizon,style=style,threshold=.35)
            for level in LEVELS for horizon in HORIZONS for style in STYLES]
EXTRA_COST_BPS=8.0  # Assumed round trip beyond crossing bid/ask; not measured.
MIN_DAYS_TOTAL=35
MIN_HOLDOUT_DAYS=12
MIN_HOLDOUT_SYMBOLS=20
MIN_HOLDOUT_OBSERVATIONS=120


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def time_(value):
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (TypeError,ValueError,OverflowError):return None


def fnum(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None


def is_session(t):
    e=t.astimezone(ET)
    good,close=standard_market_day(e.date())
    minute=e.hour*60+e.minute
    return good and 570<=minute<(close or 0)*60


def normalize_book(row):
    data=row.get('data') or {}
    if not isinstance(data,dict):return None
    bids=data.get('Bid') or [];asks=data.get('Ask') or []
    try:
        b=sorted([(fnum(z[0]),fnum(z[1])) for z in bids],reverse=True)
        a=sorted([(fnum(z[0]),fnum(z[1])) for z in asks])
        if not b or not a or any(not p or p<=0 or v is None or v<0 for p,v in b+a):return None
        if b[0][0]>=a[0][0]:return None
        spread=(a[0][0]-b[0][0])/((a[0][0]+b[0][0])/2)*10000
        if spread>200:return None
        im={}
        for n in LEVELS:
            bs=sum(v for _,v in b[:n]);ass=sum(v for _,v in a[:n])
            im[n]=(bs-ass)/(bs+ass) if bs+ass>0 else None
        return dict(bid=b[0][0],ask=a[0][0],imbalance=im,spread=spread)
    except (ValueError,TypeError,IndexError):return None


def audit_events(path,collect=True):
    by_symbol=defaultdict(list);dates=set();seen_symbols=set();total=0;books=0;regular=0;invalid=0
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            total+=1
            if not line.strip():continue
            obj=json.loads(line)
            if obj.get('kind')!='book' or obj.get('origin')!='push':continue
            books+=1
            data=obj.get('data') or {};code=str(data.get('code') or '') if isinstance(data,dict) else ''
            t=time_(obj.get('callbackReceivedAt') or obj.get('receivedAt'))
            if not code.startswith('US.') or not t or not is_session(t):continue
            regular+=1
            x=normalize_book(obj)
            if x is None:invalid+=1;continue
            day=t.astimezone(ET).date().isoformat();dates.add(day);seen_symbols.add(code)
            if collect:by_symbol[code].append((t.timestamp(),day,x))
    if collect:
        for code in by_symbol:by_symbol[code].sort(key=lambda x:x[0])
    return {'events':total,'pushBookEvents':books,'regularBookEvents':regular,'invalidRegularBooks':invalid,
            'regularUsableDates':sorted(dates),'regularSymbols':sorted(seen_symbols),
            'symbolCount':len(seen_symbols),'dayCount':len(dates),
            'marketStructureSourceTimestamp':'CALLBACK_OR_LOCAL_RECEIPT_NOT_EXCHANGE_VERIFIED',
            'trueExecutionFillsAvailable':False},by_symbol


def observations(by_symbol,days,conf):
    horizon=conf['horizon'];level=conf['level'];threshold=conf['threshold'];style=conf['style']
    day_set=set(days);out=[]
    for code,events in by_symbol.items():
        rows=[x for x in events if x[1] in day_set]
        if len(rows)<3:continue
        ts=[x[0] for x in rows]
        skip_until_by_day={}
        for at,(t,day,b) in enumerate(rows):
            if t<skip_until_by_day.get(day,0):continue
            imb=b['imbalance'][level]
            if imb is None or abs(imb)<threshold:continue
            direction=(1 if imb>0 else -1)*(1 if style=='follow' else -1)
            start=bisect.bisect_left(ts,t+2,lo=at+1)
            if start>=len(rows):continue
            en_time,en_day,entry=rows[start]
            if en_day!=day or en_time>t+15:continue
            end=bisect.bisect_left(ts,en_time+horizon,lo=start+1)
            if end>=len(rows):continue
            ex_time,ex_day,exit_=rows[end]
            if ex_day!=day or ex_time>en_time+horizon+15:continue
            entry_p=entry['ask'] if direction==1 else entry['bid']
            exit_p=exit_['bid'] if direction==1 else exit_['ask']
            net_bps=direction*(exit_p/entry_p-1)*10000-EXTRA_COST_BPS
            mid_entry=(entry['bid']+entry['ask'])/2
            mid_exit=(exit_['bid']+exit_['ask'])/2
            gross_mid=direction*(mid_exit/mid_entry-1)*10000
            out.append({'day':day,'symbol':code,'netBps':round(net_bps,5),'midBps':round(gross_mid,5),
                        'signalAt':datetime.fromtimestamp(t,timezone.utc).isoformat(),
                        'entryAt':datetime.fromtimestamp(en_time,timezone.utc).isoformat(),
                        'exitAt':datetime.fromtimestamp(ex_time,timezone.utc).isoformat(),
                        'entryPrice':entry_p,'exitPrice':exit_p})
            skip_until_by_day[day]=ex_time+2 # non-overlap per symbol and config
    return out


def daily_statistics(rows):
    days=defaultdict(list)
    for r in rows:days[r['day']].append(r['netBps'])
    means=[statistics.fmean(vals) for vals in days.values()]
    mean=statistics.fmean(means) if means else None
    sd=statistics.stdev(means) if len(means)>1 else None
    tstat=(mean/(sd/math.sqrt(len(means)))) if sd and sd>1e-12 else None
    # Approximate one-sided normal p value; Holm correction across frozen candidates.
    p=0.5*math.erfc(tstat/math.sqrt(2)) if tstat is not None else None
    return {'independentDays':len(days),'symbols':len({r['symbol'] for r in rows}),
            'observations':len(rows),'meanDailyClusterNetBps':mean,
            'meanObservationNetBps':statistics.fmean(r['netBps'] for r in rows) if rows else None,
            'meanObservationMidBps':statistics.fmean(r['midBps'] for r in rows) if rows else None,
            'approxOneSidedP':p,'caveat':'Normal approximation to daily cluster means; no exact independence proof.'}


def audit(path):
    r,_=audit_events(path,collect=False)
    r['gate']='READY_FOR_FROZEN_STUDY' if r['dayCount']>=MIN_DAYS_TOTAL and r['symbolCount']>=MIN_HOLDOUT_SYMBOLS else 'NO_GO_INDEPENDENT_SAMPLE'
    r['estimatedMinDays']=MIN_DAYS_TOTAL
    r['liveOrdersEnabled']=False
    return r


def prepare(path,out):
    audit_result=audit(path)
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    plan_path=out/'frozen_plan.json'
    if plan_path.exists():raise RuntimeError('Plan already exists; do not overwrite an experiment')
    if audit_result['gate']!='READY_FOR_FROZEN_STUDY':
        raise RuntimeError(f"NO_GO_INDEPENDENT_SAMPLE: {audit_result['dayCount']} days, {audit_result['symbolCount']} symbols")
    dates=audit_result['regularUsableDates']
    split=round(len(dates)*.7)
    if len(dates)-split<MIN_HOLDOUT_DAYS:raise RuntimeError('Too few holdout days')
    plan={'version':'1.0','rawSha256':sha(path),'studySourceSha256':sha(__file__),
          'splitDate':dates[split],'trainingDays':split,'holdoutDays':len(dates)-split,
          'candidates':CANDIDATES,'extraCostBps':EXTRA_COST_BPS,
          'sampleRule':'One nonoverlapping exposure per symbol; 2-15s future entry; 60/300s crossed bid/ask exits; expiry tolerance 15s',
          'minimumHoldoutSymbols':MIN_HOLDOUT_SYMBOLS,'minimumHoldoutDays':MIN_HOLDOUT_DAYS,
          'minimumHoldoutObservations':MIN_HOLDOUT_OBSERVATIONS,
          'sourceTimeKnownVerified':False,'liveOrdersEnabled':False}
    plan_path.write_text(json.dumps(plan,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    (out/'plan_seal.sha256').write_text(sha(plan_path)+'\n')
    return plan


def finalize(path,out):
    out=Path(out)
    plan_path=out/'frozen_plan.json'
    lock=out/'holdout_consumed.lock'
    # seal before we see any holdout performance; fail closed after attempt
    if not plan_path.exists() or not (out/'plan_seal.sha256').exists():raise RuntimeError('Need prepare first')
    if (out/'plan_seal.sha256').read_text().strip()!=sha(plan_path):
        raise RuntimeError('Frozen plan hash mismatch; parameters or split were altered')
    p=json.loads(plan_path.read_text())
    if p['rawSha256']!=sha(path) or p['studySourceSha256']!=sha(__file__) or p['candidates']!=CANDIDATES or p['extraCostBps']!=EXTRA_COST_BPS:
        raise RuntimeError('Frozen data/code/rules hash mismatch')
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        os.write(fd,b'Holdout access consumed; no reruns in the same experiment folder.\n')
    finally:os.close(fd)
    evidence,by_symbol=audit_events(path)
    holdout=[d for d in evidence['regularUsableDates'] if d>=p['splitDate']]
    results=[]
    for c in CANDIDATES:
        stats=daily_statistics(observations(by_symbol,holdout,c))
        results.append({'specification':c,**stats})
    pvals=sorted((r['approxOneSidedP'],i) for i,r in enumerate(results) if r['approxOneSidedP'] is not None)
    significant=set()
    for rank,(pv,i) in enumerate(pvals):
        if pv<=.05/(len(CANDIDATES)-rank):significant.add(i)
        else:break
    for i,r in enumerate(results):
        sample_ok=(r['independentDays']>=MIN_HOLDOUT_DAYS and r['symbols']>=MIN_HOLDOUT_SYMBOLS and
                   r['observations']>=MIN_HOLDOUT_OBSERVATIONS)
        r['sampleGate']='PASS_OBSERVATIONAL_SAMPLE' if sample_ok else 'NO_GO_SAMPLE'
        r['holmOneSided05']=bool(i in significant and sample_ok)
        r['certifiedProfitable']=False
    report={'overallGate':'NO_GO_CERTIFIED_ALPHA', 'dataGate':evidence,
            'holdoutStartDate':p['splitDate'],'frozenPlanSha256':sha(plan_path),
            'candidateCount':len(CANDIDATES),'hypothesesTested':len(CANDIDATES),
            'results':results,'returnType':'CROSSED_QUOTE_SIMULATION_NOT_EXECUTED_PNL',
            'fatalLimitations':['No validated exchange timestamps on book events','No broker executions, queue position or market impact',
                                'Current-venue sample and short stock universe ≠ whole US equities',
                                'Approx daily normal p values are not proof of independence; cross-symbol exposures correlated',
                                'One test window alone cannot establish sustainable out-of-sample performance'],
            'liveOrdersEnabled':False}
    (out/'holdout_result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description='Fail-closed pre-registered order-book study on real events')
    p.add_argument('command',choices=['audit','prepare','finalize']);p.add_argument('--events',type=Path,required=True)
    p.add_argument('--out',type=Path,default=Path(__file__).parent/'research'/'frozen_study')
    a=p.parse_args()
    r=audit(a.events) if a.command=='audit' else prepare(a.events,a.out) if a.command=='prepare' else finalize(a.events,a.out)
    output=json.dumps(r,ensure_ascii=False,indent=2)
    if a.command=='audit':
        # --out previously accepted but ignored, silently losing the QA artifact.
        a.out.parent.mkdir(parents=True,exist_ok=True)
        target=a.out/'audit_result.json' if a.out.suffix=='' else a.out
        temp=target.with_name(target.name+'.tmp');temp.write_text(output+'\n');temp.replace(target)
    print(output)

if __name__=='__main__':main()
