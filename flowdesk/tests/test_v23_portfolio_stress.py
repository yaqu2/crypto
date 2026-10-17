"""Read-only portfolio time/cost/censoring regression. No orders, no fictitious fills."""
import json, tempfile, unittest
from datetime import timedelta
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from market_archive import audit_archive
from market_forward_panel import load_scan
from market_portfolio_stress import Limits,prepare_intents,replay_scans,evaluate_portfolio
from test_v19_quote_stress import T,snapshot


def fixture_scan(t,changes=None,prices=None,spies=None,valid=None):
    changes=changes or {'US.AAOI':3.0}
    prices=prices or {}
    now=t.isoformat();source=(t-timedelta(seconds=3)).isoformat()
    quotes={}
    uni={}
    for code,change in changes.items():
        price=prices.get(code,100.)
        if valid is not None and code not in valid:continue
        quotes[code]={'code':code,'sourceAsOf':source,'lastPrice':price,'usable':'True',
            'changePct':change,'turnover':5e6,'bidPrice':price-.1,'askPrice':price+.1,
            'bidVol':1000,'askVol':1000,'bboValid':'True'}
    for code in changes:uni[code]={'code':code,'exchange':'US_Nasdaq'}
    return {'id':now,'time':t,'report':{'listedUniverseCount':20,'listedSnapshotCount':19},
            'universe':uni,'quotes':quotes,
            'bench':{'US.SPY':{'code':'US.SPY','sourceAsOf':source,'lastPrice':
                400. if spies is None else spies,'usable':'True'}}}

class PortfolioStressTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)

    def three(self,*,entry=None,exit=None,first=None):
        snapshot(self.root,T,**(first or {}))
        snapshot(self.root,T+timedelta(minutes=1),**(entry or {}))
        snapshot(self.root,T+timedelta(minutes=25),**(exit or {}))
        return evaluate_portfolio(self.root)

    def test_archive_missing_never_fabricates_trade(self):
        r=evaluate_portfolio(self.root)
        self.assertEqual(r['status'],'NO_GO_ARCHIVE_INTEGRITY')
        self.assertFalse(r['tradeEnabled'])

    def test_one_quote_crossing_is_not_a_fill(self):
        r=self.three(exit={'px':105,'bid':104.9,'ask':105.1})
        self.assertEqual(r['admitted'],1)
        self.assertEqual(r['closedQuoteCases'],1)
        self.assertEqual(r['censoredOrUnclosed'],0)
        self.assertEqual(r['confirmedFills'],0)
        self.assertFalse(r['alphaCertified'])
        self.assertFalse(r['accountProfitKnown'])
        x=next(x for x in r['observations'] if x['status']=='VERIFIED_FUTURE_DISPLAYED_QUOTE_ONLY')
        self.assertEqual(x['shares'],9)
        self.assertAlmostEqual(x['quoteNetByExtraCostUSD']['40'],(104.9-100.1)*9-9*100.1*.004,places=5)

    def test_missing_future_keeps_reserved_capital(self):
        snapshot(self.root,T)
        snapshot(self.root,T+timedelta(minutes=1))
        r=evaluate_portfolio(self.root)
        self.assertEqual(r['closedQuoteCases'],0)
        self.assertEqual(r['censoredOrUnclosed'],1)
        self.assertAlmostEqual(r['unresolvedReservedCapitalUsd'],900.9)
        self.assertEqual(r['closedOnlyQuotePnLByCostUsd']['40'],0)
        self.assertFalse(r['wholePortfolioPnlKnown'])

    def test_first_failed_exit_not_avoided_for_later_winner(self):
        snapshot(self.root,T)
        snapshot(self.root,T+timedelta(minutes=1))
        snapshot(self.root,T+timedelta(minutes=17),bid=None,ask=None)
        snapshot(self.root,T+timedelta(minutes=25),px=130,bid=129.9,ask=130.1)
        r=evaluate_portfolio(self.root)
        self.assertEqual(r['closedQuoteCases'],0)
        self.assertEqual(r['censoredOrUnclosed'],1)
        self.assertGreater(r['unresolvedReservedCapitalUsd'],0)

    def test_short_event_not_auto_borrowed(self):
        r=self.three(first={'change':-5})
        self.assertEqual(r['admitted'],0)
        self.assertEqual(r['outcomeCounts']['NO_GO_SHORT_BORROW_EVIDENCE'],1)

    def test_pre_market_is_not_valid_signal(self):
        from zoneinfo import ZoneInfo
        from datetime import datetime,timezone
        early=datetime(2026,10,14,8,0,tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
        snapshot(self.root,early)
        snapshot(self.root,early+timedelta(minutes=1))
        snapshot(self.root,early+timedelta(minutes=25))
        r=evaluate_portfolio(self.root)
        self.assertEqual(r['admitted'],0)

    def test_insufficient_entry_depth(self):
        r=self.three(entry={'avol':10})
        self.assertEqual(r['admitted'],0)
        self.assertEqual(r['outcomeCounts']['NO_GO_ENTRY_DISPLAYED_DEPTH'],1)

    def test_missing_exit_depth_is_censored(self):
        r=self.three(exit={'bvol':20})
        self.assertEqual(r['admitted'],1)
        self.assertEqual(r['closedQuoteCases'],0)
        self.assertEqual(r['censoredOrUnclosed'],1)
        self.assertIn('CENSORED_INSUFFICIENT_EXIT_DISPLAYED_DEPTH',r['outcomeCounts'])

    def test_invalid_early_entry_cannot_replace_with_later_entry(self):
        snapshot(self.root,T)
        snapshot(self.root,T+timedelta(seconds=30),bid=None,ask=None)
        snapshot(self.root,T+timedelta(minutes=2),px=100,bid=99.9,ask=100.1)
        snapshot(self.root,T+timedelta(minutes=25),px=110,bid=109.9,ask=110.1)
        r=evaluate_portfolio(self.root)
        self.assertEqual(r['admitted'],0)
        self.assertEqual(r['outcomeCounts']['NO_GO_ENTRY_BBO'],1)

    def test_missing_spy_makes_entry_ineligible(self):
        r=self.three(entry={'with_spy':False})
        self.assertEqual(r['admitted'],0)
        self.assertEqual(r['outcomeCounts']['NO_GO_ENTRY_PROVENANCE_OR_COVERAGE'],1)

    def test_future_timestamp_rejected_even_if_backend_marks_usable(self):
        r=self.three(entry={'clockshift':5})
        self.assertEqual(r['admitted'],0)
        self.assertEqual(r['outcomeCounts']['NO_GO_ENTRY_PROVENANCE_OR_COVERAGE'],1)

    def test_missing_spy_exit_makes_unknown_not_zero(self):
        r=self.three(exit={'with_spy':False})
        self.assertEqual(r['admitted'],1)
        self.assertEqual(r['censoredOrUnclosed'],1)
        self.assertEqual(r['closedQuoteCases'],0)

    def test_archive_tamper_detected(self):
        snapshot(self.root,T)
        scanned=next((self.root/'archive').iterdir())
        if not scanned.is_dir():
            scanned=next(p for p in (self.root/'archive').iterdir() if p.is_dir())
        (scanned/'latest_quotes.csv').write_text('corrupt')
        self.assertEqual(evaluate_portfolio(self.root)['status'],'NO_GO_ARCHIVE_INTEGRITY')

    def test_simultaneous_cap_precedes_future_results(self):
        codes={'US.AAOI':3.,'US.AAPL':4.}
        scans=[fixture_scan(T,codes),fixture_scan(T+timedelta(minutes=1),codes),
               fixture_scan(T+timedelta(minutes=25),codes,prices={'US.AAPL':150,'US.AAOI':90})]
        r=replay_scans(scans,Limits(concurrent_cap=1))
        self.assertEqual(r['admitted'],1)
        self.assertEqual(r['closedQuoteCases'],1)
        self.assertEqual(r['outcomeCounts']['CAP_MAX_CONCURRENT'],1)
        self.assertFalse(r['alphaCertified'])
        # Priority is alphabetical, not the future winner: AAPL is admitted.
        chosen=[x for x in r['observations'] if x['status']=='VERIFIED_FUTURE_DISPLAYED_QUOTE_ONLY']
        self.assertEqual([x['code'] for x in chosen],['US.AAOI'])

    def test_market_winners_do_not_determine_priority(self):
        codes={'US.AAOI':3.,'US.AAPL':3.}
        scans=[fixture_scan(T,codes),fixture_scan(T+timedelta(minutes=1),codes),
               fixture_scan(T+timedelta(minutes=25),codes,prices={'US.AAPL':150,'US.AAOI':90})]
        r=replay_scans(scans,Limits(concurrent_cap=1))
        chosen=[x for x in r['observations'] if x['status']=='VERIFIED_FUTURE_DISPLAYED_QUOTE_ONLY']
        self.assertEqual([x['code'] for x in chosen],['US.AAOI'])
        self.assertLess(r['closedOnlyQuotePnLByCostUsd']['40'],0)

    def test_missing_exit_holds_slot_blocks_next_day(self):
        codes={'US.AAOI':3.}
        scans=[fixture_scan(T,codes),fixture_scan(T+timedelta(minutes=1),codes)]
        t2=T+timedelta(days=1)
        scans += [fixture_scan(t2,codes),fixture_scan(t2+timedelta(minutes=1),codes),
                  fixture_scan(t2+timedelta(minutes=25),codes)]
        r=replay_scans(scans,Limits(concurrent_cap=1))
        self.assertEqual(r['admitted'],1)
        self.assertEqual(r['outcomeCounts']['CAP_MAX_CONCURRENT'],1)
        self.assertEqual(r['censoredOrUnclosed'],1)
        self.assertAlmostEqual(r['peakModeledGrossUsd'],900.9)

    def test_earliest_same_day_signal_not_reselected(self):
        scans=[fixture_scan(T),fixture_scan(T+timedelta(minutes=1)),
               fixture_scan(T+timedelta(minutes=10)),fixture_scan(T+timedelta(minutes=25))]
        r=replay_scans(scans)
        self.assertEqual(r['firstDailySignals'],1)
        self.assertEqual(r['admitted'],1)
        self.assertGreaterEqual(r['outcomeCounts']['DUPLICATE_SAME_SYMBOL_DAY_NOT_RESELECTED'],2)

    def test_gross_cap_limits_notional(self):
        codes={'US.AAOI':3.,'US.AAPL':3.}
        scans=[fixture_scan(T,codes),fixture_scan(T+timedelta(minutes=1),codes),
               fixture_scan(T+timedelta(minutes=25),codes)]
        r=replay_scans(scans,Limits(gross_cap_usd=1000.))
        self.assertEqual(r['admitted'],1)
        self.assertEqual(r['outcomeCounts']['CAP_GROSS_EXHAUSTED'],1)
        self.assertLessEqual(r['peakModeledGrossUsd'],1000)

    def test_config_is_frozen_and_short_disallowed(self):
        with self.assertRaises(ValueError):Limits(allow_short=True).verify()
        with self.assertRaises(ValueError):Limits(account_usd=float('nan')).verify()
        with self.assertRaises(ValueError):Limits(gross_cap_usd=200000).verify()
        with self.assertRaises(ValueError):Limits(concurrent_cap=2.3).verify()

    def test_adverse_cost_scenarios_monotone(self):
        r=self.three(exit={'px':104,'bid':103.9,'ask':104.1})
        x=next(x for x in r['observations'] if x['status']=='VERIFIED_FUTURE_DISPLAYED_QUOTE_ONLY')
        self.assertGreater(x['quoteNetByExtraCostUSD']['8'],x['quoteNetByExtraCostUSD']['20'])
        self.assertGreater(x['quoteNetByExtraCostUSD']['40'],x['quoteNetByExtraCostUSD']['120'])

    def test_uncertain_capital_worst_case_explicit(self):
        snapshot(self.root,T);snapshot(self.root,T+timedelta(minutes=1))
        r=evaluate_portfolio(self.root)
        self.assertAlmostEqual(r['unknownCapitalShockUsd']['100pct'],r['unresolvedReservedCapitalUsd'])
        self.assertFalse(r['wholePortfolioPnlKnown'])

    def test_daily_loss_pause_only_after_observed_exit(self):
        a={'US.AAOI':3.,'US.AAPL':0.}
        b={'US.AAOI':3.,'US.AAPL':3.}
        scans=[fixture_scan(T,a), fixture_scan(T+timedelta(minutes=1),a),
               fixture_scan(T+timedelta(minutes=25),a,prices={'US.AAOI':80}),
               fixture_scan(T+timedelta(minutes=30),b),
               fixture_scan(T+timedelta(minutes=31),b),
               fixture_scan(T+timedelta(minutes=52),b)]
        r=replay_scans(scans,Limits(modeled_daily_loss_pause_usd=100))
        self.assertEqual(r['admitted'],1)
        self.assertEqual(r['outcomeCounts']['PAUSE_MODELED_CLOSED_LOSS_ONLY'],1)
        self.assertLess(r['closedOnlyQuotePnLByCostUsd']['40'],-100)

    def test_same_timestamp_cannot_recycle_capital_in_advance(self):
        # AAOI A=10:00, entry=10:01, first exit scan=10:15.
        # AAPL A=10:14, entry=10:15. Tie: admit before releasing AAOI.
        a={'US.AAOI':3.,'US.AAPL':0.}
        b={'US.AAOI':3.,'US.AAPL':3.}
        scans=[fixture_scan(T,a),fixture_scan(T+timedelta(minutes=1),a),
               fixture_scan(T+timedelta(minutes=14),b),
               fixture_scan(T+timedelta(minutes=15),b),
               fixture_scan(T+timedelta(minutes=35),b)]
        r=replay_scans(scans,Limits(concurrent_cap=1))
        self.assertEqual(r['firstDailySignals'],2)
        self.assertEqual(r['admitted'],1)
        self.assertEqual(r['closedQuoteCases'],1)
        self.assertEqual(r['outcomeCounts']['CAP_MAX_CONCURRENT'],1)
        self.assertLessEqual(r['peakModeledGrossUsd'],1000)

    def test_nan_quote_does_not_create_entry(self):
        scans=[fixture_scan(T),fixture_scan(T+timedelta(minutes=1)),
               fixture_scan(T+timedelta(minutes=25))]
        scans[1]['quotes']['US.AAOI']['bidPrice']=float('nan')
        r=replay_scans(scans)
        self.assertEqual(r['admitted'],0)
        self.assertIn('NO_GO_ENTRY_BBO',r['outcomeCounts'])

    def test_missing_market_coverage_blocks_signal(self):
        scans=[fixture_scan(T),fixture_scan(T+timedelta(minutes=1)),
               fixture_scan(T+timedelta(minutes=25))]
        scans[0]['report']['listedSnapshotCount']=0
        r=replay_scans(scans)
        self.assertEqual(r['admitted'],0)
        self.assertIn('NO_GO_ORIGIN_PROVENANCE_OR_SCOPE',r['outcomeCounts'])

    def test_http_route_404_then_reads_report(self):
        import bridge,threading,urllib.request,urllib.error
        from runtime import Runtime,DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as tmp:
            old=bridge.ROOT;bridge.ROOT=Path(tmp)
            rt=Runtime(DEFAULT_CONFIG);server=bridge.create_server(rt,'/private/')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}/private/market-portfolio-stress'
            try:
                with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(url,timeout=2)
                self.assertEqual(cm.exception.code,404)
                output=Path(tmp)/'output'/'market';output.mkdir(parents=True)
                (output/'market_portfolio_stress.json').write_text(json.dumps({'tradeEnabled':False,'status':'NO_GO'}))
                with urllib.request.urlopen(url,timeout=2) as response:
                    self.assertFalse(json.load(response)['tradeEnabled'])
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2);bridge.ROOT=old

if __name__=='__main__':unittest.main()
