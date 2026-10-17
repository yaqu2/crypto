import sys,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from multiscale_lab import stamp,usable_context,normalized_book,pair_ofi,minute_observations,path_targets,broad_regime

def book(bid=100,bsize=100,ask=101,asize=100,code='US.AAOI'):
 return {'code':code,'Bid':[[bid,bsize,1,{}]],'Ask':[[ask,asize,1,{}]]}

def evt(n,kind,data,at=None,origin='push',run='r'):
 return {'seq':n,'kind':kind,'data':data,'origin':origin,'runId':run,
         'receivedAt':at or f'2026-10-15T14:30:{n:02d}+00:00'}

class TimestampTests(unittest.TestCase):
 def test_tz_required(self):
  with self.assertRaises(ValueError):stamp('2026-10-15T14:30:00')
 def test_z_works(self):self.assertEqual(stamp('2026-10-15T14:30:00Z').hour,14)
 def test_publish_not_received_rejected(self):
  x=[{'type':'news','key':'CPI','value':4,'provider':'source','published_at':'2026-10-15T14:30:00Z',
     'first_seen_at':'2026-10-15T14:30:01Z','received_at':'2026-10-15T14:31:00Z'}]
  result=usable_context(x,'2026-10-15T14:30:30Z')
  self.assertEqual(result['known'],[]);self.assertEqual(result['discarded']['NOT_KNOWN_YET'],1)
 def test_true_asof(self):
  x=[{'type':'macro','key':'US10Y','value':4.3,'provider':'fred','published_at':'2026-10-15T14:30:00Z',
     'first_seen_at':'2026-10-15T14:30:01Z','received_at':'2026-10-15T14:30:02Z'}]
  self.assertEqual(usable_context(x,'2026-10-15T14:30:03Z')['known'][0]['value'],4.3)
 def test_future_revision_excluded(self):
  base={'type':'macro','key':'rate','provider':'x','published_at':'2026-10-15T14:30:00Z',
    'first_seen_at':'2026-10-15T14:30:01Z','received_at':'2026-10-15T14:30:02Z','value':1,'revision':0}
  nxt={**base,'value':9,'revision':1,'first_seen_at':'2026-10-15T14:32:00Z','received_at':'2026-10-15T14:32:01Z'}
  self.assertEqual(usable_context([nxt,base],'2026-10-15T14:30:03Z')['known'][0]['value'],1)
 def test_late_publish_invalid(self):
  x=[{'type':'news','key':'item','provider':'x','published_at':'2026-10-15T14:31:00Z',
     'first_seen_at':'2026-10-15T14:30:00Z','received_at':'2026-10-15T14:30:01Z','value':1}]
  self.assertEqual(usable_context(x,'2026-10-15T14:33:00Z')['discarded']['INVALID_CONTEXT'],1)
 def test_stale_context(self):
  x=[{'type':'macro','key':'r','provider':'x','published_at':'2026-10-14T14:30:00Z',
     'first_seen_at':'2026-10-14T14:30:00Z','received_at':'2026-10-14T14:30:00Z','value':1}]
  self.assertEqual(len(usable_context(x,'2026-10-15T14:30:00Z',allowed_lag_seconds=10)['known']),0)
 def test_naive_context_rejected(self):
  x=[{'type':'news','key':'n','provider':'x','published_at':'2026-10-15 12:00:00',
     'first_seen_at':'2026-10-15T12:01:00Z','received_at':'2026-10-15T12:01:00Z','value':1}]
  self.assertEqual(usable_context(x,'2026-10-15T12:02:00Z')['known'],[])

class BookTests(unittest.TestCase):
 def test_crossed_rejected(self):self.assertIsNone(normalized_book(book(bid=102,ask=101)))
 def test_empty_rejected(self):self.assertIsNone(normalized_book({'code':'US.AAOI','Bid':[],'Ask':[]}))
 def test_up_bid(self):
  a=normalized_book(book());b=normalized_book(book(bid=100.01,bsize=150))
  self.assertEqual(pair_ofi(a,b)['ofi_top_proxy'],150)
 def test_same_bid_increment(self):
  a=normalized_book(book());b=normalized_book(book(bsize=150))
  self.assertEqual(pair_ofi(a,b)['bid_size_increase_proxy'],50)
 def test_not_true_orders(self):
  x=pair_ofi(normalized_book(book()),normalized_book(book(bsize=180)))
  self.assertFalse(x['true_order_additions_known'])
 def test_wrong_symbol(self):self.assertIsNone(pair_ofi(normalized_book(book()),normalized_book(book(code='US.COH R'))))
 def test_snapshot_not_order_messages(self):
  res=minute_observations([evt(1,'book',book()),evt(2,'book',book(bsize=130))]);b=res['observations'][0]
  self.assertEqual(b['displayedBidIncreaseProxy'],30)
  self.assertFalse(b['exchangeOrderEventsObserved'])
  self.assertFalse(b['executableSignal'])
 def test_drop_seed(self):
  res=minute_observations([evt(1,'book',book(),origin='seed')]);self.assertEqual(res['observations'],[])
 def test_drop_cached_ticker(self):
  t={'code':'US.AAOI','price':100,'volume':10,'ticker_direction':'BUY','push_data_type':'CACHE'}
  res=minute_observations([evt(1,'ticker',[t])]);self.assertEqual(res['excluded']['CACHED_TICK'],1)
 def test_direction_and_unknown_volume(self):
  a={'code':'US.AAOI','price':100,'volume':10,'ticker_direction':'BUY'}
  b={'code':'US.AAOI','price':100,'volume':5,'ticker_direction':'SELL'}
  c={'code':'US.AAOI','price':100,'volume':20,'ticker_direction':'NEUTRAL'}
  obs=minute_observations([evt(1,'ticker',[a]),evt(2,'ticker',[b]),evt(3,'ticker',[c])])['observations'][0]
  self.assertEqual(obs['signedTradeVolume'],5);self.assertEqual(obs['unknownDirectionVolume'],20)
 def test_duplicate_tick(self):
  a={'code':'US.AAOI','price':100,'volume':10,'ticker_direction':'BUY','sequence':'abc'}
  res=minute_observations([evt(1,'ticker',[a]),evt(2,'ticker',[a])])
  self.assertEqual(res['observations'][0]['ticks'],1)
  self.assertEqual(res['excluded']['DUPLICATE_TICK'],1)
 def test_book_gap_flag(self):
  obs=minute_observations([evt(1,'book',book(),at='2026-10-15T14:30:01Z'),
                           evt(2,'book',book(bsize=150),at='2026-10-15T14:30:59Z')])['observations'][0]
  self.assertEqual(obs['status'],'GAP_OR_UNVERIFIED_L2_PROVENANCE')
  self.assertEqual(obs['ofiTopProxy'],0)
 def test_kline_must_be_completed(self):
  res=minute_observations([evt(1,'kline',[{'code':'US.AAOI','close':123}])]);
  self.assertEqual(res['excluded']['BAD_MINUTE_BAR_TIMESTAMP'],1)
 def test_nonmonotonic_recorder(self):
  res=minute_observations([evt(2,'book',book()),evt(1,'book',book(bsize=150))]);
  self.assertEqual(res['excluded']['NONMONOTONIC_RECORDER'],1)

class PathTests(unittest.TestCase):
 def rows(self):
  s=datetime(2026,10,1,tzinfo=timezone.utc)
  return [{'day':(s+timedelta(days=i)).date().isoformat(),'close':100+i,'close_verified':True,'symbol':'US.AAOI'} for i in range(7)]
 def test_multihorizon_matured(self):
  r=path_targets(self.rows());self.assertEqual(len(r['labels']),6+4+2)
 def test_future_pending_kept(self):
  r=path_targets(self.rows());self.assertEqual(len(r['pending']),1+3+5)
 def test_labels_only_past_entry_features(self):
  r=path_targets(self.rows());self.assertTrue(all(not x['futurePricesUsedInFeatures'] for x in r['labels']))
 def test_intraday_unknown(self):self.assertTrue(all(not x['intraDayPathKnown'] for x in path_targets(self.rows())['labels']))
 def test_missing_close_refuse(self):
  s=self.rows();s[2]['close_verified']=False
  r=path_targets(s);self.assertGreater(r['excluded'].get('INCOMPLETE_FUTURE_CLOSE',0),0)
 def test_dates_unsorted(self):
  s=self.rows();s[0],s[1]=s[1],s[0]
  with self.assertRaises(ValueError):path_targets(s)
 def test_horizon_bad(self):
  with self.assertRaises(ValueError):path_targets(self.rows(),horizons=(0,))
 def test_empty_has_no_alpha(self):self.assertEqual(path_targets([])['status'],'NO_GO_NO_MATURED_LABELS')
 def test_regime_coverage_gate(self):self.assertEqual(broad_regime(self.rows())['regime'],None)

if __name__=='__main__':unittest.main()

class MinuteBarTests(unittest.TestCase):
 def bar(self):return {'code':'US.AAOI','time_key':'2026-10-15 09:30:00',
                      'open':100,'high':101,'low':99,'close':100.5,'volume':120}
 def test_cannot_use_before_minute_closes(self):
  result=minute_observations([evt(1,'kline',[self.bar()],at='2026-10-15T13:30:30Z')])
  self.assertEqual(result['completedMinuteBars'],[])
  self.assertEqual(result['excluded']['PARTIAL_BAR_NOT_USED'],1)
 def test_first_after_bar_end(self):
  result=minute_observations([evt(1,'kline',[self.bar()],at='2026-10-15T13:31:02Z')])
  self.assertEqual(len(result['completedMinuteBars']),1)
  self.assertEqual(result['completedMinuteBars'][0]['barEndUtc'],'2026-10-15T13:31:00+00:00')
 def test_later_bar_not_backdated(self):
  rows=[evt(1,'kline',[self.bar()],at='2026-10-15T13:31:02Z'),
        evt(2,'kline',[{**self.bar(),'close':100.6}],at='2026-10-15T13:31:04Z')]
  result=minute_observations(rows)
  self.assertEqual(result['completedMinuteBars'][0]['close'],100.5)
  self.assertEqual(result['excluded']['LATER_BAR_REVISION_NOT_BACKFILLED'],1)
 def test_premarket_not_rth(self):
  result=minute_observations([evt(1,'kline',[{**self.bar(),'time_key':'2026-10-15 04:30:00'}],at='2026-10-15T08:31:02Z')])
  self.assertEqual(result['completedMinuteBars'],[])
 def test_late_bar_not_closed_label(self):
  res=minute_observations([evt(1,'kline',[self.bar()],at='2026-10-15T14:31:02Z')]);self.assertEqual(len(res['completedMinuteBars']),0)
 def test_invalid_ohlc(self):
  res=minute_observations([evt(1,'kline',[{**self.bar(),'high':99}],at='2026-10-15T13:31:02Z')]);self.assertEqual(len(res['completedMinuteBars']),0)
 def test_callback_lag_rejected(self):
  row=evt(1,'book',book(),at='2026-10-15T13:31:02Z');row['callbackReceivedAt']='2026-10-15T13:30:50Z'
  res=minute_observations([row]);self.assertEqual(res['excluded']['LOCAL_RECORDER_LAG'],1)
from multiscale_lab import intraday_path_targets
class IntradayPathTests(unittest.TestCase):
 def rows(self):
  t=datetime(2026,10,15,13,30,tzinfo=timezone.utc)
  return [{'symbol':'US.AAOI','startUtc':(t+timedelta(minutes=i)).isoformat(),
           'barEndUtc':(t+timedelta(minutes=i+1)).isoformat(),
           'firstAvailableUtc':(t+timedelta(minutes=i+1)).isoformat(),'open':100,'high':101,
           'low':99,'close':100,'finalBarByTimeHeuristicOnly':True} for i in range(20)]
 def test_minute1_and5_labels(self):
  report=intraday_path_targets(self.rows(),horizons=(1,5))
  self.assertEqual(len(report['labels']),19+15)
 def test_missing_minute_fails(self):
  rows=self.rows();del rows[4]
  x=intraday_path_targets(rows,horizons=(5,))
  self.assertGreater(x['discarded']['MISSING_MINUTE_BAR'],0)
 def test_future_unmatured(self):
  x=intraday_path_targets(self.rows(),horizons=(5,))
  self.assertEqual(len(x['pending']),5)
 def test_both_barriers_order_unknown(self):
  rows=self.rows();rows[1].update(high=104,low=95)
  x=intraday_path_targets(rows,horizons=(1,),barrier_pct=.02)
  r=[a for a in x['labels'] if a['entryMinuteUtc']==rows[0]['startUtc']][0]
  self.assertEqual(r['barrierHitOrder'],'UNKNOWN_BOTH_DURING_WINDOW')
 def test_actual_fill_not_claimed(self):
  x=intraday_path_targets(self.rows(),horizons=(1,))
  self.assertTrue(all(not row['fillAssumed'] for row in x['labels']))

class NoIntraminuteLookaheadTests(unittest.TestCase):
 def test_feature_arrives_2s_late_skip_partially_seen_minute(self):
  rows=IntradayPathTests().rows()
  rows[0]['firstAvailableUtc']='2026-10-15T13:31:02Z'
  report=intraday_path_targets(rows,horizons=(1,))
  first=[r for r in report['labels'] if r['entryMinuteUtc']==rows[0]['startUtc']][0]
  self.assertEqual(first['firstFutureFullMinuteUtc'],rows[2]['startUtc'])
 def test_no_receipt_time_no_label(self):
  rows=IntradayPathTests().rows();del rows[0]['firstAvailableUtc']
  report=intraday_path_targets(rows,horizons=(1,))
  self.assertFalse(any(r['entryMinuteUtc']==rows[0]['startUtc'] for r in report['labels']))
  self.assertEqual(report['discarded']['NO_ENTRY_RECEIPT_TIME'],1)
