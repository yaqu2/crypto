#!/usr/bin/env python3
"""Fail-closed, SYNTHETIC-ONLY order lifecycle / risk rehearsal.

Never connects to Moomoo, brokerage, trading APIs, or claims real fills. The
input is an explicitly labeled synthetic scenario; no market quote can create
an ACK, FILL, or CANCEL_ACK. Crucially, CANCEL_REQUEST does not free funds;
partial fills and unknown disconnects continue to consume buying power.

This is an engineering fault-injection tool, NOT an execution backtest and NOT
an estimate of strategy alpha. It must not be used to authorise live trading.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'output' / 'rehearsals' / 'order_lifecycle_report.json'
ET = ZoneInfo('America/New_York')
TERMINAL = {'FILLED', 'CANCELLED', 'REJECTED'}
ACTIVE = {'PENDING_ACK', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CANCEL_PENDING'}
ALLOWED = {'PLACE', 'ACK', 'FILL', 'CANCEL_REQUEST', 'CANCEL_ACK', 'REJECT', 'DISCONNECT'}


@dataclass(frozen=True)
class Guardrails:
    account_usd: float = 100000.0
    gross_cap_usd: float = 5000.0
    max_order_usd: float = 1000.0
    max_active_orders: int = 5
    modeled_daily_loss_pause_usd: float = 100.0
    extra_round_trip_bps: float = 40.0

    def check(self):
        for x in (self.account_usd, self.gross_cap_usd, self.max_order_usd,
                  self.modeled_daily_loss_pause_usd, self.extra_round_trip_bps):
            if not math.isfinite(x) or x <= 0:
                raise ValueError('INVALID_POSITIVE_GUARDRAIL')
        if self.gross_cap_usd > self.account_usd or self.max_order_usd > self.gross_cap_usd:
            raise ValueError('INVALID_CAP_HIERARCHY')
        if type(self.max_active_orders) is not int or not 1 <= self.max_active_orders <= 100:
            raise ValueError('INVALID_ACTIVE_ORDER_LIMIT')
        if self.extra_round_trip_bps > 10000:
            raise ValueError('UNREASONABLE_COST_STRESS')


def _number(v, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError('INVALID_'+name)
    return float(v)


def _qty(v):
    if type(v) is not int or not 1 <= v <= 1_000_000:
        raise ValueError('INVALID_QUANTITY')
    return v


def _clock(v):
    if not isinstance(v, str):
        raise ValueError('TIMESTAMP_MISSING')
    try:
        t = datetime.fromisoformat(v.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('INVALID_TIMESTAMP') from None
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('NAIVE_TIMESTAMP_FORBIDDEN')
    return t.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


class PaperLedger:
    """Pure deterministic state machine. ALL events are invented scenario inputs."""
    def __init__(self, guards=Guardrails()):
        guards.check()
        self.guards = guards
        self.orders = {}
        self.positions = {}            # code -> {'qty', 'costUsd'}
        self.last_time = None
        self.seen_events = set()
        self.disconnected = False
        self.realized_gross = 0.0
        self.realized_after_stress = 0.0
        self.closed_notional = 0.0
        self.daily_after_stress = defaultdict(float)
        self.trace = []
        self.previous_hash = '0'*64
        self.peak_reserved_usd = 0.0
        self.rejected_actions = 0

    def _active_count(self):
        return sum(x['state'] in ACTIVE for x in self.orders.values())

    def reserved_usd(self):
        buys = sum((x['qty']-x['filled'])*x['limitPrice']
                   for x in self.orders.values()
                   if x['side'] == 'BUY' and x['state'] in ACTIVE)
        held = sum(max(0.0, p['costUsd']) for p in self.positions.values())
        return buys+held

    def _free_position_qty(self, code):
        held = self.positions.get(code, {}).get('qty', 0)
        outstanding = sum(x['qty']-x['filled'] for x in self.orders.values()
                          if x['symbol'] == code and x['side'] == 'SELL' and x['state'] in ACTIVE)
        return held-outstanding

    def apply(self, evt):
        if not isinstance(evt, dict) or evt.get('type') not in ALLOWED:
            raise ValueError('UNSUPPORTED_SYNTHETIC_EVENT')
        # A local synthetic scenario MUST never pass off events as broker records.
        if evt.get('source') != 'SYNTHETIC_SCENARIO':
            raise ValueError('NOT_EXPLICITLY_SYNTHETIC')
        event_id = evt.get('eventId')
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 96:
            raise ValueError('INVALID_EVENT_ID')
        if event_id in self.seen_events:
            raise ValueError('DUPLICATE_EVENT_ID_MAY_DOUBLE_COUNT_FILL')
        t = _clock(evt.get('at'))
        if self.last_time is not None and t < self.last_time:
            raise ValueError('OUT_OF_ORDER_RECEIVE_CLOCK')
        self.seen_events.add(event_id)
        self.last_time = t
        kind = evt['type']
        if kind == 'DISCONNECT':
            if self.disconnected:
                raise ValueError('DUPLICATE_DISCONNECT')
            self.disconnected = True
            result = 'UNCERTAIN_STATE_NO_NEW_ORDER_ALLOWED'
        else:
            oid = evt.get('orderId')
            if not isinstance(oid, str) or not 1 <= len(oid) <= 96:
                raise ValueError('INVALID_ORDER_ID')
            if kind == 'PLACE':
                if self.disconnected:
                    raise ValueError('NEW_ORDER_FORBIDDEN_AFTER_DISCONNECT')
                if oid in self.orders:
                    raise ValueError('ORDER_ID_REUSE')
                side = evt.get('side')
                code = evt.get('symbol')
                if side not in ('BUY', 'SELL') or not isinstance(code, str) or not code.startswith('US.') or len(code) > 24:
                    raise ValueError('INVALID_SIDE_OR_US_SYMBOL')
                qty = _qty(evt.get('qty'))
                limit = _number(evt.get('limitPrice'), 'LIMIT_PRICE')
                if self._active_count() >= self.guards.max_active_orders:
                    raise ValueError('CAP_ACTIVE_ORDERS')
                if side == 'BUY':
                    if self.daily_after_stress[t.astimezone(ET).date().isoformat()] <= -self.guards.modeled_daily_loss_pause_usd:
                        raise ValueError('PAUSE_ON_MODELED_DAILY_LOSS')
                    if qty*limit > self.guards.max_order_usd + 1e-9:
                        raise ValueError('CAP_PER_ORDER_USD')
                    if self.reserved_usd()+qty*limit > self.guards.gross_cap_usd + 1e-9:
                        raise ValueError('CAP_GROSS_EXHAUSTED')
                else:
                    if qty > self._free_position_qty(code):
                        raise ValueError('NO_SHORTS_OR_DOUBLE_SELL_RESERVATION')
                self.orders[oid] = {
                    'orderId': oid, 'symbol': code, 'side': side,
                    'qty': qty, 'limitPrice': limit, 'filled': 0,
                    'fillNotionalUsd': 0.0, 'state': 'PENDING_ACK',
                    'cancelRequested': False, 'acceptedByBroker': False,
                    'brokerEvidence': False, 'scenarioOnly': True
                }
                result = 'SYNTHETIC_ORDER_CREATED_NOT_SENT'
            else:
                if oid not in self.orders:
                    raise ValueError('UNKNOWN_ORDER_ID')
                o = self.orders[oid]
                if kind == 'ACK':
                    if o['state'] in TERMINAL:
                        raise ValueError('LATE_ACK_AFTER_TERMINAL_ORDER')
                    o['acceptedByBroker'] = False
                    if o['state'] == 'PENDING_ACK':
                        o['state'] = 'ACKNOWLEDGED'
                    result = 'SYNTHETIC_ACK_ONLY'
                elif kind == 'FILL':
                    if o['state'] in TERMINAL:
                        raise ValueError('LATE_FILL_AFTER_TERMINAL_ORDER_REQUIRES_RECONCILIATION')
                    qty = _qty(evt.get('qty'))
                    price = _number(evt.get('price'), 'FILL_PRICE')
                    if qty > o['qty']-o['filled']:
                        raise ValueError('OVERFILL')
                    if o['side'] == 'BUY' and price > o['limitPrice'] + 1e-9:
                        raise ValueError('BUY_PRICE_WORSE_THAN_LIMIT')
                    if o['side'] == 'SELL' and price < o['limitPrice'] - 1e-9:
                        raise ValueError('SELL_PRICE_WORSE_THAN_LIMIT')
                    if o['side'] == 'BUY':
                        p = self.positions.setdefault(o['symbol'], {'qty': 0, 'costUsd': 0.0})
                        p['qty'] += qty
                        p['costUsd'] += price*qty
                    else:
                        p = self.positions[o['symbol']]
                        if p['qty'] < qty:
                            raise ValueError('SELL_FILL_EXCEEDS_HELD_POSITION')
                        average = p['costUsd'] / p['qty']
                        entry_cost = average*qty
                        p['qty'] -= qty
                        p['costUsd'] -= entry_cost
                        if p['qty'] == 0:
                            p['costUsd'] = 0.0
                        self.realized_gross += price*qty-entry_cost
                        fee = (entry_cost+price*qty) * self.guards.extra_round_trip_bps / 20000.0
                        trade_pnl = price*qty-entry_cost-fee
                        self.realized_after_stress += trade_pnl
                        self.daily_after_stress[t.astimezone(ET).date().isoformat()] += trade_pnl
                        self.closed_notional += entry_cost
                    o['filled'] += qty
                    o['fillNotionalUsd'] += price*qty
                    o['state'] = 'FILLED' if o['filled'] == o['qty'] else (
                        'CANCEL_PENDING' if o['cancelRequested'] else 'PARTIALLY_FILLED')
                    result = 'SYNTHETIC_FILL_NOT_A_BROKER_FILL'
                elif kind == 'CANCEL_REQUEST':
                    if o['state'] in TERMINAL:
                        raise ValueError('CANCEL_REQUEST_AFTER_TERMINAL')
                    if o['cancelRequested']:
                        raise ValueError('DUPLICATE_CANCEL_REQUEST')
                    o['cancelRequested'] = True
                    o['state'] = 'CANCEL_PENDING'
                    result = 'CANCEL_REQUEST_NOT_CANCELLATION'
                elif kind == 'CANCEL_ACK':
                    if o['state'] != 'CANCEL_PENDING':
                        raise ValueError('CANCEL_ACK_WITHOUT_PENDING_REQUEST')
                    o['state'] = 'CANCELLED'
                    result = 'SYNTHETIC_CANCEL_ACK_ONLY_UNFILLED_RELEASED'
                elif kind == 'REJECT':
                    if o['state'] in TERMINAL:
                        raise ValueError('REJECT_AFTER_TERMINAL')
                    o['state'] = 'REJECTED'
                    result = 'SYNTHETIC_REJECT_UNFILLED_RELEASED'
        self.peak_reserved_usd = max(self.peak_reserved_usd, self.reserved_usd())
        signed = {'event': evt, 'outcome': result,
                  'reservedUsd': round(self.reserved_usd(), 6)}
        self.previous_hash = hashlib.sha256(self.previous_hash.encode()+canonical(signed)).hexdigest()
        self.trace.append({'eventId': event_id, 'type': kind, 'outcome': result,
                           'at': t.isoformat(), 'reservedUsd': round(self.reserved_usd(), 6),
                           'eventChainHash': self.previous_hash})
        return result

    def report(self, scenario_sha=None):
        outstanding = {k: {'symbol': v['symbol'], 'side': v['side'],
                           'requested': v['qty'], 'modeledFilled': v['filled'],
                           'state': v['state'], 'cancelRequested': v['cancelRequested'],
                           'brokerEvidence': False}
                       for k, v in self.orders.items()}
        return {
            'status': 'SYNTHETIC_ORDER_LIFECYCLE_REHEARSAL_ONLY',
            'executionEvidence': 'NONE_ALL_EVENTS_SYNTHETIC',
            'scenarioSha256': scenario_sha,
            'finalEventChainSha256': self.previous_hash,
            'eventCount': len(self.trace), 'orders': outstanding,
            'positionsOnlySynthetic': {k: {'modeledShares': x['qty'],
                                          'modeledCostUsd': round(x['costUsd'], 6)}
                                      for k, x in self.positions.items() if x['qty']},
            'reservedModeledCapitalUsd': round(self.reserved_usd(), 6),
            'peakReservedModeledCapitalUsd': round(self.peak_reserved_usd, 6),
            'simulatedGrossRealizedUsd': round(self.realized_gross, 6),
            'simulatedAfterStressRealizedUsd': round(self.realized_after_stress, 6),
            'simulatedDailyAfterStressUsd': {k:round(v,6) for k,v in sorted(self.daily_after_stress.items())},
            'grossExposureKnownInRealBroker': False,
            'unresolvedStateAfterDisconnect': self.disconnected,
            'limits': asdict(self.guards),
            'eventTrace': self.trace,
            'liveTradingAuthorized': False, 'tradeEnabled': False,
            'actualOrdersSent': 0, 'confirmedBrokerFills': 0,
            'alphaCertified': False, 'accountProfitKnown': False,
            'warning': ('All ACK/FILL/CANCEL events are SYNTHETIC inputs, NOT inferred from quotes '
                        'and NOT broker acknowledgements. A cancellation request does not release '
                        'reservations. In actual markets late fills, outages, costs, fees and '
                        'reconciliation require a separate broker-confirmed system.')
        }


def replay_scenario(doc, guards=Guardrails()):
    if not isinstance(doc, dict) or doc.get('scenarioType') != 'EXPLICITLY_SYNTHETIC_ONLY':
        raise ValueError('SCENARIO_TYPE_MUST_BE_SYNTHETIC')
    events = doc.get('events')
    if not isinstance(events, list) or not events or len(events) > 100000:
        raise ValueError('INVALID_EVENT_COUNT')
    ledger = PaperLedger(guards)
    for evt in events:
        ledger.apply(evt)
    return ledger.report(hashlib.sha256(canonical(doc)).hexdigest())


def sample_scenario():
    """Engineering failure injection, no connection to market observations."""
    at = '2026-10-14T13:30:'
    steps = [
        ('PLACE', 'b1', {'side':'BUY','symbol':'US.AAOI','qty':4,'limitPrice':100.0}),
        ('ACK', 'b1', {}),
        ('FILL', 'b1', {'qty':2,'price':99.9}),
        ('CANCEL_REQUEST', 'b1', {}),
        ('FILL', 'b1', {'qty':1,'price':99.95}),
        ('CANCEL_ACK', 'b1', {}),
        ('PLACE', 's1', {'side':'SELL','symbol':'US.AAOI','qty':3,'limitPrice':101.0}),
        ('ACK', 's1', {}),
        ('FILL', 's1', {'qty':1,'price':101.0}),
        ('FILL', 's1', {'qty':2,'price':101.1}),
        ('PLACE', 'b2', {'side':'BUY','symbol':'US.NVDA','qty':4,'limitPrice':200.0}),
        ('REJECT', 'b2', {}),
        ('DISCONNECT', None, {})
    ]
    return {'scenarioType': 'EXPLICITLY_SYNTHETIC_ONLY', 'description': 'FICTIONAL engineering state-machine demonstration. NOT real AAOI/NVDA fills.',
            'events': [dict(eventId=f'demo-{i+1}', type=k, at=f'{at}{i:02d}+00:00',
                            source='SYNTHETIC_SCENARIO', **({'orderId':order} if order else {}), **args)
                       for i,(k,order,args) in enumerate(steps)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Synthetic lifecycle failure-injection; no orders ever sent')
    src = parser.add_mutually_exclusive_group()
    src.add_argument('--demo', action='store_true', help='explicitly fictional engineering demonstration')
    src.add_argument('--scenario', type=Path, help='local explicitly synthetic JSON scenario')
    args = parser.parse_args(argv)
    result = {'status': 'NO_GO_NO_EXPLICIT_SYNTHETIC_SCENARIO',
              'warning': 'No scenario given: no fills, orders or PnL invented.',
              'tradeEnabled': False, 'actualOrdersSent': 0, 'confirmedBrokerFills': 0,
              'alphaCertified': False}
    try:
        if args.demo:
            result = replay_scenario(sample_scenario())
            result['inputProvenance'] = 'BUILT_IN_FICTIONAL_FIXTURE'
        elif args.scenario:
            if args.scenario.stat().st_size > 2_000_000:
                raise ValueError('SCENARIO_TOO_LARGE')
            result = replay_scenario(json.loads(args.scenario.read_text('utf-8')))
            result['inputProvenance'] = 'USER_SUPPLIED_SYNTHETIC_SCENARIO_NOT_BROKER'
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        result = {'status': 'NO_GO_INVALID_SYNTHETIC_SCENARIO',
                  'reason': str(exc)[:160], 'tradeEnabled': False,
                  'actualOrdersSent': 0, 'confirmedBrokerFills': 0, 'alphaCertified': False}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    tmp = OUTPUT.with_suffix('.tmp')
    tmp.write_text(payload, encoding='utf-8')
    tmp.replace(OUTPUT)
    print(payload)
    return 0 if result['status'].startswith('SYNTHETIC_') else 2


if __name__ == '__main__':
    raise SystemExit(main())
