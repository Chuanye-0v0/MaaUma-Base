"""Reuse the existing guarded capture store, scoped only to this module."""
from pathlib import Path
import json
import time
from itertools import chain

from core.screenshot_retention import CaptureStore


def token(path):
    value=Path(path).stat()
    return [value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns]


def register(journal,frame):
    path=Path(frame['screenshot_path']).resolve()
    root=Path(journal.data['operation_root']).resolve()
    if path.parent!=root or path.suffix.lower()!='.png' or path.is_symlink():
        raise ValueError('strongest_team_capture_scope_invalid')
    journal.data.setdefault('capture_index',{})[str(path)]=dict(token=token(path),sha=frame['frame_sha256'])


def restore_registered(journal,screenshots):
    root=Path(journal.data['operation_root']).resolve()
    store=screenshots.stores.setdefault(root,CaptureStore(root,full=False))
    for name,record in list(journal.data.get('capture_index',{}).items()):
        path=Path(name)
        if not path.exists():
            journal.data['capture_index'].pop(name,None);continue
        if path.resolve().parent!=root or path.is_symlink() or token(path)!=record['token']:
            continue
        store.record(dict(screenshot_path=str(path),frame_sha256=record['sha'],page='registered_recovery'))
    return store


def consume(journal,screenshots,current,*,keep_current=True):
    if journal.pending:return dict(skipped='pending_resource_owner')
    store=restore_registered(journal,screenshots)
    store.recent.clear()
    proof={'last_observation':current} if keep_current else {}
    store.finish(True,proof)
    index=journal.data.get('capture_index',{})
    journal.data['capture_index']={p:r for p,r in index.items() if Path(p).exists()}
    journal.save()
    return dict(registered_frames=len(index),retained_frames=len(journal.data['capture_index']),
                only_module_registered_captures=True)


def maintain(ledger_root,*,now=None,max_seconds=.5,max_files=100):
    """A bounded existing-boundary hook; no new timer and no PNG directory scan."""
    root=Path(ledger_root).resolve();now=time.time() if now is None else now
    if root.name!='strongest_team' or root.parent.name!='module-ledgers' or root.parent.parent.name!='runs':
        raise ValueError('strongest_team_archive_root_invalid')
    removed=0;protected=0;deadline=time.monotonic()+max_seconds
    if not root.exists():return dict(removed=0,protected=0)
    for book_path in sorted(chain(root.glob('account-*/*.json'),root.glob('current-*/*.json'))):
        if time.monotonic()>=deadline or removed>=max_files:break
        try:
            data=json.loads(book_path.read_text(encoding='utf8'))
            if data.get('schema')!='uma.strongest_team.day.v1':continue
            if data.get('status') not in {'done','skipped','failed','cancelled'}:
                protected+=1;continue
            if data.get('pending_entry') or data.get('pending_operation') or data.get('navigation_pending'):
                protected+=1;continue
            directory=Path(data.get('operation_root','')).resolve()
            private=directory.is_relative_to(root.parent.parent/'mfa-batches')
            public=(data.get('scope',{}).get('namespace')=='public_current'
                and directory.is_relative_to(root.parent.parent/'public-tasks')
                and book_path.parent.name==data.get('scope',{}).get('account'))
            if directory.name!='strongest_team' or not (private or public):
                continue
            if private and ('account-'+str(data.get('scope',{}).get('account','')).removeprefix('account-')) not in directory.parts:
                continue
            store=CaptureStore(directory,full=False)
            expired=[]
            age=12*3600 if data.get('status') in {'failed','cancelled'} else 4*3600
            for name,record in data.get('capture_index',{}).items():
                path=Path(name)
                if removed+len(expired)>=max_files:break
                if (path.is_file() and not path.is_symlink() and path.resolve().parent==directory
                        and token(path)==record['token'] and now-record['token'][3]/1e9>=age):
                    store.record(dict(screenshot_path=str(path),frame_sha256=record['sha'],page='expired_settled_capture'))
                    expired.append(name)
            store.recent.clear();store.finish(True,{})
            for name in expired:
                if not Path(name).exists():data['capture_index'].pop(name,None);removed+=1
            if expired:
                from .journal import atomic_write
                atomic_write(book_path,data)
        except (OSError,ValueError,TypeError,KeyError):
            protected+=1
    return dict(removed=removed,protected=protected,scope='module_registered_only',ordinary_hours=4,failure_hours=12)
