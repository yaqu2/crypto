#!/usr/bin/env python3
"""Version-pinned v2.6 -> v2.7 news integration, atomic-ish with rollback.
No changes to live strategy, subscriptions or past research artifacts.
"""
import hashlib,json,os,shutil,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path

BASE={
 'bridge.py':'3fc78303760089dc24ed1b2645aaffe47e3a8e7c56beff1583d77fcd52d3c707',
 'dashboard.html':'dc433b1ec17ab8f91c54f26d806606a28f818ad9fb43727c39867a7d92455fec',
}
NEW=('news_feed.py','news_context.py','ConfigureNews.command','PollNews.command',
     'StartNews.command','StudyNews.command','tests/test_v27_news.py',
     'README_v27_实时新闻供应商接入与因果审计.md','RELEASE_MANIFEST_v27.json','.gitignore')


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def install(project,patch):
    project=Path(project).expanduser().resolve(strict=True)
    patch=Path(patch).resolve(strict=True)
    manifest=project/'RELEASE_MANIFEST_v26.json'
    if not manifest.is_file() or not (project/'multiscale_lab.py').is_file():
        raise RuntimeError('REQUIRES_UNMODIFIED_V26_STRUCTURE')
    if not str(json.loads(manifest.read_text(encoding='utf-8')).get('version','')).startswith('2.6.'):
        raise RuntimeError('REQUIRES_V26_NOT_OTHER_VERSION')
    if any((project/'research'/'sealed_trials').glob('*')):
        raise RuntimeError('FROZEN_STUDY_PRESENT_REQUIRES_SEPARATE_COPY')
    for rel,h in BASE.items():
        dst=project/rel
        if not dst.is_file() or dst.is_symlink() or digest(dst)!=h:
            raise RuntimeError('ORIGINAL_BASE_FILE_MODIFIED: '+rel)
    for rel in NEW:
        src=patch/rel;dst=project/rel
        if not src.is_file() or src.is_symlink() or dst.exists() or dst.is_symlink():
            raise RuntimeError('NEW_FILE_MISSING_OR_ALREADY_EXISTS: '+rel)
        if dst.parent.is_symlink():raise RuntimeError('SYMLINK_FOLDER_REFUSED: '+rel)
    for rel in BASE:
        src=patch/rel
        if not src.is_file() or src.is_symlink():raise RuntimeError('PATCH_MISSING: '+rel)
    if (project/'.private').is_symlink() or (project/'research').is_symlink():
        raise RuntimeError('SENSITIVE_FOLDER_SYMLINK_REFUSED')
    backed_up={rel:(project/rel).read_bytes() for rel in BASE}
    written=[]
    backup_dir=project/'research'/'install_backups'/('v27_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup_dir.mkdir(parents=True,exist_ok=False)
    try:
        for rel,data in backed_up.items():
            f=backup_dir/rel;f.write_bytes(data);os.chmod(f,0o600)
        for rel in NEW:
            src=patch/rel;dst=project/rel;dst.parent.mkdir(parents=True,exist_ok=True)
            data=src.read_bytes()
            with dst.open('xb') as f:f.write(data)
            written.append(dst)
            if dst.suffix=='.command':dst.chmod(0o755)
            if hashlib.sha256(data).hexdigest()!=digest(dst):raise RuntimeError('WRITTEN_HASH_MISMATCH')
        for rel in BASE:
            src=patch/rel;dst=project/rel
            staging=dst.with_name(dst.name+'.v27_install_tmp')
            if staging.exists():raise RuntimeError('STAGING_FILE_EXISTS')
            data=src.read_bytes()
            with staging.open('xb') as f:f.write(data)
            written.append(staging)
            os.replace(staging,dst)
            if digest(dst)!=hashlib.sha256(data).hexdigest():raise RuntimeError('REPLACEMENT_HASH_MISMATCH')
    except BaseException:
        for rel,data in backed_up.items():
            (project/rel).write_bytes(data)
        for dst in reversed(written):
            if dst.name.endswith('.v27_install_tmp') or dst.name not in BASE:
                try:dst.unlink(missing_ok=True)
                except OSError:pass
        raise
    print('PASS_V27_INSTALLED_STRICT_V26_BASE; backup='+str(backup_dir))
    print('Config: bash ConfigureNews.command ; then bash PollNews.command --provider benzinga')

if __name__=='__main__':
    project=sys.argv[1] if len(sys.argv)>1 else '/Users/quyang/Desktop/FlowDesk'
    try:install(project,Path(__file__).resolve().parent)
    except Exception as exc:print('NO_INSTALL:',exc,file=sys.stderr);raise SystemExit(2)
