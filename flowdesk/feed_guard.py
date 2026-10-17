"""Fail-closed, research-only market-data health checks.

Quote timestamp is from the provider; book callback receipt time is NOT exchange time.
Calendar covers regular NYSE rules, not unscheduled closures. Never authorizes orders.
"""
from datetime import datetime, date, timedelta, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


def _nth_weekday(y, m, weekday, nth):
    d = date(y, m, 1)
    return d + timedelta(days=(weekday-d.weekday()) % 7 + 7 * (nth-1))


def _last_weekday(y, m, weekday):
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday()-weekday) % 7)


def _observed(d):
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def _easter(y):
    a=y%19; b=y//100; c=y%100; d=b//4; e=b%4; f=(b+8)//25; g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30; i=c//4; k=c%4; l=(32+2*e+2*i-h-k)%7
    mm=(a+11*h+22*l)//451; n=h+l-7*mm+114
    return date(y,n//31,(n%31)+1)


def standard_market_day(day):
    """Return (session, closing ET hour) for standard rules, NOT exchange certification."""
    if day.weekday() >= 5:
        return False, None
    y=day.year
    holiday={_observed(date(y,1,1)),_nth_weekday(y,1,0,3),_nth_weekday(y,2,0,3),
             _easter(y)-timedelta(days=2),_last_weekday(y,5,0),_observed(date(y,6,19)),
             _observed(date(y,7,4)),_nth_weekday(y,9,0,1),_nth_weekday(y,11,3,4),
             _observed(date(y,12,25))}
    # January 1 may be observed on Dec 31 of the previous calendar year.
    holiday.add(_observed(date(y+1,1,1)))
    if day in holiday:
        return False, None
    thanksgiving=_nth_weekday(y,11,3,4)
    black_friday=thanksgiving+timedelta(days=1)
    christmas_eve=date(y,12,24)
    # Only standard early closes, except when that day is otherwise a holiday.
    early=day==black_friday or (day==christmas_eve and day.weekday()<5)
    return True, 13 if early else 16


def clock_state(now=None):
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('必须使用有时区时间')
    d=now.astimezone(ET); valid,close_hour=standard_market_day(d.date())
    minutes=d.hour*60+d.minute
    regular=bool(valid and 570 <= minutes < close_hour*60)
    return {'regular':regular,'marketDate':d.date().isoformat(),
            'marketTimeET':d.isoformat(),'session':('REGULAR' if regular else 'OUTSIDE_REGULAR'),
            'closingHourET':close_hour,'calendarConfidence':'STANDARD_RULES_ONLY_NOT_EXCHANGE_VERIFIED'}


def as_time(s):
    try:
        d=s if isinstance(s,datetime) else datetime.fromisoformat(str(s).replace('Z','+00:00'))
        if d.tzinfo:return d.astimezone(timezone.utc)
    except (ValueError,TypeError):pass
    return None


def evaluate(config,subscriptions,good_quotes,session_start=None,now=None,queue_dropped=0,recording=None,connection_ok=False):
    """Research quality gate. No trade permission regardless of data recency."""
    now=now or datetime.now(timezone.utc)
    clock=clock_state(now)
    anchor=config['benchmark']; stale_seconds=None
    quote_at=as_time(good_quotes.get(anchor))
    if quote_at is not None:stale_seconds=(now-quote_at).total_seconds()
    subscribed=any(k.startswith(anchor+'/') and ('QUOTE' in k.upper()) and v=='成功'
                   for k,v in subscriptions.items())
    rec=recording or {}
    integrity_bad=queue_dropped>0 or (bool(rec) and (rec.get('error') or rec.get('dropped',0)>0))
    start=as_time(session_start)
    elapsed=(now-start).total_seconds() if start else None
    if integrity_bad:
        status='NO_GO_INTEGRITY'; reason='接收或录制存在丢弃/错误；此段数据不可用于正式研究'
    elif not connection_ok:
        status='NO_GO_CONNECTION'; reason='OpenD状态尚未确认或连接中断'
    elif not subscribed:
        status='NO_GO_BENCHMARK_SUBSCRIPTION'; reason=anchor+' 基准QUOTE未成功订阅'
    elif not clock['regular']:
        status='OUTSIDE_REGULAR'; reason='不在标准美股常规时段；不对盘前/盘后做常规行情推断'
    elif stale_seconds is not None and -2 <= stale_seconds <= 20:
        status='RECENT_RESEARCH_ONLY'; reason='基准报价源时间和本机接收校验通过；仍非交易授权'
    elif elapsed is None or elapsed<60:
        status='WARMUP'; reason='等待基准源时间合格的实时推送'
    else:
        status='NO_GO_STALE'; reason='基准报价源时间陈旧、缺失或超前；不能把OpenD连接成功当作行情有效'
    # This is a diagnostic reconnect suggestion, never an order authorization.
    suggest=(status=='NO_GO_STALE' and elapsed is not None and elapsed>=180 and
             (stale_seconds is None or stale_seconds>=120))
    return {**clock,'status':status,'reason':reason,'benchmark':anchor,
            'benchmarkLastValidAt':quote_at.isoformat() if quote_at else None,
            'benchmarkAgeSeconds':round(stale_seconds,1) if stale_seconds is not None else None,
            'warmupSeconds':round(elapsed,1) if elapsed is not None else None,
            'reconnectSuggested':suggest,'liveOrdersEnabled':False,
            'quoteSourceTimestampRequired':True,'benchmarkTimeProvenance':'VENDOR_QUOTE_SOURCE_AS_OF',
            'bookExchangeTimestampVerified':False}
