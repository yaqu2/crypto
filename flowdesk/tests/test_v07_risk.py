"""Adversarial v0.7 tests; synthetic quotes prove software invariants, NOT returns."""
import hashlib,json, tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from prospective import ForwardStudy,verify_chain,extract_book,sha_bytes
from forward_report import report
from alpha_gate import study_record,joint_day_signflip
from run_inventory import register_run,read_inventory
from audit_forward_runs import audit_all
from source_crosscheck import crosscheck

UTC=timezone.utc
START=datetime(2026,10,12,14,0,tzinfo=UTC) # 10am Eastern Monday


def book(bid=99.99,ask=100.01,bs=1000,ass=100):
    return {'bids':[{'price':round(bid-i*.01,4),'size':bs} for i in range(5)],
            'asks':[{'price':round(ask+i*.01,4),'size':ass} for i in range(5)]}


class V07Research(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_shadow_l1_buy_size_rejected(self):
        s=ForwardStudy(self.root/'shadow',['US.X'])
        s.on_book('US.X',book(ass=20),START,allowed=True)
        s.on_book('US.X',book(ass=20),START+timedelta(seconds=3),allowed=True)
        self.assertGreater(s.counts['liquidityRejected'],0)
        s.close()
        plan,events=verify_chain(self.root/'shadow')
        self.assertEqual(plan['version'],3)
        self.assertTrue(any(x.get('reason')=='INSUFFICIENT_DISPLAYED_ENTRY_L1_FOR_SHADOW_SIZE' for x in events))
        self.assertTrue(report(self.root/'shadow')['completeRun'])

    def test_liquidity_at_exit_not_counted_as_profit(self):
        s=ForwardStudy(self.root/'shadow',['US.X'])
        s.on_book('US.X',book(),START,allowed=True)
        s.on_book('US.X',book(),START+timedelta(seconds=3),allowed=True)
        for sec in (15,27,39,51):s.on_book('US.X',book(),START+timedelta(seconds=sec),allowed=True)
        s.on_book('US.X',book(ass=5),START+timedelta(seconds=64),allowed=True)
        self.assertGreater(s.counts['liquidityCensored'],0)
        s.close()
        _,events=verify_chain(self.root/'shadow')
        censored={x['id'] for x in events if x['type']=='CENSORED'}
        exits={x['id'] for x in events if x['type']=='PAPER_EXIT'}
        self.assertFalse(censored & exits)
        self.assertTrue(report(self.root/'shadow')['completeRun'])

    def test_model_price_includes_cross_and_cost_and_shares(self):
        s=ForwardStudy(self.root/'shadow',['US.X'])
        for sec,b in [(0,book()),(3,book())]+[(x,book()) for x in (15,27,39,51)]+[(64,book(100.19,100.21))]:
            s.on_book('US.X',b,START+timedelta(seconds=sec),allowed=True)
        s.close()
        _,events=verify_chain(self.root/'shadow')
        exits=[x for x in events if x['type']=='PAPER_EXIT']
        self.assertGreater(len(exits),0)
        for x in exits:
            self.assertGreater(x['shadowShares'],0)
            self.assertGreaterEqual(x['exitL1Size']*.1+1e-9,x['shadowShares'])
            self.assertAlmostEqual(x['netQuoteModelBps']['40'],x['quotedCrossingBps']-40,places=4)

    def test_observed_minutes_bounded_and_first_per_minute(self):
        s=ForwardStudy(self.root/'shadow',['US.X'])
        for sec in (0,1,3,15,27,39,51,60,61,73,85,97,109,120):
            s.on_book('US.X',book(bs=200,ass=200),START+timedelta(seconds=sec),allowed=True)
        self.assertEqual(len(s.last_observed_minute),1)
        s.close()
        _,events=verify_chain(self.root/'shadow')
        mins=[x for x in events if x['type']=='BOOK_MINUTE']
        self.assertEqual(len(mins),3)
        self.assertEqual(len({x['etMinute'] for x in mins}),3)

    def test_minute_lineage_detects_book_rewrite(self):
        s=ForwardStudy(self.root/'shadow',['US.X'])
        s.on_book('US.X',book(bs=200,ass=200),START,allowed=True)
        s.close()
        raw=self.root/'events.jsonl'
        rawrow={'kind':'book','origin':'push','callbackReceivedAt':START.isoformat(),
                'data':{'code':'US.X',
                        'Bid':[[x['price'],x['size']] for x in book()['bids']],
                        'Ask':[[x['price'],x['size']] for x in book()['asks']]}}
        raw.write_text(json.dumps(rawrow)+'\n')
        self.assertGreater(crosscheck(self.root/'shadow',raw)['paperEventsMatched'],0)
        rawrow['data']['Ask'][0][0]=102
        raw.write_text(json.dumps(rawrow)+'\n')
        with self.assertRaisesRegex(ValueError,'MISSING_OR_DIFFERENT_RAW_CALLBACK_BOOK'):
            crosscheck(self.root/'shadow',raw)

    def test_legacy_v06_frozen_source_kept_and_readable(self):
        s=ForwardStudy(self.root/'shadow',['US.X'])
        s.on_book('US.X',book(),START,allowed=True);s.close()
        plan_file=self.root/'shadow'/'frozen_protocol.json'
        p=json.loads(plan_file.read_text());p['version']=1
        p.pop('shadowNotionalUsd');p.pop('maxVisibleL1Participation')
        from pathlib import Path
        legacy=Path(__file__).resolve().parent.parent/'legacy_v06'/'prospective.py'
        p['sourceSha256']=sha_bytes(legacy.read_bytes())
        plan_file.write_text(json.dumps(p,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n')
        (self.root/'shadow'/'protocol_seal.sha256').write_text(sha_bytes(plan_file.read_bytes())+'\n')
        self.assertTrue(report(self.root/'shadow')['completeRun'])

    def test_inventory_detects_removed_whole_run(self):
        parent=self.root/'runs';parent.mkdir()
        register_run(parent,'R1','2026-10-12T14:00:00+00:00',True)
        (parent/'R1').mkdir()
        r=audit_all(parent)
        self.assertEqual(r['runsFound'],1)
        self.assertFalse(r['allRunsAuditable'])
        self.assertEqual(r['perRun'][0]['status'],'MISSING_SHADOW_DIRECTORY_NO_GO')

    def test_inventory_hash_tamper_detection(self):
        parent=self.root/'runs';parent.mkdir()
        register_run(parent,'R1','2026-10-12T14:00:00+00:00',True)
        register_run(parent,'R2','2026-10-12T14:00:01+00:00',False)
        rows,exists=read_inventory(parent)
        self.assertTrue(exists);self.assertEqual(len(rows),2)
        f=parent/'run_inventory.jsonl'
        f.write_bytes(f.read_bytes().replace(b'"R1"',b'"XX"'))
        with self.assertRaisesRegex(ValueError,'TAMPERED_RUN_INVENTORY'):
            read_inventory(parent)
        self.assertIsNotNone(audit_all(parent)['inventoryError'])

    def test_alpha_gate_no_forward_runs_fails_closed(self):
        r=study_record(self.root/'none')
        self.assertEqual(r['verdict'],'NO_GO_REAL_TRADING')
        self.assertEqual(r['eligibleFullerEtDays'],0)
        self.assertFalse(r['paperAlphaResearchCandidates'])

    def test_alpha_gate_does_not_count_one_minute_as_day(self):
        parent=self.root/'recordings';parent.mkdir()
        run=parent/'R1';run.mkdir()
        s=ForwardStudy(run/'forward_shadow',['US.X'],raw_run_id='R1')
        s.on_book('US.X',book(bs=200,ass=200),START,allowed=True);s.close()
        rawrow={'kind':'book','origin':'push','callbackReceivedAt':START.isoformat(),
                'data':{'code':'US.X','Bid':[[x['price'],x['size']] for x in book()['bids']],
                       'Ask':[[x['price'],x['size']] for x in book()['asks']]}}
        data=(json.dumps(rawrow)+'\n').encode()
        (run/'events.jsonl').write_bytes(data)
        (run/'metadata.json').write_text(json.dumps({'eventsSha256':hashlib.sha256(data).hexdigest(),
                                                       'error':None,'dropped':0}))
        r=study_record(parent)
        self.assertEqual(r['daysWithAnyValidBookMinute'],1)
        self.assertEqual(r['eligibleFullerEtDays'],0)
        self.assertFalse(r['paperAlphaResearchCandidates'])

    def test_joint_day_resampling_never_converts_all_zeros_to_proven_alpha(self):
        res=joint_day_signflip({'A':[0.0]*30,'B':[0.0]*30},draws=100)
        self.assertEqual(res['A']['observedDayT'],0)
        self.assertEqual(res['A']['approxFamilyAdjustedOneSidedP'],1.0)

    def test_synchronized_day_stress_makes_no_trade_permission(self):
        r=study_record(self.root/'nonexistent')
        self.assertFalse(r['liveOrdersEnabled'])
        self.assertFalse(r['certifiedProfitability'])

if __name__=='__main__':unittest.main()
