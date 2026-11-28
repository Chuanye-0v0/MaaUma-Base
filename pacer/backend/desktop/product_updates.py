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
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid
from zipfile import ZipFile

PRODUCTS = {'umaa-public', 'uma-cut', 'uma-career'}
MANIFESTS = ('public-package-manifest.json', 'product-package-manifest.json')
CHANNELS = {'stable', 'preview'}
MAX_ARCHIVE = 2 * 1024**3
MAX_UNPACKED = 5 * 1024**3
MAX_FILES = 30000
PRESERVED = {'config', 'logs', 'exports', 'review_exports', 'temp', 'updates'}
PRESERVED_FILES = {'portable_config.json', 'my-push-token.txt'}
MESSAGES = {
    'not_configured': '尚未配置此产品的更新地址。',
    'current': '当前已是所选通道的最新版本。',
    'update_available': '发现新版本，可以下载准备更新。',
    'prepared': '更新已准备好，请在任务结束后安装。',
    'installed': '更新完成，用户配置和运行记录已保留。',
    'rolled_back': '已恢复更新前的程序文件。',
    'application_running': '界面或更新程序仍在运行，请稍后重试。',
    'device_busy': '任务尚未退出，已保留下载包并停止安装。',
    'rollback_required': '上次更新未完成，需要先恢复旧版本。',
    'install_failed_rolled_back': '安装失败，已恢复旧版本；下载包和诊断记录已保留。',
    'candidate_not_released': '此更新包尚未通过发布准备检查，已停止安装。',
    'package_integrity_failed': '下载包不完整或已被改动，请重新下载。',
    'wrong_product': '更新包属于另一个产品，已停止更新。',
    'activation_required': '请先在更新页激活邀请码。',
    'access_revoked': '此邀请码的访问权限已停用，请联系维护者。',
    'release_not_available': '当前通道尚未发布可用更新。',
    'backend_running': '后台尚未退出，已停止安装；请稍后重试。',
    'download_timeout': '下载等待超过两小时，已停止等待；当前程序未替换，可检查网络后重试或使用维护者提供的小更新包。',
    'download_stalled': '下载连接长时间没有响应，已停止等待；当前程序未替换，可以重试。',
    'unmanaged_file_collision': '更新包与未登记的本地文件冲突，尚未替换程序；请把错误码交给维护者。',
    'service_origin_mismatch': '服务地址与激活信息不一致，请重新激活。',
    'device_binding_required': '授权需要绑定本机，请在更新页重新激活原邀请码。',
    'device_already_bound': '此邀请码已绑定另一台电脑，请联系维护者解绑换机。',
    'device_key_mismatch': '本机授权密钥与绑定记录不同，请联系维护者解绑后重新激活。',
    'device_signature_invalid': '本机授权验证失败，请重新激活或联系维护者。',
    'device_clock_skew': '请同步 Windows 时间后重试授权。',
    'device_request_replayed': '授权请求已过期或重复，请重试。',
    'device_identity_unavailable': '无法读取本机授权组件，请安装完整更新包后重试。',
    'device_credentials_unreadable': '授权无法在当前电脑或 Windows 用户解密，请重新激活；换机需先解绑。',
    'device_key_unreadable': '本机授权密钥无法解密，请联系维护者解绑后重新激活。',
    'device_changed': '授权来自另一台电脑，请联系维护者解绑后重新激活。',
    'service_redirect_rejected': '授权服务返回了不支持的跳转，请检查服务地址或联系维护者。',
    'service_unavailable': '暂时无法连接授权服务，请检查网络后重试。',
}


class UpdateError(RuntimeError):
    pass


def _native_path(path):
    """Use Windows long-path I/O without changing public paths or system settings."""
    path=Path(path)
    if os.name!='nt':return path
    absolute=str(path.absolute())
    if len(absolute)<=240 or absolute.startswith('\\\\?\\'):return path
    return Path('\\\\?\\UNC\\'+absolute[2:] if absolute.startswith('\\\\') else '\\\\?\\'+absolute)


def read_json(path):
    with _native_path(path).open(encoding='utf-8-sig') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise UpdateError('invalid_json_object')
    return value


def write_json(path, value):
    path = _native_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf8')
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(raw)
    try:
        os.replace(temporary, path)
    except PermissionError:
        if not (path.is_file() and not path.is_symlink()):
            raise
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        with path.open('r+b') as stream:
            stream.write(raw)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != raw:
            raise
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise UpdateError('unsafe_package_path')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {'.', '..'} for part in path.parts):
        raise UpdateError('unsafe_package_path')
    for part in path.parts:
        if part.endswith(('.', ' ')) or re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', part):
            raise UpdateError('unsafe_package_path')
    return value


def contained(root, relative):
    path = Path(root) / relative_path(relative)
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise UpdateError('external_package_path')
    current = path
    while current != Path(root):
        probe=_native_path(current)
        if probe.exists() and (probe.is_symlink() or
                getattr(probe.lstat(), 'st_file_attributes', 0) & 0x400):
            raise UpdateError('linked_package_path')
        current = current.parent
    return path


def preserved(relative):
    parts = PurePosixPath(relative).parts
    if relative.casefold() in PRESERVED_FILES or parts[0].lower() in PRESERVED or parts[0].lower() == 'runs':
        return True
    return relative.startswith('backend/runs/') and not relative.startswith('backend/runs/support-card-library/')


def version_parts(value):
    match = re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?', str(value))
    if not match:
        raise UpdateError('invalid_version')
    prerelease = match[4].split('.') if match[4] else None
    if prerelease and any(not item or item.isdigit() and len(item) > 1 and item[0] == '0' for item in prerelease):
        raise UpdateError('invalid_version')
    return tuple(map(int, match.group(1, 2, 3))), prerelease


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


def manifest_channel(manifest):
    declared=manifest.get('release_channel')
    if declared is not None:return declared if declared in ('stable','preview') else None
    return {'private-preview':'preview','private-stable':'stable'}.get(str(manifest.get('distribution')))


def channel_switch(installed,candidate):
    """Explicit private channel selection can replace a newer test build."""
    before=manifest_channel(installed);after=candidate.get('channel')
    return (installed.get('product_id') in {'uma-cut','uma-career'}
            and before in ('stable','preview') and after in ('stable','preview') and before!=after)


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
    if any('/'.join(path.split('/')[:index]).casefold() in folded
           for path in checked for index in range(1, len(path.split('/')))):
        raise UpdateError('file_directory_collision')
    if any(path in MANIFESTS or path.split('/')[0].casefold() == 'updates' for path in checked):
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
    return root, names[0], manifest


def https_url(value):
    parsed = urlsplit(str(value))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise UpdateError('https_update_source_required')
    return str(value)


def service_base(value):
    """HTTPS remotely; exact loopback HTTP is reserved for local rehearsals."""
    value=str(value).strip().rstrip('/')
    parsed=urlsplit(value)
    local=parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost','::1'}
    if not (local or parsed.scheme=='https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise UpdateError('invalid_service_address')
    return value


def service_auth(root, url, product=None):
    from desktop import cut_support_client as service
    config=service.config(root)
    base=config['base_url']
    try:service.api_path(root,url)
    except service.SupportError:raise UpdateError('service_origin_mismatch') from None
    # Import lazily: the support client also uses the shared safe path/network
    # helpers in this module. Only private feeds require Windows identity.
    from desktop.cut_support_client import credentials,SupportError
    from desktop.device_identity import signed_headers,DeviceIdentityError
    try:
        auth=credentials(root,{'base_url':base},product=product or 'uma-cut')
        parsed=urlsplit(url)
        headers=signed_headers(root,'GET',parsed.path+('?' + parsed.query if parsed.query else ''),b'',auth['access_token'])
    except (SupportError,DeviceIdentityError) as error:raise UpdateError(str(error)) from None
    return {'authorization':'Bearer '+auth['access_token'],
            'device_headers':headers,
            'allow_loopback':urlsplit(base).scheme=='http',
            'allow_private_preview':config.get('channel')=='preview' and auth.get('allow_preview') is True}


class HttpsRedirects(HTTPRedirectHandler):
    def __init__(self,allow_loopback=False):
        super().__init__();self.allow_loopback=allow_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if any(key.casefold()=='x-maauma-signature' for key,_ in req.header_items()):
            # The signature is bound to the exact path/query and must never be
            # forwarded or reused, even on a same-origin redirect.
            raise UpdateError('service_redirect_rejected')
        local=self.allow_loopback and urlsplit(newurl).scheme=='http' and urlsplit(newurl).hostname in {'127.0.0.1','localhost','::1'}
        if not local:https_url(newurl)
        redirected=super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(req.full_url)[:2]!=urlsplit(newurl)[:2]:
            redirected.remove_header('Authorization')
        return redirected


def remote_stream(url, *, authorization=None, allow_loopback=False,device_headers=None):
    local=allow_loopback and urlsplit(url).scheme=='http' and urlsplit(url).hostname in {'127.0.0.1','localhost','::1'}
    if not local:https_url(url)
    headers={'User-Agent':'MaaUma-Update/1'}
    if authorization:headers['Authorization']=authorization
    if device_headers:headers.update(device_headers)
    request = Request(url, headers=headers)
    try:response = build_opener(HttpsRedirects(allow_loopback)).open(request, timeout=30)
    except HTTPError as error:
        if authorization:
            try:code=json.loads(error.read(8192)).get('code')
            except (ValueError,OSError):code=None
            if code in MESSAGES:
                raise UpdateError(code) from None
        raise
    if not (local and urlsplit(response.url)[:2]==urlsplit(url)[:2]):https_url(response.url)
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
            return None, None
        if config.get('product_id') != manifest['product_id']:
            raise UpdateError('wrong_product')
        channel = config.get('channel', 'stable')
        context=service_auth(root,config['feed_url'],manifest['product_id']) if config.get('private_service') is True else {}
        with remote_stream(config['feed_url'],**{key:context[key] for key in ('authorization','allow_loopback','device_headers') if key in context}) as response:
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
        if config.get('private_service') is True:
            service_auth(root,package.get('url',''),manifest['product_id'])
        else:https_url(package.get('url'))
    else:
        relative_path(package.get('file'))
    if not isinstance(feed.get('notes', ''), str) or len(feed.get('notes', '')) > 10000:
        raise UpdateError('invalid_release_notes')
    return feed, source


def result(status, **fields):
    return dict(ok=True, status=status, message=MESSAGES.get(status, status), **fields)


def check_update(root, *, feed_file=None):
    root, _, installed = installation(root)
    try:feed, _ = load_feed(root, installed, feed_file)
    except UpdateError as error:
        if str(error)=='release_not_available':return result('release_not_available',version=installed['version'])
        raise
    if feed is None:
        return result('not_configured', version=installed['version'])
    switch=channel_switch(installed,feed)
    status = 'update_available' if newer(feed['version'], installed['version']) or switch else 'current'
    return result(status, version=feed['version'], current_version=installed['version'], notes=feed.get('notes', ''),channel_switch=switch)


def _progress(callback, stage, **fields):
    if callback is not None:callback(dict(event='update_progress',stage=stage,**fields))


def download_package(feed, source, target, *, context=None, progress=None, timeout_seconds=7200):
    package = feed['package']
    context=context or {}
    started=last_report=time.monotonic();created=False
    _progress(progress,'connecting',downloaded_bytes=0,total_bytes=package['size_bytes'])
    try:
        stream = _native_path(contained(source.parent, package['file'])).open('rb') if source else remote_stream(package['url'],**{key:context[key] for key in ('authorization','allow_loopback','device_headers') if key in context})
        digest, size = hashlib.sha256(), 0
        with stream, _native_path(target).open('xb') as output:
            created=True
            # HTTPResponse.read(n) may wait to fill n while tiny packets keep
            # resetting the socket timeout. read1 returns available payload so
            # the total deadline and progress are checked between socket reads.
            read=getattr(stream,'read1',stream.read)
            while True:
                if time.monotonic()-started>=timeout_seconds:raise UpdateError('download_timeout')
                block=read(64*1024)
                now=time.monotonic()
                if now-started>=timeout_seconds:raise UpdateError('download_timeout')
                if not block:break
                size += len(block)
                if size > package['size_bytes']:raise UpdateError('package_size_mismatch')
                output.write(block);digest.update(block)
                if now-last_report>=.25 or size==package['size_bytes']:
                    elapsed=max(.001,now-started)
                    _progress(progress,'downloading',downloaded_bytes=size,total_bytes=package['size_bytes'],
                              elapsed_seconds=round(elapsed,1),bytes_per_second=round(size/elapsed))
                    last_report=now
        if size != package['size_bytes'] or digest.hexdigest() != package['sha256']:
            raise UpdateError('package_integrity_failed')
    except Exception as error:
        if created:
            try:_native_path(target).unlink()
            except OSError:pass
        if isinstance(error,TimeoutError):raise UpdateError('download_stalled') from None
        raise


def inspect_archive(archive_path, expected, *, local_test=False, allow_private_preview=False):
    with ZipFile(_native_path(archive_path)) as archive:
        rows = archive.infolist()
        if len(rows) > MAX_FILES or sum(row.file_size for row in rows) > MAX_UNPACKED:
            raise UpdateError('package_too_large')
        files, folded = {}, set()
        for row in rows:
            name = relative_path(row.filename.rstrip('/'))
            if name.casefold() in folded or stat.S_ISLNK(row.external_attr >> 16):
                raise UpdateError('duplicate_or_linked_archive_path')
            folded.add(name.casefold())
            if not row.is_dir():
                files[name] = row
        names = [name for name in MANIFESTS if name in files]
        if len(names) != 1 or files[names[0]].file_size > 4 * 1024**2:
            raise UpdateError('package_manifest_missing_or_ambiguous')
        manifest = validate_manifest(json.loads(archive.read(names[0]).decode('utf-8-sig')))
        if manifest['product_id'] != expected['product_id'] or manifest['version'] != expected['version']:
            raise UpdateError('package_identity_mismatch')
        if manifest.get('release_channel') is not None and manifest['release_channel']!=expected.get('channel'):
            raise UpdateError('package_channel_mismatch')
        private_preview=(allow_private_preview and expected.get('channel')=='preview'
                         and manifest.get('product_id') in {'uma-cut','uma-career'}
                         and manifest.get('distribution')=='private-preview')
        if not local_test and not private_preview and (manifest.get('publication_ready') is not True or manifest.get('release_holds')):
            raise UpdateError('candidate_not_released')
        if set(files) != set(manifest['files']) | {names[0]}:
            raise UpdateError('package_inventory_mismatch')
    return names[0], manifest


def make_helper(root, stage):
    runtime = _native_path(contained(root, 'runtime'))
    helper = stage / 'helper'
    _native_path(helper).mkdir()
    for source in runtime.iterdir():
        if source.is_file() and source.suffix.lower() in {'.exe', '.dll', '.pyd', '.zip', '._pth'}:
            shutil.copy2(source, _native_path(helper / source.name))
    if not _native_path(helper / 'python.exe').is_file():
        raise UpdateError('embedded_runtime_missing')
    for pth in _native_path(helper).glob('python*._pth'):
        pth.write_text(pth.stem + '.zip\n.\nimport site\n', encoding='utf8')
    script = helper / 'product_update.py'
    shutil.copy2(Path(__file__), _native_path(script))
    _native_path(helper / 'recover.cmd').write_text(
        '@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
        'set "TEMP=%~dp0temp"\r\nset "TMP=%TEMP%"\r\n'
        'if not exist "%TEMP%" mkdir "%TEMP%"\r\n'
        '"%~dp0python.exe" -B -X utf8 "%~dp0product_update.py" '
        '--root "%~dp0..\\..\\..\\.." rollback --ticket "%~dp0..\\ticket.json" --restart\r\n'
        'if errorlevel 1 pause\r\n', encoding='ascii', newline='')
    return str(helper / 'python.exe'), str(script)


def cleanup_download(root, download_id, owner_pid, *, owner_exited=True):
    """Remove only one identified download; prepared/installation data is untouched."""
    outcome = dict(removed=False, status='download_preserved')
    if (not isinstance(download_id, str) or not re.fullmatch('[0-9a-f]{32}', download_id)
            or type(owner_pid) is not int or owner_pid < 1):
        return outcome
    try:
        root = Path(root).resolve(strict=True)
        stage = contained(root, 'updates/staged/' + download_id)
        if not _native_path(stage).exists():
            return dict(removed=False, status='download_absent')
        marker = read_json(contained(stage, 'download.json'))
        if (marker.get('schema_version') != 'uma.update_download.v1'
                or marker.get('download_id') != download_id or marker.get('owner_pid') != owner_pid):
            return outcome
        # The GUI invokes this only after its child exits. Recheck in the
        # cleanup process as well, so a live transfer is never removed.
        if owner_exited and process_running(owner_pid):
            return dict(removed=False, status='download_active')
        if not owner_exited and owner_pid != os.getpid():
            return outcome
        entries = list(_native_path(stage).iterdir())
        allowed = {'download.json', 'download.json.tmp', 'package.zip'}
        # A ticket, helper, journal, or any unrecognized file means the stage
        # has progressed beyond a disposable download (or is not ours).
        if any(entry.name not in allowed for entry in entries):
            return outcome
        for entry in entries:
            info = entry.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                return outcome
        removed_bytes = sum(entry.stat().st_size for entry in entries)
        # Leave the ownership record until last, allowing a later retry if
        # Windows temporarily holds the archive open.
        for entry in sorted(entries, key=lambda item: item.name == 'download.json'):
            entry.unlink()
        _native_path(stage).rmdir()
        return dict(removed=True, status='download_removed', removed_bytes=removed_bytes)
    except (OSError, ValueError, UpdateError):
        return outcome


def prepare_update(root, *, feed_file=None, build_helper=True, progress=None, download_id=None):
    root, name, installed = installation(root)
    _progress(progress,'checking')
    feed, source = load_feed(root, installed, feed_file)
    if feed is None:
        return result('not_configured', version=installed['version'])
    switch=channel_switch(installed,feed)
    if not newer(feed['version'], installed['version']) and not switch:
        return result('current', version=installed['version'])
    download_id = download_id if download_id is not None else uuid.uuid4().hex
    if not isinstance(download_id, str) or not re.fullmatch('[0-9a-f]{32}', download_id):
        raise UpdateError('invalid_download_id')
    stage = contained(root, 'updates/staged/' + download_id)
    _native_path(stage).mkdir(parents=True)
    write_json(stage / 'download.json', dict(schema_version='uma.update_download.v1',
               download_id=download_id, owner_pid=os.getpid()))
    try:
        archive = stage / 'package.zip'
        config_path=root/'config/update-source.json'
        config=read_json(config_path) if config_path.exists() else {}
        context=service_auth(root,feed['package']['url'],installed['product_id']) if source is None and config.get('private_service') is True else {}
        download_package(feed, source, archive,context=context,progress=progress)
        _progress(progress,'verifying')
        new_name, new = inspect_archive(archive, feed, local_test=source is not None,allow_private_preview=context.get('allow_private_preview') is True)
        if switch and new.get('release_channel')!=feed['channel']:raise UpdateError('package_channel_mismatch')
        if name != new_name:
            raise UpdateError('manifest_name_changed')
        ticket = dict(schema_version='uma.prepared_update.v1', product_id=installed['product_id'],
                      from_version=installed['version'], version=feed['version'], manifest=name,
                      sha256=feed['package']['sha256'], size_bytes=feed['package']['size_bytes'],
                      local_test=source is not None,channel=feed['channel'],channel_switch=switch,
                      allow_private_preview=context.get('allow_private_preview') is True)
        write_json(stage / 'ticket.json', ticket)
        _progress(progress,'preparing')
        executable, script = make_helper(root, stage) if build_helper else ('', '')
        return result('prepared', version=feed['version'], notes=feed.get('notes', ''),channel_switch=switch,
                      ticket=str(stage / 'ticket.json'), helper_executable=executable, helper_script=script)
    finally:
        # Ordinary failures can clean themselves. Forced cancellation is
        # finalized by the GUI only after this process has exited.
        cleanup_download(root, download_id, os.getpid(), owner_exited=False)


@contextmanager
def installation_lock(root, *, lock_name='application.lock'):
    path = _native_path(contained(root, 'updates/' + lock_name))
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
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return False
        raise UpdateError('process_status_unreadable')
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel.CloseHandle(handle)


def wait_for_exit(pid, timeout=90):
    deadline = time.monotonic() + timeout
    while process_running(pid):
        if time.monotonic() >= deadline:
            raise UpdateError('application_running')
        time.sleep(.25)


def assert_device_idle(root):
    connection = contained(root, 'config/public-connection.json')
    if not connection.exists():
        return
    endpoint = read_json(connection).get('endpoint', '')
    if not re.fullmatch(r'127\.0\.0\.1:\d{1,5}', endpoint):
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


@contextmanager
def _backend_lease(root, exclusive):
    path=_native_path(contained(Path(root).resolve(),'updates/backend.lock'));path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                class Overlapped(ctypes.Structure):
                    _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
                              ('Offset',ctypes.c_uint32),('OffsetHigh',ctypes.c_uint32),('hEvent',ctypes.c_void_p)]
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.LockFileEx.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_uint32,ctypes.POINTER(Overlapped)]
                kernel.UnlockFileEx.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_uint32,ctypes.POINTER(Overlapped)]
                handle=msvcrt.get_osfhandle(stream.fileno());overlapped=Overlapped()
                if not kernel.LockFileEx(handle,1|(2 if exclusive else 0),0,1,0,ctypes.byref(overlapped)):
                    raise OSError(ctypes.get_last_error(),'Backend files are in use')
            else:
                import fcntl
                fcntl.flock(stream.fileno(),(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
        except OSError:raise UpdateError('backend_running' if exclusive else 'application_running') from None
        try:yield
        finally:
            if os.name=='nt':kernel.UnlockFileEx(handle,0,1,0,ctypes.byref(overlapped))
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def backend_process_lease(root):
    return _backend_lease(root,False)


def ticket_data(root, ticket_path):
    ticket_path = Path(ticket_path).resolve(strict=True)
    if ticket_path.name != 'ticket.json' or ticket_path.parent.parent != root / 'updates/staged' or not re.fullmatch('[0-9a-f]{32}', ticket_path.parent.name):
        raise UpdateError('invalid_update_ticket_path')
    contained(root, ticket_path.relative_to(root).as_posix())
    ticket = read_json(ticket_path)
    if ticket.get('schema_version') != 'uma.prepared_update.v1':
        raise UpdateError('invalid_update_ticket')
    return ticket_path.parent, ticket


def _update_stage_size(stage):
    """Count only an ordinary, updater-owned tree; never follow reparse points."""
    total = 0
    pending = [_native_path(stage)]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise UpdateError('linked_package_path')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
                else:
                    total += info.st_size
    return total


def cleanup_staged_updates(root):
    """Retire superseded successful updates, preserving the current rollback.

    Also runs from the installed client's service page, so caches left by an
    older helper are reclaimed after upgrading to a client with this behavior.
    Cleanup is best effort and never changes the result of an installation.
    """
    outcome = dict(removed_stages=0, removed_bytes=0, skipped_stages=0, failed_stages=0)
    try:
        root, _, installed = installation(root)
        staged = contained(root, 'updates/staged')
        state_path = contained(root, 'updates/state.json')
        if not _native_path(staged).is_dir() or not state_path.is_file():
            return outcome
        # The shared backend lease excludes installers/rollback while allowing
        # the open panel and game backend to continue using installed files.
        with installation_lock(root, lock_name='cleanup.lock'), backend_process_lease(root):
            state = read_json(state_path)
            if state.get('phase') != 'installed' or state.get('version') != installed['version']:
                return outcome
            if not isinstance(state.get('ticket'), str):
                return outcome
            current, current_ticket = ticket_data(root, state['ticket'])
            current_journal = read_json(contained(current, 'journal.json'))
            if (current_journal.get('phase') != 'installed'
                    or current_journal.get('product_id') != installed['product_id']
                    or current_ticket.get('product_id') != installed['product_id']
                    or current_ticket.get('version') != installed['version']
                    or current_journal.get('version') != installed['version']):
                return outcome
            owned = {'ticket.json', 'journal.json', 'package.zip', 'download.json', 'helper', 'backup', 'incoming', 'retired'}
            for candidate in _native_path(staged).iterdir():
                if candidate.name == current.name or not re.fullmatch('[0-9a-f]{32}', candidate.name):
                    continue
                try:
                    stage = contained(root, 'updates/staged/' + candidate.name)
                    _, ticket = ticket_data(root, stage / 'ticket.json')
                    journal = read_json(contained(stage, 'journal.json'))
                    if (journal.get('schema_version') != 'uma.update_journal.v1'
                            or journal.get('phase') != 'installed'
                            or journal.get('product_id') != installed['product_id']
                            or ticket.get('product_id') != installed['product_id']
                            or journal.get('version') != ticket.get('version')
                            or journal.get('from_version') != ticket.get('from_version')
                            or any(item.name not in owned for item in _native_path(stage).iterdir())):
                        outcome['skipped_stages'] += 1
                        continue
                    size = _update_stage_size(stage)
                except (OSError, ValueError, UpdateError):
                    outcome['skipped_stages'] += 1
                    continue
                try:
                    shutil.rmtree(_native_path(stage))
                except OSError:
                    outcome['failed_stages'] += 1
                    continue
                outcome['removed_stages'] += 1
                outcome['removed_bytes'] += size
            if outcome['removed_stages'] or outcome['failed_stages']:
                write_json(root / 'updates/cleanup-result.json', outcome)
    except (OSError, ValueError, UpdateError):
        # An unavailable lock or unreadable state is not a failed update.
        pass
    return outcome


def restore_transaction(root, stage, journal):
    # Every destination and backup is derived from the owned file inventory.
    for item in reversed(journal['operations']):
        relative = relative_path(item['path'])
        if preserved(relative):
            raise UpdateError('invalid_rollback_path')
        target = _native_path(contained(root, relative))
        backup = _native_path(contained(stage / 'backup', relative))
        if item['existed']:
            if not backup.is_file():
                raise UpdateError('rollback_backup_missing')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        elif target.is_file():
            # Retain new files in the transaction rather than recursively deleting.
            retired = _native_path(contained(stage / 'retired', relative))
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
    with installation_lock(root), _backend_lease(root,True):
        assert_device_idle(root)
        state_path = root / 'updates/state.json'
        state = read_json(state_path) if state_path.exists() else {}
        if state.get('phase') in {'applying', 'rollback_failed'}:
            raise UpdateError('rollback_required')
        if ticket.get('product_id') != installed['product_id'] or ticket.get('from_version') != installed['version']:
            raise UpdateError('stale_or_wrong_product_ticket')
        switch=channel_switch(installed,ticket)
        if not newer(ticket.get('version'), installed['version']) and not switch:
            raise UpdateError('update_is_not_newer')
        archive_path = _native_path(stage / 'package.zip')
        if archive_path.stat().st_size != ticket['size_bytes']:
            raise UpdateError('package_integrity_failed')
        with archive_path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != ticket['sha256']:
                raise UpdateError('package_integrity_failed')
        new_name, new = inspect_archive(archive_path, ticket, local_test=ticket.get('local_test') is True,
                                         allow_private_preview=ticket.get('allow_private_preview') is True)
        if switch and new.get('release_channel')!=ticket['channel']:raise UpdateError('package_channel_mismatch')
        if name != new_name or ticket.get('manifest') != name:
            raise UpdateError('manifest_name_changed')
        existing_managed = set(installed['files']) | {name}
        new_managed = set(new['files']) | {name}
        changes = sorted((existing_managed | new_managed) - {name}) + [name]
        journal = dict(schema_version='uma.update_journal.v1', phase='preparing',
                       product_id=installed['product_id'], from_version=installed['version'],
                       version=new['version'], operations=[])
        adopted=[]
        for relative in changes:
            if preserved(relative):
                continue
            target = _native_path(contained(root, relative))
            collision=target.exists() and relative not in existing_managed
            if collision:
                # Cut installations can contain files from older manual fixes
                # or generated caches which are now shipped in the verified
                # package. Preserve their original bytes in this transaction's
                # backup, then bring them under the new program inventory.
                # User-data paths were skipped above; source receipts stay local.
                if installed['product_id']!='uma-cut' or relative.casefold() in {'distribution-receipt.json','distribution-notice.txt'}:
                    raise UpdateError('unmanaged_file_collision')
                adopted.append(relative)
            if target.exists() and not target.is_file():
                raise UpdateError('managed_path_not_file')
            item = dict(path=relative, existed=target.is_file(), new=relative in new_managed)
            if collision:item['adopted_unmanaged_program_file']=True
            if item['existed']:
                backup = _native_path(contained(stage / 'backup', relative))
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
                    target = _native_path(contained(root, relative))
                    if item['new']:
                        temporary = _native_path(contained(stage / 'incoming', relative))
                        temporary.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(relative) as stream, temporary.open('wb') as output:
                            shutil.copyfileobj(stream, output)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(temporary, target)
                    elif target.exists():
                        retired = _native_path(contained(stage / 'retired', relative))
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
    return result('installed', version=new['version'], entrypoint=new['entrypoint'],
                   adopted_program_files=adopted, cache_cleanup=cleanup_staged_updates(root))


def rollback_update(root, ticket_path, *, parent_pid=None):
    root = Path(root).resolve(strict=True)
    if parent_pid is not None:
        wait_for_exit(parent_pid)
    stage, ticket = ticket_data(root, ticket_path)
    with installation_lock(root), _backend_lease(root,True):
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
    parser.add_argument('action', choices=('check', 'prepare', 'cleanup-download', 'apply', 'rollback'))
    parser.add_argument('--feed-file', type=Path, help='Explicit offline rehearsal only; never used by the GUI.')
    parser.add_argument('--ticket', type=Path)
    parser.add_argument('--wait-pid', type=int)
    parser.add_argument('--restart', action='store_true')
    parser.add_argument('--progress',action='store_true',help='Emit safe progress JSON on stderr for the desktop UI.')
    parser.add_argument('--download-id')
    parser.add_argument('--download-pid', type=int)
    args = parser.parse_args(argv)
    restart=False
    try:
        if args.action == 'check':
            response = check_update(args.root, feed_file=args.feed_file)
        elif args.action == 'prepare':
            progress=(lambda value:print(json.dumps(value,ensure_ascii=False),file=sys.stderr,flush=True)) if args.progress else None
            response = prepare_update(args.root, feed_file=args.feed_file,progress=progress,download_id=args.download_id)
        elif args.action == 'cleanup-download':
            response = dict(ok=True, **cleanup_download(args.root, args.download_id, args.download_pid))
        else:
            if not args.ticket or (args.action == 'apply' and not args.wait_pid) or (args.wait_pid is not None and args.wait_pid < 1):
                raise UpdateError('gui_process_id_required')
            if args.action == 'rollback':
                response = rollback_update(args.root, args.ticket, parent_pid=args.wait_pid)
            else:
                response = apply_update(args.root, args.ticket, parent_pid=args.wait_pid)
            restart=args.restart
    except Exception as error:
        code = str(error) if isinstance(error, UpdateError) else type(error).__name__
        response = dict(ok=False, status='error', code=code, message=MESSAGES.get(code, '更新未完成，原配置和记录已保留。'))
        if args.restart and code!='rollback_required':
            try:restart=not args.wait_pid or not process_running(args.wait_pid)
            except Exception:restart=False
    print(json.dumps(response, ensure_ascii=False))
    if args.action in {'apply', 'rollback'}:
        write_json(args.root / 'updates/last-result.json', response)
    if restart:
        try:
            root, _, manifest = installation(args.root)
            state=read_json(root/'updates/state.json') if (root/'updates/state.json').exists() else {}
            if state.get('phase') not in {'applying','rollback_failed'}:
                environment=os.environ.copy();environment['DOTNET_ROOT']=str(root/'dotnet')
                subprocess.Popen([str(contained(root,manifest['entrypoint']))],cwd=root,env=environment,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except Exception:
            pass # last-result.json remains available if Windows could not reopen the panel.
    return 0 if response['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
