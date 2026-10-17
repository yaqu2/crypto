import csv, json, tempfile, unittest, hashlib
from datetime import date,timedelta
from pathlib import Path
from sector_regime_lab import evaluate_sessions,load_sessions,positive,utc_time
from market_archive import register_scan

class SectorRegimeTest(unittest.TestCase):
    def series(self, seq_a, seq_b, spy=None, missing=None):
        spy=spy or [100.0]*len(seq_a)
        missing=missing or {}
        d=date(2026,9,1)
        out=[]
        for i,(a,b,v) in enumerate(zip(seq_a,seq_b,spy)):
            prices={'A':a,'B':b,'C':a*2,'D':a*1.5, 'E':b*2,'F':b*3,'G':b*.7,'H':b*.5}
            for key in missing.get(i,[]):prices.pop(key,None)
            out.append({'day':(d+timedelta(days=i)).isoformat(), 'prices':prices,'spy':v, 'sourceArchiveId':f'r{i}'})
        return out
    def baskets(self):return {'OPT':['A','C','D','E'], 'MEM':['B','E','F','G']}

    def test_detect_synchronized_drop(self):
        s=self.series([100,98,92,85,79,74],[100,99,97,95,94,92])
        result=evaluate_sessions(s,sectors=self.baskets())
        self.assertEqual(result['sectorResults']['OPT']['phase'],'SYNCHRONIZED_SELL_OFF_OBSERVED')
        self.assertEqual(result['sectorResults']['OPT']['fractionDown20Pct'],0.75)
        self.assertFalse(result['tradeSignal'])
    def test_rebound_is_watch_only(self):
        s=self.series([100,99,92,85,79,85],[100,99,95,93,90,95])
        got=evaluate_sessions(s,sectors=self.baskets())['sectorResults']['OPT']
        self.assertEqual(got['phase'],'REBOUND_WATCH_NOT_BUY_SIGNAL')
    def test_missing_stocks_no_fabricated_breadth(self):
        s=self.series([100,95,90,85,80,75],[100]*6,missing={3:['A','B','C','D']})
        got=evaluate_sessions(s,sectors=self.baskets())['sectorResults']['OPT']
        self.assertEqual(got['status'],'NO_GO_BASKET_COVERAGE')
        self.assertIn('A',got['missingSeries'])
    def test_no_benchmark_fails(self):
        s=self.series([100]*6,[100]*6)
        s[2]['spy']=None
        self.assertEqual(evaluate_sessions(s,sectors=self.baskets())['status'],'NO_GO_MISSING_BENCHMARK_IN_WINDOW')
    def test_no_old_date_duplicate(self):
        s=self.series([100]*6,[100]*6)
        s[-1]['day']=s[-2]['day']
        self.assertEqual(evaluate_sessions(s,sectors=self.baskets())['status'],'NO_GO_BAD_SESSION_SEQUENCE')
    def test_short_series_fails(self):
        s=self.series([100]*5,[100]*5)
        self.assertEqual(evaluate_sessions(s,sectors=self.baskets())['status'],'NO_GO_INSUFFICIENT_ALIGNED_SESSIONS')
    def test_safeguard_on_bad_numbers(self):
        self.assertIsNone(positive(float('nan')))
        self.assertIsNone(positive(-1))
        self.assertIsNone(utc_time('2026-10-12T14:00:00'))
    def test_no_forward_labels_exposed(self):
        s=self.series([100]*6,[100]*6)
        result=evaluate_sessions(s,sectors=self.baskets())
        self.assertEqual(result['learningStatus'],'NOT_TRAINED_NO_SEALED_FORWARD_LABELS')
        self.assertNotIn('buyProbability',repr(result))

    def build_archive(self,root,when,price=100.0,spy=100.0,quote_asof=None,corrupt=False,include_benchmark=True):
        root=Path(root);root.mkdir(parents=True,exist_ok=True)
        with (root/'us_stock_universe.csv').open('w') as f:
            f.write('code,name,exchange,securityType\nUS.AAOI,AAOI,NYSE,STOCK\n')
        with (root/'latest_quotes.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['code','sourceAsOf','lastPrice','usable'])
            w.writeheader();w.writerow({'code':'US.AAOI','sourceAsOf':quote_asof or when,
                                          'lastPrice':price,'usable':True})
        if include_benchmark:
            with (root/'latest_benchmarks.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['code','sourceAsOf','lastPrice','usable'])
                w.writeheader();w.writerow({'code':'US.SPY','sourceAsOf':when, 'lastPrice':spy,'usable':True})
        def sha(p):return hashlib.sha256((root/p).read_bytes()).hexdigest()
        rep={'startedAt':when,'completedAt':when,
             'stockUniverseCount':1,'freshUsableCount':1,
             'dataGate':'PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY',
             'universeSha256':sha('us_stock_universe.csv'),
             'quotesSha256':sha('latest_quotes.csv')}
        if include_benchmark:rep['benchmarksSha256']=sha('latest_benchmarks.csv')
        (root/'latest_report.json').write_text(json.dumps(rep))
        register_scan(root)
        if corrupt:(root/'archive'/next(x.name for x in (root/'archive').iterdir() if x.is_dir())/'latest_quotes.csv').write_text('tamper')
    def test_real_archive_missing_is_no_go(self):
        with tempfile.TemporaryDirectory() as d:
            sess,info=load_sessions(d)
            self.assertEqual(info['status'],'NO_GO_ARCHIVE_INTEGRITY')
            self.assertEqual(sess,[])
    def test_real_archive_clocks_and_source(self):
        with tempfile.TemporaryDirectory() as d:
            self.build_archive(d,'2026-10-14T19:30:00+00:00')
            sess,info=load_sessions(d)
            self.assertEqual(info['status'],'PASS_LOCAL_ARCHIVE_INTEGRITY')
            self.assertEqual(sess[0]['prices']['US.AAOI'],100.)
            self.assertEqual(sess[0]['spy'],100.)
            self.assertEqual(len(sess),1)
    def test_real_archive_future_source_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            self.build_archive(d,'2026-10-14T19:30:00+00:00',quote_asof='2026-10-14T19:30:01+00:00')
            sess,_=load_sessions(d)
            self.assertNotIn('US.AAOI',sess[0]['prices'])
    def test_real_archive_offclock_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            self.build_archive(d,'2026-10-14T17:30:00+00:00')
            sess,info=load_sessions(d)
            self.assertEqual(info['candidateDays'],0)
            self.assertEqual(sess,[])
    def test_real_archive_hash_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            self.build_archive(d,'2026-10-14T19:30:00+00:00',corrupt=True)
            sess,info=load_sessions(d)
            self.assertEqual(info['status'],'NO_GO_ARCHIVE_INTEGRITY')
    def test_lookahead_prohibited(self):
        s=self.series([100,99,95,92,88,85,10],[100]*7)
        # results through six observations MUST not see seventh.
        a=evaluate_sessions(s[:6],sectors=self.baskets())
        s[6]['prices']={'A':1,'B':1,'C':1,'D':1,'E':1,'F':1,'G':1,'H':1}
        b=evaluate_sessions(s[:6],sectors=self.baskets())
        self.assertEqual(a,b)
    def test_missing_is_not_assumed_zero(self):
        s=self.series([100,95,90,80,70,60],[100]*6,missing={4:['A']})
        row=evaluate_sessions(s,sectors=self.baskets())['sectorResults']['OPT']
        self.assertEqual(row['completeSeriesCount'],3)
        self.assertEqual(row['coverage'],.75)

if __name__=='__main__':unittest.main()
