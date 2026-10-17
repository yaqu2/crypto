"""Contract tests for licensed news ingest. All HTTP fixtures synthetic, never real market observations."""
import json,os,sys,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import news_feed as nf
from news_context import asof_news,minute_news_features

NOW='2026-10-16T03:00:00Z'
def ben(i=1, title='AAOI optical transceiver makers face inflation pressure'):
    return {'benzinga_id':i,'published':'2026-10-16T02:59:50Z','title':title,'url':'https://example.com/news/1','tickers':['AAOI','LITE'],'teaser':'Synthetic fixture, not live news'}
def ref(i='ref1'):
    return {'id':i,'published_utc':'2026-10-16T02:59:50Z','title':'AI chip report','article_url':'https://example.com/news/ref1','tickers':['NVDA'],'description':'Synthetic'}
def finn(i=5):
    return {'id':i,'datetime':1792123190,'headline':'Fed rate and memory chip news','url':'https://example.com/news/f','related':'MU','summary':'Synthetic'}

class NormalizationTests(unittest.TestCase):
 def test_benzinga(self):
  row=nf.normalize('benzinga',ben(),NOW)
  self.assertEqual(row['value']['tickers'],['AAOI','LITE']);self.assertIn('OPTICAL_NETWORK',row['value']['topics'])
  self.assertIn('MACRO_INFLATION',row['value']['topics'])
  self.assertEqual(row['first_seen_at'],row['received_at'])
  self.assertEqual(row['firstSeenMeaning'],'LOCAL_FIRST_OBSERVED_NOT_VENDOR_DISCOVERY')
 def test_massive(self):self.assertEqual(nf.normalize('massive',ref(),NOW)['provider'],'massive_reference_v2')
 def test_finnhub(self):
  v=finn();v['datetime']=int(datetime(2026,10,16,2,59,50,tzinfo=timezone.utc).timestamp())
  self.assertEqual(nf.normalize('finnhub',v,NOW)['value']['tickers'],['MU'])
 def test_naive_published_fails(self):
  r=ben();r['published']='2026-10-16T02:59:50'
  with self.assertRaises(ValueError):nf.normalize('benzinga',r,NOW)
 def test_future_publication_fails(self):
  r=ben();r['published']='2026-10-16T04:00:00Z'
  with self.assertRaises(ValueError):nf.normalize('benzinga',r,NOW)
 def test_no_title(self):
  r=ben();r['title']=''
  with self.assertRaises(ValueError):nf.normalize('benzinga',r,NOW)
 def test_unsafe_article_url(self):
  r=ben();r['url']='javascript:alert(1)'
  with self.assertRaises(ValueError):nf.normalize('benzinga',r,NOW)
 def test_unknown_tickers(self):
  r=ben();r['tickers']=['aaoi','US.LITE','<script>','AAOI']
  self.assertEqual(nf.normalize('benzinga',r,NOW)['value']['tickers'],['AAOI','US.LITE'])
 def test_unknown_provider(self):
  with self.assertRaises(ValueError):nf.normalize('nope',ben(),NOW)
 def test_numeric_timestamp(self):
  self.assertEqual(nf.clock(1792123190).tzinfo,timezone.utc)
 def test_excessive_age(self):
  r=ben();r['published']='1999-01-01T00:00:00Z'
  with self.assertRaises(ValueError):nf.normalize('benzinga',r,NOW)

class JournalTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name)/'data'/'news.jsonl'
 def tearDown(self):self.tmp.cleanup()
 def _row(self,n=1,title='AAOI optical transceiver makers face inflation pressure',observed=NOW):
  return nf.normalize('benzinga',ben(n,title),observed)
 def test_first_append(self):
  r=nf.append_news([self._row()],self.p);self.assertEqual(r['added'],1)
  self.assertEqual(len(nf.read_log(self.p)),1)
 def test_same_article_dedup(self):
  nf.append_news([self._row()],self.p)
  r=nf.append_news([self._row(observed='2026-10-16T03:01:00Z')],self.p)
  self.assertEqual(r['duplicates'],1)
  self.assertEqual(len(nf.read_log(self.p)),1)
 def test_revision_appended(self):
  nf.append_news([self._row()],self.p)
  nf.append_news([self._row(title='AAOI optical transceiver update',observed='2026-10-16T03:02:00Z')],self.p)
  data=nf.read_log(self.p)
  self.assertEqual([x['revision'] for x in data],[0,1]);self.assertEqual(data[0]['value']['title'],ben()['title'])
 def test_replay_old_after_revision_no_new(self):
  nf.append_news([self._row(),self._row(title='Updated')],self.p)
  self.assertEqual(nf.append_news([self._row()],self.p)['added'],0)
 def test_tamper_refused(self):
  nf.append_news([self._row()],self.p)
  d=self.p.read_bytes().replace(b'AAOI',b'BBBB');self.p.write_bytes(d)
  with self.assertRaisesRegex(ValueError,'NEWS_HASH_CHAIN_FAILED'):nf.read_log(self.p)
  with self.assertRaises(ValueError):nf.append_news([self._row(2)],self.p)
 def test_truncated_refused(self):
  nf.append_news([self._row()],self.p)
  self.p.write_bytes(self.p.read_bytes()[:-6])
  with self.assertRaises(ValueError):nf.append_news([self._row(2)],self.p)
 def test_symlink_refused(self):
  target=Path(self.tmp.name)/'target';target.write_text('secret')
  self.p.parent.mkdir(parents=True,exist_ok=True);self.p.symlink_to(target)
  with self.assertRaisesRegex(ValueError,'SYMLINK'):nf.append_news([self._row()],self.p)
 def test_frozen_asof_excludes_later_revision(self):
  nf.append_news([self._row()],self.p)
  nf.append_news([self._row(title='Updated inflation risk',observed='2026-10-16T03:02:00Z')],self.p)
  x=asof_news(self.p,asof='2026-10-16T03:00:30Z')
  self.assertEqual(x['matchedArticles'],1);self.assertEqual(x['recentArticles'][0]['revision'],0)
 def test_no_news_asof_before_first_seen(self):
  nf.append_news([self._row()],self.p)
  x=asof_news(self.p,asof='2026-10-16T02:59:55Z')
  self.assertEqual(x['matchedArticles'],0)
 def test_sector_mentions(self):
  nf.append_news([self._row()],self.p)
  x=asof_news(self.p,asof='2026-10-16T03:00:30Z')
  self.assertEqual(x['bySectorMentions']['OPTICAL'],1)
  self.assertFalse(x['tradeSignal']);self.assertFalse(x['profitCertified'])
 def test_out_of_window(self):
  nf.append_news([self._row()],self.p)
  x=asof_news(self.p,asof='2026-10-16T05:01:00Z',max_age_minutes=120)
  self.assertEqual(x['matchedArticles'],0)


class MinuteJoinTests(unittest.TestCase):
 def setUp(self):
  self.r=nf.normalize('benzinga',ben(),NOW)
  self.r['revision']=0
 def test_future_receipt_never_in_past_minute(self):
  rows=[{'symbol':'US.AAOI','minuteUtc':'2026-10-16T02:58:00Z'}]
  self.assertEqual(minute_news_features(rows,[self.r])[0]['newsWindowCounts']['5m']['symbolMentions'],0)
 def test_article_after_minute_end_rejected(self):
  rows=[{'symbol':'US.AAOI','minuteUtc':'2026-10-16T02:59:00Z'}]
  self.assertEqual(minute_news_features(rows,[self.r])[0]['newsWindowCounts']['5m']['symbolMentions'],1)
 def test_earlier_news_has_correct_sector(self):
  rows=[{'symbol':'US.LITE','minuteUtc':'2026-10-16T03:00:00Z'}]
  z=minute_news_features(rows,[self.r])[0]['newsWindowCounts']['5m']
  self.assertEqual(z['sectorMentions'],1)
 def test_expired_news_not_counted(self):
  rows=[{'symbol':'US.AAOI','minuteUtc':'2026-10-16T05:02:00Z'}]
  self.assertEqual(minute_news_features(rows,[self.r])[0]['newsWindowCounts']['120m']['marketArticles'],0)
 def test_last_revision_only(self):
  r2=dict(self.r);r2['revision']=1;r2['first_seen_at']='2026-10-16T03:03:00Z';r2['received_at']=r2['first_seen_at']
  r2['value']={**r2['value'],'title':'New','tickers':['MU']}
  x=minute_news_features([{'symbol':'US.AAOI','minuteUtc':'2026-10-16T03:04:00Z'}],[self.r,r2])
  self.assertEqual(x[0]['newsWindowCounts']['5m']['symbolMentions'],0)
 def test_too_many_rows_fails(self):
  with self.assertRaises(ValueError):minute_news_features([{}]*2,[],max_rows=1)

class FetchTests(unittest.TestCase):
 def setUp(self):
  self.patcher=patch('news_feed.now_iso',return_value=NOW);self.patcher.start()
 def tearDown(self):self.patcher.stop()
 KEY='fake_TEST_KEY_123456789'

 def test_provider_schema_mismatch_fails_closed(self):
  with self.assertRaisesRegex(ValueError,'SCHEMA_OR_CLOCK'):
   nf.fetch('benzinga',self.KEY,http_get=lambda u,h:{'status':'OK','results':[{'title':'bad'}]})
 def test_partial_clock_rejection_threshold(self):
  with self.assertRaisesRegex(ValueError,'SCHEMA_OR_CLOCK'):
   nf.fetch('benzinga',self.KEY,http_get=lambda u,h:{'status':'OK','results':[ben(),{'title':'bad'}]})
 def test_benzinga_url_correct(self):
  def get(url,headers):
   self.assertIn('/benzinga/v2/news',url);self.assertIn('apiKey=',url)
   return {'status':'OK','results':[ben()]}
  x=nf.fetch('benzinga',self.KEY,http_get=get)
  self.assertEqual(len(x['items']),1);self.assertEqual(x['pages'],1)
 def test_reference_not_realtime(self):
  x=nf.fetch('massive',self.KEY,http_get=lambda u,h:{'status':'OK','results':[ref()]})
  self.assertEqual(x['items'][0]['transport'],'HOURLY_UPDATED_NOT_REALTIME')
 def test_finnhub_header(self):
  def get(url,headers):
   self.assertEqual(headers['X-Finnhub-Token'],self.KEY)
   self.assertNotIn('token=',url)
   x=finn();x['datetime']=int(datetime(2026,10,16,2,59,50,tzinfo=timezone.utc).timestamp())
   return [x]
  self.assertEqual(len(nf.fetch('finnhub',self.KEY,http_get=get)['items']),1)
 def test_bounded_pagination(self):
  def get(url,headers):return {'status':'OK','results':[ben()],
                 'next_url':'https://api.massive.com/benzinga/v2/news?cursor=TEST'}
  x=nf.fetch('benzinga',self.KEY,max_pages=2,http_get=get)
  self.assertTrue(x['possiblyIncomplete']);self.assertEqual(x['pages'],2)
 def test_pagination_ssrf_blocked(self):
  def get(url,headers):return {'status':'OK','results':[],
                'next_url':'https://evil.example/benzinga/v2/news?cursor=TEST'}
  with self.assertRaisesRegex(ValueError,'UNTRUSTED'):nf.fetch('benzinga',self.KEY,http_get=get)
 def test_pagination_port_blocked(self):
  with self.assertRaises(ValueError):nf._safe_page_url('benzinga','https://api.massive.com:8443/benzinga/v2/news')
 def test_relative_redirect_blocked(self):
  with self.assertRaises(ValueError):nf.NoRedirect().redirect_request(None,None,302,'redirect',{},'https://evil.com')
 def test_bad_key_rejected(self):
  with self.assertRaises(ValueError):nf.fetch('benzinga','xxx',http_get=lambda u,h:[])
 def test_unknown_provider_rejected(self):
  with self.assertRaises(ValueError):nf.fetch('xyz',self.KEY,http_get=lambda u,h:[])
 def test_bad_status_response(self):
  with self.assertRaises(ValueError):nf.fetch('benzinga',self.KEY,http_get=lambda u,h:{'status':'ERROR','results':[]})
 def test_poll_redacts_secret(self):
  with tempfile.TemporaryDirectory() as td:
   log=Path(td)/'log.jsonl';status=Path(td)/'status.json'
   def fail(url,h):raise ValueError('HTTP_INVALID_RESPONSE')
   result=nf.poll('benzinga',self.KEY,log=log,status_path=status,http_get=fail)
   self.assertEqual(result['status'],'NO_GO_NEWS_PROVIDER_OR_INTEGRITY')
   self.assertNotIn(self.KEY,status.read_text())
 def test_poll_success_status_still_no_verified_entitlement(self):
  with tempfile.TemporaryDirectory() as td:
   status=Path(td)/'status.json';log=Path(td)/'log.jsonl'
   out=nf.poll('benzinga',self.KEY,log=log,status_path=status,
       http_get=lambda u,h:{'status':'OK','results':[ben()]})
   self.assertEqual(out['added'],1);self.assertFalse(out['actualLiveFeedVerified'])
 def test_non_real_time_not_promoted(self):
  with tempfile.TemporaryDirectory() as td:
   out=nf.poll('massive',self.KEY,log=Path(td)/'l',status_path=Path(td)/'s',
       http_get=lambda u,h:{'status':'OK','results':[ref()]})
   self.assertEqual(out['deliveryClass'],'HOURLY_UPDATED_NOT_REALTIME')

if __name__=='__main__':unittest.main()
