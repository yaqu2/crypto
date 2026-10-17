"""Fail-closed ex-ante trial tests, synthetic records only, never market returns."""
import json, tempfile, unittest
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from unittest.mock import patch
import trial_registry as tr
from prospective import RULES
from recorder import Recorder

NOW=datetime(2026,10,14,13,0,tzinfo=timezone.utc)
TEST_FP={name:'a'*64 for name in tr.FILES}

class PreRegisteredTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.fingerprint=patch.object(tr,'_fingerprints',return_value=TEST_FP)
        self.fingerprint.start();self.addCleanup(self.fingerprint.stop)

    def freeze(self, **args):
        return tr.freeze(self.root,now=NOW,sessions=30,**args)

    def test_ex_ante_only_and_fixed_primary(self):
        x=self.freeze()
        self.assertEqual(x['status'],'FROZEN_EX_ANTE_LOCAL_ONLY')
        self.assertEqual(x['primaryRule'],RULES[0]['id'])
        self.assertTrue(date.fromisoformat(x['startEt'])>NOW.astimezone(tr.ET).date())
        plan,_=tr._verify_trial(self.root,x['trialId'])
        self.assertEqual(plan['minimumQualifiedSessions'],27)
        self.assertEqual(plan['calendarSessions'],tr._sessions(date.fromisoformat(x['startEt']),30))
        self.assertFalse(plan['liveOrdersEnabled'])
        self.assertFalse(plan['profitCertified'])

    def test_cannot_backdate_or_start_today(self):
        with self.assertRaisesRegex(ValueError,'FUTURE'):self.freeze(start='2026-10-14')
        with self.assertRaisesRegex(ValueError,'FUTURE'):self.freeze(start='2026-10-13')
        self.assertFalse((self.root/'research').exists())

    def test_cannot_start_holiday_or_weekend(self):
        with self.assertRaisesRegex(ValueError,'NOT_TRADING_DAY'):self.freeze(start='2026-11-26')
        with self.assertRaisesRegex(ValueError,'NOT_TRADING_DAY'):self.freeze(start='2026-10-17')

    def test_auto_skip_weekend(self):
        future=datetime(2026,10,16,22,0,tzinfo=timezone.utc)
        # Friday evening ET -> Monday; DST-aware.
        result=tr.freeze(self.root,now=future,sessions=30)
        self.assertEqual(result['startEt'],'2026-10-19')

    def test_second_concurrent_trial_denied(self):
        self.freeze()
        with self.assertRaisesRegex(ValueError,'EXISTING_ACTIVE'):self.freeze()

    def test_status_during_trial_hides_performance(self):
        x=self.freeze()
        r=tr.status(self.root,now=NOW+timedelta(days=3))
        self.assertEqual(r['status'],'BLINDED_FUTURE_TEST_IN_PROGRESS')
        self.assertNotIn('test',r)
        self.assertNotIn('pnl',str(r).lower())
        self.assertFalse(r['profitCertified'])
        self.assertEqual(r['trialId'],x['trialId'])

    def test_protocol_modified_rejected(self):
        x=self.freeze()
        p=tr._registry_root(self.root)/x['trialId']/'protocol.json'
        p.write_bytes(p.read_bytes().replace(b'"profitCertified":false',b'"profitCertified":true'))
        with self.assertRaisesRegex(ValueError,'TRIAL_PROTOCOL_TAMPERED'):
            tr.active_binding(self.root,now=NOW)

    def test_seal_replaced_cannot_change_registry_reference(self):
        x=self.freeze()
        p=tr._registry_root(self.root)/x['trialId']/'protocol.sha256'
        p.write_text('0'*64)
        with self.assertRaisesRegex(ValueError,'TRIAL_PROTOCOL_TAMPERED'):tr._verify_trial(self.root,x['trialId'])

    def test_registry_tampering_rejected(self):
        self.freeze()
        registry=tr._registry_root(self.root)/'registry.jsonl'
        registry.write_bytes(registry.read_bytes().replace(b'"seq":1',b'"seq":2'))
        with self.assertRaisesRegex(ValueError,'TAMPERED_TRIAL_REGISTRY'):tr.status(self.root,now=NOW)

    def test_current_source_code_change_rejected(self):
        x=self.freeze()
        altered=dict(TEST_FP);altered['prospective.py']='f'*64
        with patch.object(tr,'_fingerprints',return_value=altered):
            with self.assertRaisesRegex(ValueError,'FROZEN_RESEARCH_CODE_CHANGED'):
                tr.active_binding(self.root,now=NOW)
        self.assertEqual(tr.active_binding(self.root,now=NOW)['trialSealSha256'],x['protocolSha256'])

    def test_active_protocol_removed_during_trial_rejected(self):
        self.freeze()
        (tr._registry_root(self.root)/'active_trial.json').unlink()
        with self.assertRaisesRegex(ValueError,'ACTIVE_TRIAL_FILE_REMOVED'):
            tr.active_binding(self.root,now=NOW+timedelta(days=2))

    def test_active_binding_expiration(self):
        x=self.freeze()
        plan,_=tr._trial(self.root,x['trialId'])
        later=datetime.fromisoformat(plan['endEt']+'T00:00:00+00:00')+timedelta(days=2)
        self.assertEqual(tr.active_binding(self.root,now=later)['status'],'TRIAL_WINDOW_ENDED')

    def test_window_cannot_finalize_early(self):
        x=self.freeze()
        with self.assertRaisesRegex(ValueError,'NOT_YET_CLOSED'):
            tr.finalize(self.root,self.root/'recordings',now=NOW,trial_id=x['trialId'])
        self.assertFalse((tr._registry_root(self.root)/x['trialId']/'finalization.claim').exists())

    def test_after_window_one_shot_and_no_go_empty(self):
        x=self.freeze()
        plan,_=tr._trial(self.root,x['trialId'])
        later=datetime.fromisoformat(plan['endEt']+'T00:00:00+00:00')+timedelta(days=3)
        result=tr.finalize(self.root,self.root/'recordings',now=later,trial_id=x['trialId'])
        self.assertEqual(result['status'],'NO_GO_FUTURE_EVIDENCE')
        self.assertEqual(len(result['missingSessions']),30)
        self.assertFalse(result['profitCertified'])
        report=tr._registry_root(self.root)/x['trialId']/'final_report.json'
        self.assertEqual(tr._sha(report),(report.parent/'final_report.sha256').read_text().strip())
        with self.assertRaises(FileExistsError):tr.finalize(self.root,self.root/'recordings',now=later,trial_id=x['trialId'])

    def test_fake_unbound_run_forbidden(self):
        self.freeze()
        from run_inventory import register_run
        from datetime import time as dt_time
        begin=tr.status(self.root,now=NOW)['startEt']
        when=datetime.fromisoformat(begin+'T15:00:00+00:00')
        recroot=self.root/'recordings';recroot.mkdir()
        register_run(recroot,'run-0001',when.isoformat(),True)
        # An unbound run within the frozen range can't silently join the study.
        d=recroot/'run-0001';d.mkdir();(d/'metadata.json').write_text(json.dumps({'trialId':None,'closed':True}))
        plan,_=tr._trial(self.root,tr.status(self.root,now=NOW)['trialId'])
        later=datetime.fromisoformat(plan['endEt']+'T00:00:00+00:00')+timedelta(days=3)
        audit=tr.evaluate_trial(self.root,recroot,plan['trialId'],now=later)
        self.assertTrue(any('UNBOUND_RUN' in e for e in audit['runErrors']))
        self.assertEqual(audit['status'],'NO_GO_FUTURE_EVIDENCE')

    def test_run_recorder_seals_binding_into_evidence(self):
        x=self.freeze();b=tr.active_binding(self.root,now=NOW)
        rec=Recorder(self.root/'recordings',trial_binding=b,segment_bytes=4000)
        rec.close()
        meta=json.loads((rec.folder/'metadata.json').read_text())
        self.assertEqual(meta['trialId'],x['trialId'])
        self.assertEqual(meta['trialSealSha256'],x['protocolSha256'])
        from segment_store import verify_recording
        self.assertEqual(verify_recording(rec.folder)['status'],'VERIFIED_LOCAL_RAW_RECORDING_NOT_PROVIDER_COMPLETENESS')

    def _stubbed_full_window(self, *, missing_last=False, capital_complete=True):
        from run_inventory import register_run
        from zoneinfo import ZoneInfo
        x=self.freeze()
        plan,_=tr._trial(self.root,x['trialId'])
        recorded=self.root/'recordings'
        recorded.mkdir()
        for i,day in enumerate(plan['calendarSessions']):
            # Every run is created AFTER the freeze and DURING the target session.
            started=datetime.fromisoformat(day+'T09:29:00').replace(tzinfo=ZoneInfo('America/New_York'))
            rid='session-%03d'%i
            register_run(recorded,rid,started.isoformat(),True)
            p=recorded/rid;p.mkdir()
            (p/'metadata.json').write_text(json.dumps({'closed':True,'trialId':x['trialId'],
                                        'trialSealSha256':x['protocolSha256']}))
        dayreports=lambda folder: {'days':([] if missing_last and folder.name=='session-029' else [
            {'marketDateET':plan['calendarSessions'][int(folder.name.split('-')[1])],
             'status':'RESEARCH_FEED_COVERAGE_DAY_ONLY'}])}
        pnls={d:{x['primaryRule']:{'8':30+i%4,'20':20+i%4,'40':10+i%4}}
              for i,d in enumerate(plan['calendarSessions'])}
        counts={d:{x['primaryRule']:1} for d in plan['calendarSessions']}
        fake_account={'diagnosticComplete':capital_complete,'closedLongModelExits':30,
                      'censoredAdmittedPositions':0 if capital_complete else 1,
                      'accountUsd':100000.0,'ruleExitDayClosedOnlyPnlUsd':pnls,
                      'ruleExitDayClosedOnlyCounts':counts}
        later=datetime.fromisoformat(plan['endEt']+'T00:00:00+00:00')+timedelta(days=3)
        with patch('session_coverage.audit_recording_coverage',side_effect=dayreports), \
             patch('prospective.verify_chain',return_value=({'sourceSha256':TEST_FP['prospective.py']},[])), \
             patch('source_crosscheck.crosscheck',return_value={'status':'MATCHED_LOCAL_CALLBACK_RECORDS'}), \
             patch('portfolio_audit.replay_portfolio',return_value=fake_account) as replay:
            result=tr.evaluate_trial(self.root,recorded,x['trialId'],now=later)
            if replay.called:
                # The capital replay considers only the frozen primary strategy.
                for _,events in replay.call_args.args[0]:
                    self.assertTrue(all(e.get('rule')==x['primaryRule'] or e.get('type')=='RUN_END' for e in events))
            return result

    def test_all_days_complete_mocked_quote_model_can_be_promising_not_certified(self):
        result=self._stubbed_full_window()
        self.assertEqual(result['status'],'PROMISING_COUNTERFACTUAL_QUOTE_MODEL_ONLY')
        self.assertEqual(result['qualifiedSessions'],30)
        self.assertFalse(result['profitCertified'])
        self.assertFalse(result['tradeEnabled'])
        self.assertTrue(result['riskDescriptive']['unrealizedAndTailRiskNotKnown'])

    def test_one_missing_day_forbids_advancement_even_if_other_days_profit(self):
        result=self._stubbed_full_window(missing_last=True)
        self.assertEqual(result['status'],'NO_GO_FUTURE_EVIDENCE')
        self.assertEqual(result['qualifiedSessions'],29)
        self.assertIn(result['endEt'],result['nonQualifiedSessions'])
        self.assertIsNone(result['test'])

    def test_one_censored_capital_position_forbids_advancement(self):
        result=self._stubbed_full_window(capital_complete=False)
        self.assertEqual(result['status'],'NO_GO_FUTURE_EVIDENCE')
        self.assertFalse(result['capitalAudit']['diagnosticComplete'])
        self.assertIsNone(result['test'])

    def test_live_running_lock_blocks_midstream_freeze(self):
        import fcntl
        with (self.root/'.flowdesk_live.lock').open('a+b') as fh:
            fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'LIVE_COLLECTOR_ACTIVE'):
                self.freeze()
        self.assertEqual(tr.status(self.root)['status'],'UNREGISTERED')

    def test_published_realtime_ui_blinds_future_pnl(self):
        from runtime import Runtime, DEFAULT_CONFIG
        class FakeForward:
            journal=None
            def summary(self):
                return {'status':'NO_GO_LIVE_ALPHA','hypotheses':12,'counts':{'signals':4,'paperExits':3},
                        'recent':[{'netQuoteModelBps':{'20':999999}}],
                        'perRule':{'follow-L1-H60':{'meanNet20Bps':888888}},
                        'disabled':False,'journal':{'error':None}}
        rt=Runtime(DEFAULT_CONFIG,trial_binding={'trialId':'frozen-test','trialSealSha256':'c'*64})
        rt.forward=FakeForward()
        rt.publish()
        pub=json.loads(rt.published)
        f=pub['forwardResearch']
        self.assertEqual(f['status'],'BLINDED_FROZEN_FUTURE_TEST')
        self.assertEqual(f['recent'],[])
        self.assertEqual(f['perRule'],{})
        self.assertNotIn('999999',rt.published.decode())
        self.assertNotIn('888888',rt.published.decode())
        self.assertFalse(f['liveOrdersEnabled'])

    def test_no_trial_must_not_pretend_oos(self):
        self.assertEqual(tr.status(self.root)['status'],'UNREGISTERED')
        self.assertEqual(tr.active_binding(self.root)['status'],'NO_PRE_REGISTERED_TRIAL')

    def test_local_time_not_exchange_notary(self):
        result=self.freeze()
        plan,_=tr._trial(self.root,result['trialId'])
        self.assertFalse(plan['independentNotary'])
        self.assertEqual(plan['execution'],'COUNTERFACTUAL_ORDER_BOOK_QUOTES_ONLY')

    def test_primary_choice_is_frozen(self):
        x=self.freeze(rule=RULES[4]['id'])
        plan,_=tr._verify_trial(self.root,x['trialId'])
        self.assertEqual(plan['primaryRule'],RULES[4]['id'])
        with self.assertRaisesRegex(ValueError,'NOT_PRE_REGISTERED'):
            tr.freeze(self.root,now=NOW,sessions=30,rule='future-trend-plus')

if __name__=='__main__':unittest.main()
