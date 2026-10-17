#!/usr/bin/env python3
"""FlowDesk v2.5: chronological, read-only sector reversal model challenger.

This is an *exploratory* label/prediction research pipeline, NEVER live trading
or fill evidence. ONLY hash-audited, source-time validated daily scan sessions
from sector_regime_lab.load_sessions are accepted by the CLI.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median, pstdev

from sector_regime_lab import SECTORS, load_sessions, positive

FEATURES = (
    'sector_id', 'sector_ret1', 'sector_ret3', 'sector_ret5',
    'spy_ret1', 'spy_ret3', 'spy_ret5', 'relative_ret1',
    'relative_ret5', 'basket_dispersion1', 'sector_vol5',
    'down_breadth1', 'down_breadth5', 'rebound_from_low5', 'selloff_regime', 'rebound_watch_regime',
)
DEFAULTS = {
    'horizon': 3,
    'label_absolute_return_pct': 2.5,
    'label_excess_over_spy_pct': 2.0,
    'max_calendar_gap_days': 4,
    'train_days': 120,
    'selection_days': 25,
    'calibration_days': 25,
    'test_days': 20,
    'gap_days': 4,
    'step_days': 20,
    'min_outer_folds': 3,
    'min_positive_train': 12,
    'min_negative_train': 12,
}
CHALLENGERS = ('BASE_RATE', 'REGULARIZED_LOGIT', 'HIST_GBDT', 'ORDERED_CATBOOST')


def _pct(a, b):
    return 100.0 * (a / b - 1.0)


def _valid_segment(seq, a, b, max_gap):
    try:
        ds = [date.fromisoformat(seq[i]['day']) for i in range(a, b + 1)]
        return all(0 < (ds[i] - ds[i-1]).days <= max_gap for i in range(1, len(ds)))
    except (ValueError, TypeError, KeyError):
        return False


def _basket_prices(row, codes):
    p = row.get('prices', {})
    vals = [positive(p.get(k)) for k in codes]
    return vals if all(v is not None for v in vals) else None


def _basket_return(cur, older):
    return median([_pct(a, b) for a, b in zip(cur, older)])


def construct_rows(sessions, *, sectors=None, config=None):
    """Build *as-of* sector features and explicit future price labels.

    Incomplete basket series are excluded, never silently imputed with survivors.
    All features depend solely on t and before. label_end_index documents the
    exact later price observation that was used to form the future label.
    """
    c = dict(DEFAULTS, **(config or {}))
    sectors = sectors or SECTORS
    n = len(sessions)
    days = [s.get('day') for s in sessions]
    if days != sorted(set(days)):
        return [], {'status':'NO_GO_BAD_SESSION_SEQUENCE', 'reason':'duplicate or unordered market days'}
    rows=[]; rejected=Counter(); horizon=int(c['horizon'])
    if horizon<1 or horizon>10:
        raise ValueError('INVALID_HORIZON')
    for t in range(5, n-horizon):
        if not _valid_segment(sessions, t-5, t+horizon, int(c['max_calendar_gap_days'])):
            rejected['NON_CONTIGUOUS_OBSERVATION_DAYS'] += len(sectors); continue
        spy = [positive(sessions[j].get('spy')) for j in range(t-5,t+horizon+1)]
        if any(x is None for x in spy):
            rejected['MISSING_SPY'] += len(sectors); continue
        for sector_index,(sector,codes) in enumerate(sectors.items()):
            # Complete fixed basket over the required past AND future path:
            # deliberate fail closed; no dynamic selection of survivor winners.
            matrix=[_basket_prices(sessions[j],codes) for j in range(t-5,t+horizon+1)]
            if any(x is None for x in matrix):
                rejected['INCOMPLETE_SECTOR_BASKET']+=1;continue
            # Historical prices indexed relative to window start (t = 5).
            hist=matrix[:6]; current=hist[-1]
            rets1=[_pct(a,b) for a,b in zip(hist[-1],hist[-2])]
            momentum1=median(rets1)
            momentum3=_basket_return(hist[-1],hist[-4])
            momentum5=_basket_return(hist[-1],hist[0])
            spy1=_pct(spy[5],spy[4]);spy3=_pct(spy[5],spy[2]);spy5=_pct(spy[5],spy[0])
            daily=[_basket_return(hist[j],hist[j-1]) for j in range(1,6)]
            lows=[min(row[i] for row in hist) for i in range(len(codes))]
            rebound=median([_pct(current[i],lows[i]) for i in range(len(codes))])
            selloff = (momentum5 <= -10 and sum(_pct(a,b)<=-10 for a,b in zip(current,hist[0]))/len(codes)>=.60 and momentum5-spy5<=-5)
            prior4=_basket_return(hist[-2],hist[0])
            rebound_watch=(prior4 <= -10 and momentum1>=2 and momentum1-spy1>=1)
            regime='REBOUND_WATCH' if rebound_watch else ('SYNCHRONIZED_SELLOFF' if selloff else 'OTHER')
            f={
                'sector_id':float(sector_index),
                'sector_ret1':momentum1, 'sector_ret3':momentum3,
                'sector_ret5':momentum5,
                'spy_ret1':spy1,'spy_ret3':spy3,'spy_ret5':spy5,
                'relative_ret1':momentum1-spy1,
                'relative_ret5':momentum5-spy5,
                'basket_dispersion1':pstdev(rets1),
                'sector_vol5':pstdev(daily),
                'down_breadth1':sum(x<0 for x in rets1)/len(rets1),
                'down_breadth5':sum(_pct(a,b)<0 for a,b in zip(current,hist[0]))/len(codes),
                'rebound_from_low5':rebound,
                'selloff_regime':float(selloff),
                'rebound_watch_regime':float(rebound_watch),
            }
            end=matrix[-1]
            forward=_basket_return(end,current)
            spy_forward=_pct(spy[-1],spy[5])
            label=int(forward >= float(c['label_absolute_return_pct'])
                      and forward-spy_forward >= float(c['label_excess_over_spy_pct']))
            rows.append({'day':sessions[t]['day'], 'sector':sector, 'asOfRegime':regime,
                         'features':[round(f[k],9) for k in FEATURES],
                         'featureIndex':t, 'labelEndIndex':t+horizon,
                         'labelEndDay':sessions[t+horizon]['day'],
                         'target':label, 'forwardBasketPct':round(forward,6),
                         'forwardExcessSpyPct':round(forward-spy_forward,6),
                         'sourceArchiveId':sessions[t].get('sourceArchiveId')})
    return rows, {'status':'ROWS_CONSTRUCTED_NO_TRAINING', 'totalMarketDays':n,
                  'candidateDays':max(0,n-5-horizon),
                  'acceptedRows':len(rows), 'rejected':dict(rejected),
                  'horizonTradingObservations':horizon,
                  'futureLabelsUsedOnlyForTargets':True,
                  'labelDefinition':'basket future median return >= threshold and relative to SPY >= threshold; observation NOT fill PnL',
                  'survivorshipWarning':'Present-day static declared baskets; NOT historical point-in-time memberships'}


def _brier(labels,prob):
    return sum((float(y)-float(p))**2 for y,p in zip(labels,prob))/len(labels)


def _logloss(labels,prob):
    return -sum(y*math.log(max(1e-9,min(1-1e-9,p)))+(1-y)*math.log(max(1e-9,min(1-1e-9,1-p))) for y,p in zip(labels,prob))/len(labels)


def _constant_train(ys):
    # Beta(2,2) prior prevents probabilities 0 and 1 for rare outcomes.
    p=(sum(ys)+2)/(len(ys)+4)
    return lambda X: [p]*len(X)


def _fit_model(name, data):
    X=[r['features'] for r in data]
    y=[r['target'] for r in data]
    if name=='BASE_RATE':return _constant_train(y)
    if name=='REGULARIZED_LOGIT':
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        model=make_pipeline(StandardScaler(),LogisticRegression(C=.15,max_iter=600,random_state=17))
    elif name=='HIST_GBDT':
        from sklearn.ensemble import HistGradientBoostingClassifier
        model=HistGradientBoostingClassifier(max_iter=110,learning_rate=.035,
                    max_leaf_nodes=7,min_samples_leaf=25,l2_regularization=12,
                    early_stopping=False,random_state=17)
    elif name=='ORDERED_CATBOOST':
        from catboost import CatBoostClassifier
        model=CatBoostClassifier(iterations=170,depth=3,learning_rate=.035,
                    l2_leaf_reg=12,loss_function='Logloss',has_time=True,
                    random_seed=17,thread_count=2,verbose=False,
                    allow_writing_files=False)
    else:raise ValueError('UNKNOWN_MODEL')
    model.fit(X,y)
    return lambda other: [float(p[1]) for p in model.predict_proba(other)]


def _calibrate(model, cal, min_each=8):
    """Platt sigmoid fit ONLY on separate calendar calibration block."""
    labels=[r['target'] for r in cal]
    if min(sum(labels),len(labels)-sum(labels))<min_each:
        return model, 'SKIPPED_INSUFFICIENT_CALIBRATION_CLASSES'
    from sklearn.linear_model import LogisticRegression
    x=model([r['features'] for r in cal])
    logits=[math.log(max(1e-5,min(1-1e-5,p))/(1-max(1e-5,min(1-1e-5,p)))) for p in x]
    calibrator=LogisticRegression(C=1.0,random_state=17)
    calibrator.fit([[v] for v in logits],labels)
    def call(X):
        raw=model(X)
        z=[math.log(max(1e-5,min(1-1e-5,p))/(1-max(1e-5,min(1-1e-5,p)))) for p in raw]
        return [float(a[1]) for a in calibrator.predict_proba([[v] for v in z])]
    return call, 'PLATT_SEPARATE_CHRONOLOGICAL_BLOCK'


def _breakdown(rows, probs, key):
    groups=defaultdict(list)
    for r,p in zip(rows,probs):groups[r[key]].append((r['target'],p))
    return {k:{'n':len(v),'brier':round(_brier([x[0] for x in v],[x[1] for x in v]),6),
               'eventRate':round(sum(x[0] for x in v)/len(v),5)} for k,v in groups.items()}

def _div_by_sector(rows, probs):
    groups=defaultdict(list)
    for r,p in zip(rows,probs):groups[r['sector']].append((r['target'],p))
    return {k:{'n':len(v),'brier':round(_brier([x[0] for x in v],[x[1] for x in v]),6),
               'eventRate':round(sum(x[0] for x in v)/len(v),5)} for k,v in groups.items()}


def walk_forward(rows, sessions, *, config=None, challengers=CHALLENGERS):
    """Model selected on a past selection slice; sigmoid fitted on DIFFERENT
    calibration slice; reported on later untouched-by-fit outer test dates.

    This is research model comparison, never sealed prospective validation.
    """
    c=dict(DEFAULTS, **(config or {}))
    days=[s['day'] for s in sessions]
    if days!=sorted(set(days)):
        return {'status':'NO_GO_BAD_SESSION_SEQUENCE','tradeSignal':False}
    gap=max(int(c['horizon']),int(c['gap_days']))
    trainN=int(c['train_days']);selectN=int(c['selection_days']);calN=int(c['calibration_days'])
    testN=int(c['test_days']); step=int(c['step_days'])
    if any(x<5 for x in (trainN,selectN,calN,testN,step)) or gap<1:
        raise ValueError('INVALID_SPLIT_GEOMETRY')
    startTest=trainN+gap+selectN+gap+calN+gap
    required=startTest+testN+int(c['horizon'])
    summary={'status':'NO_GO_TRAINING_DATA_INSUFFICIENT', 'tradeSignal':False,
        'executionAuthorized':False, 'selectedModelIsProfitCertified':False,
        'architecture':'BASE_RATE vs REGULARIZED_LOGIT vs HIST_GBDT vs ORDERED_CATBOOST',
        'datasetRows':len(rows), 'marketDays':len(days), 'minDaysForOneFold':required,
        'minOuterFolds':int(c['min_outer_folds']), 'folds':[],
        'dataSource':'HASH_VERIFIED_SCANNED_QUOTES_NOT_ACTUAL_EXCHANGE_CLOSING_PRICES',
        'labelIsFutureObservationNotTradeFill':True}
    if len(days)<required:return summary
    if not rows:
        summary['status']='NO_GO_NO_COMPLETE_SECTOR_ROWS';return summary
    by_index=defaultdict(list)
    for row in rows:by_index[row['featureIndex']].append(row)
    final_records=[];baseline_records=[];folds=[];chosen=Counter();missing_imports=set();disabled=[]
    # No concurrent fitting on future data; anchored expanding train section.
    for test0 in range(startTest,len(days)-testN-int(c['horizon'])+1,step):
        cal0=test0-gap-calN
        selection0=cal0-gap-selectN
        trainEnd=selection0-gap
        testEnd=test0+testN
        def slice_rows(lo,hi,label_before):
            return [r for i in range(lo,hi) for r in by_index[i] if r['labelEndIndex']<label_before]
        train=slice_rows(0,trainEnd,selection0)
        select=slice_rows(selection0,cal0-gap,cal0)
        cal=slice_rows(cal0,test0-gap,test0)
        test=slice_rows(test0,testEnd,len(days))
        foldmeta={'trainDays':days[0]+'..'+days[trainEnd-1],
            'selectionDays':days[selection0]+'..'+days[cal0-gap-1],
            'calibrationDays':days[cal0]+'..'+days[test0-gap-1],
            'testDays':days[test0]+'..'+days[testEnd-1],
            'testRows':len(test),'trainRows':len(train),
            'trainMaxLabelEnd':max([r['labelEndIndex'] for r in train],default=None),
            'selectionStartsAtIndex':selection0,
            'selectionMaxLabelEnd':max([r['labelEndIndex'] for r in select],default=None),
            'calibrationStartsAtIndex':cal0,
            'calibrationMaxLabelEnd':max([r['labelEndIndex'] for r in cal],default=None),
            'testStartsAtIndex':test0}
        if not train or not select or not cal or not test:
            foldmeta['status']='SKIP_MISSING_COMPLETE_BASKETS';folds.append(foldmeta);continue
        y=[r['target'] for r in train]
        if min(sum(y),len(y)-sum(y))<int(c['min_positive_train']):
            foldmeta['status']='SKIP_TRAINING_CLASS_IMBALANCE';folds.append(foldmeta);continue
        baseline=_constant_train(y)
        ysel=[r['target'] for r in select];Xsel=[r['features'] for r in select]
        fits={};scores={}
        for name in challengers:
            try:
                fn=_fit_model(name,train)
                p=fn(Xsel)
                scores[name]=_brier(ysel,p)
                fits[name]=fn
            except ImportError:
                missing_imports.add(name)
            except Exception as e:
                disabled.append({'model':name,'reason':type(e).__name__})
        if 'BASE_RATE' not in fits:
            fits['BASE_RATE']=baseline;scores['BASE_RATE']=_brier(ysel,baseline(Xsel))
        # The selection rule is predetermined. 1% Brier gain must beat base to
        # justify complexity; tie prefers simpler models by challengers order.
        rank={'BASE_RATE':0,'REGULARIZED_LOGIT':1,'HIST_GBDT':2,'ORDERED_CATBOOST':3}
        best=min(scores,key=lambda k:(scores[k],rank[k]))
        if best!='BASE_RATE' and scores[best]>=scores['BASE_RATE']*.99:
            best='BASE_RATE'
        chosen[best]+=1
        calibrated,calstatus=(fits[best], 'NOT_APPLICABLE_CONSTANT_BASELINE') if best=='BASE_RATE' else _calibrate(fits[best],cal)
        predictions=calibrated([r['features'] for r in test])
        basepred=baseline([r['features'] for r in test])
        for r,p,q in zip(test,predictions,basepred):
            final_records.append((r,p));baseline_records.append((r,q))
        foldmeta.update(status='EXPLORATORY_OUTER_HOLDOUT_ONLY',selected=best,
                        selectionBrier={k:round(v,7) for k,v in scores.items()},
                        calibration=calstatus,
                        testBrier=round(_brier([r['target'] for r in test],predictions),7),
                        baseRateTestBrier=round(_brier([r['target'] for r in test],basepred),7),
                        calibrationSampleRows=len(cal),selectionSampleRows=len(select),
                        lastTestLabelEnd=days[max(r['labelEndIndex'] for r in test)])
        folds.append(foldmeta)
    summary['folds']=folds
    summary['selectedModelCounts']=dict(chosen)
    summary['optionalModelsNotAvailable']=sorted(missing_imports)
    summary['modelErrors']=disabled[:12]
    complete=[r for r in folds if r['status']=='EXPLORATORY_OUTER_HOLDOUT_ONLY']
    summary['completedFolds']=len(complete)
    summary['testMarketDays']=len(set(r['day'] for r,p in final_records))
    if len(complete)<int(c['min_outer_folds']):
        summary['status']='NO_GO_INSUFFICIENT_OUTER_FOLDS'
        return summary
    testrows=[r for r,p in final_records]
    ps=[p for r,p in final_records]
    bp=[p for r,p in baseline_records]
    ys=[r['target'] for r in testrows]
    modelb=_brier(ys,ps);baseb=_brier(ys,bp)
    summary.update({
        'status':'EXPLORATORY_WALK_FORWARD_COMPARISON_ONLY',
        'testRows':len(ys), 'testEventRate':round(sum(ys)/len(ys),6),
        'outerBrier':round(modelb,7), 'outerBaselineBrier':round(baseb,7),
        'outerLogLoss':round(_logloss(ys,ps),7),
        'outerBaselineLogLoss':round(_logloss(ys,bp),7),
        'brierImprovementVsBasePct':round(100*(baseb-modelb)/baseb,4) if baseb>0 else None,
        'bySector':_breakdown(testrows,ps,'sector'),
        'byAsOfRegime':_breakdown(testrows,ps,'asOfRegime'),
        'modelSelectionRule':'per fold: selection Brier; >=1% improvement over base required; calibrate later distinct date block',
        'probabilityDeciles':[
            {'range':f'{j/10:.1f}–{(j+1)/10:.1f}', 'n':len(block),
             'observedRate':round(sum(y for y,p in block)/len(block),5) if block else None,
             'meanPrediction':round(sum(p for y,p in block)/len(block),5) if block else None}
            for j in range(10)
            for block in [[(y,p) for y,p in zip(ys,ps) if j/10<=p<(j+1)/10 if p<1 or j==9]]
        ],
        'evidence':'Historical outer-fold simulated out-of-sample only; not a sealed prospective trial',
    })
    if not any(k!='BASE_RATE' for k in set(chosen) | {f for z in complete for f in z['selectionBrier']}):
        summary['status']='NO_GO_ML_CHALLENGERS_UNAVAILABLE'
        summary['interpretation']='BASE_RATE_ONLY_CANNOT_COMPARE_LEARNERS'
    elif modelb>=baseb:
        summary['interpretation']='MODEL_NOT_BETTER_THAN_BASE_RATE_NO_MODEL_PROMOTION'
    else:
        summary['interpretation']='MODEL_HAS_EXPLORATORY_CALIBRATION_EDGE_UNCONFIRMED_BY_SEALED_FUTURE'
    return summary


def run(market_dir, out, config=None, *, target_et='15:30', tolerance_minutes=30):
    config=dict(DEFAULTS, **(config or {}))
    sessions,evidence=load_sessions(market_dir,clock=target_et,tolerance_minutes=tolerance_minutes)
    rows,construct=construct_rows(sessions,config=config)
    report=walk_forward(rows,sessions,config=config)
    report['datasetAudit']=construct
    report['archiveEvidence']=evidence
    report['config']=config
    report['featureNames']=list(FEATURES)
    report['targetDefinition']='At t+3 observed trading sessions, median fixed-basket return >=2.5% AND >=2% above SPY (configurable); no intraday barrier / no execution evidence'
    report['maxDataTimeUsed']=sessions[-1]['day'] if sessions else None
    report['generatedAt']=datetime.now(timezone.utc).isoformat()
    report['researchDesignSha256']=hashlib.sha256(json.dumps({'config':config,'features':FEATURES,'candidates':CHALLENGERS},sort_keys=True).encode()).hexdigest()
    report['noTradingPermission']=True
    report['noProfitCertification']=True
    if evidence['status']!='PASS_LOCAL_ARCHIVE_INTEGRITY':
        report['status']='NO_GO_ARCHIVE_INTEGRITY'
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    tmp=out.with_suffix(out.suffix+'.tmp')
    tmp.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(out)
    return report


def main():
    ap=argparse.ArgumentParser(description='STRICT read-only sector probabilistic model selection; never orders stocks')
    here=Path(__file__).resolve().parent
    ap.add_argument('--market-dir',type=Path,default=here/'output'/'market')
    ap.add_argument('--out',type=Path,default=here/'output'/'research'/'sector_model_selection.json')
    ap.add_argument('--target-et',default='15:30')
    ap.add_argument('--tolerance-minutes',type=float,default=30)
    args=ap.parse_args()
    try:
        report=run(args.market_dir,args.out,target_et=args.target_et,tolerance_minutes=args.tolerance_minutes)
    except Exception as e:
        report={'status':'NO_GO_MODEL_RESEARCH_INTERNAL_ERROR','reason':str(e)[:300],
                'tradeSignal':False,'noTradingPermission':True,'noProfitCertification':True}
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='EXPLORATORY_WALK_FORWARD_COMPARISON_ONLY' else 2

if __name__=='__main__':raise SystemExit(main())
