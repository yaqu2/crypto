"""Regression tests derived from an actual Mac OpenD failed whole-market scan.

No OpenD access. All tests are deterministic mocks and do not certify live data.
"""
import json,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from market_scan import exchange_scope,normalize_snapshot,run,summarize

ET=ZoneInfo('America/New_York')

class Fixture:
    def __init__(self,items):
        self.items=items;self.calls=[]
    def get_stock_basicinfo(self,*args):return 0,self.items
    def get_market_snapshot(self,codes):
        self.calls.append(tuple(codes))
        if 'US.BAD' in codes:
            return 1,'US OTC market quote is not available for BAD.'
        return 0,[{'code':x,'update_time':'2026-10-14 10:59:55','last_price':20,'prev_close_price':19,
                   'volume':200,'turnover':4000} for x in codes]

class OTCBatchRecovery(unittest.TestCase):
    def test_moomoo_exchange_classification(self):
        for raw in ('US_NYSE','ExchType.US_Nasdaq','US_AMEX',4,5,7,'NYSE'):
            self.assertEqual(exchange_scope(raw),'US_PRIMARY_LISTED',raw)
        for raw in ('US_Pink','ExchType.US_Pink',6,'OTCQX','OTC'):
            self.assertEqual(exchange_scope(raw),'US_OTC',raw)
        self.assertEqual(exchange_scope(''),'UNKNOWN_EXCHANGE')

    def test_failed_otc_cannot_poison_nyse_nasdaq_batch(self):
        items=[{'code':'US.AAPL','exchange_type':'US_Nasdaq'},
               {'code':'US.AHNRF','exchange_type':'US_Pink'},
               {'code':'US.AAOI','exchange_type':'US_Nasdaq'},
               {'code':'US.BAC','exchange_type':'US_NYSE'},
               {'code':'US.XYZZ','exchange_type':'unknown'}]
        fake=Fixture(items)
        with tempfile.TemporaryDirectory() as td:
            result=run(fake,'US','STOCK',0,td)
            self.assertEqual(result['stockUniverseCount'],5)
            self.assertEqual(result['listedUniverseCount'],3)
            self.assertEqual(result['otcExcludedCount'],1)
            self.assertEqual(result['unknownExchangeCount'],1)
            self.assertEqual(result['snapshotReceivedCount'],3)
            self.assertEqual(result['quoteDenialCount'],0)
            self.assertEqual(result['batchErrorCount'],0)
            self.assertEqual(set(fake.calls[0]),{'US.AAPL','US.AAOI','US.BAC'})
            self.assertEqual(result['dataGate'],'NO_GO_COVERAGE')  # premkt & tiny sample
            self.assertFalse(result['fullCurrentStockUniverse'])
            report=json.loads((Path(td)/'latest_report.json').read_text())
            self.assertEqual(report['listedMissingSnapshotCount'],0)

    def test_misclassified_otc_isolated_and_others_salvaged(self):
        items=[{'code':c,'exchange_type':'US_Nasdaq'} for c in ('US.AAOI','US.BAD','US.NVDA')]
        fake=Fixture(items)
        with tempfile.TemporaryDirectory() as td:
            result=run(fake,'US','STOCK',0,td,interval=.55)
            self.assertEqual(result['snapshotReceivedCount'],2)
            self.assertEqual(result['quoteDenialCount'],1)
            self.assertEqual(result['quoteDenials'][0]['code'],'US.BAD')
            self.assertEqual(result['listedMissingSnapshotCount'],1)
            self.assertEqual(len(fake.calls),2)
            self.assertEqual(result['dataGate'],'NO_GO_COVERAGE')

    def test_opaque_error_binary_recovery_bounded(self):
        class Opaque(Fixture):
            def get_market_snapshot(self,codes):
                self.calls.append(tuple(codes))
                if 'US.BAD' in codes:return 1,'unsupported symbol'
                return 0,[{'code':x,'update_time':'2026-10-14 10:59:55','last_price':20,'prev_close_price':19} for x in codes]
        fake=Opaque([{'code':c,'exchange_type':'US_Nasdaq'} for c in ('US.AAA','US.BAD','US.BBB','US.CCC')])
        with tempfile.TemporaryDirectory() as td:
            result=run(fake,'US','STOCK',0,td,interval=.55)
            self.assertEqual(result['snapshotReceivedCount'],3)
            self.assertGreaterEqual(result['batchErrorCount'],1)
            self.assertLessEqual(result['snapshotRequestCount'],13)

    def test_unknown_only_must_not_certify_listed(self):
        universe=[{'code':f'US.X{i}','exchange':''} for i in range(2200)]
        quotes=[{'code':x['code'],'usable':True,'changePct':1} for x in universe]
        now=datetime(2026,10,14,11,tzinfo=ET).astimezone(timezone.utc)
        r=summarize(universe,quotes,now,now,requested_codes=[x['code'] for x in universe])
        self.assertNotEqual(r['dataGate'],'PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY')
        self.assertFalse(r['fullListedUniverse'])

    def test_primary_listed_can_pass_only_as_limited_scope(self):
        universe=[{'code':f'US.X{i}','exchange':'US_Nasdaq'} for i in range(2100)]
        universe.append({'code':'US.OTC','exchange':'US_Pink'})
        quotes=[{'code':x['code'],'usable':True,'changePct':1} for x in universe[:-1]]
        now=datetime(2026,10,14,11,tzinfo=ET).astimezone(timezone.utc)
        r=summarize(universe,quotes,now,now,requested_codes=[x['code'] for x in universe[:-1]])
        self.assertEqual(r['dataGate'],'PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY')
        self.assertTrue(r['fullListedUniverse'])
        self.assertFalse(r['fullCurrentStockUniverse'])
        self.assertEqual(r['otcExcludedCount'],1)

    def test_early_premarket_snapshot_cannot_be_usably_fresh(self):
        now=datetime(2026,10,14,8,38,tzinfo=ET).astimezone(timezone.utc)
        snap=normalize_snapshot({'code':'US.AAOI','update_time':'2026-10-14 08:37:55',
                                 'last_price':20,'prev_close_price':19},now)
        self.assertFalse(snap['fresh'])
        self.assertFalse(snap['usable'])

    def test_request_budget_fails_closed(self):
        items=[{'code':f'US.A{i:04d}','exchange_type':'US_Nasdaq'} for i in range(401)]
        fake=Fixture(items)
        with tempfile.TemporaryDirectory() as td:
            report=run(fake,'US','STOCK',0,td,max_requests=1)
            self.assertEqual(report['snapshotRequestCount'],1)
            self.assertEqual(report['batchErrorCount'],1)
            self.assertEqual(report['dataGate'],'NO_GO_COVERAGE')

if __name__=='__main__':unittest.main()
