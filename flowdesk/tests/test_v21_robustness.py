"""Adversarial tests: no claim of live profit / end-to-end broker execution."""
import json,tempfile,threading,unittest,urllib.error,urllib.request
from datetime import timedelta
from pathlib import Path
from market_robustness import analyze_rows,run,_week_leave_one_out,_bootstrap_week_ci
from tests.test_v20_market_laws import observation


class RobustnessTests(unittest.TestCase):
    def test_empty_blocks_profit(self):
        r=analyze_rows([])
        self.assertEqual(r['status'],'NO_GO_INSUFFICIENT_EVIDENCE')
        self.assertIsNone(r['profitVsRelativeMarket']['absoluteQuotedNet40BpsDailyEqualMean'])
        self.assertFalse(r['alphaCertified'])
        self.assertFalse(r['tradeEnabled'])

    def test_spy_outperformance_does_not_mean_profit(self):
        r=analyze_rows([observation(gross_bps=-30,spy_bps=-100)])
        p=r['profitVsRelativeMarket']
        self.assertEqual(p['absoluteQuotedNet40BpsDailyEqualMean'],-30)
        self.assertEqual(p['spyRelativeQuotedNet40BpsDailyEqualMean'],70)
        self.assertFalse(r['positiveQuotedOutcomeRobustnessConfirmed'])

    def test_keep_first_losing_signal(self):
        r=analyze_rows([observation(gross_bps=-80),observation(gross_bps=5000,minutes=25)])
        self.assertEqual(r['population']['firstEligibleSignals'],1)
        self.assertEqual(r['profitVsRelativeMarket']['absoluteQuotedNet40BpsDailyEqualMean'],-80)

    def test_missing_outcome_counterfactual_not_profit(self):
        a=observation(gross_bps=100)
        b=observation(code='US.NOQUOTE',status='NO_GO_SOURCE_MISSING')
        r=analyze_rows([a,b])
        sens=r['missingOutcomeSensitivity']
        self.assertEqual(sens['0']['equalETDayMeanBps'],50)
        self.assertEqual(sens['-50']['equalETDayMeanBps'],25)
        self.assertEqual(sens['-150']['equalETDayMeanBps'],-25)
        self.assertTrue(sens['-150']['isCounterfactualImputationNotARealizedLoss'])
        self.assertEqual(r['population']['unresolvedOrRejected'],1)
        self.assertEqual(r['population']['observedFraction'],.5)

    def test_cost_stress_deducts_further_cost(self):
        r=analyze_rows([observation(gross_bps=25)])
        self.assertEqual(r['extraCostOnObservedQuoteScenarioBps'],{'0':25,'20':5,'60':-35})

    def test_day_equal_weight_not_trades(self):
        rows=[observation(code=f'US.DENSE{i}',gross_bps=100) for i in range(40)]
        rows.append(observation(day=1,code='US.SPARSE',gross_bps=-100))
        r=analyze_rows(rows)
        self.assertEqual(r['profitVsRelativeMarket']['absoluteQuotedNet40BpsDailyEqualMean'],0)

    def test_leave_one_week_out_reveals_single_dominant_week(self):
        r=_week_leave_one_out({'2026-01-05':100,'2026-01-12':-20,'2026-01-19':-20})
        self.assertEqual(r['weekBlocks'],3)
        self.assertEqual(r['leaveOneWeekOutMinBps'],-20)
        self.assertEqual(r['leaveOneWeekOutMaxBps'],40)

    def test_no_significance_from_one_trading_day(self):
        rows=[observation(code=f'US.X{i:03d}',gross_bps=100) for i in range(300)]
        r=analyze_rows(rows)
        self.assertFalse(r['sampleCriteriaPassed'])
        self.assertIsNone(r['weekBlockBootstrap95PctIntervalBpsExploratory'])

    def test_interval_deterministic_with_multiple_weeks(self):
        d={str((__import__('datetime').date(2026,1,5)+timedelta(days=7*i)).isoformat()):float(i)
           for i in range(10)}
        self.assertIsNone(_bootstrap_week_ci(d))  # >=30 unique days required
        d={str((__import__('datetime').date(2026,1,5)+timedelta(days=i)).isoformat()):float(i)
           for i in range(70)}
        self.assertEqual(_bootstrap_week_ci(d),_bootstrap_week_ci(d))

    def test_regimes_not_training_filter(self):
        a=observation(code='US.A',benchmark_change=-1,gross_bps=-20)
        b=observation(code='US.B',benchmark_change=1,gross_bps=40)
        c=observation(code='US.C',benchmark_change=None,gross_bps=20)
        r=analyze_rows([a,b,c])
        self.assertEqual(r['population']['observedQuoteScenarios'],3)
        self.assertEqual(r['regimesDescriptiveOnly']['SPY_DOWN']['observations'],1)
        self.assertEqual(r['regimesDescriptiveOnly']['UNKNOWN_SPY_REGIME']['observations'],1)

    def test_tampered_quote_stress_numbers_rejected(self):
        row=observation(gross_bps=10)
        row['quoteExcessOverSpyBps40']=500000
        r=analyze_rows([row])
        self.assertEqual(r['population']['observedQuoteScenarios'],0)
        self.assertEqual(r['population']['missingReasons']['NO_GO_INVALID_SCENARIO_AMOUNTS'],1)

    def test_insufficient_archive_not_promoted(self):
        rows=[]
        for d in range(70):
            for k in range(20):rows.append(observation(day=d,code=f'US.T{k:02d}',gross_bps=100))
        ok=analyze_rows(rows)
        self.assertEqual(ok['status'],'EXPLORATORY_ROBUSTNESS_DIAGNOSTICS_ONLY')
        self.assertTrue(ok['sampleCriteriaPassed'])
        self.assertIsNotNone(ok['weekBlockBootstrap95PctIntervalBpsExploratory'])
        self.assertFalse(ok['alphaCertified'])
        bad=analyze_rows(rows,'NO_GO_ARCHIVE_TAMPER')
        self.assertFalse(bad['sampleCriteriaPassed'])
        self.assertEqual(bad['status'],'NO_GO_INSUFFICIENT_EVIDENCE')

    def test_live_archive_absent_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            r=run(Path(td))
            self.assertEqual(r['status'],'NO_GO_ARCHIVE_INTEGRITY')
            self.assertFalse(r['tradeEnabled'])

    def test_end_to_end_three_scans_and_tamper(self):
        from tests.test_v19_quote_stress import snapshot
        from tests.test_v20_market_laws import FIRST
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);at=FIRST.astimezone(__import__('datetime').timezone.utc)
            ident=snapshot(p,at,change=4,px=100,spy=400)
            snapshot(p,at+timedelta(minutes=1),change=4,px=100.1,spy=400.05)
            snapshot(p,at+timedelta(minutes=25),change=4,px=103,spy=402,bid=102.9,ask=103.1)
            r=run(p)
            self.assertEqual(r['population']['firstEligibleSignals'],1)
            self.assertEqual(r['population']['observedQuoteScenarios'],1)
            self.assertFalse(r['tradeEnabled'])
            (p/'archive'/ident/'latest_quotes.csv').write_text('tampered\n')
            self.assertEqual(run(p)['status'],'NO_GO_ARCHIVE_INTEGRITY')

    def test_http_optional_report_404_when_absent(self):
        import bridge
        from runtime import Runtime,DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as td:
            old=bridge.ROOT;bridge.ROOT=Path(td)
            server=bridge.create_server(Runtime(DEFAULT_CONFIG),'/safe/')
            th=threading.Thread(target=server.serve_forever,daemon=True);th.start()
            try:
                url=f'http://127.0.0.1:{server.server_port}/safe/market-robustness-report'
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url,timeout=3)
                self.assertEqual(caught.exception.code,404)
                p=Path(td)/'output'/'market';p.mkdir(parents=True)
                (p/'market_robustness_report.json').write_text(json.dumps({'status':'NO_GO_INSUFFICIENT_EVIDENCE'}))
                with urllib.request.urlopen(url,timeout=3) as response:
                    self.assertEqual(json.load(response)['status'],'NO_GO_INSUFFICIENT_EVIDENCE')
            finally:
                server.shutdown();server.server_close();th.join(timeout=3);bridge.ROOT=old

if __name__=='__main__':unittest.main()
