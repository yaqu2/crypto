"""Adversarial tests for delayed, three-scan top-of-book *quote* scenarios."""
import csv,hashlib,json,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from market_archive import register_scan
from market_quote_stress import evaluate_quote_stress,bbo_point
from market_scan import normalize_snapshot

T=datetime(2026,10,14,10,tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def snapshot(root,t,code='US.AAOI',change=3.0,px=100.,spy=400.,
             bid=99.9,ask=100.1,bvol=1000, avol=1000, is_listed=True,
             with_stock=True,with_spy=True, srcdelay=4,spy_delay=4,clockshift=0,
             cover=.95):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    def write(name,rows,cols):
        with (root/name).open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(rows)
    src=(t-timedelta(seconds=srcdelay)+timedelta(seconds=clockshift)).isoformat()
    bp=(t-timedelta(seconds=spy_delay)).isoformat()
    code_fields=['code','sourceAsOf','lastPrice','usable','changePct','turnover','bidPrice','askPrice','bidVol','askVol','bboValid']
    univ=[dict(code=code,exchange='US_Nasdaq' if is_listed else 'US_Pink',name='Mock',securityType='STOCK')]
    write('us_stock_universe.csv',univ,['code','exchange','name','securityType'])
    quote=[dict(code=code,sourceAsOf=src,lastPrice=px,usable=True,changePct=change,turnover=5e6,
                bidPrice=bid,askPrice=ask,bidVol=bvol,askVol=avol,bboValid=True)] if with_stock else []
    write('latest_quotes.csv',quote,code_fields)
    benchmarks=[dict(code='US.SPY',sourceAsOf=bp,lastPrice=spy,usable=True)] if with_spy else []
    write('latest_benchmarks.csv',benchmarks,['code','sourceAsOf','lastPrice','usable'])
    # Small local fixtures (1 symbol) are never market-wide Alpha evidence.
    report=dict(stockUniverseCount=1,snapshotReceivedCount=len(quote),freshUsableCount=len(quote),
                listedUniverseCount=20,listedSnapshotCount=round(20*cover),
                dataGate='PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY',
                completedAt=t.isoformat(),benchmarkRequested=['US.SPY'],
                universeSha256=sha(root/'us_stock_universe.csv'),
                quotesSha256=sha(root/'latest_quotes.csv'),
                benchmarksSha256=sha(root/'latest_benchmarks.csv'))
    (root/'latest_report.json').write_text(json.dumps(report))
    return register_scan(root)['archiveId']

class QuoteStress(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
    def three(self,entryparams=None,exitparams=None,origparams=None):
        a=snapshot(self.root,T,**(origparams or {}))
        b=snapshot(self.root,T+timedelta(minutes=1),**(entryparams or {}))
        c=snapshot(self.root,T+timedelta(minutes=25),**(exitparams or {}))
        return a,b,c
    def first(self):
        x=evaluate_quote_stress(self.root)
        return x,next((r for r in x.get('results',[]) if r['originAt']==T.isoformat()),None)
    def test_empty_archive_no_go(self):
        x=evaluate_quote_stress(self.root)
        self.assertEqual(x['status'],'NO_GO_ARCHIVE_INTEGRITY')
        self.assertFalse(x['alphaCertified'])
    def test_two_scans_never_fake_delayed_entry(self):
        snapshot(self.root,T)
        snapshot(self.root,T+timedelta(minutes=25),bid=104,ask=104.1)
        x,r=self.first()
        self.assertEqual(r['status'],'PENDING_NO_DELAYED_ENTRY_SCAN')
        self.assertIsNone(r['quoteCrossingPnLByCostUSD'])
    def test_three_scans_bid_ask_crossing_and_cost(self):
        self.three(exitparams=dict(bid=103.9,ask=104.1))
        x,r=self.first()
        self.assertEqual(r['status'],'EXPLORATORY_QUOTE_CROSSING_ONLY')
        self.assertEqual(r['shares'],9)
        self.assertAlmostEqual(r['quoteCrossingBeforeExtraCostsUSD'],(103.9-100.1)*9,places=3)
        self.assertAlmostEqual(r['quoteCrossingPnLByCostUSD']['40'],round((103.9-100.1)*9-9*100.1*.004,4))
        self.assertFalse(r['executionVerified']);self.assertFalse(x['alphaCertified']);self.assertFalse(x['realizedProfitVerified'])
        self.assertEqual(x['originObservedETDays'],1)
    def test_negative_when_price_rises_but_spread_eats_move(self):
        self.three(entryparams=dict(bid=99.7,ask=100.3),exitparams=dict(bid=100.25,ask=100.6))
        _,r=self.first()
        self.assertEqual(r['status'],'EXPLORATORY_QUOTE_CROSSING_ONLY')
        self.assertLess(r['quoteCrossingBeforeExtraCostsUSD'],0)
    def test_missing_bbo_never_infer_from_last_trade(self):
        self.three(entryparams=dict(bid=None,ask=None))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_MISSING_OR_WIDE_BBO')
    def test_crossed_bbo_rejected(self):
        self.three(entryparams=dict(bid=101,ask=100))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_MISSING_OR_WIDE_BBO')
    def test_large_spread_rejected(self):
        self.three(entryparams=dict(bid=99,ask=100))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_MISSING_OR_WIDE_BBO')
    def test_entry_depth_limit(self):
        self.three(entryparams=dict(avol=20))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_DISPLAYED_DEPTH_LIMIT')
    def test_exit_depth_limit(self):
        self.three(exitparams=dict(bvol=20))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_DISPLAYED_DEPTH_LIMIT')
    def test_no_share_with_1000_notional(self):
        self.three(entryparams=dict(px=1501,bid=1500.9,ask=1501.1),exitparams=dict(px=1501,bid=1500.9,ask=1501.1))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_BELOW_ONE_SHARE_NOTIONAL')
    def test_short_requires_borrow_not_simulated(self):
        self.three(origparams=dict(change=-3))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_SHORT_BORROW_UNVERIFIED')
    def test_missing_spy_at_entry(self):
        self.three(entryparams=dict(with_spy=False))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_TARGET_SPY_CLOCK_OR_SOURCE')
    def test_missing_spy_at_origin(self):
        self.three(origparams=dict(with_spy=False))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_ORIGIN_SOURCE_OR_BENCHMARK')
    def test_unsynchronized_clock(self):
        self.three(entryparams=dict(spy_delay=40))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_TARGET_SPY_CLOCK_OR_SOURCE')
    def test_quote_repeated_from_before_signal_fails(self):
        self.three(entryparams=dict(srcdelay=70))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_TARGET_SPY_CLOCK_OR_SOURCE')
    def test_future_source_time_fails(self):
        self.three(entryparams=dict(clockshift=5))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_CAUSAL_SOURCE_ORDER')
    def test_insufficient_market_cover(self):
        self.three(entryparams=dict(cover=.8))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_MARKET_QUOTE_COVERAGE')
    def test_first_failed_entry_not_replaced_with_later_winner(self):
        snapshot(self.root,T)
        snapshot(self.root,T+timedelta(seconds=30),bid=None,ask=None)
        snapshot(self.root,T+timedelta(minutes=2),bid=99.1,ask=99.2)
        snapshot(self.root,T+timedelta(minutes=25),bid=120,ask=120.1)
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_MISSING_OR_WIDE_BBO')
    def test_late_exit_not_used_to_claim_win(self):
        snapshot(self.root,T)
        snapshot(self.root,T+timedelta(minutes=1))
        snapshot(self.root,T+timedelta(minutes=50),bid=130,ask=130.1)
        _,r=self.first();self.assertEqual(r['status'],'PENDING_NO_FUTURE_EXIT_SCAN')
    def test_first_failed_exit_not_replaced_with_later_winner(self):
        snapshot(self.root,T)
        snapshot(self.root,T+timedelta(minutes=1))
        snapshot(self.root,T+timedelta(minutes=17),bid=None,ask=None)
        snapshot(self.root,T+timedelta(minutes=25),bid=130,ask=130.1)
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_MISSING_OR_WIDE_BBO')
    def test_tampered_bbo_archives_fail_integrity(self):
        a,_,_=self.three()
        p=self.root/'archive'/a/'latest_quotes.csv'
        p.write_text(p.read_text().replace('100.1','0.1'))
        x=evaluate_quote_stress(self.root)
        self.assertEqual(x['status'],'NO_GO_ARCHIVE_INTEGRITY')
    def test_missing_stock_future_is_not_closed_at_fabricated_price(self):
        self.three(exitparams=dict(with_stock=False))
        _,r=self.first();self.assertEqual(r['status'],'NO_GO_TARGET_SPY_CLOCK_OR_SOURCE')
    def test_cold_scan_before_open_is_not_valid_regular_session(self):
        t=datetime(2026,10,14,8,37,tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
        snapshot(self.root,t)
        snapshot(self.root,t+timedelta(minutes=1))
        snapshot(self.root,t+timedelta(minutes=25))
        x=evaluate_quote_stress(self.root)
        self.assertEqual(x['results'][0]['status'],'NO_GO_ORIGIN_SOURCE_OR_BENCHMARK')
    def test_sdk_bbo_fields_are_preserved_not_guaranteed(self):
        sdk={'code':'US.AAOI','update_time':'2026-10-14 10:00:00','last_price':100,
             'prev_close_price':98,'bid_price':99.9,'ask_price':100.1,
             'bid_vol':900,'ask_vol':800}
        now=datetime(2026,10,14,10,0,5,tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
        d=normalize_snapshot(sdk,now)
        self.assertTrue(d['bboValid']);self.assertEqual(d['bidVol'],900)
        self.assertEqual(d['askPrice'],100.1)
        self.assertIsNotNone(bbo_point(d,now))
    def test_old_csv_bbo_fields_absent_always_no_go(self):
        d={'code':'US.AAOI','sourceAsOf':T.isoformat(),'lastPrice':'100','usable':'True'}
        self.assertIsNone(bbo_point(d,T))


class HttpIntegration(unittest.TestCase):
    def test_quote_stress_route_404_until_run_then_200(self):
        import threading,urllib.request,urllib.error
        import bridge
        from runtime import Runtime,DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as td:
            old=bridge.ROOT
            bridge.ROOT=Path(td)
            rt=Runtime(DEFAULT_CONFIG)
            server=bridge.create_server(rt,'/private/')
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            url=f'http://127.0.0.1:{server.server_port}/private/quote-execution-stress'
            try:
                with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url,timeout=3)
                self.assertEqual(e.exception.code,404)
                folder=Path(td)/'output'/'market';folder.mkdir(parents=True)
                (folder/'quote_execution_stress.json').write_text(json.dumps({'status':'NO_GO','tradeEnabled':False}))
                with urllib.request.urlopen(url,timeout=3) as f:
                    self.assertEqual(f.status,200)
                    self.assertFalse(json.load(f)['tradeEnabled'])
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)
                bridge.ROOT=old

    def test_symbol_url_cannot_access_quote_stress_without_token(self):
        import threading,urllib.request,urllib.error
        import bridge
        from runtime import Runtime,DEFAULT_CONFIG
        rt=Runtime(DEFAULT_CONFIG);server=bridge.create_server(rt,'/unguessable/')
        th=threading.Thread(target=server.serve_forever,daemon=True);th.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/quote-execution-stress'
            with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url,timeout=3)
            self.assertEqual(e.exception.code,404)
        finally:
            server.shutdown();server.server_close();th.join(timeout=3)

if __name__=='__main__':unittest.main()
