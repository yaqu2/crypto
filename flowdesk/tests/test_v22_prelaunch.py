"""Fail-closed prelaunch tests, no Moomoo connection, no real orders."""
import json,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import prelaunch_audit as p

NOW=datetime(2026,10,15,10,0,tzinfo=timezone.utc)

class PrelaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_code_missing_fails(self):
        r=p.check_sources(self.root)
        self.assertEqual(r['status'],'NO_GO_CODE_STRUCTURE')
        self.assertIn('bridge.py',r['missingFiles'])

    def test_action_call_detected(self):
        (self.root/'attack.py').write_text('def x(ctx):\n    ctx.place_order(1)\n')
        r=p.check_sources(self.root)
        self.assertEqual(r['status'],'NO_GO_CODE_STRUCTURE')
        self.assertEqual(r['orderActionCallSites'][0]['api'],'place_order')

    def test_irrelevant_comment_does_not_fake_api_call(self):
        (self.root/'safe.py').write_text('# place_order(1) is forbidden\nx=3\n')
        r=p.check_sources(self.root)
        self.assertEqual(r['orderActionCallSites'],[])

    def test_syntax_error_blocks(self):
        (self.root/'broken.py').write_text('def (: ...\n')
        r=p.check_sources(self.root)
        self.assertTrue(r['unparsedSources'])

    def test_config_invalid(self):
        (self.root/'watchlist.json').write_text('{"symbols": []}')
        r=p.check_config(self.root)
        self.assertEqual(r['status'],'NO_GO_CONFIG')

    def _scan(self, *,age=5, gate='PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY', tamper=False):
        folder=self.root/'output'/'market';folder.mkdir(parents=True)
        (folder/'us_stock_universe.csv').write_text('code\nUS.AAPL\n')
        (folder/'latest_quotes.csv').write_text('code\nUS.AAPL\n')
        report={'universeSha256':p.digest(folder/'us_stock_universe.csv'),
                'quotesSha256':p.digest(folder/'latest_quotes.csv'),
                'completedAt':(NOW-timedelta(seconds=age)).isoformat(),
                'dataGate':gate,'snapshotReceivedCount':1}
        (folder/'latest_report.json').write_text(json.dumps(report))
        if tamper:(folder/'latest_quotes.csv').write_text('fake quote\n')
        return folder

    def test_scan_missing_fails(self):
        self.assertEqual(p.check_latest(self.root,NOW)['status'],'NO_GO_NO_CURRENT_MARKET_SCAN')

    def test_scan_tampering_precedes_profit_gate(self):
        self._scan(tamper=True)
        self.assertEqual(p.check_latest(self.root,NOW)['status'],'NO_GO_CURRENT_SCAN_TAMPERED')

    def test_old_scan_not_a_live_quote(self):
        self._scan(age=3600)
        self.assertEqual(p.check_latest(self.root,NOW)['status'],'NO_GO_SCAN_TOO_OLD')

    def test_no_coverage_not_a_pass(self):
        self._scan(gate='NO_GO_COVERAGE')
        self.assertEqual(p.check_latest(self.root,NOW)['status'],'NO_GO_SCAN_COVERAGE')

    def test_valid_snapshot_not_alpha(self):
        self._scan()
        r=p.check_latest(self.root,NOW)
        self.assertEqual(r['status'],'PASS_RECENT_RESEARCH_SNAPSHOT_ONLY')
        self.assertFalse(r['realOrderFlowConfirmed'])

    def test_future_scan_rejected(self):
        self._scan(age=-30)
        self.assertEqual(p.check_latest(self.root,NOW)['status'],'NO_GO_SCAN_FUTURE_CLOCK')

    def test_no_recording_inventory(self):
        self.assertEqual(p.check_recordings(self.root)['status'],'NO_GO_NO_RUN_INVENTORY')

    def test_missing_registered_run_fails(self):
        from run_inventory import register_run
        register_run(self.root/'recordings','test-run',NOW.isoformat(),False)
        r=p.check_recordings(self.root)
        self.assertEqual(r['status'],'NO_GO_RUN_INVENTORY_MISMATCH')
        self.assertIn('test-run',r['missingRuns'])

    def test_extra_unregistered_run_fails(self):
        (self.root/'recordings'/'orphan').mkdir(parents=True)
        r=p.check_recordings(self.root)
        self.assertEqual(r['status'],'NO_GO_RUN_INVENTORY_MISMATCH')

    def test_unverified_segments_not_accepted_as_complete(self):
        from run_inventory import register_run
        register_run(self.root/'recordings','test-run',NOW.isoformat(),False)
        (self.root/'recordings'/'test-run').mkdir()
        self.assertEqual(p.check_recordings(self.root)['status'],'UNVERIFIED_RAW_SEGMENTS_DEEP_AUDIT_REQUIRED')

    def test_trial_not_registered(self):
        r=p.check_trial(self.root,NOW)
        self.assertEqual(r['status'],'UNREGISTERED')

    def test_opend_not_contacted_by_default(self):
        with patch.object(p.socket,'create_connection',side_effect=AssertionError('unexpected')):
            self.assertEqual(p.check_opend(False)['status'],'NOT_CHECKED_LOCAL_TCP')

    def test_tcp_alone_does_not_verify_vendor(self):
        class Connection:
            def __enter__(self):return self
            def __exit__(self,*_):return None
        with patch.object(p.socket,'create_connection',return_value=Connection()):
            self.assertEqual(p.check_opend(True)['status'],'TCP_PORT_RESPONDS_ONLY')

    def test_regression_skipped_fails_software_gate(self):
        with patch.object(p,'check_sources',return_value={'status':'PASS_LOCAL_SOURCE_SHAPE_ONLY'}),\
             patch.object(p,'check_config',return_value={'status':'PASS_CONFIG_ONLY'}),\
             patch.object(p,'check_sdk',return_value={'status':'PASS_PINNED_SDK'}),\
             patch.object(p,'check_archive',return_value={'status':'NO_ARCHIVED_SCANS'}),\
             patch.object(p,'check_latest',return_value={'status':'NO_GO_NO_CURRENT_MARKET_SCAN'}),\
             patch.object(p,'check_recordings',return_value={'status':'NO_GO_NO_RUN_INVENTORY'}),\
             patch.object(p,'check_trial',return_value={'status':'UNREGISTERED'}):
            r=p.audit(self.root,now=NOW,tests=False)
        self.assertEqual(r['softwareRegressionGate'],'NO_GO_OFFLINE_ENGINEERING')
        self.assertFalse(r['liveOrdersEnabled'])
        self.assertFalse(r['alphaCertified'])

    def test_passing_every_offline_gate_is_still_no_live_trading(self):
        with patch.object(p,'check_sources',return_value={'status':'PASS_LOCAL_SOURCE_SHAPE_ONLY'}),\
             patch.object(p,'check_config',return_value={'status':'PASS_CONFIG_ONLY'}),\
             patch.object(p,'run_tests',return_value={'status':'PASS_REGRESSION_TESTS'}),\
             patch.object(p,'check_sdk',return_value={'status':'PASS_PINNED_SDK'}),\
             patch.object(p,'check_archive',return_value={'status':'PASS_LOCAL_ARCHIVE_INTEGRITY'}),\
             patch.object(p,'check_latest',return_value={'status':'PASS_RECENT_RESEARCH_SNAPSHOT_ONLY'}),\
             patch.object(p,'check_recordings',return_value={'status':'PASS_LOCAL_FILE_INTEGRITY_ONLY'}),\
             patch.object(p,'check_trial',return_value={'status':'UNREGISTERED'}):
            r=p.audit(self.root,now=NOW,tests=True)
        self.assertEqual(r['softwareRegressionGate'],'PASS_OFFLINE_TESTS_ONLY')
        self.assertEqual(r['canAttemptControlledCollection'],'YES_ENGINEERING_ONLY')
        self.assertFalse(r['liveMoomooStreamCertified'])
        self.assertEqual(r['liveTradingVerdict'],'NO_GO_NO_VERIFIED_ALPHA_OR_BROKER_EXECUTION')

if __name__=='__main__':unittest.main()
