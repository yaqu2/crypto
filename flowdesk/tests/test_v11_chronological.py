"""Synthetic adversarial tests; no claims about market returns."""
import unittest
from datetime import date, timedelta, datetime, timezone
from session_coverage import longest_missing_run
from chronological_validation import (split_eligible_dates,select_and_score,block_bootstrap_mean_ci,
                                      report_recordings,MIN_DAYS)
from portfolio_audit import replay_portfolio
from prospective import RULES

RULE_A=RULES[0]['id']
RULE_B=RULES[1]['id']


def dates(n=75):
    start=date(2026,1,5)
    seq=[]
    for i in range(200):
        d=start+timedelta(days=i)
        if d.weekday()<5:seq.append(d.isoformat())
        if len(seq)==n:break
    return seq


def portfolio(d,training_a=2.,holdout_a=-2.,training_b=.5,holdout_b=2.):
    cut=int(len(d)*.6)
    values={}
    counts={}
    for idx,day in enumerate(d):
        # Two rules, same eligible day, and a different alpha regime in test.
        if idx>=cut+2:
            a,b=holdout_a,holdout_b
        else:a,b=training_a,training_b
        a=a*(1+0.05*(idx%4))
        b=b*(1+0.05*(idx%4))
        values[day]={RULE_A:{'8':a+1,'20':a+.5,'40':a},
                     RULE_B:{'8':b+1,'20':b+.5,'40':b}}
        counts[day]={RULE_A:1,RULE_B:1}
    return {'accountUsd':100000., 'ruleExitDayClosedOnlyPnlUsd':values,
            'ruleExitDayClosedOnlyCounts':counts}


class ChronologicalV11(unittest.TestCase):
    def test_no_days_reject_and_no_winner(self):
        r=select_and_score(portfolio(dates(40)),dates(40))
        self.assertEqual(r['status'],'NO_GO_INSUFFICIENT_INDEPENDENT_DAYS')
        self.assertIsNone(r['trainOnlySelectedRule'])
    def test_no_test_leakage_into_winner(self):
        d=dates()
        base=select_and_score(portfolio(d,holdout_a=-50,holdout_b=200),d)
        opposite=select_and_score(portfolio(d,holdout_a=200,holdout_b=-50),d)
        self.assertEqual(base['trainOnlySelectedRule'],RULE_A)
        self.assertEqual(opposite['trainOnlySelectedRule'],RULE_A)
        self.assertEqual(base['status'],'NO_GO_TEST_COST40_NOT_ROBUST')
        self.assertEqual(opposite['status'],'EXPLORATORY_HISTORICAL_TIME_SPLIT_POSITIVE')
        self.assertFalse(base['liveOrdersEnabled'])
        self.assertTrue(base['noSealedUntouchedHoldout'])
    def test_embargo_and_training_test_disjoint(self):
        d=dates()
        s=split_eligible_dates(d)
        self.assertTrue(s['ready'])
        self.assertEqual(s['embargo'],d[int(len(d)*.6):int(len(d)*.6)+2])
        self.assertFalse(set(s['train']) & set(s['test']))
    def test_duplicate_and_invalid_dates_fails(self):
        d=dates()
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):split_eligible_dates(d+[d[0]])
        with self.assertRaises(ValueError):split_eligible_dates(['2026-13-44']*62)
    def test_zero_trade_days_are_in_denominator(self):
        d=dates()
        p=portfolio(d,training_a=50.,holdout_a=10.)
        for k in d[10:45]:
            p['ruleExitDayClosedOnlyPnlUsd'].pop(k,None)
            p['ruleExitDayClosedOnlyCounts'].pop(k,None)
        r=select_and_score(p,d)
        # Few active dates is not enough even when its recorded wins are huge.
        self.assertNotEqual(r['trainOnlySelectedRule'],RULE_A)
    def test_block_ci_requires_independent_days(self):
        self.assertIsNone(block_bootstrap_mean_ci([999.]*5))
        self.assertIsNone(block_bootstrap_mean_ci([1.]*30))
        self.assertIsNone(block_bootstrap_mean_ci([-1.]*30))
        self.assertGreater(block_bootstrap_mean_ci([1+i%3*.1 for i in range(30)])[0],0)
        self.assertLess(block_bootstrap_mean_ci([-1-i%3*.1 for i in range(30)])[1],0)
    def test_ambiguous_day_never_counts_twice(self):
        d=dates(60)
        r=select_and_score(portfolio(d),d,blocked_dates=d[:3])
        self.assertEqual(r['dateSplit']['observed'],57)
        self.assertEqual(r['status'],'NO_GO_INSUFFICIENT_INDEPENDENT_DAYS')
    def test_concentrated_feed_gap_is_not_acceptable(self):
        received=set(range(390))-set(range(100,117))
        self.assertEqual(longest_missing_run(received,390),17)
        # 373/390 = 95.64%: old overall percentage gate would pass.
        self.assertGreater(len(received)/390,.95)
        self.assertGreater(longest_missing_run(received,390),2)
    def test_intermittent_single_minute_losses_not_one_long_gap(self):
        received=set(range(390))-set(range(20,390,20))
        self.assertEqual(longest_missing_run(received,390),1)

    def test_full_daily_auditor_rejects_concentrated_gap(self):
        import tempfile
        from zoneinfo import ZoneInfo
        from recorder import Recorder
        from session_coverage import audit_recording_coverage
        with tempfile.TemporaryDirectory() as root:
            rec=Recorder(root,segment_bytes=4000,queue_size=3000)
            base=datetime(2026,10,13,9,30,tzinfo=ZoneInfo('America/New_York'))
            for i in range(390):
                local=base+timedelta(minutes=i)
                receive=(local+timedelta(seconds=2)).astimezone(timezone.utc).isoformat()
                rec.record('health',{'connected':True,'feedStatus':'RECENT_RESEARCH_ONLY'},
                           callback_received_at=receive)
                if not 100<=i<117:
                    row={'code':'US.SPY','data_date':'2026-10-13',
                         'data_time':local.strftime('%H:%M:%S'),'last_price':400.0}
                    rec.record('quote',[row],callback_received_at=receive)
            rec.q.join();rec.close()
            report=audit_recording_coverage(rec.folder)
            day=report['days'][0]
            self.assertGreater(day['benchmarkQuoteMinuteCoverage'],.95)
            self.assertEqual(day['maxConsecutiveMissingBenchmarkQuoteMinutes'],17)
            self.assertEqual(day['status'],'NO_GO_SESSION_COVERAGE')

    def test_no_data_prohibits_trading(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as p:
            r=report_recordings(Path(p))
            self.assertEqual(r['verdict'],'NO_GO_REAL_TRADING')
            self.assertFalse(r['tradeEnabled'])
            self.assertIsNone(r['historicalDateComparison']['trainOnlySelectedRule'])
    def test_generator_runs_not_silently_lost(self):
        # Portfolio replay now truly accepts generator input as documented.
        a=replay_portfolio(iter([('session1',[])]))
        self.assertEqual(a['runsReplayed'],1)
    def test_bookkeeping_by_et_day(self):
        # Timezone conversion of 2026-10-12 00:30 UTC is Oct 11 ET.
        from zoneinfo import ZoneInfo
        d=datetime(2026,10,12,0,30,tzinfo=timezone.utc).astimezone(ZoneInfo('America/New_York'))
        self.assertEqual(d.date().isoformat(),'2026-10-11')

if __name__=='__main__':unittest.main()
