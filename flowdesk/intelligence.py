"""Bounded, deterministic research evidence. No trade API or return prediction.

Last-price windows are regular-session only because pre/after/overnight prices
have different SDK provenance. Session labels are clock based, not a holiday
calendar. A valid rolling anchor is at/before target and at most five seconds
older. Cross-symbol endpoints and anchors must each match within five seconds.
"""
from collections import deque
from datetime import datetime, timedelta, timezone
import math
from threading import RLock
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

def _time(value):
    try:
        d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo is not None else None
    except (TypeError, ValueError):
        return None

def _now(value):
    d = _time(value) if value is not None else datetime.now(timezone.utc)
    if d is None: raise ValueError('now must be a timezone-aware datetime')
    return d

def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError): return None

def _session(t):
    e = t.astimezone(ET); minute = e.hour * 60 + e.minute
    regime = ('closed' if e.weekday() >= 5 else 'regular' if 570 <= minute < 960 else
              'pre' if 240 <= minute < 570 else 'after' if 960 <= minute < 1200 else 'overnight')
    return e.date().isoformat() + '/' + regime

def _fresh(t, now, maxage=15):
    return t is not None and -2 <= (now-t).total_seconds() <= maxage

def _missing(reason):
    return dict(available=False, pct=None, start=None, end=None, reason=reason)

class Intelligence:
    def __init__(self, symbols, benchmark='US.SPY', peers=None):
        self.symbols = list(dict.fromkeys(symbols))
        self.benchmark = benchmark
        self.peers = {k: [x for x in v if x != k] for k,v in (peers or {}).items()}
        self._lock = RLock()
        self._state = {s: self._empty() for s in self.symbols}
        self._alerts = deque(maxlen=100)
        self._last_alert = {}
        self._serial = 0
        self.reset_reason = '首次启动'

    @staticmethod
    def _empty():
        return dict(points=deque(maxlen=4096), latest=None, book=None, rejected=0,
                    lastReject=None, accepted=0)

    def reset(self, reason='连接重置'):
        with self._lock:
            self._state = {s: self._empty() for s in self.symbols}
            self._alerts.clear(); self._last_alert.clear()
            self.reset_reason = str(reason)

    def _reject(self, s, reason):
        s['rejected'] += 1; s['lastReject'] = reason
        return False

    def ingest_quote(self, code, cleaned_quote, origin='push', now=None):
        now = _now(now)
        with self._lock:
            s = self._state.get(code)
            if s is None: return False
            if origin != 'push': return self._reject(s, '种子、轮询或回放不进入实时证据')
            q = dict(cleaned_quote or {}); t = _time(q.get('sourceAsOf')); p = _num(q.get('last'))
            if t is None: return self._reject(s, '缺少带时区的报价源时间')
            if not _fresh(t, now): return self._reject(s, '报价源时间陈旧或超前超过2秒')
            if p is None or p <= 0: return self._reject(s, '报价价格无效')
            session = _session(t)
            if not session.endswith('/regular') or session != _session(now):
                return self._reject(s, 'last字段仅用于同一常规时段；节假日尚未验证')
            state = str(q.get('stockState') or '').upper()
            if state in ('HALT','HALTED','SUSPENDED','DELISTED','FUTURE','UNLISTED'):
                s['latest']=None; s['points'].clear()
                return self._reject(s, '证券状态不支持连续研究')
            prev = s['latest']
            if prev and t <= prev['time']: return self._reject(s, '重复或乱序源时间')
            if prev and session != prev['session']:
                s['points'].clear()
            point = dict(time=t, price=p, session=session)
            s['latest'] = point; s['accepted'] += 1
            # Last accepted sample of each source second. This is bounded memory,
            # not an invented quote at a fabricated one-second timestamp.
            points = s['points']
            if points and points[-1]['time'].replace(microsecond=0) == t.replace(microsecond=0):
                points[-1] = point
            else: points.append(point)
            self._detect(code, s, now)
            return True

    def ingest_book(self, code, cleaned_book, origin='push', now=None):
        now = _now(now)
        with self._lock:
            s = self._state.get(code)
            if s is None: return False
            if origin != 'push': return self._reject(s, '回放盘口不进入实时观察')
            b = dict(cleaned_book or {}); t = _time(b.get('receivedAt'))
            if not _fresh(t, now): return self._reject(s, '盘口接收时间无效或陈旧')
            prev = s['book']
            if prev and t <= prev['time']: return self._reject(s, '重复或乱序盘口接收时间')
            bids, asks = b.get('bids') or [], b.get('asks') or []
            valid = False; spread = imbalance = None
            try:
                bp, ap = _num(bids[0]['price']), _num(asks[0]['price'])
                sizes_b = [_num(x['size']) for x in bids[:5]]
                sizes_a = [_num(x['size']) for x in asks[:5]]
                valid = bp is not None and ap is not None and 0 < bp <= ap and all(x is not None and x >= 0 for x in sizes_b+sizes_a)
                if valid:
                    spread = (ap-bp)/((ap+bp)/2)*10000
                    vb,va = sum(sizes_b),sum(sizes_a)
                    imbalance = (vb-va)/(vb+va) if vb+va else None
            except (IndexError, TypeError, KeyError): pass
            s['book'] = dict(time=t, valid=valid, spreadBps=spread, imbalance=imbalance)
            if valid and spread >= 30:
                self._alert(code, 'spread_watch', '展示盘口点差达到30基点；仅接收时间新鲜，源时钟与市场覆盖未验证', t, now)
            return valid

    def _window(self, s, span, now):
        latest = s['latest']
        if not latest: return _missing('尚无合格常规时段推送')
        if not _fresh(latest['time'], now) or latest['session'] != _session(now):
            return _missing('当前报价已陈旧或跨交易时段')
        target = latest['time'] - timedelta(seconds=span)
        anchor = next((p for p in reversed(s['points']) if p['time'] <= target), None)
        if not anchor or (target-anchor['time']).total_seconds() > 5 or anchor['session'] != latest['session']:
            return _missing('缺少目标时点之前5秒内的同交易时段锚点')
        return dict(available=True, pct=(latest['price']/anchor['price']-1)*100,
                    start=anchor['time'].isoformat(), end=latest['time'].isoformat(),
                    startPrice=anchor['price'], endPrice=latest['price'],
                    actualSeconds=(latest['time']-anchor['time']).total_seconds(), reason=None)

    def _matched(self, code, window, now):
        s = self._state.get(code)
        if not s or not window['available']: return _missing('缺少共同窗口')
        start, end = _time(window['start']), _time(window['end'])
        finish = next((p for p in reversed(s['points']) if p['time'] <= end), None)
        anchor = next((p for p in reversed(s['points']) if p['time'] <= start), None)
        if not finish or not anchor: return _missing('缺少共同窗口锚点')
        if (end-finish['time']).total_seconds() > 5 or (start-anchor['time']).total_seconds() > 5:
            return _missing('跨标的起止时间未在5秒内对齐')
        if not _fresh(finish['time'], now) or anchor['session'] != finish['session'] or finish['session'] != _session(now):
            return _missing('共同窗口陈旧或跨交易时段')
        return dict(available=True,pct=(finish['price']/anchor['price']-1)*100,
                    start=anchor['time'].isoformat(),end=finish['time'].isoformat(),reason=None)

    def _alert(self, code, kind, message, source, now):
        key = (code,kind)
        if key in self._last_alert and (now-self._last_alert[key]).total_seconds() < 60: return
        self._last_alert[key] = now; self._serial += 1
        self._alerts.append(dict(id=self._serial,code=code,type=kind,createdAt=now.isoformat(),
                                 sourceAsOf=source.isoformat(),expiresAt=(now+timedelta(seconds=60)).isoformat(),message=message))

    def _detect(self, code, s, now):
        w = self._window(s,60,now)
        if w['available'] and abs(w['pct']) >= .5:
            self._alert(code, 'price_up' if w['pct'] > 0 else 'price_down',
                        f"约60秒价格变化 {w['pct']:+.3f}%；启发式观察阈值，非交易信号", s['latest']['time'], now)

    def export(self, now=None):
        now = _now(now)
        with self._lock:
            symbols = {}
            for code,s in self._state.items():
                q = s['latest']; t = q['time'] if q else None
                fresh = bool(q and _fresh(t,now) and q['session']==_session(now))
                returns = {str(span)+'s': self._window(s,span,now) for span in (60,300)}
                w = returns['60s']; benchmark = self._matched(self.benchmark,w,now)
                benchmark.update(code=self.benchmark,excessPct=(w['pct']-benchmark['pct']) if benchmark['available'] else None)
                peer_codes = self.peers.get(code,[])
                matches = [(p,self._matched(p,w,now)) for p in peer_codes]
                usable = [(p,x) for p,x in matches if x['available']]
                peers = dict(available=bool(usable),count=len(usable),total=len(peer_codes),
                             meanPct=sum(x['pct'] for _,x in usable)/len(usable) if usable else None,
                             breadthPositive=sum(x['pct']>0 for _,x in usable)/len(usable) if usable else None,
                             members=[dict(code=p,**x) for p,x in matches],
                             reason=None if usable else '缺少新鲜且起止对齐的同业数据')
                b=s['book']; b_fresh=bool(b and _fresh(b['time'],now))
                book=dict(receivedFresh=b_fresh,valid=bool(b and b['valid']),sourceVerified=False,
                          receivedAt=b['time'].isoformat() if b else None,
                          spreadBps=b['spreadBps'] if b else None,imbalance=b['imbalance'] if b else None,
                          note='展示深度的前五档量差；源时钟、覆盖和可成交性未验证。接收新鲜不等于市场数据新鲜。')
                observed=[]
                if w['available']: observed.append(f"约60秒价格变化 {w['pct']:+.3f}%")
                if benchmark['available']: observed.append(f"与{self.benchmark}对齐窗口相差 {benchmark['excessPct']:+.3f} 个百分点")
                if peers['available']: observed.append(f"{len(usable)}/{len(peer_codes)}个同业可对齐，平均变化 {peers['meanPct']:+.3f}%")
                inference='证据不足，无法区分个股变化与共同市场变化。'
                if w['available'] and peers['available'] and len(usable)==len(peer_codes) and len(usable)>=2:
                    aligned = w['pct']*peers['meanPct']>0
                    inference=('价格与可见同业同向，支持共同波动这一待验证解释；不能据此确认原因。' if aligned else
                               '价格与可见同业均值不同向，提示相对分化；不能据此确认公司事件。')
                hypotheses=[dict(title='价格共同变化检查',observed=observed,inference=inference,
                                 unknown=['未接入已核验新闻，不能认定利空或资金意图','没有交易商净持仓，不能推断净Gamma方向','同业组为人工配置，未验证代表性'],
                                 invalidation=['任一源时间陈旧即撤回实时解释','共同窗口不足或方向改变时重新检查','不将相关性当作因果或未来收益预测'])]
                symbols[code]=dict(code=code,status='证据可观察' if w['available'] else '积累窗口中' if fresh else '数据不足或陈旧',
                    quality=dict(quoteFresh=fresh,sourceAsOf=t.isoformat() if t else None,
                                 sourceAgeSeconds=(now-t).total_seconds() if t else None,
                                 session=q['session'] if q else _session(now),rejected=s['rejected'],lastReject=s['lastReject'],accepted=s['accepted'],retainedPoints=len(s['points'])),
                    returns=returns,benchmark=benchmark,peers=peers,book=book,hypotheses=hypotheses)
            alerts=[]
            for a in self._alerts:
                item=dict(a); s=symbols[a['code']]
                isbook=a['type']=='spread_watch'
                item['dataFresh']=bool((s['book']['receivedFresh'] if isbook else s['quality']['quoteFresh']) and _fresh(_time(a['sourceAsOf']),now) and now<=_time(a['expiresAt']))
                item['sourceVerified']=not isbook
                alerts.append(item)
            return dict(asOf=now.isoformat(),mode='research_watch_only',symbols=symbols,alerts=alerts,
                        methodology=dict(quoteMaxAgeSeconds=15,futureToleranceSeconds=2,anchorToleranceSeconds=5,
                            alertCooldownSeconds=60,priceAlert60sPct=.5,spreadAlertBps=30,
                            thresholdStatus='预设启发式，未校准或验证收益',resetReason=self.reset_reason),
                        limitations=['仅常规时段last价格；星期与时钟分类，未核验节假日和半日市','只接受推送，不把轮询、回放或接收时间制造为报价源时间','阈值仅提醒检查；无买卖建议，无收益预测或概率置信度','同业与基准要求共同窗口；当前股票池存在选择偏差'])
