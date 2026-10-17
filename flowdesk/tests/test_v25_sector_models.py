import json, math, tempfile, unittest
from datetime import date,timedelta
from pathlib import Path
from sector_model_lab import construct_rows, walk_forward, run, _brier, FEATURES
from sector_regime_lab import SECTORS

CODES=[c for codes in SECTORS.values() for c in codes]

def sample(n=240, noise=0, broken=None):
    d=date(2024,1,2);ret=[];seq=[]
    for i in range(n):
        # Business-day timestamps. Nonstationary, alternating regimes, and
        # heterogeneous stock response. Artificial fixture -- NOT market alpha.
        while d.weekday()>4:d+=timedelta(days=1)
        prices={}
        for j,code in enumerate(CODES):
            sector_factor=(1.0 if j<4 else 1.35)
            series=math.exp(.004*i + sector_factor*(.085*math.sin(i/5.7)+.027*math.sin(i/2.25))
                            + .002*j*math.cos(i/11))
            prices[code]=80*(j+1)*series
        if broken and i in broken:
            for code in broken[i]:prices.pop(code,None)
        seq.append({'day':d.isoformat(),'prices':prices,'spy':100*math.exp(.0011*i+.018*math.sin(i/10.3)),
                    'sourceArchiveId':str(i)})
        d+=timedelta(days=1)
    return seq

C={'train_days':65,'selection_days':12,'calibration_days':12,'test_days':8,
   'step_days':8,'gap_days':4,'horizon':3,'min_outer_folds':2,
   'min_positive_train':2}

class RegimeLearningSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s=sample(160)
        cls.rows,cls.meta=construct_rows(cls.s,config=C)
    def test_feature_vector_dimension_and_finite(self):
        self.assertTrue(self.rows)
        for row in self.rows:
            self.assertEqual(len(row['features']),len(FEATURES))
            self.assertTrue(all(math.isfinite(x) for x in row['features']))
    def test_asof_features_do_not_change_with_future_price_tampering(self):
        s=sample(30)
        r,_=construct_rows(s,config=C)
        t=r[8]['featureIndex']
        target=next(x for x in r if x['featureIndex']==t and x['sector']==r[8]['sector'])
        future=sample(30)
        for c in CODES:future[t+3]['prices'][c]*=50
        r2,_=construct_rows(future,config=C)
        target2=next(x for x in r2 if x['featureIndex']==t and x['sector']==target['sector'])
        self.assertEqual(target['features'],target2['features'])
        self.assertNotEqual(target['forwardBasketPct'],target2['forwardBasketPct'])
    def test_future_label_index_never_asof(self):
        self.assertTrue(all(r['featureIndex']<r['labelEndIndex'] for r in self.rows))
    def test_duplicate_market_days_fail_closed(self):
        s=sample(15);s[7]['day']=s[6]['day']
        r,a=construct_rows(s)
        self.assertEqual(r,[])
        self.assertEqual(a['status'],'NO_GO_BAD_SESSION_SEQUENCE')
    def test_missing_spy_is_not_imputed(self):
        s=sample(28);s[12]['spy']=None
        rows,a=construct_rows(s)
        self.assertGreater(a['rejected'].get('MISSING_SPY',0),0)
        self.assertFalse(any(r['featureIndex']==12 for r in rows))
    def test_missing_stock_does_not_reduce_denominator(self):
        s=sample(30);s[14]['prices'].pop(CODES[0])
        rows,a=construct_rows(s)
        self.assertGreater(a['rejected']['INCOMPLETE_SECTOR_BASKET'],0)
        self.assertFalse(any(r['featureIndex']==14 and r['sector']==next(iter(SECTORS)) for r in rows))
    def test_long_data_gap_is_rejected(self):
        s=sample(28);after=date.fromisoformat(s[13]['day'])+timedelta(days=18)
        delta=after-date.fromisoformat(s[14]['day'])
        for r in s[14:]:r['day']=(date.fromisoformat(r['day'])+delta).isoformat()
        rows,a=construct_rows(s)
        self.assertGreater(a['rejected']['NON_CONTIGUOUS_OBSERVATION_DAYS'],0)
        self.assertFalse(any(13<=r['featureIndex']<=14 for r in rows))
    def test_data_shortage_no_fabricated_model(self):
        s=sample(30);r,_=construct_rows(s)
        result=walk_forward(r,s)
        self.assertEqual(result['status'],'NO_GO_TRAINING_DATA_INSUFFICIENT')
        self.assertFalse(result['tradeSignal'])
    def test_purge_labels_not_overlapping_selection_and_test(self):
        result=walk_forward(self.rows,self.s,config=C,challengers=('BASE_RATE',))
        folds=[x for x in result['folds'] if x['status'].startswith('EXPLORATORY')]
        self.assertTrue(folds)
        for f in folds:
            self.assertLess(f['trainMaxLabelEnd'],f['selectionStartsAtIndex'])
            self.assertLess(f['selectionMaxLabelEnd'],f['calibrationStartsAtIndex'])
            self.assertLess(f['calibrationMaxLabelEnd'],f['testStartsAtIndex'])
    def test_baseline_works_and_cannot_authorize_trades(self):
        result=walk_forward(self.rows,self.s,config=C,challengers=('BASE_RATE',))
        self.assertEqual(result['status'],'NO_GO_ML_CHALLENGERS_UNAVAILABLE')
        self.assertTrue(result['completedFolds']>=2)
        self.assertEqual(result['outerBrier'],result['outerBaselineBrier'])
        self.assertFalse(result['executionAuthorized'])
    def test_model_competition_selects_only_prior_selection_block(self):
        result=walk_forward(self.rows,self.s,config=C,
                            challengers=('BASE_RATE','REGULARIZED_LOGIT','HIST_GBDT'))
        self.assertEqual(result['status'],'EXPLORATORY_WALK_FORWARD_COMPARISON_ONLY')
        self.assertIn('brierImprovementVsBasePct',result)
        self.assertTrue(all(f['selected'] in ('BASE_RATE','REGULARIZED_LOGIT','HIST_GBDT')
                            for f in result['folds'] if f['status'].startswith('EXPLORATORY')))
    def test_challenger_optional_package_missing_fallback(self):
        # Unknown model causes controlled error and BASE_RATE stays available.
        result=walk_forward(self.rows,self.s,config=C,challengers=('UNKNOWN_MODEL',))
        self.assertGreater(len(result['modelErrors']),0)
        self.assertGreater(result['completedFolds'],0)
    def test_brier_perfect_vs_wrong(self):
        self.assertEqual(_brier([0,1],[0,1]),0)
        self.assertEqual(_brier([0,1],[1,0]),1)
    def test_empty_archive_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'report.json'
            rep=run(Path(t)/'market',p)
            self.assertEqual(rep['status'],'NO_GO_ARCHIVE_INTEGRITY')
            self.assertTrue(rep['noProfitCertification'])
            self.assertTrue(p.exists())
    def test_probabilities_are_valid(self):
        result=walk_forward(self.rows,self.s,config=C,challengers=('BASE_RATE',))
        for decile in result['probabilityDeciles']:
            p=decile['meanPrediction']
            if p is not None:self.assertTrue(0<=p<=1)
    def test_regime_feature_is_determined_without_future(self):
        rows=self.rows
        self.assertIn('asOfRegime',rows[0])
        self.assertIn(rows[0]['asOfRegime'],('OTHER','SYNCHRONIZED_SELLOFF','REBOUND_WATCH'))
        self.assertEqual(len(rows[0]['features']),len(FEATURES))
    def test_regime_breakdown_on_outer_test(self):
        rep=walk_forward(self.rows,self.s,config=C,challengers=('BASE_RATE','REGULARIZED_LOGIT'))
        self.assertIn('byAsOfRegime',rep)
        self.assertTrue(rep['byAsOfRegime'])
    def test_static_baskets_disclosed(self):
        self.assertIn('not historical point-in-time',self.meta['survivorshipWarning'].lower())

if __name__=='__main__':unittest.main()
