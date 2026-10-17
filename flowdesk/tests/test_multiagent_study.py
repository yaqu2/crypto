import csv,hashlib,json,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
from multiagent import evaluate_symbol,evaluate_all,NAMES
from universe_agents import review,sha
from research_study import (normalize_book,is_session,observations,prepare,finalize,audit,MIN_DAYS_TOTAL,
                            CANDIDATES, EXTRA_COST_BPS)

UTC=timezone.utc


def when():return datetime(2026,10,14,15,0,tzinfo=UTC)


def good_state(t):
    ts=t.isoformat()
    book={'bids':[{'price':99.99,'size':1000}],'asks':[{'price':100.01,'size':100}],
          'imbalance':.82,'spreadBps':2,'receivedAt':ts}
    data={'US.TEST':{'book':book,'tape':{'asOf':ts,'count':50,'classifiedFraction':1.0,'cvd':900,
                                        'buyVolume':950,'sellVolume':50}}}
    intelligence={'US.TEST':{'quality':{'quoteFresh':True,'sourceAsOf':ts},
                             'benchmark':{'available':True,'excessPct':0.2},'peers':{'available':True,'count':2}}}
    feed={'status':'RECENT_RESEARCH_ONLY','deepBookFreshness':{'US.TEST':{'callbackFresh':True}}}
    return data,intelligence,feed


class TestAgent(unittest.TestCase):
    def test_all_eight_agents_independent_veto(self):
        now=when();state,intelligence,feed=good_state(now)
        x=evaluate_symbol('US.TEST',state,feed,intelligence,now=now)
        self.assertEqual(len(x['agents']),8)
        self.assertEqual(x['status'],'OBSERVATION_ONLY')
        self.assertTrue(x['hypothesis'])
        self.assertFalse(x['qualifiedForProfitClaim'])
        self.assertFalse(x['tradeEnabled'])
        self.assertIn(NAMES[5],x['hardVetoes'])
        self.assertIn(NAMES[7],x['hardVetoes'])

    def test_stale_feed_never_signals(self):
        now=when();state,intelligence,feed=good_state(now)
        feed['status']='NO_GO_STALE'
        x=evaluate_symbol('US.TEST',state,feed,intelligence,now=now)
        self.assertEqual(x['status'],'NO_GO_DATA')
        self.assertIsNone(x['hypothesis'])

    def test_old_source_timestamp_does_not_bypass_gate(self):
        now=when();state,intelligence,feed=good_state(now)
        intelligence['US.TEST']['quality']['sourceAsOf']=(now-timedelta(days=1)).isoformat()
        x=evaluate_symbol('US.TEST',state,feed,intelligence,now=now)
        self.assertFalse(x['agents'][0]['status']=='OBSERVED')
        self.assertIsNone(x['hypothesis'])

    def test_source_unverified_book_not_profit_certification(self):
        now=when();state,intelligence,feed=good_state(now)
        x=evaluate_all(state,feed,{'symbols':intelligence},now=now)
        self.assertFalse(x['fullMarketMBOAvailable'])
        self.assertEqual(x['verifiedAlphaCount'],0)
        self.assertFalse(x['symbols']['US.TEST']['agents'][2]['exchangeTimestampVerified'])

    def test_bad_book_crossed_blocks_hypothesis(self):
        now=when();state,intelligence,feed=good_state(now)
        state['US.TEST']['book']['asks'][0]['price']=99
        x=evaluate_symbol('US.TEST',state,feed,intelligence,now=now)
        self.assertEqual(x['agents'][2]['status'],'LIMITED')
        self.assertIsNone(x['hypothesis'])

    def test_missing_tape_blocks_direction(self):
        now=when();state,intelligence,feed=good_state(now)
        state['US.TEST']['tape']['count']=0
        x=evaluate_symbol('US.TEST',state,feed,intelligence,now=now)
        self.assertIsNone(x['hypothesis'])


class TestWholeMarket(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        stock=[{'code':'US.%04d'%i} for i in range(2100)]
        quotes=[{'code':x['code'],'usable':'True','turnover':str(1e6+i*1000),
                 'changePct':str(i%10),'fresh':'True'} for i,x in enumerate(stock)]
        for filename,fields,rows in [('us_stock_universe.csv',['code'],stock),
                                     ('latest_quotes.csv',['code','usable','turnover','changePct','fresh'],quotes)]:
            with (self.root/filename).open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
        self.report={'stockUniverseCount':2100,'snapshotReceivedCount':2100,'freshUsableCount':2100,
                     'dataGate':'PASS_SNAPSHOT_QUALITY_RESEARCH_ONLY','fullCurrentStockUniverse':True,
                     'completedAt':when().isoformat(),'batchErrors':[],
                     'universeSha256':sha(self.root/'us_stock_universe.csv'),
                     'quotesSha256':sha(self.root/'latest_quotes.csv')}
        (self.root/'latest_report.json').write_text(json.dumps(self.report))
    def tearDown(self):self.tmp.cleanup()

    def test_rank_many_without_indicating_profit(self):
        x=review(self.root,now=when())
        self.assertEqual(len(x['candidates']),60)
        self.assertEqual(x['status'],'RESEARCH_WATCHLIST_ONLY')
        self.assertFalse(x['predictionValidated'])

    def test_tampered_quotes_raise(self):
        with (self.root/'latest_quotes.csv').open('a') as f:f.write('US.HACK,True,1,500,True\n')
        with self.assertRaisesRegex(ValueError,'TAMPER'):review(self.root,now=when())

    def test_stale_report_blocks_certification(self):
        x=review(self.root,now=when()+timedelta(days=1))
        self.assertEqual(x['status'],'NO_GO_FULL_MARKET_COVERAGE')
        self.assertFalse(x['candidates'])

    def test_partial_coverage_blocks_candidates(self):
        self.report['dataGate']='NO_GO_COVERAGE'
        (self.root/'latest_report.json').write_text(json.dumps(self.report))
        x=review(self.root,now=when())
        self.assertFalse(x['candidates'])


class TestStudy(unittest.TestCase):
    def test_invalid_cross_book(self):
        self.assertIsNone(normalize_book({'data':{'Bid':[[102,100]],'Ask':[[101,20]]}}))

    def test_no_future_entry_spoofing(self):
        base=datetime(2026,10,12,15,0,tzinfo=UTC).timestamp()
        x=dict(bid=99,ask=101,imbalance={1:.8,5:.8},spread=200)
        # signal and an 'exit' only; entry within 2-15sec is missing
        rows={'US.X':[(base,'2026-10-12',x),(base+62,'2026-10-12',x)]}
        self.assertEqual(observations(rows,['2026-10-12'],CANDIDATES[0]),[])

    def test_study_halts_on_old_sample(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'events.jsonl'
            src.write_text(json.dumps({'kind':'book','origin':'push','receivedAt':'2026-10-12T15:00:00+00:00',
                                       'data':{'code':'US.A','Bid':[[100,100]],'Ask':[[100.01,50]]}})+'\n')
            x=audit(src)
            self.assertEqual(x['gate'],'NO_GO_INDEPENDENT_SAMPLE')
            with self.assertRaisesRegex(RuntimeError,'NO_GO'):prepare(src,Path(td)/'experiment')
            self.assertFalse((Path(td)/'experiment'/'frozen_plan.json').exists())

    def test_frozen_plan_is_one_shot_and_tamper_protected(self):
        # Small controlled high-level synthetic corpus to exercise sealing; NOT returns/profit evidence.
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);src=root/'data.jsonl'
            start=datetime(2026,8,10,14,0,tzinfo=UTC)
            times=[]
            for d in range(100):
                t=start+timedelta(days=d)
                if is_session(t):times.append(t)
                if len(times)>=MIN_DAYS_TOTAL+7:break
            lines=[]
            for dt in times:
                for k in range(22):
                    for sec in (0,5,70,350):
                        t=dt+timedelta(seconds=sec)
                        lines.append(json.dumps({'kind':'book','origin':'push','receivedAt':t.isoformat(),
                           'data':{'code':f'US.Z{k:03}','Bid':[[100,1000]],'Ask':[[100.01,100]]}}))
            src.write_text('\n'.join(lines)+'\n')
            out=root/'exp';plan=prepare(src,out)
            self.assertEqual(len(plan['candidates']),8)
            # refuse changed source before consuming holdout
            with src.open('a') as f:f.write(' \n')
            with self.assertRaisesRegex(RuntimeError,'mismatch'):finalize(src,out)
            src.write_text('\n'.join(lines)+'\n')
            report=finalize(src,out)
            self.assertEqual(report['overallGate'],'NO_GO_CERTIFIED_ALPHA')
            self.assertEqual(report['hypothesesTested'],8)
            self.assertTrue((out/'holdout_consumed.lock').exists())
            with self.assertRaises(FileExistsError):finalize(src,out)

if __name__=='__main__':unittest.main()

class TestWatchlistProposal(unittest.TestCase):
    def test_refuses_uncertified_market(self):
        from prepare_watchlist import propose
        with tempfile.TemporaryDirectory() as td:
            with patch('prepare_watchlist.review',return_value={'status':'NO_GO_COVERAGE'}):
                x=propose(td,None)
                self.assertEqual(x['symbols'],[])
                self.assertFalse(x['tradeEnabled'])

    def test_real_snapshot_ranking_is_hypothesis_only(self):
        from prepare_watchlist import propose
        rows=[{'code':f'US.TEST{i:03}'} for i in range(60)]
        with patch('prepare_watchlist.review',return_value={
            'status':'RESEARCH_WATCHLIST_ONLY','candidates':rows}):
            x=propose('.',None,36,5)
            self.assertEqual(len(x['symbols']),36)
            self.assertEqual(len(x['deepSymbols']),5)
            self.assertEqual(x['benchmark'],'US.SPY')
            self.assertFalse(x['tradeEnabled'])

class TestPlanSeal(unittest.TestCase):
    def test_changed_freeze_date_rejected_before_holdout(self):
        import research_study as study
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);data=out/'data.jsonl';data.write_text('{}\n')
            fp=out/'frozen_plan.json'
            plan={'rawSha256':sha(data),'studySourceSha256':sha(study.__file__),
                  'candidates':CANDIDATES,'extraCostBps':EXTRA_COST_BPS,'splitDate':'2026-01-01'}
            fp.write_text(json.dumps(plan));(out/'plan_seal.sha256').write_text(sha(fp))
            plan['splitDate']='2026-06-01';fp.write_text(json.dumps(plan))
            with self.assertRaisesRegex(RuntimeError,'Frozen plan hash mismatch'):
                finalize(data,out)
            self.assertFalse((out/'holdout_consumed.lock').exists())
