import json,hashlib,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from news_impact_lab import study,replay_source

ET=ZoneInfo('America/New_York')
BASE=datetime(2026,10,15,10,0,tzinfo=ET).astimezone(timezone.utc)

def ts(n,sec=0):return (BASE+timedelta(minutes=n,seconds=sec)).isoformat()

def b(sym,n,p,*,receipt=None,valid=True):
    return {'symbol':'US.'+sym,'startUtc':ts(n),'barEndUtc':ts(n+1),
        'firstAvailableUtc':receipt or ts(n+1,1),'open':p,'high':p*1.001,
        'low':p*.999,'close':p,'volume':100,'finalBarByTimeHeuristicOnly':valid}

def news(*,minute=3,sec=25,key='NEWS:test:1',revision=0,tickers=None):
    at=ts(minute,sec)
    return {'type':'news','key':key,'provider':'test','source_id':'1',
      'revision':revision,'published_at':ts(minute-1),'first_seen_at':at,
      'received_at':at,'value':{'title':'news','topics':['OPTICAL_NETWORK'],
                               'tickers':tickers if tickers is not None else ['AAOI']}}

def bars():
    return [b(sym,n,p0*(1+per*(n-2)))
            for sym,p0,per in [('AAOI',100,.001),('SPY',500,.0002),('SMH',250,.0005)]
            for n in range(0,22)]

class EventImpact(unittest.TestCase):
    def go(self,newslist=None,barlist=None,asof=None,**kwargs):
        return study(newslist if newslist is not None else [news()],
            barlist if barlist is not None else bars(),asof=asof or ts(22),**kwargs)

    def test_all_horizons_matured(self):
        r=self.go();self.assertEqual(r['maturedMatchedOutcomes'],3)
        self.assertEqual({x['horizonMinutes'] for x in r['results']},{1,5,15})
        self.assertTrue(all(x['netExecutablePnl'] is None for x in r['results']))
        self.assertFalse(r['profitCertified'])

    def test_return_spy_and_sector_are_separate(self):
        x=self.go()['results'][0]
        self.assertGreater(x['stockObservedReturn'],x['spyObservedReturn'])
        self.assertAlmostEqual(x['excessVsSPY'],x['stockObservedReturn']-x['spyObservedReturn'])
        self.assertIsNotNone(x['excessVsSectorProxy'])

    def test_event_cannot_see_same_minute_unfinished_bar(self):
        r=self.go()
        self.assertEqual(r['results'][0]['preEventReferenceUtc'],ts(3))
        self.assertEqual(r['results'][0]['firstFutureMinuteUtc'],ts(4))

    def test_bad_news_clock_is_rejected(self):
        row=news();row['published_at']=ts(5)
        with self.assertRaisesRegex(ValueError,'INVALID_NEWS_EVENT'):self.go([row])

    def test_future_news_is_not_selected(self):
        r=self.go([news(minute=19)],asof=ts(18))
        self.assertEqual(r['selectedNewsSymbolEvents'],0)
        self.assertEqual(r['excludedReasons']['NOT_OBSERVED_BY_ASOF'],1)

    def test_news_revision_does_not_create_signal(self):
        rows=[news(),news(revision=1,minute=7)]
        self.assertEqual(self.go(rows)['selectedNewsSymbolEvents'],1)

    def test_duplicate_first_revision_refused(self):
        with self.assertRaisesRegex(ValueError,'DUPLICATE_FIRST'):
            self.go([news(),news(minute=7)])

    def test_overlapping_same_symbol_events_dropped_first_wins(self):
        rows=[news(),news(minute=12,key='NEWS:test:2')]
        r=self.go(rows)
        self.assertEqual(r['selectedNewsSymbolEvents'],1)
        self.assertEqual(r['excludedReasons']['OVERLAPPING_EVENT_FIRST_ONLY'],1)

    def test_no_market_baseline(self):
        barlist=[x for x in bars() if x['symbol']!='US.SPY']
        self.assertEqual(self.go(barlist=barlist)['maturedMatchedOutcomes'],0)

    def test_no_sector_baseline(self):
        barlist=[x for x in bars() if x['symbol']!='US.SMH']
        self.assertEqual(self.go(barlist=barlist)['maturedMatchedOutcomes'],0)

    def test_no_sector_map_is_rejected(self):
        self.assertEqual(self.go([news(tickers=['ABCD'])])['excludedReasons']['NO_PREDECLARED_SECTOR_PROXY'],1)

    def test_unfinished_future_not_counted_as_failure(self):
        r=self.go(asof=ts(5,2))
        self.assertEqual(r['maturedMatchedOutcomes'],1)
        self.assertEqual(r['unmaturedCount'],2)

    def test_missing_future_bar_not_stitched(self):
        arr=[b for b in bars() if not (b['symbol']=='US.AAOI' and b['startUtc']==ts(7))]
        r=self.go(barlist=arr)
        self.assertEqual(r['maturedMatchedOutcomes'],1)
        self.assertEqual(r['excludedReasons']['MISSING_SYNCHRONIZED_FUTURE_BARS'],2)

    def test_duplicates_dont_select_favorable_revision(self):
        arr=bars();arr.append(b('AAOI',4,10000))
        r=self.go(barlist=arr)
        self.assertEqual(r['maturedMatchedOutcomes'],0)
        self.assertEqual(r['barExcludedReasons']['DUPLICATE_MINUTE_REVISION'],1)

    def test_stale_before_news_not_allowed(self):
        arr=[x for x in bars() if x['symbol']!='US.AAOI' or x['startUtc']>ts(3)]
        self.assertEqual(self.go(barlist=arr)['maturedMatchedOutcomes'],0)

    def test_late_received_pre_bar_is_not_hindsight_baseline(self):
        arr=bars()
        for x in arr:
            if x['symbol']=='US.AAOI' and x['startUtc']==ts(2):x['firstAvailableUtc']=ts(3,40)
        r=self.go(barlist=arr)
        self.assertEqual(r['results'][0]['preEventReferenceUtc'],ts(2))

    def test_non_completed_bar_excluded(self):
        arr=[{**x,'finalBarByTimeHeuristicOnly':False} if x['startUtc']==ts(4) else x for x in bars()]
        self.assertEqual(self.go(barlist=arr)['maturedMatchedOutcomes'],0)

    def test_nan_price_rejected(self):
        arr=[{**x,'close':float('nan')} if x['startUtc']==ts(4) else x for x in bars()]
        self.assertEqual(self.go(barlist=arr)['maturedMatchedOutcomes'],0)

    def test_regular_session_only(self):
        self.assertEqual(self.go([news(minute=-120)])['excludedReasons']['OUTSIDE_REGULAR_EVENT_WINDOW'],1)

    def test_only_stock_tickers_not_benchmarks(self):
        r=self.go([news(tickers=['SPY','SMH','AAOI'])])
        self.assertEqual(r['selectedNewsSymbolEvents'],1)
        self.assertEqual(r['excludedReasons']['BENCHMARK_IS_NOT_TRADE_TARGET'],2)

    def test_not_trade_fill_claim(self):
        r=self.go()
        self.assertTrue(all(not x['brokerFillVerified'] for x in r['results']))
        self.assertTrue(all(x['priceResponseNotCausality'] for x in r['results']))
        self.assertTrue(all(x['quoteExecutionVerified'] is False for x in r['results']))

    def test_incomplete_source_refuses_inference(self):
        r=self.go(source_coverage={'sourceCompleteness':'INCOMPLETE_OR_TRUNCATED_NEVER_CERTIFY'})
        self.assertEqual(r['status'],'NO_GO_INCOMPLETE_MARKET_RECORDING')

    def test_weekend_news_excluded(self):
        row=news();sat=datetime(2026,10,17,10,3,25,tzinfo=ET).astimezone(timezone.utc)
        row['published_at']=(sat-timedelta(minutes=2)).isoformat()
        row['first_seen_at']=row['received_at']=sat.isoformat()
        r=self.go([row],asof=(sat+timedelta(hours=1)).isoformat())
        self.assertEqual(r['excludedReasons']['OUTSIDE_REGULAR_EVENT_WINDOW'],1)

    def test_pre_news_ofi_never_includes_post_news(self):
        obs=[{'symbol':'US.AAOI','minuteUtc':ts(2),'status':'ARRIVAL_CLOCK_PROXY_ONLY','ofiTopProxy':8},
             {'symbol':'US.AAOI','minuteUtc':ts(4),'status':'ARRIVAL_CLOCK_PROXY_ONLY','ofiTopProxy':900}]
        r=self.go(micro_obs=obs)
        ctx=r['results'][0]['orderflowProxyContext']
        self.assertEqual(ctx['availableAtDecision']['sumTopOfiProxy'],8)
        self.assertEqual(ctx['futureOutcomeDiagnosticsNeverModelFeature']['sumTopOfiProxy'],900)

    def test_gapped_ofi_never_used_as_zero(self):
        obs=[{'symbol':'US.AAOI','minuteUtc':ts(2),'status':'GAP_OR_UNVERIFIED_L2_PROVENANCE','ofiTopProxy':500}]
        r=self.go(micro_obs=obs)
        self.assertIsNone(r['results'][0]['orderflowProxyContext']['availableAtDecision']['sumTopOfiProxy'])

    def test_source_replay_passes_for_closed_source(self):
        from multiscale_lab import minute_observations
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.jsonl'
            raw=b'{"kind":"health"}\n{"kind":"run_stop"}\n';p.write_bytes(raw)
            micro=minute_observations([{'kind':'health'},{'kind':'run_stop'}])
            report={'sourceCoverage':{'readEvents':2,'sha256ReadPrefix':hashlib.sha256(raw).hexdigest(),
                                      'sawRunStop':True},'microstructure':micro}
            self.assertEqual(replay_source(p,report)['readEvents'],2)

    def test_source_replay_detects_tamper(self):
        from multiscale_lab import minute_observations
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.jsonl';p.write_bytes(b'{"kind":"run_stop"}\n')
            micro=minute_observations([{'kind':'run_stop'}])
            report={'sourceCoverage':{'readEvents':1,'sha256ReadPrefix':'0'*64,
                                      'sawRunStop':True},'microstructure':micro}
            with self.assertRaisesRegex(ValueError,'MISMATCH'):replay_source(p,report)

    def test_source_replay_requires_full_closed_source(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.jsonl';p.write_text('{"kind":"health"}\n')
            with self.assertRaises(ValueError):replay_source(p,{'sourceCoverage':{'sawRunStop':False}})

if __name__=='__main__':unittest.main()
