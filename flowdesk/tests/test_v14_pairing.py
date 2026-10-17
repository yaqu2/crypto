"""Research provenance audit tests on synthetic market messages, not real performance."""
import tempfile, unittest, json, hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder import Recorder
from quote_pair_audit import audit_pairing

NOW=datetime(2026,10,14,15,0,0,tzinfo=timezone.utc)


def quote(code,source=NOW,last=100.):
    local=source.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York'))
    return {'code':code,'data_date':local.date().isoformat(),
            'data_time':local.strftime('%H:%M:%S'),'last_price':last}

def book(code='US.AAOI'):
    return {'code':code,'Bid':[(99.99,1000)],'Ask':[(100.01,200)]}


class PairedQuoteAudit(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.rec=Recorder(self.tmp.name,segment_bytes=10000)
        self.closed=False
        self.addCleanup(self.finish)
    def finish(self):
        if not self.closed:self.rec.close();self.closed=True
    def add_quote(self,code,sec=0,source_sec=None,origin='push',price=100.):
        when=NOW+timedelta(seconds=sec)
        source=NOW+timedelta(seconds=sec if source_sec is None else source_sec)
        self.rec.record('quote',[quote(code,source,price)],origin=origin,callback_received_at=when.isoformat())
    def add_book(self,sec=1,code='US.AAOI',valid=True,origin='push'):
        row=book(code)
        if not valid:row['Ask']=[(98.,100.)]
        self.rec.record('book',row,origin=origin,callback_received_at=(NOW+timedelta(seconds=sec)).isoformat())
    def audit(self,**kwargs):
        self.rec.q.join();self.finish()
        return audit_pairing(self.rec.folder,**kwargs)
    def test_matching_prior_quotes_count_pair_not_certified_alpha(self):
        self.add_quote('US.SPY');self.add_quote('US.AAOI');self.add_book()
        r=self.audit()
        self.assertEqual(r['counts']['pairedCandidateBookCallbacks'],1)
        self.assertEqual(r['perStock']['US.AAOI']['pairedCandidateBookCallbacks'],1)
        self.assertFalse(r['readyForForwardProfitEvaluation'])
        self.assertFalse(r['liveOrdersEnabled'])
    def test_missing_base_excluded(self):
        self.add_quote('US.AAOI');self.add_book()
        r=self.audit();self.assertEqual(r['rejectionReasons']['noCausallyPriorFreshBenchmarkQuote'],1)
    def test_missing_own_quote_excluded(self):
        self.add_quote('US.SPY');self.add_book()
        r=self.audit();self.assertEqual(r['rejectionReasons']['noCausallyPriorFreshOwnQuote'],1)
    def test_six_seconds_exceeds_max_pairing(self):
        self.add_quote('US.SPY');self.add_quote('US.AAOI');self.add_book(sec=6)
        r=self.audit();self.assertNotIn('pairedCandidateBookCallbacks',r['counts'])
    def test_quote_callback_after_book_not_causal(self):
        self.add_book(0);self.add_quote('US.SPY',1);self.add_quote('US.AAOI',1)
        r=self.audit();self.assertEqual(r['rejectionReasons']['noCausallyPriorFreshOwnQuote'],1)
    def test_seed_quote_never_as_live_evidence(self):
        self.add_quote('US.SPY');self.add_quote('US.AAOI',origin='seed');self.add_book()
        r=self.audit();self.assertEqual(r['rejectionReasons']['noCausallyPriorFreshOwnQuote'],1)
    def test_future_source_timestamp_excluded(self):
        self.add_quote('US.SPY');self.add_quote('US.AAOI',source_sec=7);self.add_book()
        r=self.audit();self.assertGreater(r['counts']['invalidOrStaleQuotes'],0)
    def test_stale_provider_source_excluded(self):
        self.add_quote('US.SPY');self.add_quote('US.AAOI',source_sec=-30);self.add_book()
        r=self.audit();self.assertGreater(r['counts']['invalidOrStaleQuotes'],0)
    def test_nonregular_snapshots_excluded(self):
        t=NOW-timedelta(hours=3)
        self.rec.record('book',book(),callback_received_at=t.isoformat())
        r=self.audit();self.assertNotIn('regularBookPushEvents',r['counts'])
    def test_crossed_book_rejected(self):
        self.add_quote('US.SPY');self.add_quote('US.AAOI');self.add_book(valid=False)
        r=self.audit();self.assertEqual(r['rejectionReasons']['invalidBook'],1)
    def test_tampered_full_run_is_rejected(self):
        self.add_quote('US.SPY');self.add_book();self.audit()
        p=self.rec.folder/'events.jsonl'
        p.write_bytes(p.read_bytes()+b'{}\n')
        with self.assertRaisesRegex(ValueError,'RAW_(NONCONTIGUOUS_SEQUENCE|SEGMENT_SHA256_MISMATCH)'):
            audit_pairing(self.rec.folder)
    def test_prefix_requires_explicit_diagnostic_flag(self):
        self.add_quote('US.SPY');self.add_book();self.audit()
        p=self.rec.folder/'metadata.json';m=json.loads(p.read_text());m['closed']=False;p.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'RAW_RECORDER_NOT_CLOSED'):
            audit_pairing(self.rec.folder)
        result=audit_pairing(self.rec.folder,allow_prefix=True)
        self.assertFalse(result['closedRun'])
    def test_out_of_order_callback_excluded(self):
        self.add_quote('US.SPY',sec=2);self.add_quote('US.AAOI',sec=1);self.add_book(sec=3)
        r=self.audit();self.assertEqual(r['counts']['outOfOrderCallback'],1)

if __name__=='__main__':unittest.main()
