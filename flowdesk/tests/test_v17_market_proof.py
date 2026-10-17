"""Prevent false coverage, bad provenance, and non-evidence model recommendations."""
import unittest, json, tempfile
from datetime import datetime,timezone
from pathlib import Path
from market_scan import normalize_snapshot,summarize,run
from multiagent import evaluate_symbol
from symbol_diagnostic import diagnose

ET = __import__('zoneinfo').ZoneInfo('America/New_York')
NOW=datetime(2026,10,14,10,0,5,tzinfo=ET).astimezone(timezone.utc)

class MarketProof(unittest.TestCase):
    def test_good_regular_quote_needs_valid_clock(self):
        q=normalize_snapshot({'code':'US.AAOI','update_time':'2026-10-14 10:00:02',
            'last_price':99,'prev_close_price':100},NOW)
        self.assertEqual(q['diagnostic'],'USABLE_REGULAR')
        self.assertTrue(q['usable'])
        self.assertEqual(q['changePct'],-1)

    def test_premarket_access_is_not_fresh_regular(self):
        now=datetime(2026,10,14,8,37,5,tzinfo=ET).astimezone(timezone.utc)
        q=normalize_snapshot({'code':'US.AAOI','update_time':'2026-10-14 08:37:02',
            'last_price':99,'prev_close_price':100,'pre_price':95},now)
        self.assertEqual(q['diagnostic'],'OUTSIDE_REGULAR')
        self.assertFalse(q['usable']);self.assertEqual(q['prePriceUnverified'],95)

    def test_missing_source_time_not_signal(self):
        q=normalize_snapshot({'code':'US.SPY','last_price':100,'prev_close_price':99},NOW)
        self.assertEqual(q['diagnostic'],'MISSING_SOURCE_TIME')
        self.assertFalse(q['usable'])

    def test_quote_snapshot_no_response_distinct_from_session_issue(self):
        stock=[{'code':'US.AAOI','exchange':'US_Nasdaq'}]
        out=summarize(stock,[],NOW,NOW,batch_errors=[{'reason':'No quote access'}])
        self.assertEqual(out['snapshotReceivedCount'],0)
        self.assertFalse(out['snapshotAccessConfirmed'])
        self.assertEqual(out['batchErrorCount'],1)

    def test_summary_reports_scope_and_reasons(self):
        stock=[{'code':'US.AAOI','exchange':'US_Nasdaq'},{'code':'US.OTC','exchange':'US_Pink'}]
        q=normalize_snapshot({'code':'US.AAOI','update_time':'2026-10-14 10:00:02',
                              'last_price':99,'prev_close_price':100},NOW)
        s=summarize(stock,[q],NOW,NOW)
        self.assertEqual(s['listedSnapshotCount'],1)
        self.assertEqual(s['otcExcludedCount'],1)
        self.assertEqual(s['quoteDiagnosticCounts']['USABLE_REGULAR'],1)

    def test_model_acknowledges_verified_listed_scope(self):
        state={'US.AAOI':{}}
        market={'dataGate':'PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY',
                'fullListedUniverse':True,'completedAt':NOW.isoformat()}
        x=evaluate_symbol('US.AAOI',state,{},market=market,now=NOW)
        self.assertEqual(x['agents'][1]['status'],'OBSERVED')
        self.assertFalse(x['tradeEnabled'])

    def test_model_rejects_unscoped_listed(self):
        market={'dataGate':'PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY',
                'fullListedUniverse':False,'completedAt':NOW.isoformat()}
        x=evaluate_symbol('US.AAOI',{'US.AAOI':{}},{},market=market,now=NOW)
        self.assertEqual(x['agents'][1]['status'],'LIMITED')

    def test_symbol_state_cannot_invent_signal_without_feed(self):
        obj={'symbols':{'US.AAOI':{'quote':{'sourceAsOf':NOW.isoformat(), 'last':99},
            'book':{'receivedAt':NOW.isoformat(),'spreadBps':10}}},
            'feedQuality':{'status':'NO_GO_CONNECTION'}}
        out=diagnose(obj,'US.AAOI',now=NOW)
        self.assertEqual(out['status'],'NO_GO_GLOBAL_FEED')
        self.assertIsNone(out['modelHypothesis'])
        self.assertFalse(out['tradeEnabled'])

    def test_invalid_symbol_prevents_leak(self):
        self.assertEqual(diagnose({},'../etc/passwd')['status'],'BAD_SYMBOL')
        self.assertEqual(diagnose({'symbols':{}},'US.AAOI')['status'],'NO_GO_NOT_WATCHED')

    def test_delayed_book_denies_observation(self):
        earlier=datetime(2026,10,14,9,30,tzinfo=ET).astimezone(timezone.utc)
        x={'symbols':{'US.AAOI':{'quote':{'sourceAsOf':NOW.isoformat()},
           'book':{'receivedAt':earlier.isoformat(),'spreadBps':1}}},
           'feedQuality':{'status':'RECENT_RESEARCH_ONLY'},
           'multiAgent':{'symbols':{'US.AAOI':{'status':'OBSERVATION_ONLY','hypothesis':'偏多观察'}}}}
        out=diagnose(x,'US.AAOI',now=NOW)
        self.assertEqual(out['status'],'NO_GO_SYMBOL_ORDER_BOOK')
        self.assertIsNone(out['modelHypothesis'])

    def test_no_market_scope_no_trade_even_technically_fresh(self):
        state={'symbols':{'US.AAOI':{'quote':{'sourceAsOf':NOW.isoformat()},
            'book':{'receivedAt':NOW.isoformat(),'spreadBps':2}}},
            'feedQuality':{'status':'RECENT_RESEARCH_ONLY'},
            'multiAgent':{'symbols':{'US.AAOI':{'status':'OBSERVATION_ONLY','hypothesis':'供需压力观察：同向偏多','hardVetoes':['统计反证审查']}}}}
        out=diagnose(state,'US.AAOI',NOW)
        self.assertEqual(out['status'],'OBSERVATION_ONLY_NOT_TRADE_PERMISSION')
        self.assertIn('统计反证审查',out['agentVetoes'])
        self.assertFalse(out['profitCertified'])

    def test_symbol_diagnostic_no_account_data(self):
        state={'symbols':{'US.AAOI':{}},'accountID':'SECRET', 'feedQuality':{}}
        result=json.dumps(diagnose(state,'US.AAOI',NOW))
        self.assertNotIn('SECRET',result)

if __name__=='__main__':unittest.main()

class BrowserDiagnosticApi(unittest.TestCase):
    def test_localhost_diagnostic_api_is_only_evidence(self):
        import threading,urllib.request
        from bridge import create_server
        from runtime import Runtime
        config={'symbols':['US.SPY','US.AAOI'],'deepSymbols':['US.AAOI'],
                'benchmark':'US.SPY','peers':{}}
        rt=Runtime(config)
        server=create_server(rt,'/t17-')
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            endpoint='http://127.0.0.1:%d/t17-symbol-diagnostic?symbol=US.AAOI'%server.server_port
            data=json.loads(urllib.request.urlopen(endpoint,timeout=2).read())
            self.assertEqual(data['code'],'US.AAOI')
            self.assertEqual(data['status'],'NO_GO_GLOBAL_FEED')
            self.assertFalse(data['tradeEnabled'])
            bad=json.loads(urllib.request.urlopen(endpoint.replace('US.AAOI','bad%2Fpath'),timeout=2).read())
            self.assertEqual(bad['status'],'BAD_SYMBOL')
        finally:
            server.shutdown();server.server_close();rt.close();worker.join(timeout=3)
