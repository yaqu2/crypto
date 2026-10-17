"""All synthetic, designed to falsify false profitability claims."""
import unittest
from datetime import datetime,timedelta,timezone
from portfolio_audit import replay_portfolio
from prospective import RULES

ORIGIN=datetime(2026,10,12,15,0,tzinfo=timezone.utc)
def at(s):return (ORIGIN+timedelta(seconds=s)).isoformat()
R=RULES[0]['id'];R2=RULES[1]['id']

def entry(ident,sec=0,code='US.AAA',rule=R,short=False,qty=10):
    return {'type':'PAPER_ENTRY','id':ident,'code':code,'rule':rule,'at':at(sec),
            'price':99.99 if short else 100.01,'bid':99.99,'ask':100.01,
            'shadowShares':qty,'entryL1Size':1000}

def exit_event(ident,sec=60,code='US.AAA',rule=R,short=False,exitpx=100.09,qty=10):
    start=99.99 if short else 100.01
    side=-1 if short else 1
    gross=(exitpx/start-1)*10000*side
    return {'type':'PAPER_EXIT','id':ident,'code':code,'rule':rule,'entryAt':at(0),'exitAt':at(sec),
            'direction':side,'shadowShares':qty,'entryPrice':start,'exitPrice':exitpx,
            'quotedCrossingBps':round(gross,6),'exitL1Size':1000,
            'exitBid':exitpx if not short else exitpx-.02,'exitAsk':exitpx if short else exitpx+.02,
            'netQuoteModelBps':{str(c):round(gross-c,6) for c in (8,20,40)}}

def audit(runs,settings=None):
    # Fixtures include the immutable causal signal preceding each paper entry.
    prepared=[]
    for run,events in runs:
        origin=[]
        for e in events:
            if e['type']=='PAPER_ENTRY':
                side=-1 if abs(e['price']-e['bid'])<1e-7 else 1
                origin.append({'type':'SIGNAL','id':e['id'],'code':e['code'],
                    'rule':e['rule'],'direction':side,'at':at(int((datetime.fromisoformat(e['at'])-ORIGIN).total_seconds())-3)})
            origin.append(e)
        prepared.append((run,origin))
    return replay_portfolio(prepared,settings)

class PortfolioV08(unittest.TestCase):
    def test_long_cash_pnl_recomputed(self):
        r=audit([('one',[entry('a'),exit_event('a')])])
        self.assertEqual(r['closedLongModelExits'],1)
        expected=10*(100.09-100.01)-1000.1*40/10000
        self.assertAlmostEqual(r['recomputedClosedOnlyModelPnlUsd']['40'],expected,places=3)
        self.assertTrue(r['diagnosticComplete'])
        self.assertFalse(r['claimableAsRealPortfolioReturn'])
    def test_short_excluded_without_borrow(self):
        r=audit([('one',[entry('s',short=True),exit_event('s',short=True,exitpx=99)])])
        self.assertEqual(r['admittedLongModelEntries'],0)
        self.assertEqual(r['closedLongModelExits'],0)
        self.assertEqual(r['blockedOrExcluded']['blocked_BORROW_NOT_VERIFIED'],1)
    def test_same_ticker_rule_collision(self):
        events=[entry('two',rule=R2),entry('one',rule=R),exit_event('one',rule=R),exit_event('two',rule=R2)]
        r=audit([('same',events)])
        self.assertEqual(r['closedLongModelExits'],1)
        self.assertEqual(r['blockedOrExcluded']['blocked_OVERLAPPING_SYMBOL_RULE'],1)
    def test_two_tickers_compete_for_cash_budget(self):
        r=audit([('one',[entry('one'),entry('two',code='US.BBB'),
             exit_event('one'),exit_event('two',code='US.BBB')])],
             {'maxGrossUsd':1500.0})
        self.assertEqual(r['closedLongModelExits'],1)
        self.assertEqual(r['blockedOrExcluded']['blocked_GROSS_CAPITAL_EXHAUSTED'],1)
    def test_censor_is_unknown_not_zero_pnl(self):
        r=audit([('one',[entry('a'),{'type':'CENSORED','id':'a','rule':R,'code':'US.AAA','at':at(90)}])])
        self.assertEqual(r['censoredAdmittedPositions'],1)
        self.assertFalse(r['diagnosticComplete'])
        self.assertEqual(r['closedLongModelExits'],0)
    def test_unresolved_entry_stays_unknown(self):
        r=audit([('one',[entry('a')])])
        self.assertEqual(r['stillOpenUnresolved'],1)
        self.assertFalse(r['diagnosticComplete'])
    def test_corrupted_pnl_rejected(self):
        ex=exit_event('a');ex['netQuoteModelBps']['40']=999999
        with self.assertRaisesRegex(ValueError,'COST_PNL_DISAGREEMENT'):
            audit([('one',[entry('a'),ex])])
    def test_invalid_exit_without_entry_is_error(self):
        with self.assertRaisesRegex(ValueError,'ORPHAN_PAPER_EXIT'):
            audit([('one',[exit_event('x')])])
    def test_conflicted_symbol_not_transferred_between_runs(self):
        r=audit([('one',[entry('a'),exit_event('a')]),
                            ('two',[entry('a',sec=180),{**exit_event('a',sec=240),'entryAt':at(180)}])])
        self.assertEqual(r['closedLongModelExits'],2)
    def test_fabricated_entry_without_signal_never_makes_pnl(self):
        r=replay_portfolio([('one',[entry('fabricated'),exit_event('fabricated')])])
        self.assertEqual(r['closedLongModelExits'],0)
        self.assertEqual(r['blockedOrExcluded']['blocked_MISSING_SOURCE_SIGNAL'],1)

    def test_short_delay_never_enters(self):
        e=entry('late',sec=30)
        signal={'type':'SIGNAL','id':'late','code':'US.AAA','rule':R,
                'direction':1,'at':at(0)}
        r=replay_portfolio([('one',[signal,e])])
        self.assertEqual(r['blockedOrExcluded']['blocked_SIGNAL_PROVENANCE_OR_CAUSAL_DELAY_INVALID'],1)

    def test_not_enough_level_one_depth_never_enters(self):
        e=entry('thin');e['entryL1Size']=15
        r=audit([('one',[e,exit_event('thin')])])
        self.assertEqual(r['blockedOrExcluded']['blocked_UNVERIFIED_ENTRY_DEPTH'],1)
        self.assertEqual(r['closedLongModelExits'],0)

    def test_exit_wrong_side_cannot_manufacture_profit(self):
        e=exit_event('a');e['exitBid']=200.0
        with self.assertRaisesRegex(ValueError,'EXIT_CROSS_SIDE_MISMATCH'):
            audit([('one',[entry('a'),e])])

    def test_invalid_borrow_override_refused(self):
        with self.assertRaisesRegex(ValueError,'SHORT_BORROW'):
            audit([('one',[])],{'allowShort':True})
    def test_no_data_no_approval(self):
        r=audit([])
        self.assertFalse(r['diagnosticComplete'])
        self.assertFalse(r['claimableAsRealPortfolioReturn'])

if __name__=='__main__':unittest.main()
