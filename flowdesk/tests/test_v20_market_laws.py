"""Quant research red-team tests, no broker data, all synthetic numbers."""
import json,tempfile,threading,unittest,urllib.error,urllib.request
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from market_law_lab import prepare_events,analyze_stress_rows,_signflip_p,_holm,run

ET=ZoneInfo('America/New_York')
FIRST=datetime(2026,1,5,10,0,tzinfo=ET)


def observation(day=0,code='US.AAOI',change=3.,turnover=3e6,
                gross_bps=100.,spy_bps=10.,status='EXPLORATORY_QUOTE_CROSSING_ONLY',
                minutes=0,benchmark_change=1.,notional=1000.):
    t=(FIRST+timedelta(days=day,minutes=minutes)).astimezone(timezone.utc).isoformat()
    # Declared 40bp already deducted from gross_bps.
    p=round(notional*gross_bps/10000,4)
    return dict(code=code,originAt=t,originScan=f'mock-{day}-{minutes}-{code}',
                signalDirection='UP_EVENT',originChangePct=change,
                originObservedTurnover=turnover,originBenchmarkChangePct=benchmark_change,
                status=status,quoteCrossingPnLByCostUSD={'40':p},
                entryNotionalUSD=notional,spyReturnBpsDuringHold=spy_bps,
                quoteExcessOverSpyBps40=gross_bps-spy_bps)

class MarketLawTests(unittest.TestCase):
    def test_empty_no_claim(self):
        result=analyze_stress_rows([])
        self.assertEqual(result['status'],'NO_GO_INSUFFICIENT_INDEPENDENT_EVIDENCE')
        self.assertEqual(result['population']['observedETDays'],0)
        self.assertFalse(result['alphaCertified'])

    def test_first_per_day_stock_not_winner(self):
        rows=[observation(gross_bps=-50),observation(gross_bps=9999,minutes=20)]
        events=prepare_events(rows)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['netQuoteScenarioBps'],-50)

    def test_failed_first_is_not_replaced(self):
        a=observation(status='NO_GO_TARGET_SPY_CLOCK_OR_SOURCE')
        b=observation(gross_bps=9999,minutes=10)
        p=analyze_stress_rows([a,b])
        self.assertEqual(p['population']['censoredOrRejected'],1)
        self.assertEqual(p['population']['quoteScenariosObserved'],0)

    def test_relative_spy_not_fake_positive(self):
        p=analyze_stress_rows([observation(gross_bps=50,spy_bps=110)])
        e=p['sampleRows'][0]
        self.assertEqual(e['quoteExcessOverSpyBps40'],-60)

    def test_tampered_relative_rejected(self):
        r=observation();r['quoteExcessOverSpyBps40']=100000
        x=analyze_stress_rows([r])
        self.assertEqual(x['population']['quoteScenariosObserved'],0)
        self.assertEqual(x['population']['failureReasons']['NO_GO_INVALID_SCENARIO_AMOUNTS'],1)

    def test_censored_denominator_not_deleted(self):
        rows=[observation(code=f'US.T{n:04d}',status='NO_GO_DISPLAYED_DEPTH_LIMIT') for n in range(99)]
        rows.append(observation(code='US.LAST',gross_bps=100))
        x=analyze_stress_rows(rows)
        self.assertEqual(x['population']['firstUpEventPerETDaySymbol'],100)
        self.assertEqual(x['population']['observedFraction'],.01)
        self.assertIsNone(x['hypotheses']['H1_up_event_quote_crossing_positive']['exploratorySignFlipP'])

    def test_future_outcome_not_candidate_filter(self):
        r=observation(gross_bps=110)
        x=prepare_events([r])[0]
        r['quoteCrossingPnLByCostUSD']['40']=-5.
        y=prepare_events([r])[0]
        self.assertEqual(x['firstAt'],y['firstAt'])
        self.assertEqual(x['code'],y['code'])

    def test_unknown_and_negative_event_excluded(self):
        rows=[observation(change=-3),observation(change=1,code='US.B'),observation(change=3,code='US.C')]
        rows[0]['signalDirection']='DOWN_EVENT'
        self.assertEqual(len(prepare_events(rows)),1)

    def test_strong_vs_mild_is_paired_within_date(self):
        rows=[observation(code='US.A',change=7,gross_bps=100,spy_bps=0),
              observation(code='US.B',change=3,gross_bps=-40,spy_bps=0)]
        x=analyze_stress_rows(rows)
        h=x['hypotheses']['H2_stronger_up_event_outperforms']
        self.assertEqual(h['daysWithPairedEvidence'],1)
        self.assertEqual(h['dailyDifferenceBps'],140)
        self.assertFalse(h['sampleCriteriaPassed'])

    def test_missing_control_means_no_paired_comparison(self):
        x=analyze_stress_rows([observation(code=f'US.{i}',change=7) for i in range(30)])
        self.assertEqual(x['hypotheses']['H2_stronger_up_event_outperforms']['daysWithPairedEvidence'],0)
        self.assertIsNone(x['hypotheses']['H2_stronger_up_event_outperforms']['exploratorySignFlipP'])

    def test_permutation_one_week_not_significant(self):
        self.assertIsNone(_signflip_p([1000]*7))
        self.assertAlmostEqual(_signflip_p([1]*8),1/256)

    def test_holm_monotone_and_not_smaller_than_p(self):
        x=_holm({'a':.01,'b':.02,'c':.4})
        self.assertEqual(x,{'a':.03,'b':.04,'c':.4})
        self.assertEqual(_holm({}),{})

    def test_statistical_evidence_remains_exploratory(self):
        # Synthetic 70 days * 20 symbols with paired strong/weak turnover.
        rows=[]
        for day in range(70):
            for n in range(20):
                rows.append(observation(day=day,code=f'US.S{n:03d}',
                           change=7 if n%2 else 3,turnover=1e7 if n%2 else 3e6,
                           gross_bps=50 if n%2 else 5,spy_bps=0))
        result=analyze_stress_rows(rows)
        self.assertEqual(result['status'],'EXPLORATORY_PATTERNS_NOT_CERTIFIED')
        self.assertFalse(result['alphaCertified'])
        self.assertFalse(result['tradeEnabled'])
        h=result['hypotheses']['H2_stronger_up_event_outperforms']
        self.assertGreaterEqual(h['weekBlocks'],8)
        self.assertIsNotNone(h['holmAdjustedP'])
        self.assertFalse(h['isPreregisteredHoldout'])

    def test_one_missing_record_blocks_statistics_even_with_many_days(self):
        rows=[]
        for day in range(70):
            for n in range(20):
                rows.append(observation(day=day,code=f'US.S{n:03d}',
                    change=7 if n%2 else 3,turnover=1e7 if n%2 else 3e6,gross_bps=30,spy_bps=0,
                    status='NO_GO_SOURCE_MISSING' if n==day%20 else 'EXPLORATORY_QUOTE_CROSSING_ONLY'))
        result=analyze_stress_rows(rows)
        self.assertEqual(result['status'],'EXPLORATORY_PATTERNS_NOT_CERTIFIED')
        # Below 95% outcome coverage => no exploratory statistical p-values.
        rows[1]['status']='NO_GO_SOURCE_MISSING'
        rows[2]['status']='NO_GO_SOURCE_MISSING'
        result2=analyze_stress_rows(rows)
        self.assertEqual(result2['status'],'NO_GO_INSUFFICIENT_INDEPENDENT_EVIDENCE')
        self.assertIsNone(result2['hypotheses']['H1_up_event_quote_crossing_positive']['exploratorySignFlipP'])

    def test_archive_integrity_is_hard_gate(self):
        r=run(Path('/tmp/does-not-exist-market-law-lab'))
        self.assertEqual(r['status'],'NO_GO_ARCHIVE_INTEGRITY')

    def test_market_regime_is_only_descriptive(self):
        r1=observation(benchmark_change=-1)
        r2=observation(code='US.C',benchmark_change=1)
        x=analyze_stress_rows([r1,r2])
        self.assertEqual(x['regimeDescriptiveOnly']['SPY_DOWN']['count'],1)
        self.assertEqual(x['regimeDescriptiveOnly']['SPY_UP_OR_FLAT']['count'],1)

class RealArchivePipelineTests(unittest.TestCase):
    def test_three_archived_scans_flow_into_new_lab(self):
        from tests.test_v19_quote_stress import snapshot
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            origin=FIRST.astimezone(timezone.utc)
            snapshot(base,origin,change=4.,px=100.,spy=400.)
            snapshot(base,origin+timedelta(minutes=1),change=4.,px=100.1,spy=400.05)
            snapshot(base,origin+timedelta(minutes=25),change=5.,px=103.,spy=402.,bid=102.9,ask=103.1)
            result=run(base)
            self.assertEqual(result['population']['firstUpEventPerETDaySymbol'],1)
            self.assertEqual(result['population']['quoteScenariosObserved'],1)
            r=result['sampleRows'][0]
            self.assertIsNotNone(r['quoteExcessOverSpyBps40'])
            self.assertLess(r['quoteExcessOverSpyBps40'],r['netQuoteScenarioBps'])
            self.assertFalse(result['alphaCertified'])

    def test_integrity_tamper_blocks_new_lab(self):
        from tests.test_v19_quote_stress import snapshot
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            a=snapshot(base,FIRST.astimezone(timezone.utc))
            snapshot(base,(FIRST+timedelta(minutes=1)).astimezone(timezone.utc))
            snapshot(base,(FIRST+timedelta(minutes=25)).astimezone(timezone.utc))
            p=base/'archive'/a/'latest_quotes.csv'
            p.write_text('forged')
            result=run(base)
            self.assertEqual(result['status'],'NO_GO_ARCHIVE_INTEGRITY')

class EndpointTests(unittest.TestCase):
    def test_http_research_report_is_optional_not_fake(self):
        import bridge
        from runtime import Runtime,DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as td:
            old=bridge.ROOT
            bridge.ROOT=Path(td)
            rt=Runtime(DEFAULT_CONFIG)
            server=bridge.create_server(rt,'/token/')
            th=threading.Thread(target=server.serve_forever,daemon=True);th.start()
            try:
                url=f'http://127.0.0.1:{server.server_port}/token/market-law-report'
                with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(url,timeout=3)
                self.assertEqual(e.exception.code,404)
                folder=Path(td)/'output'/'market';folder.mkdir(parents=True)
                (folder/'market_law_report.json').write_text(json.dumps({'status':'NO_GO','tradeEnabled':False}))
                with urllib.request.urlopen(url,timeout=3) as rsp:
                    self.assertEqual(json.load(rsp)['status'],'NO_GO')
            finally:
                server.shutdown();server.server_close();th.join(timeout=3)
                bridge.ROOT=old

if __name__=='__main__':unittest.main()
