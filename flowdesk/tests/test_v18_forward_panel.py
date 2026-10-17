"""Causal, tamper, market-time and no-invented-profit adversarial tests."""
import csv,hashlib,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from market_archive import register_scan,audit_archive
from market_scan import run
from market_forward_panel import evaluate_panel

ET=ZoneInfo('America/New_York')
ORIGIN=datetime(2026,10,14,10,0,tzinfo=ET).astimezone(timezone.utc)


def h(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def capture(root,t,stock_px=110,spy_px=400,change=3, source_delta=5,spy_delta=4,
            with_spy=True, with_stock=True, stock_code='US.AAOI', listed=True):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    src=(t-timedelta(seconds=source_delta)).isoformat()
    benchmark=(t-timedelta(seconds=spy_delta)).isoformat()
    univ=[{'code':stock_code,'name':'Test','exchange':'US_Nasdaq' if listed else 'US_Pink','securityType':'STOCK'}]
    with (root/'us_stock_universe.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=univ[0]);w.writeheader();w.writerows(univ)
    quo=[{'code':stock_code,'sourceAsOf':src,'lastPrice':stock_px,'usable':True,
          'changePct':change,'turnover':3000000,'diagnostic':'USABLE_REGULAR'}] if with_stock else []
    with (root/'latest_quotes.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['code','sourceAsOf','lastPrice','usable','changePct','turnover','diagnostic']);w.writeheader();w.writerows(quo)
    bs=[{'code':'US.SPY','sourceAsOf':benchmark,'lastPrice':spy_px,'usable':True}] if with_spy else []
    with (root/'latest_benchmarks.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['code','sourceAsOf','lastPrice','usable']);w.writeheader();w.writerows(bs)
    report={'stockUniverseCount':1,'snapshotReceivedCount':len(quo),'freshUsableCount':len(quo),
            'dataGate':'PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY','fullListedUniverse':True,
            'completedAt':t.isoformat(),'benchmarkRequested':['US.SPY'],
            'universeSha256':h(root/'us_stock_universe.csv'),'quotesSha256':h(root/'latest_quotes.csv'),
            'benchmarksSha256':h(root/'latest_benchmarks.csv')}
    (root/'latest_report.json').write_text(json.dumps(report))
    return register_scan(root)

class ForwardPanel(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_no_scans_is_not_a_strategy(self):
        x=evaluate_panel(self.root)
        self.assertEqual(x['status'],'NO_GO_ARCHIVE_INTEGRITY')
        self.assertFalse(x['alphaCertified'])

    def test_origin_only_pending_future_not_clever_win(self):
        capture(self.root,ORIGIN)
        x=evaluate_panel(self.root)
        self.assertEqual(x['outcomeCounts']['PENDING_NO_FUTURE_SNAPSHOT'],1)
        self.assertFalse(x['profitMeasured'])

    def test_future_scan_market_relative_observation_not_profit(self):
        capture(self.root,ORIGIN)
        capture(self.root,ORIGIN+timedelta(minutes=25),stock_px=112,spy_px=402,change=5)
        x=evaluate_panel(self.root)
        r=[v for v in x['results'] if v['originCompletedAt']==ORIGIN.isoformat()][0]
        self.assertEqual(r['outcomeStatus'],'OBSERVED_MARKET_RELATIVE_NOT_NET')
        self.assertAlmostEqual(r['marketRelativePriceMoveBps'],(112/110-402/400)*10000,places=4)
        self.assertTrue(r['NOT_Executable_Profit'])
        self.assertFalse(x['alphaCertified'])
        self.assertEqual(x['independentObservedETDays'],1)

    def test_out_of_window_cannot_be_used_as_future(self):
        capture(self.root,ORIGIN)
        capture(self.root,ORIGIN+timedelta(minutes=60),stock_px=150)
        x=evaluate_panel(self.root)
        self.assertEqual(x['outcomeCounts'].get('OBSERVED_MARKET_RELATIVE_NOT_NET',0),0)

    def test_contemporaneous_scan_is_not_future(self):
        capture(self.root,ORIGIN)
        capture(self.root,ORIGIN+timedelta(minutes=1),stock_px=145)
        x=evaluate_panel(self.root)
        self.assertEqual(x['outcomeCounts']['PENDING_NO_FUTURE_SNAPSHOT'],2)

    def test_future_independent_of_initial_candidate_selection(self):
        capture(self.root,ORIGIN,change=1)
        capture(self.root,ORIGIN+timedelta(minutes=25),stock_px=140,change=12)
        x=evaluate_panel(self.root)
        self.assertEqual(x['outcomeCounts'].get('OBSERVED_MARKET_RELATIVE_NOT_NET',0),0)
        self.assertTrue(all(i['originCompletedAt']!=ORIGIN.isoformat() for i in x['results']))

    def test_missing_benchmark_does_not_make_excess_return(self):
        capture(self.root,ORIGIN,with_spy=False)
        capture(self.root,ORIGIN+timedelta(minutes=25))
        x=evaluate_panel(self.root)
        r=x['results'][0]
        self.assertEqual(r['outcomeStatus'],'NO_GO_ORIGIN_BENCHMARK_OR_CLOCK')

    def test_benchmark_clock_skew_rejected(self):
        capture(self.root,ORIGIN,source_delta=5,spy_delta=43)
        capture(self.root,ORIGIN+timedelta(minutes=25))
        x=evaluate_panel(self.root)
        self.assertEqual(x['results'][0]['outcomeStatus'],'NO_GO_ORIGIN_BENCHMARK_OR_CLOCK')

    def test_outdated_source_rejected_before_selector(self):
        capture(self.root,ORIGIN,source_delta=70)
        capture(self.root,ORIGIN+timedelta(minutes=25))
        x=evaluate_panel(self.root)
        self.assertFalse(any(i['originCompletedAt']==ORIGIN.isoformat() for i in x['results']))

    def test_only_listed_stocks_enter_research(self):
        capture(self.root,ORIGIN,listed=False)
        capture(self.root,ORIGIN+timedelta(minutes=25))
        x=evaluate_panel(self.root)
        self.assertFalse(any(a['originCompletedAt']==ORIGIN.isoformat() for a in x['results']))

    def test_tampered_benchmark_archive_rejected(self):
        run=capture(self.root,ORIGIN)
        (self.root/'archive'/run['archiveId']/'latest_benchmarks.csv').write_text('fake')
        x=evaluate_panel(self.root)
        self.assertEqual(x['status'],'NO_GO_ARCHIVE_INTEGRITY')
        self.assertIn('TAMPERED_ARCHIVED_SCAN',str(x['archiveAudit']))

    def test_missing_optional_benchmark_prevents_orphan(self):
        capture(self.root,ORIGIN)
        (self.root/'latest_benchmarks.csv').unlink()
        with self.assertRaises((ValueError,FileNotFoundError)):register_scan(self.root)
        self.assertEqual(audit_archive(self.root)['status'],'PASS_LOCAL_ARCHIVE_INTEGRITY')

    def test_missing_future_quote_is_censored(self):
        capture(self.root,ORIGIN)
        capture(self.root,ORIGIN+timedelta(minutes=25),with_stock=False)
        x=evaluate_panel(self.root)
        self.assertEqual(x['results'][0]['outcomeStatus'],'MISSING_OR_UNSYNC_EXIT_QUOTES')
        self.assertIsNone(x['results'][0]['marketRelativePriceMoveBps'])

class BenchmarkApi(unittest.TestCase):
    def test_benchmark_is_separate_from_stock_universe(self):
        class Fake:
            def __init__(self):self.calls=[]
            def get_stock_basicinfo(self,*args):return 0,[{'code':'US.AAOI','exchange_type':'US_Nasdaq'}]
            def get_market_snapshot(self,codes):
                self.calls.append(codes)
                return 0,[{'code':x,'update_time':'2026-10-14 10:00:00',
                           'last_price':100,'prev_close_price':99} for x in codes]
        fake=Fake()
        with tempfile.TemporaryDirectory() as td:
            r=run(fake,'US','STOCK',0,td,interval=.55,benchmark_codes=('US.SPY','US.QQQ'))
            self.assertEqual(r['stockUniverseCount'],1)
            self.assertEqual(r['snapshotReceivedCount'],1)
            self.assertEqual(r['benchmarkReceivedCount'],2)
            self.assertEqual(r['snapshotRequestCount'],2)
            self.assertEqual(audit_archive(td)['status'],'PASS_LOCAL_ARCHIVE_INTEGRITY')
            with (Path(td)/'latest_benchmarks.csv').open() as f:
                self.assertEqual(len(list(csv.DictReader(f))),2)
            with (Path(td)/'latest_quotes.csv').open() as f:
                self.assertEqual(len(list(csv.DictReader(f))),1)

    def test_benchmark_denial_is_visible(self):
        class Fake:
            def get_stock_basicinfo(self,*args):return 0,[{'code':'US.AAOI','exchange_type':'US_NYSE'}]
            def get_market_snapshot(self,codes):
                if 'US.SPY' in codes:return 1,'benchmark access denied'
                return 0,[{'code':'US.AAOI','last_price':20,'prev_close_price':18}]
        with tempfile.TemporaryDirectory() as td:
            x=run(Fake(),'US','STOCK',0,td,interval=.55,benchmark_codes=('US.SPY',))
            self.assertEqual(x['benchmarkReceivedCount'],0)
            self.assertIn('denied',x['benchmarkFailure'])
            self.assertEqual(audit_archive(td)['status'],'PASS_LOCAL_ARCHIVE_INTEGRITY')

if __name__=='__main__':unittest.main()
