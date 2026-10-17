#!/usr/bin/env python3
"""Local immutable-ish market scans with hash-chain inventory.

TAMPER-EVIDENT within a retained local inventory, not externally notarized.
No historical PIT universe reconstruction. Each scan is an immutable observation
of the CURRENT stock directory, with known differences in per-batch timestamps.
"""
import argparse,fcntl,hashlib,json,os,re,shutil,uuid
from datetime import datetime,timezone
from pathlib import Path

FILES=('us_stock_universe.csv','latest_quotes.csv','latest_report.json')
OPTIONAL_BENCHMARK='latest_benchmarks.csv'

def scan_files(root):
    report=json.loads((Path(root)/'latest_report.json').read_text())
    if report.get('benchmarksSha256') is not None:
        if sha(Path(root)/OPTIONAL_BENCHMARK)!=report['benchmarksSha256']:
            raise ValueError('BENCHMARK_SOURCE_HASH_MISMATCH')
        return (*FILES,OPTIONAL_BENCHMARK)
    return FILES

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1<<20),b''):h.update(part)
    return h.hexdigest()

def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()

def register_scan(market_root):
    root=Path(market_root);archive=root/'archive';archive.mkdir(parents=True,exist_ok=True)
    for name in FILES:
        if not (root/name).is_file():raise FileNotFoundError('SCAN_SOURCE_MISSING:'+name)
    lock=archive/'.archive.lock';inv=archive/'scan_inventory.jsonl'
    with lock.open('a+b') as fd:
        fcntl.flock(fd.fileno(),fcntl.LOCK_EX)
        try:
            audit_archive(market_root)
            prior=[]
            if inv.exists():prior=[json.loads(x) for x in inv.read_text().splitlines() if x.strip()]
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            run_id=stamp+'-'+uuid.uuid4().hex[:8]
            # Fail before creating an archive folder if the latest scan is corrupt.
            report=json.loads((root/'latest_report.json').read_text())
            expected={'us_stock_universe.csv':report.get('universeSha256'),
                      'latest_quotes.csv':report.get('quotesSha256')}
            for name,claimed in expected.items():
                if claimed!=sha(root/name):raise ValueError('SNAPSHOT_SOURCE_HASH_MISMATCH')
            # Fail before creating a folder if the optional benchmark proof is
            # missing/tampered; an orphan would permanently block the inventory.
            included=scan_files(root)
            target=archive/run_id;target.mkdir(exist_ok=False)
            for name in included:
                shutil.copy2(root/name,target/name)
            hashes={name:sha(target/name) for name in included}
            for name in expected:
                if hashes[name]!=expected[name]:raise ValueError('SOURCE_CHANGED_DURING_ARCHIVE')
            manifest={'runId':run_id,'archivedAt':datetime.now(timezone.utc).isoformat(),
                      'filesSha256':hashes,'universeCount':report['stockUniverseCount'],
                      'freshUsableCount':report['freshUsableCount'],
                      'dataGate':report['dataGate'],
                      'scope':'CURRENT_US_STOCK_DIRECTORY_BATCH_SNAPSHOTS_NOT_PIT_OR_MBO'}
            (target/'archive_manifest.json').write_bytes(canonical(manifest)+b'\n')
            prev=prior[-1]['hash'] if prior else '0'*64
            line={'seq':len(prior)+1,'previousHash':prev,'runId':run_id,
                  'manifestSha256':sha(target/'archive_manifest.json')}
            line['hash']=hashlib.sha256(canonical(line)).hexdigest()
            with inv.open('ab') as out:
                out.write(canonical(line)+b'\n');out.flush();os.fsync(out.fileno())
            return {'archiveId':run_id,'archiveManifestSha256':line['manifestSha256'],'localChainSequence':line['seq']}
        finally:fcntl.flock(fd.fileno(),fcntl.LOCK_UN)

def audit_archive(market_root):
    archive=Path(market_root)/'archive';inv=archive/'scan_inventory.jsonl'
    if not inv.exists():
        dirs=[x.name for x in archive.iterdir() if x.is_dir()] if archive.exists() else []
        return {'status':'NO_GO_UNREGISTERED_SCAN' if dirs else 'NO_ARCHIVED_SCANS',
                'scanCount':len(dirs),'verified':0,'errors':['UNREGISTERED_SCAN_DIRECTORY'] if dirs else [],
                'liveOrdersEnabled':False}
    prev='0'*64;count=0;errors=[];seen=set()
    with inv.open('rb') as stream:
        for raw in stream:
            if not raw.endswith(b'\n'):errors.append('INCOMPLETE_INVENTORY_TAIL');break
            try:
                x=json.loads(raw);unsigned={k:x[k] for k in ('seq','previousHash','runId','manifestSha256')}
                if x['seq']!=count+1 or x['previousHash']!=prev or hashlib.sha256(canonical(unsigned)).hexdigest()!=x['hash']:
                    raise ValueError('BROKEN_ARCHIVE_INVENTORY_CHAIN')
                if x['runId'] in seen:raise ValueError('DUPLICATE_SCAN_ARCHIVE_RUN')
                if not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}',x['runId']):
                    raise ValueError('INVALID_SCAN_ID')
                folder=archive/x['runId']
                if not folder.is_dir() or sha(folder/'archive_manifest.json')!=x['manifestSha256']:
                    raise ValueError('MISSING_OR_TAMPERED_SCAN_MANIFEST')
                manifest=json.loads((folder/'archive_manifest.json').read_text())
                if manifest['runId']!=x['runId']:raise ValueError('ARCHIVE_ID_MISMATCH')
                declared=manifest['filesSha256']
                if set(declared) not in (set(FILES),set((*FILES,OPTIONAL_BENCHMARK))):
                    raise ValueError('INVALID_ARCHIVE_FILE_SET')
                for name,claimed in declared.items():
                    if name not in (*FILES,OPTIONAL_BENCHMARK) or sha(folder/name)!=claimed:
                        raise ValueError('TAMPERED_ARCHIVED_SCAN:'+name)
                if OPTIONAL_BENCHMARK in declared:
                    audit_report=json.loads((folder/'latest_report.json').read_text())
                    if audit_report.get('benchmarksSha256')!=declared[OPTIONAL_BENCHMARK]:
                        raise ValueError('BENCHMARK_REPORT_HASH_MISMATCH')
                prev=x['hash'];seen.add(x['runId']);count+=1
            except Exception as e:
                errors.append(str(e)[:200]);break
    orphan=[x.name for x in archive.iterdir() if x.is_dir() and x.name not in seen]
    if orphan:errors.append('UNREGISTERED_SCAN_DIRECTORY:'+','.join(orphan[:5]))
    return {'status':'PASS_LOCAL_ARCHIVE_INTEGRITY' if not errors else 'NO_GO_ARCHIVE_TAMPER',
            'scanCount':count+len(errors),'verified':count,'errors':errors,
            'liveOrdersEnabled':False,'pointInTimeHistoricalUniverse':False,
            'note':'本地哈希链无法阻止有权限者整体替换/删除资料；不是第三方可信时间戳。'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--market-dir',type=Path,default=Path(__file__).resolve().parent/'output'/'market')
    a=p.parse_args();print(json.dumps(audit_archive(a.market_dir),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
