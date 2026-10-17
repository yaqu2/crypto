"""Fault injection against the synthetic-only broker-order state machine."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from order_lifecycle_lab import (PaperLedger, Guardrails, replay_scenario,
                                 sample_scenario, canonical, main)


def event(num, kind, order='b', **data):
    return {'eventId':f'e{num}', 'type':kind, 'source':'SYNTHETIC_SCENARIO',
            'at':(datetime(2026,10,14,13,30,tzinfo=timezone.utc)
                  + timedelta(seconds=num)).isoformat(),
            **({'orderId':order} if order is not None else {}), **data}


def buy(num=0, order='b', qty=5, limit=100., code='US.AAOI'):
    return event(num, 'PLACE', order, side='BUY', symbol=code, qty=qty, limitPrice=limit)


def sell(num=0, order='s', qty=5, limit=101., code='US.AAOI'):
    return event(num, 'PLACE', order, side='SELL', symbol=code, qty=qty, limitPrice=limit)


class OrderLifecycleTests(unittest.TestCase):
    def assertInvalid(self, ledger, evt, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            ledger.apply(evt)

    def test_demo_is_explicitly_fictional_and_not_broker_proof(self):
        out = replay_scenario(sample_scenario())
        self.assertTrue(out['status'].startswith('SYNTHETIC_'))
        self.assertEqual(out['actualOrdersSent'], 0)
        self.assertEqual(out['confirmedBrokerFills'], 0)
        self.assertFalse(out['alphaCertified'])
        self.assertFalse(out['accountProfitKnown'])
        self.assertTrue(out['unresolvedStateAfterDisconnect'])
        self.assertIn('CANCELLED', [x['state'] for x in out['orders'].values()])

    def test_quote_never_fills_without_explicit_synthetic_fill_event(self):
        x=PaperLedger();x.apply(buy());x.apply(event(1,'ACK'))
        self.assertEqual(x.orders['b']['filled'],0)
        self.assertEqual(x.report()['reservedModeledCapitalUsd'],500)
        self.assertEqual(x.report()['simulatedGrossRealizedUsd'],0)

    def test_cancel_request_does_not_release_capital(self):
        x=PaperLedger();x.apply(buy());x.apply(event(1,'CANCEL_REQUEST'))
        self.assertEqual(x.reserved_usd(),500)
        self.assertEqual(x.orders['b']['state'],'CANCEL_PENDING')
        x.apply(event(2,'CANCEL_ACK'))
        self.assertEqual(x.reserved_usd(),0)

    def test_partial_fill_cancel_ack_keeps_held_shares_reserved(self):
        x=PaperLedger()
        for a in (buy(),event(1,'FILL',qty=2,price=99.5),
                  event(2,'CANCEL_REQUEST'),event(3,'CANCEL_ACK')):x.apply(a)
        self.assertEqual(x.positions['US.AAOI']['qty'],2)
        self.assertAlmostEqual(x.reserved_usd(),199.)
        self.assertEqual(x.orders['b']['state'],'CANCELLED')

    def test_fill_during_pending_cancel_counts_and_does_not_vanish(self):
        x=PaperLedger()
        for a in (buy(), event(1,'CANCEL_REQUEST'),
                  event(2,'FILL',qty=2,price=99.9),event(3,'CANCEL_ACK')):x.apply(a)
        self.assertEqual(x.orders['b']['filled'],2)
        self.assertEqual(x.positions['US.AAOI']['qty'],2)
        self.assertAlmostEqual(x.reserved_usd(),199.8)

    def test_late_fill_after_acknowledged_cancel_is_conflict(self):
        x=PaperLedger()
        for a in (buy(),event(1,'CANCEL_REQUEST'),event(2,'CANCEL_ACK')):x.apply(a)
        self.assertInvalid(x,event(3,'FILL',qty=1,price=100),'LATE_FILL_AFTER_TERMINAL')

    def test_partial_fill_then_reject_preserves_position(self):
        x=PaperLedger()
        for a in (buy(),event(1,'FILL',qty=1,price=100),event(2,'REJECT')):x.apply(a)
        self.assertEqual(x.orders['b']['state'],'REJECTED')
        self.assertEqual(x.positions['US.AAOI']['qty'],1)
        self.assertEqual(x.reserved_usd(),100)

    def test_sell_outstanding_cannot_be_double_reserved(self):
        x=PaperLedger()
        for a in (buy(),event(1,'FILL',qty=5,price=100),sell(2,qty=3)):x.apply(a)
        self.assertInvalid(x,sell(3,order='s2',qty=3),'NO_SHORTS_OR_DOUBLE_SELL')

    def test_no_short_sell(self):
        self.assertInvalid(PaperLedger(),sell(),'NO_SHORTS_OR_DOUBLE_SELL')

    def test_sell_fill_releases_position_and_calculates_synthetic_pnl(self):
        x=PaperLedger()
        for a in (buy(),event(1,'FILL',qty=5,price=99.),sell(2,qty=5),
                  event(3,'ACK',order='s'),event(4,'FILL',order='s',qty=5,price=101.)):
            x.apply(a)
        self.assertEqual(x.reserved_usd(),0)
        self.assertAlmostEqual(x.realized_gross,10.)
        self.assertAlmostEqual(x.realized_after_stress,10.-(495+505)*.004/2)
        self.assertFalse(x.report()['accountProfitKnown'])

    def test_partial_sell_fill_keeps_remaining_held(self):
        x=PaperLedger()
        for a in (buy(),event(1,'FILL',qty=5,price=100),sell(2),
                  event(3,'FILL',order='s',qty=2,price=101)):
            x.apply(a)
        self.assertEqual(x.positions['US.AAOI']['qty'],3)
        self.assertEqual(x.reserved_usd(),300)
        self.assertEqual(x.orders['s']['state'],'PARTIALLY_FILLED')

    def test_buy_price_cannot_worsen_beyond_limit(self):
        x=PaperLedger();x.apply(buy())
        self.assertInvalid(x,event(1,'FILL',qty=1,price=100.1),'BUY_PRICE_WORSE')

    def test_sell_price_cannot_worsen_beyond_limit(self):
        x=PaperLedger();x.apply(buy());x.apply(event(1,'FILL',qty=5,price=100));x.apply(sell(2))
        self.assertInvalid(x,event(3,'FILL',order='s',qty=1,price=99),'SELL_PRICE_WORSE')

    def test_overfill_rejected(self):
        x=PaperLedger();x.apply(buy())
        self.assertInvalid(x,event(1,'FILL',qty=6,price=99),'OVERFILL')

    def test_fill_allowed_before_delayed_ack(self):
        x=PaperLedger()
        x.apply(buy());x.apply(event(1,'FILL',qty=2,price=99));x.apply(event(2,'ACK'))
        self.assertEqual(x.orders['b']['state'],'PARTIALLY_FILLED')
        self.assertEqual(x.orders['b']['filled'],2)

    def test_duplicate_event_id_cannot_double_count(self):
        x=PaperLedger();x.apply(buy());f=event(1,'FILL',qty=2,price=100)
        x.apply(f)
        self.assertInvalid(x,f,'DUPLICATE_EVENT_ID')
        self.assertEqual(x.orders['b']['filled'],2)

    def test_out_of_order_event_time_fails(self):
        x=PaperLedger();x.apply(buy(2))
        self.assertInvalid(x,event(1,'ACK'),'OUT_OF_ORDER')

    def test_naive_timestamp_rejected(self):
        e=buy();e['at']='2026-10-14T13:30:00'
        self.assertInvalid(PaperLedger(),e,'NAIVE_TIMESTAMP')

    def test_source_must_be_explicitly_synthetic(self):
        e=buy();e['source']='MOOMOO_REAL'
        self.assertInvalid(PaperLedger(),e,'NOT_EXPLICITLY_SYNTHETIC')

    def test_market_price_nan_and_infinity_rejected(self):
        for price in [float('nan'),float('inf'),float('-inf'),0,-1]:
            with self.subTest(price=price):
                x=PaperLedger();self.assertInvalid(x,buy(limit=price),'INVALID_LIMIT_PRICE')

    def test_cannot_accept_float_or_bool_quantity(self):
        for qty in [True,1.5,0,-1,1000001]:
            with self.subTest(qty=qty):
                self.assertInvalid(PaperLedger(),buy(qty=qty),'INVALID_QUANTITY')

    def test_cap_per_order(self):
        self.assertInvalid(PaperLedger(),buy(qty=11,limit=100),'CAP_PER_ORDER')

    def test_gross_cap_across_multiple_orders(self):
        x=PaperLedger(Guardrails(gross_cap_usd=1000.))
        x.apply(buy(qty=8,limit=100))
        self.assertInvalid(x,buy(1,order='b2',qty=3,limit=100,code='US.NVDA'),'CAP_GROSS')

    def test_max_active_orders(self):
        x=PaperLedger(Guardrails(max_active_orders=1))
        x.apply(buy())
        self.assertInvalid(x,buy(1,order='b2',code='US.NVDA'),'CAP_ACTIVE_ORDERS')

    def test_cancel_ack_without_cancel_request_rejected(self):
        x=PaperLedger();x.apply(buy())
        self.assertInvalid(x,event(1,'CANCEL_ACK'),'CANCEL_ACK_WITHOUT')

    def test_cancel_after_full_fill_rejected(self):
        x=PaperLedger();x.apply(buy());x.apply(event(1,'FILL',qty=5,price=100))
        self.assertInvalid(x,event(2,'CANCEL_REQUEST'),'CANCEL_REQUEST_AFTER_TERMINAL')

    def test_feed_disconnect_blocks_new_orders_and_keeps_reserve(self):
        x=PaperLedger();x.apply(buy());x.apply(event(1,'DISCONNECT',order=None))
        self.assertEqual(x.reserved_usd(),500)
        self.assertInvalid(x,buy(2,order='b2'),'NEW_ORDER_FORBIDDEN')
        self.assertTrue(x.report()['unresolvedStateAfterDisconnect'])

    def test_invalid_scenario_type_rejected(self):
        with self.assertRaisesRegex(ValueError,'SCENARIO_TYPE'):
            replay_scenario({'scenarioType':'BROKER_LIVE','events':[buy()]})

    def test_missing_scenario_rejected(self):
        with self.assertRaisesRegex(ValueError,'INVALID_EVENT_COUNT'):
            replay_scenario({'scenarioType':'EXPLICITLY_SYNTHETIC_ONLY','events':[]})

    def test_same_input_same_hash_trace(self):
        a=replay_scenario(sample_scenario());b=replay_scenario(sample_scenario())
        self.assertEqual(a['finalEventChainSha256'],b['finalEventChainSha256'])
        self.assertEqual(a['scenarioSha256'],b['scenarioSha256'])
        c=sample_scenario();c['events'][2]['price']=99.8
        z=replay_scenario(c)
        self.assertNotEqual(z['scenarioSha256'],b['scenarioSha256'])

    def test_synthetic_rehearsal_cannot_claim_account_profit(self):
        r=replay_scenario(sample_scenario())
        self.assertFalse(r['accountProfitKnown'])
        self.assertFalse(r['liveTradingAuthorized'])
        self.assertEqual(r['executionEvidence'],'NONE_ALL_EVENTS_SYNTHETIC')
        self.assertNotIn('realizedAccountProfit',r)

    def test_cli_default_has_no_fake_results(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('order_lifecycle_lab.OUTPUT', Path(d)/'r.json'):
                self.assertEqual(main([]),2)
                r=json.loads((Path(d)/'r.json').read_text())
                self.assertEqual(r['status'],'NO_GO_NO_EXPLICIT_SYNTHETIC_SCENARIO')
                self.assertEqual(r['actualOrdersSent'],0)

    def test_cli_demo_writes_fictionally_labeled_report(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('order_lifecycle_lab.OUTPUT', Path(d)/'r.json'):
                self.assertEqual(main(['--demo']),0)
                r=json.loads((Path(d)/'r.json').read_text())
                self.assertEqual(r['inputProvenance'],'BUILT_IN_FICTIONAL_FIXTURE')
                self.assertEqual(r['confirmedBrokerFills'],0)

    def test_cli_broker_claimed_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            inp=Path(d)/'fake.json';e=buy();e['source']='BROKER_FILLS'
            inp.write_text(json.dumps({'scenarioType':'EXPLICITLY_SYNTHETIC_ONLY','events':[e]}))
            with patch('order_lifecycle_lab.OUTPUT',Path(d)/'r.json'):
                self.assertEqual(main(['--scenario',str(inp)]),2)
                r=json.loads((Path(d)/'r.json').read_text())
                self.assertEqual(r['status'],'NO_GO_INVALID_SYNTHETIC_SCENARIO')
                self.assertEqual(r['confirmedBrokerFills'],0)

    def test_invalid_limits_rejected(self):
        for kwargs in [{'gross_cap_usd':200000},{'max_order_usd':6000},
                       {'max_active_orders':False},{'extra_round_trip_bps':float('nan')}]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    PaperLedger(Guardrails(**kwargs))

    def test_existing_quote_price_does_not_create_order(self):
        x=PaperLedger();self.assertEqual(x.report()['orders'],{})
        self.assertEqual(x.report()['confirmedBrokerFills'],0)
        self.assertEqual(x.report()['reservedModeledCapitalUsd'],0)

    def test_no_real_trading_calls_imported(self):
        import inspect, order_lifecycle_lab
        s=inspect.getsource(order_lifecycle_lab)
        for prohibited in ('place_order(', 'modify_order(', 'unlock_trade('):
            self.assertNotIn(prohibited,s)


if __name__=='__main__':unittest.main()

class DailyPauseTests(unittest.TestCase):
    def test_loss_pause_applies_only_to_same_et_day(self):
        from order_lifecycle_lab import PaperLedger, Guardrails
        x=PaperLedger(Guardrails(modeled_daily_loss_pause_usd=5.))
        for a in (buy(), event(1,'FILL',qty=5,price=100),sell(2,qty=5,limit=90),
                  event(3,'FILL',order='s',qty=5,price=90)):
            x.apply(a)
        with self.assertRaisesRegex(ValueError,'PAUSE_ON_MODELED_DAILY_LOSS'):
            x.apply(buy(4,order='b2'))
        nxt=buy(5,order='b3')
        nxt['at']='2026-10-15T13:30:05+00:00'
        x.apply(nxt)
        self.assertEqual(x.orders['b3']['state'],'PENDING_ACK')
        self.assertLess(x.report()['simulatedDailyAfterStressUsd']['2026-10-14'],-5)

class BrowserLifecycleRoute(unittest.TestCase):
    def test_404_before_rehearsal_then_readonly_json(self):
        import threading, urllib.request, urllib.error
        import bridge
        from runtime import Runtime, DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as tmp:
            with patch('bridge.ROOT',Path(tmp)):
                rt=Runtime(DEFAULT_CONFIG);server=bridge.create_server(rt,'/private/')
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                url=f'http://127.0.0.1:{server.server_port}/private/order-lifecycle-rehearsal'
                try:
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        urllib.request.urlopen(url,timeout=3)
                    self.assertEqual(e.exception.code,404)
                    report=Path(tmp)/'output'/'rehearsals'/'order_lifecycle_report.json'
                    report.parent.mkdir(parents=True)
                    report.write_text(json.dumps(replay_scenario(sample_scenario())))
                    with urllib.request.urlopen(url,timeout=3) as f:
                        data=json.load(f)
                    self.assertEqual(data['confirmedBrokerFills'],0)
                    self.assertTrue(data['status'].startswith('SYNTHETIC_'))
                finally:
                    server.shutdown();server.server_close();thread.join(timeout=2)

    def test_dashboard_link_explicitly_labels_synthetic(self):
        text=(Path(__file__).resolve().parents[1]/'dashboard.html').read_text()
        self.assertIn('href="order-lifecycle-rehearsal"',text)
        self.assertIn('合成订单状态机',text)
