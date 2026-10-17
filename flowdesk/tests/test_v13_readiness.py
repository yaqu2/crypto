"""No external SDK, no order API and no fake revenue claims in readiness tests."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import live_readiness as rd
import paper_probe as pp

NOW=datetime(2026,10,14,13,0,tzinfo=timezone.utc)


class FakeContext:
    def __init__(self,records,return_code=0):
        self.records=records
        self.return_code=return_code
        self.called=[]
    def get_acc_list(self):
        self.called.append('get_acc_list')
        return self.return_code,self.records
    def close(self):self.called.append('close')
    def place_order(self,*args,**kwargs):raise AssertionError('TRADE_API_MUST_NEVER_BE_CALLED')


class PaperAccessTests(unittest.TestCase):
    def test_no_trading_calls_and_no_real_account_leak(self):
        private={'acc_id':'SECRET_REAL_ACCOUNT_123','cash':12345,'trd_env':'REAL',
                 'sim_acc_type':'STOCK_AND_OPTION'}
        simulated={'acc_id':'SECRET_PAPER_ACCOUNT_999','trd_env':'SIMULATE',
                   'sim_acc_type':'STOCK_AND_OPTION'}
        ctx=FakeContext([private,simulated])
        x=pp.probe(context_factory=lambda:ctx,success_code=0,inspected_at=NOW)
        self.assertEqual(x['status'],'PAPER_ACCOUNT_DISCOVERED_NOT_EXECUTION_TESTED')
        self.assertEqual(x['paperStockAccountCount'],1)
        self.assertEqual(ctx.called,['get_acc_list','close'])
        self.assertFalse(x['orderApiUsed'])
        payload=json.dumps(x)
        self.assertNotIn('SECRET_REAL',payload)
        self.assertNotIn('SECRET_PAPER',payload)
        self.assertNotIn('12345',payload)
    def test_only_real_accounts_no_paper(self):
        ctx=FakeContext([{'trd_env':'REAL','sim_acc_type':'STOCK'}])
        r=pp.probe(context_factory=lambda:ctx,success_code=0,inspected_at=NOW)
        self.assertEqual(r['status'],'NO_GO_PAPER_CONNECTIVITY')
    def test_paper_not_stock_type(self):
        ctx=FakeContext([{'trd_env':'SIMULATE','sim_acc_type':'FUTURES'}])
        r=pp.probe(context_factory=lambda:ctx,success_code=0,inspected_at=NOW)
        self.assertEqual(r['paperStockAccountCount'],0)
    def test_query_error_does_not_enable(self):
        ctx=FakeContext([],1)
        r=pp.probe(context_factory=lambda:ctx,success_code=0,inspected_at=NOW)
        self.assertEqual(r['status'],'NO_GO_PAPER_CONNECTIVITY')
        self.assertEqual(ctx.called,['get_acc_list','close'])
    def test_unreachable_opend_fails_closed(self):
        def boom():raise ConnectionError('not reachable')
        r=pp.probe(context_factory=boom,success_code=0,inspected_at=NOW)
        self.assertEqual(r['reason'],'OPEN_D_OR_SDK_UNAVAILABLE')
    def test_future_timestamp_requires_awareness(self):
        with self.assertRaisesRegex(ValueError,'UTC_AWARE'):
            pp.probe(context_factory=lambda:FakeContext([]),success_code=0,
                     inspected_at=datetime(2026,1,1))


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'research'/'readiness').mkdir(parents=True)

    def write_probe(self,**kwargs):
        data={'status':'PAPER_ACCOUNT_DISCOVERED_NOT_EXECUTION_TESTED',
              'inspectedAt':NOW.isoformat(),'paperStockAccountCount':1,
              'orderApiUsed':False}
        data.update(kwargs)
        p=self.root/'research'/'readiness'/'paper_probe.json'
        p.write_text(json.dumps(data))
        return p

    def test_blank_project_is_no_go_and_blind(self):
        r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['verdict'],'NO_GO_LIVE_REAL_MONEY')
        self.assertFalse(r['liveOrdersEnabled'])
        self.assertNotIn('pnl',str(r).lower())
        self.assertIn('NO_POSITIVE_PRE_REGISTERED_INDEPENDENT_QUOTE_MODEL',r['blockers'])
    def test_paper_account_alone_not_approval(self):
        self.write_probe()
        r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['paperAccountAccess']['status'],'PAPER_ACCOUNT_ACCESS_ONLY')
        self.assertEqual(r['verdict'],'NO_GO_LIVE_REAL_MONEY')
    def test_stale_probe_not_accepted(self):
        self.write_probe(inspectedAt=(NOW-timedelta(days=4)).isoformat())
        r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['paperAccountAccess']['status'],'STALE')
    def test_future_dated_probe_not_accepted(self):
        self.write_probe(inspectedAt=(NOW+timedelta(hours=3)).isoformat())
        r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['paperAccountAccess']['status'],'STALE')
    def test_fake_paper_success_with_order_api_used_not_accepted(self):
        self.write_probe(orderApiUsed=True)
        r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['paperAccountAccess']['status'],'NO_GO')
    def test_corrupt_probe_not_accepted(self):
        (self.root/'research'/'readiness'/'paper_probe.json').write_text('{bad')
        r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['paperAccountAccess']['status'],'NO_GO')
    def test_invalid_raw_run_fails_closed(self):
        with patch('session_coverage.audit_recording_coverage',side_effect=ValueError('TAMPERED_DATA')):
            r=rd.assess(self.root,recording=self.root/'does_not_exist',now=NOW)
        self.assertEqual(r['evidence']['closedFeedEvidence']['status'],'NO_GO_INVALID_RAW_EVIDENCE')
    def test_only_one_closed_good_day_not_enough(self):
        daily={'status':'NO_GO_INDEPENDENT_SESSION_COVERAGE','independentQualifiedDays':1,
               'tradingDaysObserved':1,'closedRun':True,'rawIntegrity':'VERIFIED'}
        with patch('session_coverage.audit_recording_coverage',return_value=daily):
            r=rd.assess(self.root,recording=self.root/'a',now=NOW)
        self.assertIn('NO_30_INDEPENDENT_QUALIFIED_CLOSED_FEED_DAYS',r['blockers'])
    def test_naive_clock_denied(self):
        with self.assertRaisesRegex(ValueError,'UTC_AWARE'):
            rd.assess(self.root,now=datetime(2026,10,14))
    def test_active_trial_state_hidden_and_no_final_file_read(self):
        with patch('trial_registry.status',return_value={'status':'BLINDED_FUTURE_TEST_IN_PROGRESS',
                     'trialId':'trial-test','registeredTrials':1}):
            with patch('trial_registry._verify_trial',side_effect=AssertionError('DO_NOT_UNBLIND')):
                r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['preregisteredForwardTrial']['status'],
                         'BLINDED_FUTURE_TEST_IN_PROGRESS')
    def test_final_report_requires_seal_and_claim(self):
        f=self.root/'research'/'sealed_trials'/'trial-test';f.mkdir(parents=True)
        (f/'final_report.json').write_text('{}')
        with patch('trial_registry.status',return_value={'status':'READY_FOR_ONE_SHOT_FINAL',
                                                         'trialId':'trial-test','registeredTrials':1}):
            r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['preregisteredForwardTrial']['status'],
                         'WAITING_FOR_OFFICIAL_ONE_SHOT_FINALIZATION')
    def seeded_final(self,*,good=True,wrong_id=False,changed=False):
        f=self.root/'research'/'sealed_trials'/'trial-test';f.mkdir(parents=True,exist_ok=True)
        report={'status':'PROMISING_COUNTERFACTUAL_QUOTE_MODEL_ONLY',
                'quoteModelNotBrokerFills':True,'brokerFillVerified':False,
                'profitCertified':False,'tradeEnabled':False,
                'trialId':'other' if wrong_id else 'trial-test','protocolSha256':'sealed-plan',
                'qualifiedSessions':30,'scheduledSessions':30,
                'capitalAudit':{'diagnosticComplete':True,'censoredAdmittedPositions':0},
                'test':{'40':{'exploratoryBlockBootstrapMeanCi95':[1.5,4.2] if good else [-1,4.2]}}}
        raw=json.dumps(report).encode();(f/'final_report.json').write_bytes(raw)
        (f/'final_report.sha256').write_text(rd._sha(raw))
        (f/'finalization.claim').write_text(NOW.isoformat())
        if changed:(f/'final_report.json').write_text('{}')
        return f
    def verified_trial(self):
        return patch('trial_registry.status',return_value={'status':'READY_FOR_ONE_SHOT_FINAL',
                                   'trialId':'trial-test','registeredTrials':1})
    def test_strong_mock_trial_still_no_live_authorization(self):
        self.seeded_final()
        self.write_probe()
        with self.verified_trial(),patch('trial_registry._verify_trial',return_value=(
                {'calendarSessions':list(range(30))},'sealed-plan')):
            r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['preregisteredForwardTrial']['status'],
                         'POSITIVE_QUOTE_MODEL_REQUIRES_BROKER_VERIFICATION')
        self.assertEqual(r['verdict'],'NO_GO_LIVE_REAL_MONEY')
        self.assertIn('NO_AUTHENTICATED_BROKER_EXECUTION_SLIPPAGE_AND_FEES',r['blockers'])
    def test_negative_ci_not_passing(self):
        self.seeded_final(good=False)
        with self.verified_trial(),patch('trial_registry._verify_trial',return_value=(
                {'calendarSessions':list(range(30))},'sealed-plan')):
            r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['preregisteredForwardTrial']['status'],'NO_GO_FUTURE_TRIAL')
    def test_tampered_final_not_passing(self):
        self.seeded_final(changed=True)
        with self.verified_trial():r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['preregisteredForwardTrial']['status'],'NO_GO_FINAL_REPORT_TAMPERED')
    def test_wrong_trial_not_passing(self):
        self.seeded_final(wrong_id=True)
        with self.verified_trial(),patch('trial_registry._verify_trial',return_value=(
                {'calendarSessions':list(range(30))},'sealed-plan')):
            r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['preregisteredForwardTrial']['status'],
                         'NO_GO_FINAL_REPORT_WRONG_TRIAL_OR_SEAL')
    def test_trials_cannot_be_manually_declared_pass(self):
        self.seeded_final()
        with self.verified_trial(),patch('trial_registry._verify_trial',
                                      side_effect=ValueError('FROZEN_RESEARCH_CODE_CHANGED')):
            r=rd.assess(self.root,now=NOW)
        self.assertEqual(r['evidence']['preregisteredForwardTrial']['status'],'NO_GO_TRIAL_INTEGRITY')


if __name__=='__main__':unittest.main()
