"""Adversarial tests for trading-session data coverage and next-run candidate lineage."""
import csv,hashlib,json,shutil,tempfile,unittest,fcntl
from datetime import datetime,timedelta,timezone
from pathlib import Path
from recorder import Recorder
from session_coverage import audit_recording_coverage
from audit_daily_coverage import audit_every_run
from market_archive import register_scan
from prepare_watchlist import propose
from activate_watchlist import activate,audit_activations


def callback(et_day,minute,seconds=2):
    from zoneinfo import ZoneInfo
    local=datetime.fromisoformat(et_day+'T09:30:00').replace(tzinfo=ZoneInfo('America/New_York'))
    return (local+timedelta(minutes=minute,seconds=seconds)).astimezone(timezone.utc).isoformat()

class FeedCoverageV10(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def capture(self,dates=('2026-10-13',),quotes=True,healthy=True,with_book=False):
        r=Recorder(self.root,segment_bytes=2600,queue_size=2000)
        for day in dates:
            mins=210 if day=='2026-11-27' else 390
            for minute in range(mins):
                ts=callback(day,minute)
                r.record('health',{'connected':True,'feedStatus':'RECENT_RESEARCH_ONLY' if healthy else 'NO_GO_STALE'},
                         callback_received_at=ts)
                if quotes:
                    source=(datetime.fromisoformat(day+'T09:30:00')+timedelta(minutes=minute))
                    row={'code':'US.SPY','data_date':day,'data_time':source.strftime('%H:%M:%S'),
                         'last_price':400.0}
                    r.record('quote',[row],callback_received_at=ts)
                if with_book and minute%2==0:
                    r.record('book',{'code':'US.SPY','Bid':[[400.,200]],'Ask':[[400.02,210]]},callback_received_at=ts)
        r.q.join();r.close()
        return r

    def test_full_day_health_and_quote_coverage_reported_not_alpha(self):
        r=self.capture(with_book=True)
        x=audit_recording_coverage(r.folder)
        self.assertEqual(x['independentQualifiedDays'],1)
        self.assertEqual(x['tradingDaysObserved'],1)
        d=x['days'][0]
        self.assertEqual(d['benchmarkSourceVerifiedQuoteMinutes'],390)
        self.assertEqual(d['heartbeatMinutes'],390)
        self.assertEqual(d['bookCallbacksBySymbol']['US.SPY']['callbackBookMinutes'],195)
        self.assertFalse(x['profitCertified'])
        self.assertEqual(x['status'],'NO_GO_INDEPENDENT_SESSION_COVERAGE')
        self.assertFalse(audit_every_run(self.root)['status']=='RESEARCH_FEED_SAMPLE_DAYS_ONLY')

    def test_heartbeat_without_quotes_is_not_coverage(self):
        r=self.capture(quotes=False)
        x=audit_recording_coverage(r.folder)
        self.assertEqual(x['days'][0]['benchmarkQuoteMinuteCoverage'],0)
        self.assertEqual(x['independentQualifiedDays'],0)

    def test_quote_only_without_healthy_feed_status_not_qualified(self):
        r=self.capture(healthy=False)
        x=audit_recording_coverage(r.folder)
        self.assertEqual(x['days'][0]['benchmarkQuoteMinuteCoverage'],1.0)
        self.assertEqual(x['independentQualifiedDays'],0)

    def test_early_close_210_minutes(self):
        r=self.capture(dates=('2026-11-27',))
        x=audit_recording_coverage(r.folder)
        self.assertEqual(x['days'][0]['expectedRegularMinutes'],210)
        self.assertEqual(x['independentQualifiedDays'],1)

    def test_prefix_export_does_not_certify_full_run(self):
        r=self.capture();meta=r.folder/'metadata.json';x=json.loads(meta.read_text());x['closed']=False;meta.write_text(json.dumps(x))
        self.assertEqual(audit_recording_coverage(r.folder,strict=False)['status'],'NO_GO_PREFIX_EXPORT')
        with self.assertRaisesRegex(ValueError,'RAW_RECORDER_NOT_CLOSED'):
            audit_recording_coverage(r.folder)

    def test_stale_quote_source_is_not_counted(self):
        r=Recorder(self.root,segment_bytes=3000)
        day='2026-10-13';ts=callback(day,0)
        r.record('health',{'connected':True,'feedStatus':'RECENT_RESEARCH_ONLY'},callback_received_at=ts)
        r.record('quote',[{'code':'US.SPY','data_date':'2026-10-12','data_time':'09:30:00','last_price':400}],callback_received_at=ts)
        r.q.join();r.close()
        x=audit_recording_coverage(r.folder)
        self.assertEqual(x['days'][0]['staleOrOutOfOrderQuotes'],1)
        self.assertEqual(x['days'][0]['benchmarkSourceVerifiedQuoteMinutes'],0)

    def test_missing_raw_segment_blocks_coverage(self):
        r=self.capture();(r.folder/'events.000001.jsonl').unlink()
        with self.assertRaisesRegex(ValueError,'RAW_SEGMENT_MISSING_OR_UNREGISTERED'):
            audit_recording_coverage(r.folder)
        self.assertEqual(audit_every_run(self.root)['status'],'NO_GO_FEED_EVIDENCE')

class CandidateActivationV10(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.market=self.root/'market';self.project=self.root/'project'
        self.market.mkdir();self.project.mkdir()
        current={'symbols':['US.SPY','US.QQQ','US.IWM'],'deepSymbols':['US.SPY'],
                 'benchmark':'US.SPY','peers':{}}
        (self.project/'watchlist.json').write_text(json.dumps(current))
        with (self.market/'us_stock_universe.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['code','name','exchange','securityType']);writer.writeheader()
            for i in range(2100):writer.writerow(dict(code=f'US.{i:04d}',name='S',exchange='NASDAQ',securityType='STOCK'))
        with (self.market/'latest_quotes.csv').open('w',newline='') as f:
            fields=['code','sourceAsOf','ageSeconds','lastPrice','previousClose','volume','turnover','fresh','usable','changePct']
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
            for i in range(2100):writer.writerow({'code':f'US.{i:04d}','turnover':1000000+i*10000,
                  'changePct':(i%90)-45,'fresh':'True','usable':'True'})
        def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
        report={'stockUniverseCount':2100,'snapshotReceivedCount':2100,'freshUsableCount':2100,
                'dataGate':'PASS_SNAPSHOT_QUALITY_RESEARCH_ONLY','fullCurrentStockUniverse':True,
                'completedAt':datetime.now(timezone.utc).isoformat(),'batchErrors':[],
                'universeSha256':sha(self.market/'us_stock_universe.csv'),
                'quotesSha256':sha(self.market/'latest_quotes.csv')}
        (self.market/'latest_report.json').write_text(json.dumps(report))
        register_scan(self.market)
        planned=propose(self.market,current)
        self.assertEqual(planned['status'],'REVIEW_REQUIRED_NOT_APPLIED')
        (self.market/'proposed_watchlist.json').write_text(json.dumps(planned))

    def test_no_permission_no_mutation(self):
        before=(self.project/'watchlist.json').read_bytes()
        with self.assertRaisesRegex(PermissionError,'APPROVAL'):
            activate(self.project,self.market)
        self.assertEqual((self.project/'watchlist.json').read_bytes(),before)

    def test_explicit_activation_next_restart_only(self):
        x=activate(self.project,self.market,approve=True)
        self.assertEqual(x['status'],'ACTIVATED_NEXT_RESTART_ONLY')
        self.assertEqual(x['activationNumber'],1)
        self.assertEqual(audit_activations(self.project)['status'],'PASS_LOCAL_ACTIVATION_LOG')
        new=json.loads((self.project/'watchlist.json').read_text())
        self.assertIn('US.SPY',new['symbols']);self.assertIn('US.QQQ',new['symbols']);self.assertIn('US.IWM',new['symbols'])
        self.assertLessEqual(len(new['symbols']),36)
        self.assertFalse(x['tradeEnabled']);self.assertFalse(x['profitCertified'])
        self.assertTrue((self.project/'watchlist.before-activation-000001.json').exists())

    def test_changed_proposal_rejected(self):
        path=self.market/'proposed_watchlist.json';plan=json.loads(path.read_text());plan['symbols'][3]='US.FAKE'
        path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(RuntimeError,'PROPOSAL_CHANGED'):
            activate(self.project,self.market,approve=True)
        self.assertEqual(audit_activations(self.project)['status'],'NO_ACTIVATION_HISTORY')

    def test_changed_latest_snapshot_blocked(self):
        with (self.market/'latest_quotes.csv').open('a') as f:f.write('\n')
        with self.assertRaisesRegex((RuntimeError,ValueError),'TAMPER|MISMATCH|CHANGED'):
            activate(self.project,self.market,approve=True)

    def test_running_bridge_lock_blocks_activation(self):
        fd=(self.project/'.flowdesk_live.lock').open('a+b')
        fcntl.flock(fd.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(RuntimeError,'FLOWDESK_IS_RUNNING'):
                activate(self.project,self.market,approve=True)
        finally:fcntl.flock(fd.fileno(),fcntl.LOCK_UN);fd.close()

    def test_changing_live_config_after_activation_is_detected(self):
        activate(self.project,self.market,approve=True)
        cfg=self.project/'watchlist.json';x=json.loads(cfg.read_text());x['deepSymbols']=['US.QQQ'];cfg.write_text(json.dumps(x))
        self.assertEqual(audit_activations(self.project)['status'],'NO_GO_ACTIVATION_INTEGRITY')

    def test_incomplete_archive_rejects_activation(self):
        first=next((self.market/'archive').iterdir())
        # first may be lock file; delete selected archived manifest through inventory.
        inv=json.loads((self.market/'archive'/'scan_inventory.jsonl').read_text().splitlines()[-1])
        (self.market/'archive'/inv['runId']/'archive_manifest.json').unlink()
        with self.assertRaisesRegex(RuntimeError,'ARCHIVE_NOT_VERIFIED'):
            activate(self.project,self.market,approve=True)

if __name__=='__main__':unittest.main()

class CohortProvenanceV10(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.records=self.root/'recordings'
        self.config={'symbols':['US.SPY'],'deepSymbols':['US.SPY'],'benchmark':'US.SPY','peers':{}}

    def capture(self,config=None):
        r=Recorder(self.records,config=config or self.config)
        r.q.join();r.close();return r

    def test_unregistered_static_runs_cannot_claim_dynamic_selection(self):
        from cohort_lineage import check_cohort_lineage
        r=self.capture();x=check_cohort_lineage(r.folder,self.root)
        self.assertEqual(x['status'],'UNREGISTERED_STATIC_WATCHLIST_ONLY')
        self.assertFalse(x['profitCertified'])

    def test_run_activated_after_start_cannot_claim_preselection(self):
        from cohort_lineage import check_cohort_lineage
        r=self.capture();root=self.root/'research';root.mkdir()
        from market_archive import canonical
        ts=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        cfg=json.loads((r.folder/'events.jsonl').read_text().splitlines()[0])['data']['watchlist']
        x={'seq':1,'prev':'0'*64,'activatedAt':ts,'newConfigSha256':hashlib.sha256(canonical(cfg)).hexdigest(),
           'scanArchiveId':'future'}
        x['hash']=hashlib.sha256(canonical(x)).hexdigest()
        (root/'watchlist_activation.jsonl').write_bytes(canonical(x)+b'\n')
        self.assertEqual(check_cohort_lineage(r.folder,self.root)['status'],'UNREGISTERED_BEFORE_ACTIVATION')

    def test_mismatched_registered_cohort_fails(self):
        from cohort_lineage import check_cohort_lineage
        from market_archive import canonical
        r=self.capture();root=self.root/'research';root.mkdir()
        entry={'seq':1,'prev':'0'*64,
               'activatedAt':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
               'scanArchiveId':'historical','newConfigSha256':'f'*64}
        entry['hash']=hashlib.sha256(canonical(entry)).hexdigest()
        (root/'watchlist_activation.jsonl').write_bytes(canonical(entry)+b'\n')
        self.assertEqual(check_cohort_lineage(r.folder,self.root)['status'],'NO_GO_COHORT_MISMATCH')

    def test_tampered_activation_history_fails(self):
        from cohort_lineage import check_cohort_lineage
        r=self.capture();root=self.root/'research';root.mkdir()
        (root/'watchlist_activation.jsonl').write_text('{"seq":1,"prev":"hacked"}\n')
        self.assertEqual(check_cohort_lineage(r.folder,self.root)['status'],'NO_GO_ACTIVATION_LEDGER')
