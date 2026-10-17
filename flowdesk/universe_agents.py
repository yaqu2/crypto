"""Whole-current-US-universe snapshot reviewer and candidate finder.

No prediction, trading, or historical claims. Verifies two source-file digests.
"""
import argparse,csv,hashlib,json,math,statistics
from datetime import datetime,timezone
from pathlib import Path


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1<<20),b''):h.update(part)
    return h.hexdigest()


def num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):return None


def yes(v):return str(v).lower() in ('true','1','yes')


def robust_z(x, xs):
    med=statistics.median(xs);mad=statistics.median(abs(v-med) for v in xs)
    return max(-5,min(5,(x-med)/(1.4826*mad))) if mad>1e-9 else 0.0


def review(directory,now=None):
    now=now or datetime.now(timezone.utc)
    directory=Path(directory);report=json.loads((directory/'latest_report.json').read_text())
    for name,key in [('us_stock_universe.csv','universeSha256'),('latest_quotes.csv','quotesSha256')]:
        if report.get(key)!=sha(directory/name):raise ValueError('DATA_TAMPER_OR_MISMATCH: '+name)
    if report.get('benchmarksSha256') is not None:
        if report['benchmarksSha256']!=sha(directory/'latest_benchmarks.csv'):
            raise ValueError('BENCHMARK_DATA_TAMPER_OR_MISMATCH')
    with (directory/'us_stock_universe.csv').open(newline='') as f:stock=list(csv.DictReader(f))
    with (directory/'latest_quotes.csv').open(newline='') as f:quotes=list(csv.DictReader(f))
    original={r['code'] for r in stock}
    if len(original)!=len(stock) or len(stock)!=report.get('stockUniverseCount'):
        raise ValueError('Universe denominator invalid/duplicate')
    if any(q['code'] not in original for q in quotes) or len({q['code'] for q in quotes})!=len(quotes):
        raise ValueError('Unexpected or duplicate snapshot rows')
    if len(quotes)!=report.get('snapshotReceivedCount'):raise ValueError('Snapshot count mismatch')
    at=datetime.fromisoformat(report['completedAt'])
    age=(now-at).total_seconds()
    recency=bool(-5<=age<=600)
    listed_ok=(report.get('scanScope')=='US_PRIMARY_LISTED'
               and report.get('dataGate')=='PASS_LISTED_SNAPSHOT_QUALITY_RESEARCH_ONLY'
               and report.get('fullListedUniverse')
               and report.get('listedSnapshotCount')==report.get('listedUniverseCount')
               and report.get('listedUniverseCount',0)>=2000)
    certified=bool(recency and not report.get('batchErrors') and not report.get('quoteDenials')
                   and (listed_ok or (report.get('dataGate')=='PASS_SNAPSHOT_QUALITY_RESEARCH_ONLY'
                                      and report.get('fullCurrentStockUniverse'))))
    usable=[]
    for row in quotes:
        if not yes(row.get('usable')):continue
        turnover=num(row.get('turnover'));value=num(row.get('changePct'))
        if turnover is not None and turnover>=0 and value is not None:
            usable.append({'code':row['code'],'turnover':turnover,'changePct':value})
    if len(usable)!=report.get('freshUsableCount'):
        certified=False
    # Cross-sectional deviation & *same-snapshot* liquidity only; no forward prediction.
    moves=[r['changePct'] for r in usable]
    volumes=[math.log1p(r['turnover']) for r in usable]
    ranked=[]
    if len(usable)>=10:
        for row in usable:
            mr=abs(robust_z(row['changePct'],moves))
            lr=robust_z(math.log1p(row['turnover']),volumes)
            score=round(mr+0.4*max(0,lr),4)
            ranked.append({'code':row['code'],'observationPriority':score,
                           'crossSectionDeviation':round(mr,4),
                           'relativeLiquidity':round(lr,4),
                           'changePct':row['changePct'], 'turnover':row['turnover']})
    ranked.sort(key=lambda r:(-r['observationPriority'],r['code']))
    return {'status':('LISTED_RESEARCH_WATCHLIST_ONLY' if listed_ok else 'RESEARCH_WATCHLIST_ONLY') if certified else 'NO_GO_FULL_MARKET_COVERAGE',
            'asOf':at.isoformat(),'ageSeconds':round(age,1),'currentUniverseCount':len(stock),'listedUniverseCount':report.get('listedUniverseCount'),
            'otcExcludedCount':report.get('otcExcludedCount'),
            'scanScope':report.get('scanScope','LEGACY_UNSPECIFIED'),
            'quotedCount':len(quotes),'freshUsableCount':len(usable),'sourceSha256Verified':True,
            'historicalPointInTimeUniverse':False,'predictionValidated':False,'tradeEnabled':False,
            'candidates':ranked[:60] if certified else [],
            'uncertifiedPreview':ranked[:10] if not certified else [],
            'note':'现时截面异常幅度及成交额，未使用订单流；观察优先级不是胜率/Alpha或买入排序。'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--market-dir',type=Path,default=Path(__file__).parent/'output/market');p.add_argument('--out',type=Path,default=None)
    a=p.parse_args();r=review(a.market_dir)
    target=a.out or a.market_dir/'agent_candidates.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in r.items() if k not in ('candidates','uncertifiedPreview')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
