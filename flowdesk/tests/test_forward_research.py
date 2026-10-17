"""Adversarial tests for forward-only shadow execution, never actual fills."""
import json,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from prospective import ForwardStudy,verify_chain,extract_book
from forward_report import report
from runtime import Runtime

UTC=timezone.utc
START=datetime(2026,10,12,14,0,tzinfo=UTC) # Mon 10:00 EDT, standard session


def book(bid=99.99,ask=100.01,bs=1000,ass=100):
    return {'bids':[{'price':round(bid-i*.01,4),'size':bs} for i in range(5)],
            'asks':[{'price':round(ask+i*.01,4),'size':ass} for i in range(5)]}


class TestBookValidation(unittest.TestCase):
    def test_requires_five_real_levels(self):
        b=book();b['asks']=b['asks'][:1];self.assertIsNone(extract_book(b))

    def test_crossed_is_rejected(self):
        self.assertIsNone(extract_book(book(100.05,100.01)))

    def test_wide_spread_rejected(self):
        self.assertIsNone(extract_book(book(90,101)))

    def test_unsorted_rejected(self):
        b=book();b['bids'][1]['price']=100.04;self.assertIsNone(extract_book(b))


class ForwardCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'shadow'
        self.lab=ForwardStudy(self.root,['US.TEST'])
    def tearDown(self):
        self.lab.close();self.tmp.cleanup()
    def send(self,sec,gate=True,b=None,code='US.TEST',origin='push'):
        self.lab.on_book(code,b or book(),START+timedelta(seconds=sec),allowed=gate,origin=origin)

    def test_future_entry_only_and_cost_is_charged(self):
        self.send(0)
        self.send(1.9)
        self.assertEqual(self.lab.counts['paperEntries'],0)
        self.send(3)
        self.assertEqual(self.lab.counts['paperEntries'],8)
        for sec in (15,27,39,51):self.send(sec)
        self.send(62.9)
        self.assertEqual(self.lab.counts['paperExits'],0)
        self.send(64,b=book(100.09,100.11))
        self.assertEqual(self.lab.counts['paperExits'],4) # 60s 4 strategies; 300s still open
        self.lab.close()
        plan,ev=verify_chain(self.root)
        self.assertEqual(len(plan['rules']),12)
        exits=[e for e in ev if e['type']=='PAPER_EXIT']
        self.assertEqual(len(exits),4)
        self.assertTrue(all(e['entryAt']>e['signalAt'] and e['exitAt']>e['entryAt'] for e in exits))
        for e in exits:
            self.assertAlmostEqual(e['netQuoteModelBps']['20'],e['quotedCrossingBps']-20,places=4)
            self.assertFalse(e['brokerFillVerified'])
        audit=report(self.root)
        self.assertTrue(audit['journalValid']);self.assertTrue(audit['completeRun'])
        self.assertFalse(audit['paperFillEvidence']);self.assertEqual(audit['verdict'],'NO_GO_REAL_TRADING')

    def test_no_future_tick_no_imaginary_fill(self):
        self.send(0);self.send(1)
        self.lab.close()
        self.assertEqual(self.lab.counts['paperExits'],0)
        self.assertEqual(self.lab.counts['censored'],0)
        self.assertGreater(self.lab.counts['cancelled'],0)
        self.assertTrue(report(self.root)['completeRun'])

    def test_gap_censors_open_without_pnl(self):
        self.send(0);self.send(3);self.send(700)
        self.assertEqual(self.lab.counts['paperExits'],0)
        self.assertGreater(self.lab.counts['censored'],0)
        self.lab.close();self.assertEqual(report(self.root)['eventCounts'].get('PAPER_EXIT',0),0)

    def test_feed_loss_censors_ongoing(self):
        self.send(0);self.send(3);self.send(4,gate=False)
        self.assertGreater(self.lab.counts['censored'],0)
        self.assertEqual(self.lab.counts['paperExits'],0)
        self.lab.close()
        self.assertTrue(report(self.root)['completeRun'])

    def test_ignored_cache_and_non_session(self):
        self.send(0,origin='seed');self.send(1)
        self.assertEqual(self.lab.counts['signals'],8) # only 1s event, no earlier OFI
        self.lab.reset('RECONNECT',START+timedelta(seconds=2));self.lab.close()
        self.assertEqual(self.lab.counts['paperEntries'],0)

    def test_out_of_order_book_cannot_advance(self):
        self.send(10);self.send(8);self.send(11)
        self.assertEqual(self.lab.counts['outOfOrder'],1)
        self.assertEqual(self.lab.counts['paperEntries'],0)

    def test_chain_event_tamper_fails(self):
        self.send(0);self.lab.close()
        f=self.root/'shadow_events.jsonl'
        payload=f.read_text();f.write_text(payload.replace('follow-L1-H60','follow-TAMPER-H60',1))
        with self.assertRaisesRegex(ValueError,'TAMPERED_EVENT'):report(self.root)

    def test_protocol_tamper_fails(self):
        self.send(0);self.lab.close()
        f=self.root/'frozen_protocol.json';f.write_text(f.read_text().replace('0.35','0.99'))
        with self.assertRaisesRegex(ValueError,'PROTOCOL_SEAL_MISMATCH'):verify_chain(self.root)

    def test_causal_ofi_uses_only_previous_book(self):
        first=book(bs=200,ass=200);second=book(bs=1200,ass=100)
        self.send(0,b=first)
        self.assertEqual(self.lab.counts['signals'],0)
        self.send(1,b=second)
        self.assertEqual(self.lab.counts['signals'],12)
        self.lab.close();_,events=verify_chain(self.root)
        self.assertEqual(len([e for e in events if e['type']=='SIGNAL' and e['feature']=='ofi_proxy']),4)

    def test_no_fake_ofi_from_long_gap(self):
        first=book(bs=200,ass=200);second=book(bs=1200,ass=100)
        self.send(0,b=first);self.send(10,b=second)
        self.lab.close();_,events=verify_chain(self.root)
        self.assertEqual(len([e for e in events if e['type']=='SIGNAL' and e['feature']=='ofi_proxy']),0)

    def test_short_is_always_shadow_only(self):
        self.send(0);self.send(3)
        for sec in (15,27,39,51,63,64):self.send(sec)
        self.lab.close();_,events=verify_chain(self.root)
        self.assertTrue(any(e.get('direction')==-1 for e in events if e['type']=='PAPER_EXIT'))
        self.assertTrue(all(e.get('shortBorrowModelled')==False for e in events if e['type']=='PAPER_EXIT'))

    def test_book_events_must_match_recorder_byte_stream(self):
        from source_crosscheck import crosscheck
        self.send(0);self.send(3);self.lab.close()
        raw=Path(self.tmp.name)/'events.jsonl'
        rows=[]
        for sec in (0,3):
            rows.append({'kind':'book','origin':'push','callbackReceivedAt':(START+timedelta(seconds=sec)).isoformat(),
                         'data':{'code':'US.TEST',
                                 'Bid':[[x['price'],x['size']] for x in book()['bids']],
                                 'Ask':[[x['price'],x['size']] for x in book()['asks']]}})
        raw.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
        x=crosscheck(self.root,raw)
        self.assertEqual(x['status'],'MATCHED_LOCAL_CALLBACK_RECORDS')
        self.assertGreater(x['paperEventsMatched'],0)
        rows[1]['data']['Ask'][0][0]=100.99
        raw.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
        with self.assertRaisesRegex(ValueError,'MISSING_OR_DIFFERENT'):
            crosscheck(self.root,raw)

    def test_no_standalone_trade_permissions(self):
        self.send(0);self.lab.close()
        a=report(self.root)
        self.assertFalse(a['liveOrdersEnabled'])
        self.assertFalse(a['sampleFloorMet'])

    def test_runtime_real_processing_path_uses_future_pushes(self):
        import time
        from runtime import DEFAULT_CONFIG
        from unittest.mock import Mock
        mock_rec=Mock();mock_rec.run_id='test-run';mock_rec.status.return_value={'error':None,'dropped':0}
        root=Path(self.tmp.name)/'runtime_forward'
        rt=Runtime(DEFAULT_CONFIG,recorder=mock_rec,forward_folder=root)
        code='US.AAOI'
        def emit_quote(sec,symbol=code):
            when=START+timedelta(seconds=sec)
            t=when.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York'))
            row={'code':symbol,'last_price':100.,'data_date':str(t.date()),'data_time':t.strftime('%H:%M:%S')}
            rt.process(('quote',[row],'push',when.isoformat(),time.monotonic_ns(),0))
        def emit_book(sec):
            when=START+timedelta(seconds=sec)
            b=book();raw={'code':code,'Bid':[(x['price'],x['size']) for x in b['bids']],
                               'Ask':[(x['price'],x['size']) for x in b['asks']]}
            rt.process(('book',raw,'push',when.isoformat(),time.monotonic_ns(),0))
        with patch.object(rt.engine,'ingest_quote',return_value=True),patch.object(rt.engine,'ingest_book',return_value=True),patch.object(rt,'feed_quality',return_value={'status':'RECENT_RESEARCH_ONLY'}):
            emit_quote(0,'US.SPY');emit_quote(0);emit_book(0)
            emit_quote(3,'US.SPY');emit_quote(3);emit_book(3)
            for sec in (15,27,39,51,63,64):
                emit_quote(sec,'US.SPY');emit_quote(sec);emit_book(sec)
            rt.publish()
        self.assertGreater(rt.state['forwardResearch']['counts'].get('paperExits',0),0)
        self.assertFalse(rt.state['forwardResearch']['profitCertified'])
        rt.close()
        self.assertTrue(report(root)['completeRun'])

    def test_all_runs_auditor_does_not_cherry_pick_corrupt_run(self):
        from audit_forward_runs import audit_all
        base=Path(self.tmp.name)/'recordings'
        (base/'A').mkdir(parents=True)
        lab=ForwardStudy(base/'A'/'forward_shadow',['US.TEST'])
        lab.on_book('US.TEST',book(),START,allowed=True)
        lab.close()
        (base/'A'/'events.jsonl').write_text(json.dumps({
            'kind':'book','origin':'push','callbackReceivedAt':START.isoformat(),
            'data':{'code':'US.TEST',
              'Bid':[[v['price'],v['size']] for v in book()['bids']],
              'Ask':[[v['price'],v['size']] for v in book()['asks']]}})+'\n')
        (base/'B'/'forward_shadow').mkdir(parents=True)
        (base/'B'/'forward_shadow'/'frozen_protocol.json').write_text('{}')
        result=audit_all(base)
        self.assertEqual(result['runsFound'],2)
        self.assertEqual(result['validCompleteRuns'],1)
        self.assertEqual(result['incompleteOrCorrupt'],1)
        self.assertFalse(result['allRunsAuditable'])
        self.assertEqual(result['verdict'],'NO_GO_LIVE_TRADING')

    def test_runtime_without_recorder_does_not_start_lab(self):
        from runtime import DEFAULT_CONFIG
        rt=Runtime(DEFAULT_CONFIG)
        self.assertIsNone(rt.forward)
        self.assertFalse(rt.state['forwardResearch']['liveOrdersEnabled'])
        rt.close()


if __name__=='__main__':unittest.main()
