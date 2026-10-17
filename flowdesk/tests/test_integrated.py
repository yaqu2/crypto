import json, sys, tempfile, unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from feed_guard import standard_market_day,clock_state,evaluate
from market_scan import normalize_snapshot,summarize, get_universe, run
from runtime import Runtime,DEFAULT_CONFIG

ET=ZoneInfo('America/New_York')
def utc(y,m,d,h,minute=0):return datetime(y,m,d,h,minute,tzinfo=ET).astimezone(timezone.utc)

class GuardTests(unittest.TestCase):
 def setUp(self):
  self.now=utc(2026,10,14,11,0)
  self.start=(self.now-timedelta(minutes=6)).isoformat()
  self.subs={'US.SPY/QUOTE':'成功'}
 def quality(self,quotes=None,**kwargs):
  return evaluate(DEFAULT_CONFIG,self.subs,quotes or {},self.start,now=self.now,connection_ok=True,**kwargs)
 def test_regular(self):
  self.assertTrue(clock_state(self.now)['regular'])
 def test_mlk_holiday(self):
  self.assertFalse(standard_market_day(datetime(2026,1,19).date())[0])
 def test_good_friday(self):
  self.assertFalse(standard_market_day(datetime(2026,4,3).date())[0])
 def test_columbus_still_trades(self):
  self.assertTrue(standard_market_day(datetime(2026,10,12).date())[0])
 def test_early_close(self):
  self.assertEqual(standard_market_day(datetime(2026,11,27).date()),(True,13))
  self.assertFalse(clock_state(utc(2026,11,27,13,20))['regular'])
 def test_fresh(self):
  r=self.quality({'US.SPY':(self.now-timedelta(seconds=7)).isoformat()})
  self.assertEqual(r['status'],'RECENT_RESEARCH_ONLY')
  self.assertFalse(r['liveOrdersEnabled'])
 def test_stale_and_reconnect(self):
  r=self.quality({'US.SPY':(self.now-timedelta(seconds=450)).isoformat()})
  self.assertEqual(r['status'],'NO_GO_STALE')
  self.assertTrue(r['reconnectSuggested'])
 def test_missing_spy(self):
  self.assertEqual(self.quality()['status'],'NO_GO_STALE')
 def test_missing_subscription_no_retry(self):
  self.subs.clear()
  r=self.quality()
  self.assertEqual(r['status'],'NO_GO_BENCHMARK_SUBSCRIPTION')
  self.assertFalse(r['reconnectSuggested'])
 def test_integrity_overrides_fresh(self):
  r=self.quality({'US.SPY':self.now.isoformat()},queue_dropped=1)
  self.assertEqual(r['status'],'NO_GO_INTEGRITY')
 def test_record_failure(self):
  r=self.quality({'US.SPY':self.now.isoformat()},recording={'error':'disk full','dropped':0})
  self.assertEqual(r['status'],'NO_GO_INTEGRITY')
 def test_off_hours_not_false_alarm(self):
  r=evaluate(DEFAULT_CONFIG,self.subs,{},self.start,now=utc(2026,10,14,19),connection_ok=True)
  self.assertEqual(r['status'],'OUTSIDE_REGULAR')
  self.assertFalse(r['reconnectSuggested'])
 def test_warmup(self):
  r=evaluate(DEFAULT_CONFIG,self.subs,{},(self.now-timedelta(seconds=30)).isoformat(),now=self.now,connection_ok=True)
  self.assertEqual(r['status'],'WARMUP')
 def test_older_record_does_not_auto_allow(self):
  r=self.quality({'US.SPY':(self.now-timedelta(minutes=1)).isoformat()})
  self.assertEqual(r['status'],'NO_GO_STALE')

class ScannerTests(unittest.TestCase):
 def test_fresh_valid_source_et(self):
  now=utc(2026,10,14,11)
  r=normalize_snapshot({'code':'US.TEST','update_time':'2026-10-14 10:59:10','last_price':103,'prev_close_price':100},now)
  self.assertTrue(r['usable']);self.assertEqual(r['changePct'],3)
 def test_missing_time_rejected(self):
  now=utc(2026,10,14,11)
  r=normalize_snapshot({'code':'US.TEST','last_price':103,'prev_close_price':100},now)
  self.assertFalse(r['fresh'])
 def test_stale_rejected(self):
  now=utc(2026,10,14,11)
  r=normalize_snapshot({'code':'US.TEST','update_time':'2026-10-13 10:59:10','last_price':103,'prev_close_price':100},now)
  self.assertFalse(r['usable'])
 def test_halted_rejected(self):
  now=utc(2026,10,14,11)
  r=normalize_snapshot({'code':'US.TEST','update_time':'2026-10-14 10:59:10','last_price':103,'prev_close_price':100,'suspension':True},now)
  self.assertFalse(r['usable'])
 def test_partial_universe_never_passes(self):
  now=utc(2026,10,14,11)
  uni=[{'code':f'US.X{i}'} for i in range(2200)]
  snaps=[{'code':f'US.X{i}','usable':True,'changePct':3} for i in range(300)]
  report=summarize(uni,snaps,now,now)
  self.assertEqual(report['dataGate'],'NO_GO_COVERAGE')
 def test_market_coverage(self):
  now=utc(2026,10,14,11)
  uni=[{'code':f'US.X{i}'} for i in range(2200)]
  snaps=[{'code':f'US.X{i}','usable':True,'changePct':3} for i in range(2200)]
  report=summarize(uni,snaps,now,now)
  self.assertEqual(report['dataGate'],'PASS_SNAPSHOT_QUALITY_RESEARCH_ONLY')
  self.assertFalse(report['tradeEnabled'])
 def test_never_accept_bogus_certification(self):
  now=utc(2026,10,14,11)
  report=summarize([{'code':'US.A'}],[{'code':'US.A','usable':True,'changePct':1}],now,now)
  self.assertFalse(report['fullCurrentStockUniverse'])
 def test_mock_sdk_batches_and_writes_hashes(self):
  class Fake:
   def get_stock_basicinfo(self,*a):return (0,[{'code':f'US.X{i}','name':'x','stock_type':'STOCK'} for i in range(403)])
   def get_market_snapshot(self,codes):return (0,[{'code':c,'update_time':'2026-10-14 11:00:00','last_price':10,'prev_close_price':9} for c in codes])
  with tempfile.TemporaryDirectory() as d:
   report=run(Fake(),'US','STOCK',0,d,limit=10)
   self.assertEqual(report['snapshotReceivedCount'],10)
   self.assertEqual(report['stockUniverseCount'],403)
   self.assertEqual(report['dataGate'],'NO_GO_COVERAGE')
   self.assertEqual(json.loads((Path(d)/'latest_report.json').read_text())['snapshotReceivedCount'],10)
 def test_failed_batch_not_silently_ignored(self):
  class Fake:
   def get_stock_basicinfo(self,*a):return (0,[{'code':f'US.X{i}'} for i in range(3)])
   def get_market_snapshot(self,codes):return (1,'unauthorized')
  with tempfile.TemporaryDirectory() as d:
   r=run(Fake(),'US','STOCK',0,d,interval=.8)
   self.assertTrue(r['batchErrors']);self.assertEqual(r['dataGate'],'NO_GO_COVERAGE')

class RuntimeTests(unittest.TestCase):
 def test_dashboard_feed_quality_present(self):
  rt=Runtime(DEFAULT_CONFIG,recorder=None)
  state=json.loads(rt.published)
  self.assertIn('feedQuality',state)
  self.assertFalse(state['feedQuality']['liveOrdersEnabled'])
 def test_reconnect_cap(self):
  rt=Runtime(DEFAULT_CONFIG,recorder=None)
  self.assertTrue(rt.may_reconnect())
  self.assertFalse(rt.may_reconnect())
 def test_reset_clears_quote(self):
  rt=Runtime(DEFAULT_CONFIG,recorder=None)
  rt.good_quotes['US.SPY']=datetime.now(timezone.utc).isoformat()
  rt.good_books['US.SPY']=datetime.now(timezone.utc).isoformat()
  rt.reset('test')
  self.assertEqual(rt.good_quotes,{})
  self.assertEqual(rt.good_books,{})

if __name__=='__main__':unittest.main()

from subscription_guard import verify
class SubscriptionTests(unittest.TestCase):
 def test_subscription_actual(self):
  x=verify({'US.SPY/QUOTE':'成功','US.AAOI/ORDER_BOOK':'成功'},
           {'sub_list':{'QUOTE':['US.SPY'],'ORDER_BOOK':[]},'remain':10})
  self.assertEqual(x['US.SPY/QUOTE'],'成功')
  self.assertEqual(x['US.AAOI/ORDER_BOOK'],'失败')
 def test_subscription_state_missing_fail_closed(self):
  with self.assertRaises(ValueError):verify({'US.SPY/QUOTE':'成功'},{'remain':10})
