import json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sector_watchlist_proposal import propose

class ETFSafeWatchlistProposal(unittest.TestCase):
 def setup(self):
  return {'symbols':['US.SPY','US.QQQ','US.AAOI'],'deepSymbols':['US.SPY','US.AAOI'],
          'benchmark':'US.SPY','peers':{}}
 def test_sector_etfs_added(self):
  result=propose(self.setup())
  self.assertTrue(all(s in result['proposedConfig']['symbols'] for s in ['US.SMH','US.SOXX','US.XSD','US.XLK']))
 def test_not_activated(self):
  result=propose(self.setup())
  self.assertFalse(result['liveSubscriptionsChanged']);self.assertEqual(result['status'],'REVIEW_ONLY_NOT_ACTIVATED')
 def test_deep_permissions_not_invented(self):
  result=propose(self.setup())
  self.assertEqual(result['proposedConfig']['deepSymbols'],['US.SPY','US.AAOI'])
 def test_preserves_original(self):
  original=self.setup();before=json.dumps(original,sort_keys=True)
  propose(original);self.assertEqual(json.dumps(original,sort_keys=True),before)
 def test_invalid_initial_config(self):
  bad=self.setup();bad['deepSymbols']=['US.NVDA']
  with self.assertRaises(ValueError):propose(bad)
