#!/usr/bin/env python3
"""FlowDesk read-only provider news polling, audited point-in-time, stdlib only.

No key or provider access is shipped. NO claims of exchange/newswire first-availability
from REST publication timestamps. A live article enters research ONLY on local receipt.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'research' / 'news'
PROVIDERS = {
    'benzinga': {'base':'https://api.massive.com/benzinga/v2/news','env':'MASSIVE_API_KEY','class':'LICENSED_REALTIME_POLLING'},
    'massive': {'base':'https://api.massive.com/v2/reference/news','env':'MASSIVE_API_KEY','class':'HOURLY_UPDATED_NOT_REALTIME'},
    'finnhub': {'base':'https://finnhub.io/api/v1/news','env':'FINNHUB_API_KEY','class':'PLAN_DEPENDENT_NOT_PROVEN_REALTIME'},
}
PROVIDER_IDS = {'benzinga':'massive_benzinga_v2','massive':'massive_reference_v2','finnhub':'finnhub_general'}
KEY_PATTERN = re.compile(r'^[a-zA-Z0-9._-]{8,256}$')
SYMBOL_RE = re.compile(r'^[A-Z][A-Z0-9.\-]{0,12}$')
DEFAULT_WATCH = ('AAOI','LITE','COHR','CIEN','MU','SNDK','WDC','STX','NVDA','AMD','QQQ','SMH','SOXX','SPY')
NEWS_LOG = DATA / 'news_events.jsonl'
STATUS = DATA / 'news_status.json'
MAX_RESPONSE_BYTES = 5_000_000
TOPICS = {
    'MACRO_INFLATION': ('cpi','inflation','consumer prices','pce','物价'),
    'MACRO_RATES': ('federal reserve','fomc','interest rate','bond yield','treasury yield','联储'),
    'SEMICONDUCTOR': ('semiconductor','chipmaker','chip stock','晶片','芯片'),
    'AI_DATACENTER': ('ai data center','ai infrastructure','artificial intelligence','数据中心','人工智能'),
    'OPTICAL_NETWORK': ('optical','transceiver','photonics','fiber optic','光模块','光通信'),
    'MEMORY': ('dram','nand','memory chip','hbm','存储芯片','高带宽内存'),
    'EARNINGS': ('quarterly earnings','earnings results','revenue guidance','财报'),
}


def now_iso(): return datetime.now(timezone.utc).isoformat(timespec='microseconds')

def clock(v):
    if isinstance(v,(int,float)) and not isinstance(v,bool):
        if v<1e8 or v>4e9: raise ValueError('BAD_UNIX_SECONDS')
        d=datetime.fromtimestamp(v,timezone.utc)
    elif isinstance(v,str):
        d=datetime.fromisoformat(v.replace('Z','+00:00'))
    else: raise ValueError('BAD_TIME_TYPE')
    if d.tzinfo is None: raise ValueError('TIMEZONE_REQUIRED')
    return d.astimezone(timezone.utc)

def iso(v):return clock(v).isoformat(timespec='microseconds')

def canonical(d):return json.dumps(d,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')

def sha(data):return hashlib.sha256(data).hexdigest()

def clean_text(v,max_len=600):
    if not isinstance(v,str):return ''
    return ''.join(c if c.isprintable() else ' ' for c in v).strip()[:max_len]

def topics(title):
    t=title.lower()
    return [name for name,terms in TOPICS.items() if any(term in t for term in terms)]

def symbols(values):
    if not isinstance(values,list):return []
    return sorted({s.strip().upper() for s in values if isinstance(s,str) and SYMBOL_RE.fullmatch(s.strip().upper())})[:100]


def normalize(provider,raw,observed_at):
    """Normalize metadata; observation is first *local* sight, NOT vendor first_seen.
    Provider fields may be revised; every changed version is appended separately.
    """
    if provider not in PROVIDERS or not isinstance(raw,dict):raise ValueError('BAD_PROVIDER_ITEM')
    if provider=='benzinga':
        identifier=raw.get('benzinga_id'); pub=raw.get('published'); title=raw.get('title')
        link=raw.get('url'); ticks=raw.get('tickers'); snippet=raw.get('teaser','')
    elif provider=='massive':
        identifier=raw.get('id');pub=raw.get('published_utc');title=raw.get('title')
        link=raw.get('article_url');ticks=raw.get('tickers');snippet=raw.get('description','')
    else:
        identifier=raw.get('id');pub=raw.get('datetime');title=raw.get('headline')
        link=raw.get('url');ticks=[raw['related']] if raw.get('related') else []
        snippet=raw.get('summary','')
    if not str(identifier or '').strip() or not clean_text(title):raise ValueError('MISSING_ARTICLE_ID_OR_TITLE')
    if not isinstance(link,str) or urlsplit(link).scheme!='https' or not urlsplit(link).netloc:
        raise ValueError('INVALID_ARTICLE_URL')
    published=clock(pub); observed=clock(observed_at)
    if published>observed:raise ValueError('ARTICLE_PUBLISHED_IN_FUTURE')
    if (observed-published)>timedelta(days=7300):raise ValueError('ARTICLE_EXCESSIVELY_OLD')
    key='NEWS:'+PROVIDER_IDS[provider]+':'+str(identifier).strip()[:128]
    value={'title':clean_text(title,400), 'summary':clean_text(snippet,600),
           'url':link[:1200], 'tickers':symbols(ticks), 'topics':topics(title)}
    return {'type':'news','key':key,'value':value,'provider':PROVIDER_IDS[provider],
            'source_id':str(identifier).strip()[:128], 'published_at':iso(pub),
            'first_seen_at':iso(observed_at), 'received_at':iso(observed_at),
            'firstSeenMeaning':'LOCAL_FIRST_OBSERVED_NOT_VENDOR_DISCOVERY',
            'transport':PROVIDERS[provider]['class'], 'tradeSignal':False}


def _verified_lines(f):
    f.seek(0);prev='0'*64;items=[]
    for ln,line in enumerate(f,1):
        if not line.strip():raise ValueError('EMPTY_LOG_LINE_'+str(ln))
        try:obj=json.loads(line)
        except (UnicodeError,json.JSONDecodeError):raise ValueError('INVALID_NEWS_LOG_LINE_'+str(ln)) from None
        proof=obj.pop('eventHash',None)
        if obj.get('previousHash')!=prev or proof!=sha(canonical(obj)):
            raise ValueError('NEWS_HASH_CHAIN_FAILED_'+str(ln))
        obj['eventHash']=proof;items.append(obj);prev=proof
    return items,prev


def read_log(path=NEWS_LOG):
    path=Path(path)
    if not path.exists():return []
    if path.is_symlink():raise ValueError('REFUSE_LOG_SYMLINK')
    if path.stat().st_size>80_000_000:raise ValueError('NEWS_JOURNAL_TOO_LARGE_FOR_INTERACTIVE_VIEW')
    with path.open('rb') as f:return _verified_lines(f)[0]


def append_news(rows,path=NEWS_LOG):
    """Atomic append under flock, fsync. Refuse corrupted logs and unsafe links.
    Revisions preserve original historical states; corrected articles do not rewrite history.
    """
    path=Path(path)
    if path.exists() and path.is_symlink():raise ValueError('REFUSE_LOG_SYMLINK')
    if path.parent.is_symlink():raise ValueError('REFUSE_SYMLINK_DATA_DIRECTORY')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.is_symlink():raise ValueError('REFUSE_LOG_SYMLINK')
    fd=os.open(str(path),os.O_CREAT|os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW,0o600)
    added=0;duplicates=0
    with os.fdopen(fd,'r+b',buffering=0) as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        history,prev=_verified_lines(f)
        known={}
        for row in history:
            item=row.copy();item.pop('eventHash',None);item.pop('previousHash',None)
            h=sha(canonical(item['value']))
            known.setdefault(item['key'],set()).add(h)
        rev={}
        for item in history:rev[item['key']]=max(rev.get(item['key'],-1),int(item['revision']))
        for row in rows:
            key=row['key'];content_hash=sha(canonical(row['value']))
            if content_hash in known.get(key,set()):duplicates+=1;continue
            r=dict(row);r['revision']=rev.get(key,-1)+1;rev[key]=r['revision']
            r['previousHash']=prev;r['eventHash']=sha(canonical(r))
            f.write(canonical(r)+b'\n');prev=r['eventHash']
            known.setdefault(key,set()).add(content_hash);added+=1
        f.flush();os.fsync(f.fileno())
        fcntl.flock(f,fcntl.LOCK_UN)
    return {'added':added,'duplicates':duplicates,'totalVerified':len(history)+added,'lastEventHash':prev}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise ValueError('REDIRECT_NOT_ALLOWED')


def _safe_page_url(provider,url):
    parts=urlsplit(url);expected=urlsplit(PROVIDERS[provider]['base'])
    if parts.scheme!='https' or parts.netloc!=expected.netloc or parts.path!=expected.path or parts.fragment:
        raise ValueError('UNTRUSTED_PAGINATION_URL')
    return url


def _http_json(url,headers,timeout=12):
    req=Request(url,headers=headers)
    # Redirects disabled to avoid secret leakage to arbitrary hosts.
    opener=build_opener(NoRedirect())
    with opener.open(req,timeout=timeout) as rsp:
        if rsp.status!=200:raise ValueError('HTTP_UNEXPECTED_STATUS')
        raw=rsp.read(MAX_RESPONSE_BYTES+1)
        if len(raw)>MAX_RESPONSE_BYTES:raise ValueError('NEWS_RESPONSE_TOO_LARGE')
        return json.loads(raw)


def fetch(provider,key,*,limit=100,max_pages=2,http_get=None):
    if provider not in PROVIDERS:raise ValueError('UNKNOWN_PROVIDER')
    if not KEY_PATTERN.fullmatch(key or ''):raise ValueError('API_KEY_MISSING_OR_INVALID_FORMAT')
    if not 1<=limit<=500 or not 1<=max_pages<=5:raise ValueError('FETCH_BOUNDS_INVALID')
    if provider=='benzinga':query={'limit':limit,'sort':'published.desc','apiKey':key}
    elif provider=='massive':query={'limit':limit,'sort':'published_utc','order':'desc','apiKey':key}
    else:query={'category':'general'}
    endpoint=PROVIDERS[provider]['base'];url=endpoint+'?'+urlencode(query)
    headers={'User-Agent':'FlowDesk-News-PIT-Research/2.7','Accept':'application/json'}
    if provider=='finnhub':headers['X-Finnhub-Token']=key
    get=http_get or _http_json
    out=[];pages=0;truncated=False;invalid=0;raw_count=0
    while url and pages<max_pages:
        _safe_page_url(provider,url)
        # Separate observed timestamp per response, to avoid attributing earlier availability.
        response=get(url,headers)
        seen=now_iso()
        if provider=='finnhub':
            if not isinstance(response,list):raise ValueError('FINNHUB_NEWS_RESPONSE_INVALID')
            articles=response[:limit];next_url=None;truncated=len(response)>limit
        else:
            if not isinstance(response,dict) or response.get('status') not in (None,'OK') or not isinstance(response.get('results'),list):
                raise ValueError('MASSIVE_NEWS_RESPONSE_INVALID')
            articles=response['results'];next_url=response.get('next_url')
            if next_url is not None and not isinstance(next_url,str):raise ValueError('PAGINATION_INVALID')
        raw_count+=len(articles)
        for article in articles:
            try:out.append(normalize(provider,article,seen))
            except (ValueError,TypeError,OverflowError,KeyError):invalid+=1
        pages+=1
        if next_url:
            _safe_page_url(provider,next_url)
            # Massive pagination URL does not always include the key. Enforce one valid copy.
            parsed=urlsplit(next_url)
            q={k:v[-1] for k,v in parse_qs(parsed.query).items() if k!='apiKey'}
            q['apiKey']=key
            url=endpoint+'?'+urlencode(q)
        else:url=None
    if url is not None:truncated=True
    if raw_count and (not out or invalid/raw_count>0.2):
        raise ValueError('NEWS_PROVIDER_SCHEMA_OR_CLOCK_REJECTION_RATE_EXCEEDED')
    return {'items':out,'pages':pages,'possiblyIncomplete':truncated,'rawArticleCount':len(out),
            'rawProviderArticles':raw_count,'invalidArticles':invalid}


def update_status(data,path=STATUS):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    os.chmod(tmp,0o600);os.replace(tmp,path)


def poll(provider,key,*,log=NEWS_LOG,status_path=STATUS,limit=100,max_pages=2,http_get=None):
    checked=now_iso()
    basic={'provider':PROVIDER_IDS[provider],'deliveryClass':PROVIDERS[provider]['class'],
           'lastPollAt':checked,'actualLiveFeedVerified':False,'liveOrdersEnabled':False,
           'profitCertified':False,'secretStoredInStatus':False}
    try:
        response=fetch(provider,key,limit=limit,max_pages=max_pages,http_get=http_get)
        outcome=append_news(response['items'],log)
        basic.update({'status':'OK_POLLING_COVERAGE_UNVERIFIED' if not response['possiblyIncomplete'] else 'NO_GO_POSSIBLE_TRUNCATION',
                      'lastSuccessAt':now_iso(),'observedThisPoll':response['rawArticleCount'],
                      'pages':response['pages'],'possiblyIncomplete':response['possiblyIncomplete'],
                      'invalidArticles':response['invalidArticles'],**outcome})
    except (HTTPError,URLError,ValueError,OSError,TypeError,KeyError,TimeoutError) as err:
        # NEVER propagate raw URL from exception: it may contain apiKey.
        code=err.code if isinstance(err,HTTPError) else None
        basic.update({'status':'NO_GO_NEWS_PROVIDER_OR_INTEGRITY','errorType':type(err).__name__,
                      'httpStatus':code,'errorCode':str(err) if isinstance(err,ValueError) and len(str(err))<100 and 'http' not in str(err).lower() else 'REDACTED'})
    update_status(basic,status_path)
    return basic


def load_key(provider):
    name=PROVIDERS[provider]['env'];key=os.environ.get(name,'')
    if key:return key
    # Optional local user-controlled secret file; never shipped in ZIP.
    secret=ROOT/'.private'/'news.env'
    if secret.is_file() and not secret.is_symlink():
        if secret.stat().st_mode & 0o077:raise ValueError('SECRET_FILE_PERMISSIONS_NOT_PRIVATE')
        for line in secret.read_text(encoding='utf-8').splitlines():
            if line.startswith(name+'='):return line.split('=',1)[1].strip()
    return ''


def main(argv=None):
    p=argparse.ArgumentParser(description='Read-only point-in-time news poll, not a live trading signal')
    p.add_argument('--provider',choices=sorted(PROVIDERS),default='benzinga')
    p.add_argument('--watch',action='store_true',help='Continue polling while this Terminal remains open')
    p.add_argument('--interval',type=int,default=60)
    p.add_argument('--limit',type=int,default=100)
    p.add_argument('--max-pages',type=int,default=2)
    p.add_argument('--log',type=Path,default=NEWS_LOG)
    p.add_argument('--status-file',type=Path,default=STATUS)
    args=p.parse_args(argv)
    if args.interval<15 or args.interval>3600:raise SystemExit('POLL_INTERVAL_MUST_BE_15_TO_3600_SECONDS')
    key=load_key(args.provider)
    if not key:
        out={'status':'NO_GO_MISSING_API_KEY','provider':args.provider,'actualLiveFeedVerified':False,
             'instruction':'ConfigureNews.command or provider API key environment variable','liveOrdersEnabled':False}
        update_status(out,args.status_file);print(json.dumps(out,ensure_ascii=False));return 2
    while True:
        report=poll(args.provider,key,log=args.log,status_path=args.status_file,limit=args.limit,max_pages=args.max_pages)
        print(json.dumps(report,ensure_ascii=False),flush=True)
        if not args.watch:return 0 if report['status'].startswith('OK_') else 2
        time.sleep(args.interval)

if __name__=='__main__':raise SystemExit(main())
