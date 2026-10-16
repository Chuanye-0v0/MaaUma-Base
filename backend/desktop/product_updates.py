"""Product-scoped portable updates. Standard library only; no device control.

Online feeds require HTTPS and released full packages. Explicit --feed-file is
for local candidate rehearsal only. Installation runs from an isolated helper
after the GUI exits, preserves user data, and journals rollback operations.
"""
from contextlib import contextmanager
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid
from zipfile import ZipFile
PRODUCTS = {'umaa-public', 'uma-cut', 'uma-career'}
MANIFESTS = ('public-package-manifest.json', 'product-package-manifest.json')
CHANNELS = {'stable', 'preview'}
MAX_ARCHIVE = 2 * 1024 ** 3
MAX_UNPACKED = 5 * 1024 ** 3
MAX_FILES = 30000
PRESERVED = {'config', 'logs', 'exports', 'review_exports', 'temp', 'updates'}
MESSAGES = {'not_configured': '尚未配置此产品的更新地址。', 'current': '当前已是所选通道的最新版本。', 'update_available': '发现新版本，可以下载准备更新。', 'prepared': '更新已准备好，请在任务结束后安装。', 'installed': '更新完成，用户配置和运行记录已保留。', 'rolled_back': '已恢复更新前的程序文件。', 'application_running': '界面或更新程序仍在运行，请稍后重试。', 'device_busy': '任务尚未退出，已保留下载包并停止安装。', 'rollback_required': '上次更新未完成，需要先恢复旧版本。', 'install_failed_rolled_back': '安装失败，已恢复旧版本；下载包和诊断记录已保留。', 'candidate_not_released': '此更新包尚未通过发布准备检查，已停止安装。', 'package_integrity_failed': '下载包不完整或已被改动，请重新下载。', 'wrong_product': '更新包属于另一个产品，已停止更新。'}

class UpdateError(RuntimeError):
    pass

def read_json(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise UpdateError('invalid_json_object')
    return value

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    os.replace(temporary, path)

def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value or (':' in value):
        raise UpdateError('unsafe_package_path')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any((part in {'.', '..'} for part in path.parts)):
        raise UpdateError('unsafe_package_path')
    for part in path.parts:
        if part.endswith(('.', ' ')) or re.match('(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\\.|$)', part):
            raise UpdateError('unsafe_package_path')
    return value

def contained(root, relative):
    path = Path(root) / relative_path(relative)
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise UpdateError('external_package_path')
    current = path
    while current != Path(root):
        if current.exists() and (current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 1024):
            raise UpdateError('linked_package_path')
        current = current.parent
    return path

def preserved(relative):
    parts = PurePosixPath(relative).parts
    if parts[0].lower() in PRESERVED or parts[0].lower() == 'runs':
        return True
    return relative.startswith('backend/runs/') and (not relative.startswith('backend/runs/support-card-library/'))

def version_parts(value):
    match = re.fullmatch('(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)(?:-([0-9A-Za-z.-]+))?', str(value))
    if not match:
        raise UpdateError('invalid_version')
    prerelease = match[4].split('.') if match[4] else None
    if prerelease and any((not item or (item.isdigit() and len(item) > 1 and (item[0] == '0')) for item in prerelease)):
        raise UpdateError('invalid_version')
    return (tuple(map(int, match.group(1, 2, 3))), prerelease)

def newer(candidate, installed):
    left, lp = version_parts(candidate)
    right, rp = version_parts(installed)
    if left != right:
        return left > right
    if lp is None or rp is None:
        return lp is None and rp is not None
    for a, b in zip(lp, rp):
        if a == b:
            continue
        if a.isdigit() and b.isdigit():
            return int(a) > int(b)
        if a.isdigit() != b.isdigit():
            return not a.isdigit()
        return a > b
    return len(lp) > len(rp)

def validate_manifest(manifest):
    if manifest.get('schema_version') not in {'umaa.public_package.v1', 'uma.product_package.v1'}:
        raise UpdateError('invalid_package_manifest')
    if manifest.get('product_id') not in PRODUCTS:
        raise UpdateError('wrong_product')
    version_parts(manifest.get('version'))
    files = manifest.get('files')
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise UpdateError('invalid_file_inventory')
    checked = [relative_path(path) for path in files]
    folded = {path.casefold() for path in checked}
    if len(folded) != len(checked):
        raise UpdateError('duplicate_package_path')
    if any(('/'.join(path.split('/')[:index]).casefold() in folded for path in checked for index in range(1, len(path.split('/'))))):
        raise UpdateError('file_directory_collision')
    if any((path in MANIFESTS or path.split('/')[0].casefold() == 'updates' for path in checked)):
        raise UpdateError('reserved_package_path')
    if relative_path(manifest.get('entrypoint')) not in checked:
        raise UpdateError('entrypoint_missing')
    return manifest

def installation(root):
    root = Path(root).resolve(strict=True)
    names = [name for name in MANIFESTS if (root / name).is_file()]
    if len(names) != 1:
        raise UpdateError('installation_manifest_missing_or_ambiguous')
    manifest = validate_manifest(read_json(contained(root, names[0])))
    return (root, names[0], manifest)

def https_url(value):
    parsed = urlsplit(str(value))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise UpdateError('https_update_source_required')
    return str(value)

class HttpsRedirects(HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def remote_stream(url):
    request = Request(https_url(url), headers={'User-Agent': 'UmaSuite-Update/1'})
    response = build_opener(HttpsRedirects()).open(request, timeout=20)
    https_url(response.url)
    return response

def load_feed(root, manifest, feed_file=None):
    if feed_file:
        source = Path(feed_file).resolve(strict=True)
        feed = read_json(source)
        channel = feed.get('channel')
    else:
        source = None
        config_path = contained(root, 'config/update-source.json')
        config = read_json(config_path) if config_path.exists() else {}
        if not config.get('feed_url'):
            return (None, None)
        if config.get('product_id') != manifest['product_id']:
            raise UpdateError('wrong_product')
        channel = config.get('channel', 'stable')
        with remote_stream(config['feed_url']) as response:
            raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise UpdateError('feed_too_large')
        feed = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(feed, dict) or feed.get('schema_version') != 'uma.update_feed.v1':
        raise UpdateError('invalid_update_feed')
    if feed.get('product_id') != manifest['product_id']:
        raise UpdateError('wrong_product')
    if channel not in CHANNELS or feed.get('channel') != channel:
        raise UpdateError('wrong_update_channel')
    if feed.get('platform') != 'windows-x64':
        raise UpdateError('wrong_update_platform')
    _, pre = version_parts(feed.get('version'))
    if channel == 'stable' and pre is not None:
        raise UpdateError('prerelease_in_stable_channel')
    package = feed.get('package')
    if not isinstance(package, dict) or not re.fullmatch('[0-9a-f]{64}', str(package.get('sha256'))):
        raise UpdateError('invalid_package_digest')
    if type(package.get('size_bytes')) is not int or not 0 < package['size_bytes'] <= MAX_ARCHIVE:
        raise UpdateError('invalid_package_size')
    if source is None:
        https_url(package.get('url'))
    else:
        relative_path(package.get('file'))
    if not isinstance(feed.get('notes', ''), str) or len(feed.get('notes', '')) > 10000:
        raise UpdateError('invalid_release_notes')
    return (feed, source)

def result(status, **fields):
    return dict(ok=True, status=status, message=MESSAGES.get(status, status), **fields)

def check_update(root, *, feed_file=None):
    root, _, installed = installation(root)
    feed, _ = load_feed(root, installed, feed_file)
    if feed is None:
        return result('not_configured', version=installed['version'])
    status = 'update_available' if newer(feed['version'], installed['version']) else 'current'
    return result(status, version=feed['version'], current_version=installed['version'], notes=feed.get('notes', ''))

def download_package(feed, source, target):
    package = feed['package']
    stream = contained(source.parent, package['file']).open('rb') if source else remote_stream(package['url'])
    digest, size = (hashlib.sha256(), 0)
    with stream, target.open('xb') as output:
        while (block := stream.read(1024 * 1024)):
            size += len(block)
            if size > package['size_bytes']:
                raise UpdateError('package_size_mismatch')
            output.write(block)
            digest.update(block)
    if size != package['size_bytes'] or digest.hexdigest() != package['sha256']:
        raise UpdateError('package_integrity_failed')

def inspect_archive(archive_path, expected, *, local_test=False):
    with ZipFile(archive_path) as archive:
        rows = archive.infolist()
        if len(rows) > MAX_FILES or sum((row.file_size for row in rows)) > MAX_UNPACKED:
            raise UpdateError('package_too_large')
        files, folded = ({}, set())
        for row in rows:
            name = relative_path(row.filename.rstrip('/'))
            if name.casefold() in folded or stat.S_ISLNK(row.external_attr >> 16):
                raise UpdateError('duplicate_or_linked_archive_path')
            folded.add(name.casefold())
            if not row.is_dir():
                files[name] = row
        names = [name for name in MANIFESTS if name in files]
        if len(names) != 1 or files[names[0]].file_size > 4 * 1024 ** 2:
            raise UpdateError('package_manifest_missing_or_ambiguous')
        manifest = validate_manifest(json.loads(archive.read(names[0]).decode('utf-8-sig')))
        if manifest['product_id'] != expected['product_id'] or manifest['version'] != expected['version']:
            raise UpdateError('package_identity_mismatch')
        if not local_test and (manifest.get('publication_ready') is not True or manifest.get('release_holds')):
            raise UpdateError('candidate_not_released')
        if set(files) != set(manifest['files']) | {names[0]}:
            raise UpdateError('package_inventory_mismatch')
    return (names[0], manifest)

def make_helper(root, stage):
    runtime = contained(root, 'runtime')
    helper = stage / 'helper'
    helper.mkdir()
    for source in runtime.iterdir():
        if source.is_file() and source.suffix.lower() in {'.exe', '.dll', '.pyd', '.zip', '._pth'}:
            shutil.copy2(source, helper / source.name)
    if not (helper / 'python.exe').is_file():
        raise UpdateError('embedded_runtime_missing')
    for pth in helper.glob('python*._pth'):
        pth.write_text(pth.stem + '.zip\n.\nimport site\n', encoding='utf8')
    script = helper / 'product_update.py'
    shutil.copy2(Path(__file__), script)
    (helper / 'recover.cmd').write_text('@echo off\r\nsetlocal DisableDelayedExpansion\r\nset "TEMP=%~dp0temp"\r\nset "TMP=%TEMP%"\r\nif not exist "%TEMP%" mkdir "%TEMP%"\r\n"%~dp0python.exe" -B -X utf8 "%~dp0product_update.py" --root "%~dp0..\\..\\..\\.." rollback --ticket "%~dp0..\\ticket.json" --restart\r\nif errorlevel 1 pause\r\n', encoding='ascii', newline='')
    return (str(helper / 'python.exe'), str(script))

def prepare_update(root, *, feed_file=None, build_helper=True):
    root, name, installed = installation(root)
    feed, source = load_feed(root, installed, feed_file)
    if feed is None:
        return result('not_configured', version=installed['version'])
    if not newer(feed['version'], installed['version']):
        return result('current', version=installed['version'])
    stage = contained(root, 'updates/staged/' + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    archive = stage / 'package.zip'
    download_package(feed, source, archive)
    new_name, _ = inspect_archive(archive, feed, local_test=source is not None)
    if name != new_name:
        raise UpdateError('manifest_name_changed')
    ticket = dict(schema_version='uma.prepared_update.v1', product_id=installed['product_id'], from_version=installed['version'], version=feed['version'], manifest=name, sha256=feed['package']['sha256'], size_bytes=feed['package']['size_bytes'], local_test=source is not None)
    write_json(stage / 'ticket.json', ticket)
    executable, script = make_helper(root, stage) if build_helper else ('', '')
    return result('prepared', version=feed['version'], notes=feed.get('notes', ''), ticket=str(stage / 'ticket.json'), helper_executable=executable, helper_script=script)

@contextmanager
def installation_lock(root):
    path = contained(root, 'updates/application.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = path.open('a+b')
    except OSError:
        raise UpdateError('application_running') from None
    with stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UpdateError('application_running') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

def process_running(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(1048576, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return False
        raise UpdateError('process_status_unreadable')
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)

def wait_for_exit(pid, timeout=90):
    deadline = time.monotonic() + timeout
    while process_running(pid):
        if time.monotonic() >= deadline:
            raise UpdateError('application_running')
        time.sleep(0.25)

def assert_device_idle(root):
    connection = contained(root, 'config/public-connection.json')
    if not connection.exists():
        return
    endpoint = read_json(connection).get('endpoint', '')
    if not re.fullmatch('127\\.0\\.0\\.1:\\d{1,5}', endpoint):
        raise UpdateError('device_lock_unknown')
    base = os.environ.get('UMA_PUBLIC_LOCK_ROOT') or os.environ.get('LOCALAPPDATA')
    if not base:
        raise UpdateError('device_lock_unknown')
    lock = Path(base) / 'UmaSuite/device-locks' / (endpoint.replace(':', '_') + '.lock')
    if not lock.exists():
        return
    try:
        with lock.open('r+b') as stream:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        raise UpdateError('device_busy') from None

def ticket_data(root, ticket_path):
    ticket_path = Path(ticket_path).resolve(strict=True)
    if ticket_path.name != 'ticket.json' or ticket_path.parent.parent != root / 'updates/staged' or (not re.fullmatch('[0-9a-f]{32}', ticket_path.parent.name)):
        raise UpdateError('invalid_update_ticket_path')
    contained(root, ticket_path.relative_to(root).as_posix())
    ticket = read_json(ticket_path)
    if ticket.get('schema_version') != 'uma.prepared_update.v1':
        raise UpdateError('invalid_update_ticket')
    return (ticket_path.parent, ticket)

def transaction_io_path(path):
    """Extended Windows names only after normal owned-path validation.

    Deep backup/incoming paths add a UUID and several directories to otherwise
    valid dependency names. Keep root/ticket identities normal for the GUI and
    journal; only file I/O needs the long-path spelling.
    """
    path = Path(path)
    if os.name != 'nt':
        return path
    value = str(path.absolute())
    if not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)

def restore_transaction(root, stage, journal):
    for item in reversed(journal['operations']):
        relative = relative_path(item['path'])
        if preserved(relative):
            raise UpdateError('invalid_rollback_path')
        target = contained(root, relative)
        backup = transaction_io_path(contained(stage / 'backup', relative))
        if item['existed']:
            if not backup.is_file():
                raise UpdateError('rollback_backup_missing')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        elif target.is_file():
            retired = transaction_io_path(contained(stage / 'retired', relative))
            retired.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, retired)
    journal['phase'] = 'rolled_back'
    write_json(stage / 'journal.json', journal)
    write_json(root / 'updates/state.json', dict(phase='rolled_back', ticket=str(stage / 'ticket.json')))

def apply_update(root, ticket_path, *, parent_pid=None, wait_timeout=90):
    root, name, installed = installation(root)
    stage, ticket = ticket_data(root, ticket_path)
    if parent_pid is not None:
        wait_for_exit(parent_pid, wait_timeout)
    with installation_lock(root):
        assert_device_idle(root)
        state_path = root / 'updates/state.json'
        state = read_json(state_path) if state_path.exists() else {}
        if state.get('phase') in {'applying', 'rollback_failed'}:
            raise UpdateError('rollback_required')
        if ticket.get('product_id') != installed['product_id'] or ticket.get('from_version') != installed['version']:
            raise UpdateError('stale_or_wrong_product_ticket')
        if not newer(ticket.get('version'), installed['version']):
            raise UpdateError('update_is_not_newer')
        archive_path = stage / 'package.zip'
        if archive_path.stat().st_size != ticket['size_bytes']:
            raise UpdateError('package_integrity_failed')
        with archive_path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != ticket['sha256']:
                raise UpdateError('package_integrity_failed')
        new_name, new = inspect_archive(archive_path, ticket, local_test=ticket.get('local_test') is True)
        if name != new_name or ticket.get('manifest') != name:
            raise UpdateError('manifest_name_changed')
        existing_managed = set(installed['files']) | {name}
        new_managed = set(new['files']) | {name}
        changes = sorted((existing_managed | new_managed) - {name}) + [name]
        journal = dict(schema_version='uma.update_journal.v1', phase='preparing', product_id=installed['product_id'], from_version=installed['version'], version=new['version'], operations=[])
        for relative in changes:
            if preserved(relative):
                continue
            target = contained(root, relative)
            if target.exists() and relative not in existing_managed:
                raise UpdateError('unmanaged_file_collision')
            if target.exists() and (not target.is_file()):
                raise UpdateError('managed_path_not_file')
            item = dict(path=relative, existed=target.is_file(), new=relative in new_managed)
            if item['existed']:
                backup = transaction_io_path(contained(stage / 'backup', relative))
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            journal['operations'].append(item)
        journal['phase'] = 'applying'
        write_json(stage / 'journal.json', journal)
        write_json(state_path, dict(phase='applying', ticket=str(stage / 'ticket.json')))
        try:
            with ZipFile(archive_path) as archive:
                for item in journal['operations']:
                    relative = item['path']
                    target = contained(root, relative)
                    if item['new']:
                        temporary = transaction_io_path(contained(stage / 'incoming', relative))
                        temporary.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(relative) as stream, temporary.open('wb') as output:
                            shutil.copyfileobj(stream, output)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(temporary, target)
                    elif target.exists():
                        retired = transaction_io_path(contained(stage / 'retired', relative))
                        retired.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(target, retired)
            journal['phase'] = 'installed'
            write_json(stage / 'journal.json', journal)
            write_json(state_path, dict(phase='installed', version=new['version'], ticket=str(stage / 'ticket.json')))
        except Exception:
            try:
                restore_transaction(root, stage, journal)
            except Exception:
                write_json(state_path, dict(phase='rollback_failed', ticket=str(stage / 'ticket.json')))
                raise UpdateError('rollback_required') from None
            raise UpdateError('install_failed_rolled_back') from None
    return result('installed', version=new['version'], entrypoint=new['entrypoint'])

def rollback_update(root, ticket_path, *, parent_pid=None):
    root = Path(root).resolve(strict=True)
    if parent_pid is not None:
        wait_for_exit(parent_pid)
    stage, ticket = ticket_data(root, ticket_path)
    with installation_lock(root):
        assert_device_idle(root)
        journal = read_json(stage / 'journal.json')
        state = read_json(root / 'updates/state.json')
        if Path(state.get('ticket', '')).resolve() != (stage / 'ticket.json').resolve() or journal.get('product_id') != ticket.get('product_id'):
            raise UpdateError('stale_rollback_ticket')
        restore_transaction(root, stage, journal)
    return result('rolled_back', version=ticket['from_version'])

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('action', choices=('check', 'prepare', 'apply', 'rollback'))
    parser.add_argument('--feed-file', type=Path, help='Explicit offline rehearsal only; never used by the GUI.')
    parser.add_argument('--ticket', type=Path)
    parser.add_argument('--wait-pid', type=int)
    parser.add_argument('--restart', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'check':
            response = check_update(args.root, feed_file=args.feed_file)
        elif args.action == 'prepare':
            response = prepare_update(args.root, feed_file=args.feed_file)
        else:
            if not args.ticket or (args.action == 'apply' and (not args.wait_pid)) or (args.wait_pid is not None and args.wait_pid < 1):
                raise UpdateError('gui_process_id_required')
            if args.action == 'rollback':
                response = rollback_update(args.root, args.ticket, parent_pid=args.wait_pid)
            else:
                response = apply_update(args.root, args.ticket, parent_pid=args.wait_pid)
            if args.restart:
                root, _, manifest = installation(args.root)
                environment = os.environ.copy()
                environment['DOTNET_ROOT'] = str(root / 'dotnet')
                subprocess.Popen([str(contained(root, manifest['entrypoint']))], cwd=root, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception as error:
        code = str(error) if isinstance(error, UpdateError) else type(error).__name__
        response = dict(ok=False, status='error', code=code, message=MESSAGES.get(code, '更新未完成，原配置和记录已保留。'))
    print(json.dumps(response, ensure_ascii=False))
    if args.action in {'apply', 'rollback'}:
        write_json(args.root / 'updates/last-result.json', response)
    return 0 if response['ok'] else 2
if __name__ == '__main__':
    raise SystemExit(main())
