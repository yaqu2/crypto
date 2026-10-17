"""Adversarial provenance and execution tests, using synthetic prices only.

Tests validate rejection semantics, NEVER predicted market returns.
"""
import tempfile, unittest, json, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from feed_guard import evaluate
from runtime import Runtime, DEFAULT_CONFIG
from prospective import ForwardStudy, verify_chain

ET=ZoneInfo('America/New_York')
NOW=datetime(2026,10,14,15,0,0,tzinfo=timezone.utc)


def mkbook(price=100., bidsize=1000., asksize=100., symbol='US.AAOI'):
    return {'code':symbol,
            'Bid':[(round(price-.01-i*.01,4),bidsize) for i in range(5)],
            'Ask':[(round(price+.01+i*.01,4),asksize) for i in range(5)]}


def shadowbook(price=100., bidsize=1000., asksize=100.):
    return {'bids':[{'price':x[0],'size':x[1]} for x in mkbook(price,bidsize,asksize)['Bid']],
            'asks':[{'price':x[0],'size':x[1]} for x in mkbook(price,bidsize,asksize)['Ask']]}


class SourceClockSafety(unittest.TestCase):
    def quality(self, at):
        return evaluate(DEFAULT_CONFIG,{'US.SPY/QUOTE':'成功'}, {'US.SPY':at},
                        (NOW-timedelta(minutes=3)).isoformat(), now=NOW,connection_ok=True)

    def test_v14_still_rejects_stale_provider_timestamp(self):
        self.assertEqual(self.quality((NOW-timedelta(seconds=21)).isoformat())['status'],'NO_GO_STALE')

    def test_future_provider_clock_over_two_seconds_denied(self):
        self.assertEqual(self.quality((NOW+timedelta(seconds=4)).isoformat())['status'],'NO_GO_STALE')

    def test_two_seconds_of_future_clock_bounded(self):
        self.assertEqual(self.quality((NOW+timedelta(seconds=2)).isoformat())['status'],'RECENT_RESEARCH_ONLY')

    def test_feed_gate_states_vendor_source_not_receipt(self):
        q=self.quality((NOW-timedelta(seconds=3)).isoformat())
        self.assertEqual(q['benchmarkTimeProvenance'],'VENDOR_QUOTE_SOURCE_AS_OF')
        self.assertEqual(q['benchmarkAgeSeconds'],3.)
        self.assertFalse(q['liveOrdersEnabled'])


class IntegratedCausality(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        rec=Mock();rec.run_id='synthetic-not-real';rec.status.return_value={'error':None,'dropped':0}
        self.rt=Runtime(DEFAULT_CONFIG,recorder=rec,forward_folder=Path(self.tmp.name)/'forward')
        self.addCleanup(self.rt.close)
        patch_quote=patch.object(self.rt.engine,'ingest_quote',return_value=True)
        patch_book=patch.object(self.rt.engine,'ingest_book',return_value=True)
        patch_gate=patch.object(self.rt,'feed_quality',return_value={'status':'RECENT_RESEARCH_ONLY'})
        for p in (patch_quote,patch_book,patch_gate):p.start();self.addCleanup(p.stop)

    def pushquote(self,code='US.AAOI',at=NOW,source=None,origin='push',last=100.):
        src=(source or at).astimezone(ET)
        row={'code':code,'last_price':last,'data_date':src.date().isoformat(),
             'data_time':src.strftime('%H:%M:%S')}
        self.rt.process(('quote',[row],origin,at.isoformat(),time.monotonic_ns(),0))

    def pushbook(self,at=NOW,origin='push',symbol='US.AAOI',**kwargs):
        self.rt.process(('book',mkbook(symbol=symbol,**kwargs),origin,at.isoformat(),time.monotonic_ns(),0))

    def test_book_without_any_quotes_cannot_signal(self):
        self.pushbook()
        self.assertEqual(self.rt.forward.counts.get('signals',0),0)

    def test_benchmark_alone_cannot_enable_stock_book(self):
        self.pushquote('US.SPY');self.pushbook()
        self.assertEqual(self.rt.forward.counts.get('signals',0),0)

    def test_stock_quote_alone_cannot_enable_global_gate(self):
        self.pushquote();self.pushbook()
        self.assertEqual(self.rt.forward.counts.get('signals',0),0)

    def test_seed_quote_does_not_authorize_forward_study(self):
        self.pushquote('US.SPY');self.pushquote(origin='seed');self.pushbook()
        self.assertEqual(self.rt.forward.counts.get('signals',0),0)
        self.assertNotIn('US.AAOI',self.rt.good_quotes)

    def test_seven_second_old_stock_source_denied_even_if_received_now(self):
        self.pushquote('US.SPY')
        self.pushquote(source=NOW-timedelta(seconds=7));self.pushbook()
        self.assertEqual(self.rt.forward.counts.get('signals',0),0)
        self.assertEqual(self.rt.good_quotes['US.AAOI'],(NOW-timedelta(seconds=7)).isoformat())

    def test_stale_benchmark_source_denied_even_with_valid_stock(self):
        self.pushquote('US.SPY',source=NOW-timedelta(seconds=8));self.pushquote();self.pushbook()
        self.assertEqual(self.rt.forward.counts.get('signals',0),0)

    def test_fresh_matching_pushed_sources_permit_just_a_study(self):
        self.pushquote('US.SPY');self.pushquote();self.pushbook()
        self.assertGreater(self.rt.forward.counts['signals'],0)
        self.assertFalse(self.rt.forward.summary()['liveOrdersEnabled'])

    def test_accepted_quote_records_provider_time_not_receipt(self):
        self.pushquote('US.SPY',source=NOW-timedelta(seconds=3))
        self.assertEqual(self.rt.good_quotes['US.SPY'],(NOW-timedelta(seconds=3)).isoformat())

    def test_rejected_quote_cannot_overwrite_last_good_snapshot(self):
        self.pushquote('US.SPY',last=100.)
        self.rt.engine.ingest_quote.return_value=False
        self.pushquote('US.SPY',at=NOW+timedelta(seconds=1),last=700.)
        self.assertEqual(self.rt.state['symbols']['US.SPY']['quote']['last'],100.)
        self.assertEqual(self.rt.good_quotes['US.SPY'],NOW.isoformat())

    def test_cached_stock_quote_is_not_upgraded_by_book_push(self):
        self.pushquote('US.SPY')
        self.pushquote(origin='seed')
        self.pushbook(at=NOW+timedelta(seconds=2))
        self.assertEqual(self.rt.forward.counts.get('paperEntries',0),0)


class NoFakeExecutions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'shadow'
        self.s=ForwardStudy(self.root,['US.AAOI'])
        self.addCleanup(self.s.close)

    def send(self,second,**kwargs):
        self.s.on_book('US.AAOI',shadowbook(**kwargs),NOW+timedelta(seconds=second),allowed=True)

    def test_one_share_exceeds_notional_cap_no_entry(self):
        self.send(0,price=2000.);self.send(3,price=2000.)
        self.assertEqual(self.s.counts.get('paperEntries',0),0)
        self.assertGreater(self.s.counts['notionalRejected'],0)

    def test_cooldown_after_completed_exit_not_retriggered_immediately(self):
        self.send(0);self.send(3)
        for sec in (15,27,39,51,63,64):self.send(sec)
        before=self.s.counts['signals']
        self.send(65)
        self.assertEqual(self.s.counts['signals'],before)
        self.assertGreater(self.s.counts['cooldownSuppressed'],0)
        self.send(75)
        self.assertGreater(self.s.counts['signals'],before)

    def test_cooldown_after_failed_liquidity_not_spam(self):
        self.send(0,asksize=20);self.send(3,asksize=20)
        before=self.s.counts['signals']
        self.send(4,asksize=20)
        self.assertEqual(self.s.counts['signals'],before)
        self.assertGreater(self.s.counts['cooldownSuppressed'],0)

    def test_frozen_protocol_includes_cooldown(self):
        self.s.close()
        p,events=verify_chain(self.root)
        self.assertEqual(p['postDispositionCooldownSeconds'],10.0)
        self.assertFalse(p['liveOrdersEnabled'])

if __name__=='__main__':unittest.main()
