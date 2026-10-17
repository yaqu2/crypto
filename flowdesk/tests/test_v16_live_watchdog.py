"""v1.6 fail-closed online observation tests. Synthetic only, not alpha evidence."""
import json, tempfile, unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from prospective import ForwardStudy, verify_chain, sha_bytes
from forward_report import report
from feed_guard import clock_state

T = datetime(2026,10,12,14,0,0,tzinfo=timezone.utc)

def book():
    return {
      'bids':[{'price':round(99.99-i*0.01,4),'size':1000} for i in range(5)],
      'asks':[{'price':round(100.01+i*0.01,4),'size':100} for i in range(5)]}

def q(when):return {'US.TEST':when.isoformat(),'US.SPY':when.isoformat()}

class TestOnlineContinuity(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.dir=Path(t.name)
        self.s=ForwardStudy(self.dir/'shadow',['US.TEST'])
        self.addCleanup(self.s.close)

    def feed(self,sec,book_data=None):
        self.s.on_book('US.TEST',book_data or book(),T+timedelta(seconds=sec),allowed=True)

    def test_does_not_credit_exit_across_sixty_second_silence(self):
        self.feed(0);self.feed(3);self.feed(65)
        self.assertEqual(self.s.counts['paperExits'],0)
        self.assertGreater(self.s.counts['censored'],0)
        self.assertEqual(self.s.counts['discontinuousBookCensored'],1)
        self.s.close();_,events=verify_chain(self.dir/'shadow')
        self.assertTrue(any(x.get('reason')=='UNOBSERVED_BOOK_GAP' for x in events))
        self.assertFalse(any(x['type']=='PAPER_EXIT' for x in events))

    def test_returning_book_cannot_immediately_trigger_after_silence(self):
        self.feed(0);self.feed(50)
        self.assertEqual(self.s.counts['signals'],8)
        self.feed(61) # wait beyond disposition cooldown; do not resignal at recovery tick
        self.assertGreater(self.s.counts['signals'],8)
        # The resumed book at t=50 is a boundary, not a cross-gap OFI sample.
        self.s.close();_,ev=verify_chain(self.dir/'shadow')
        self.assertFalse(any(x['type']=='SIGNAL' and x['at']==(T+timedelta(seconds=50)).isoformat() for x in ev))

    def test_silent_book_clock_censors_before_new_event(self):
        self.feed(0);self.feed(3)
        self.assertGreater(self.s.counts['paperEntries'],0)
        self.s.monitor(T+timedelta(seconds=19),q(T+timedelta(seconds=19)),'US.SPY')
        self.assertFalse(self.s.positions)
        self.assertGreater(self.s.counts['silentBookCensored'],0)
        self.assertEqual(self.s.counts['paperExits'],0)

    def test_stale_vendor_benchmark_censors_even_if_books_look_fresh(self):
        self.feed(0);self.feed(3)
        self.s.monitor(T+timedelta(seconds=8),{'US.SPY':T.isoformat(),'US.TEST':(T+timedelta(seconds=8)).isoformat()},'US.SPY')
        self.assertFalse(self.s.positions)
        self.assertGreater(self.s.counts['quoteProofCensored'],0)
        self.assertEqual(self.s.counts['paperExits'],0)

    def test_stale_target_censors_even_with_spy_fresh(self):
        self.feed(0);self.feed(3)
        self.s.monitor(T+timedelta(seconds=9),{'US.SPY':(T+timedelta(seconds=9)).isoformat(),'US.TEST':T.isoformat()},'US.SPY')
        self.assertFalse(self.s.positions)
        self.assertGreater(self.s.counts['quoteProofCensored'],0)

    def test_missing_quote_provenance_is_not_healthy(self):
        self.feed(0);self.feed(3)
        self.s.monitor(T+timedelta(seconds=4),{},'US.SPY')
        self.assertFalse(self.s.positions)
        self.assertGreater(self.s.counts['quoteProofCensored'],0)

    def test_proper_continuity_still_allows_a_quotation_model_exit(self):
        for sec in (0,3,15,27,39,51,62,64):
            self.feed(sec)
            self.s.monitor(T+timedelta(seconds=sec, milliseconds=100),q(T+timedelta(seconds=sec)),'US.SPY')
        self.assertGreater(self.s.counts['paperExits'],0)
        self.assertEqual(self.s.counts['discontinuousBookCensored'],0)
        self.assertFalse(self.s.summary()['fillVerified'])

    def test_vendor_future_clock_is_not_valid_proof(self):
        self.feed(0);self.feed(3)
        self.s.monitor(T+timedelta(seconds=4),q(T+timedelta(seconds=10)),'US.SPY')
        self.assertFalse(self.s.positions)

    def test_v3_protocol_source_is_frozen(self):
        self.s.close()
        x=report(self.dir/'shadow')
        self.assertTrue(x['journalValid'])
        mf=self.dir/'shadow'/'frozen_protocol.json'
        plan=json.loads(mf.read_text())
        self.assertEqual(plan['version'],3)
        self.assertEqual(plan['maxBookSilenceSeconds'],15)
        plan['maxBookSilenceSeconds']=500
        mf.write_text(json.dumps(plan)+'\n')
        (self.dir/'shadow'/'protocol_seal.sha256').write_text(sha_bytes(mf.read_bytes())+'\n')
        with self.assertRaisesRegex(ValueError,'FROZEN_RULES_OR_COSTS_DIFFER_FROM_ENGINE'):report(self.dir/'shadow')

    def test_legacy_v15_protocol_uses_original_engine(self):
        self.s.close()
        m=self.dir/'shadow'/'frozen_protocol.json'
        plan=json.loads(m.read_text())
        plan['version']=2
        plan.pop('maxBookSilenceSeconds')
        plan.pop('maxVendorQuoteAgeDuringHoldSeconds')
        src=Path(__file__).parent.parent/'legacy_v15'/'prospective.py'
        plan['sourceSha256']=sha_bytes(src.read_bytes())
        m.write_text(json.dumps(plan,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n')
        (self.dir/'shadow'/'protocol_seal.sha256').write_text(sha_bytes(m.read_bytes())+'\n')
        self.assertTrue(report(self.dir/'shadow')['journalValid'])

if __name__=='__main__':unittest.main()
