#!/usr/bin/env python3
"""Strict v2.7 -> v2.8 installer, no broker orders, rollback on failure."""
import hashlib,json,os,sys
from pathlib import Path
from datetime import datetime,timezone

BASE={
    'bridge.py':'e2c90489baedc786396fd66af4a128e2706c1152455d8fa6d0aba40e48c34925',
    'dashboard.html':'671af244a264a30e292bf235a5f8fbcac65f14f87b64c6112adf58bf52ea7716',
}
NEW=('news_impact_lab.py','StudyNewsImpact.command','tests/test_v28_news_impact.py',
     'README_v28_新闻冲击与市场相对反应研究.md','RELEASE_MANIFEST_v28.json',
     'install_v28.py','安装v28.command','SHA256SUMS_v28.txt')

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def install(project,patch):
    project=Path(project).expanduser().resolve(strict=True)
    patch=Path(patch).resolve(strict=True)
    release=project/'RELEASE_MANIFEST_v27.json'
    if not release.is_file() or not str(json.loads(release.read_text(encoding='utf-8')).get('version','')).startswith('2.7.'):
        raise RuntimeError('REQUIRES_UNMODIFIED_V27')
    if (project/'RELEASE_MANIFEST_v28.json').exists():raise RuntimeError('V28_ALREADY_PRESENT')
    sealed=project/'research'/'sealed_trials'
    if sealed.is_symlink() or (sealed.exists() and any(sealed.iterdir())):
        raise RuntimeError('FROZEN_STUDY_PRESENT_INSTALL_TO_SEPARATE_FOLDER')
    for d in ('research','output','recordings','.private','tests'):
        if (project/d).is_symlink():raise RuntimeError('REFUSE_DIRECTORY_SYMLINK_'+d)
    for rel,digest in BASE.items():
        p=project/rel
        if not p.is_file() or p.is_symlink() or sha(p)!=digest:
            raise RuntimeError('BASE_SOURCE_HASH_MISMATCH_'+rel)
    for rel in NEW:
        a=patch/rel;b=project/rel
        if not a.is_file() or a.is_symlink() or b.exists() or b.is_symlink() or b.parent.is_symlink():
            raise RuntimeError('PATCH_FILE_OR_DESTINATION_INVALID_'+rel)
    for rel in BASE:
        if not (patch/rel).is_file() or (patch/rel).is_symlink():raise RuntimeError('PATCH_MISSING_'+rel)
    original={rel:(project/rel).read_bytes() for rel in BASE}
    backups=project/'research'/'install_backups'/('v28_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ%f'))
    backups.mkdir(parents=True,exist_ok=False)
    written=[]
    try:
        for rel,raw in original.items():
            p=backups/rel;p.write_bytes(raw);p.chmod(0o600)
        for rel in NEW:
            p=project/rel;p.parent.mkdir(parents=True,exist_ok=True)
            raw=(patch/rel).read_bytes()
            with p.open('xb') as f:f.write(raw)
            written.append(p)
            if p.suffix=='.command':p.chmod(0o755)
            if sha(p)!=hashlib.sha256(raw).hexdigest():raise RuntimeError('COPY_SHA_MISMATCH')
        for rel in BASE:
            dest=project/rel;staging=dest.with_name(dest.name+'.v28_stage')
            if staging.exists():raise RuntimeError('INSTALL_STAGING_COLLISION')
            raw=(patch/rel).read_bytes()
            with staging.open('xb') as f:f.write(raw)
            written.append(staging)
            os.replace(staging,dest)
            if sha(dest)!=hashlib.sha256(raw).hexdigest():raise RuntimeError('REPLACEMENT_SHA_MISMATCH')
    except BaseException:
        for rel,raw in original.items():(project/rel).write_bytes(raw)
        for p in reversed(written):
            try:p.unlink(missing_ok=True)
            except OSError:pass
        raise
    print('PASS_V28_INSTALLED_LOCAL_RESEARCH_ONLY',str(backups))
    print('NEXT: bash StudyNewsImpact.command --asof <UTC_ISO> --events-file <closed_events.jsonl>')

if __name__=='__main__':
    project=sys.argv[1] if len(sys.argv)>1 else '/Users/quyang/Desktop/FlowDesk'
    try:install(project,Path(__file__).resolve().parent)
    except Exception as e:print('NO_GO_V28_INSTALL:',str(e),file=sys.stderr);raise SystemExit(2)
