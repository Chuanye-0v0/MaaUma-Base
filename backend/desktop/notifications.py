"""Shared local PushPlus configuration and business events; no game authority.

This file is mirrored byte-for-byte into the optional Pacer backend. Importing
it never contacts a provider, decrypts a credential, or requires an emulator.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import uuid

SCHEMA = 'maauma.notifications.v1'
CONFIG = 'config/notifications.json'
EVENTS = {
    'task_completed': ('任务完成', 'public'),
    'task_failed': ('任务失败', 'public'),
    'seed_factor_hit': ('种马因子命中', 'public'),
    'cut_hit': ('刷切命中', 'pacer'),
    'manual_takeover': ('等待人工接管', 'pacer'),
    'career_completed': ('育成完成', 'pacer'),
    'career_stopped': ('育成异常停止', 'pacer'),
}
MESSAGES = {
    'saved': '推送令牌已加密保存，所有任务共用。',
    'configured': '已保存推送令牌，内容已隐藏。',
    'disabled': 'PushPlus推送已关闭。',
    'legacy': '尚未保存公共令牌；旧版推送来源仍可沿用。',
    'events_saved': '通知事件开关已保存。',
    'accepted': 'PushPlus已接收通知，正在异步投递。',
    'rejected': 'PushPlus未接受通知，请检查令牌与服务状态。',
    'failed': '通知未能发送，任务结果与进度不受影响。',
    'muted': '此类通知已关闭。',
    'duplicate': '此事件已经尝试通知，不再重复发送。',
    'not_configured': '尚未配置可用的PushPlus令牌。',
    'saved_token_unreadable': '推送配置或本机加密令牌无法读取，请重新保存。',
    'token_file_unreadable': '旧版令牌文件无法读取，请在系统设置中重新保存令牌。',
    'invalid_request': '通知请求无效。',
    'invalid_token': '请填写有效的单行PushPlus令牌。',
    'invalid_events': '通知事件开关无效。',
    'notification_crypto_unavailable': '本机加密组件不可用，令牌未保存。',
    'notification_config_write_failed': '推送设置未能保存，原设置已保留。',
    'notification_ledger_unavailable': '通知记录无法保存，已停止本次发送。',
}


class NotificationError(ValueError):
    """Only fixed, non-secret error codes cross the API boundary."""


def installation_root(root=None):
    value = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    value = value.resolve()
    if value.name.casefold() == 'backend' or (value / 'desktop/__init__.py').is_file():
        value = value.parent
    if value.name.casefold() == 'pacer':
        marker = value.parent / 'maauma-suite.json'
        if marker.is_file():
            try:
                if _read_json(marker).get('schema_version') == 'maauma.suite.v1':
                    return value.parent
            except NotificationError:
                pass
    return value


def _read_json(path):
    try:
        raw = path.read_bytes()
        if len(raw) > 128 * 1024:
            raise ValueError()
        value = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, UnicodeError, ValueError):
        raise NotificationError('saved_token_unreadable') from None


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _config_lock(root):
    directory = root / 'config'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.notifications.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        deadline = time.monotonic() + 3
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise NotificationError('notification_config_write_failed') from None
                time.sleep(.02)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _defaults():
    return {'schema_version': SCHEMA, 'pushplus': {'mode': 'legacy'},
            'events': dict.fromkeys(EVENTS, True)}


def _event_settings(events):
    if not isinstance(events, dict) or any(
            key not in EVENTS or type(value) is not bool for key, value in events.items()):
        raise NotificationError('invalid_events')
    return {**dict.fromkeys(EVENTS, True), **events}


def _provider(value):
    if not isinstance(value, dict) or value.get('mode') not in {'token', 'off', 'legacy'}:
        raise NotificationError('saved_token_unreadable')
    if value['mode'] == 'token':
        protected = value.get('protected')
        if (not isinstance(protected, dict) or not isinstance(protected.get('encrypted'), str)
                or not protected['encrypted']):
            raise NotificationError('saved_token_unreadable')
    return deepcopy(value)


def _load_unlocked(root):
    path = root / CONFIG
    if path.is_file():
        value = _read_json(path)
        if value.get('schema_version') != SCHEMA:
            raise NotificationError('saved_token_unreadable')
        try:
            return {**value, 'pushplus': _provider(value.get('pushplus')),
                    'events': _event_settings(value.get('events', {}))}
        except NotificationError:
            raise NotificationError('saved_token_unreadable') from None
    value = _defaults()
    # Existing encrypted data is copied without opening or logging its contents.
    # Keep the old key for downgrade recovery; the new file is authoritative.
    for backend in (root / 'pacer/backend', root / 'backend'):
        old = backend / 'runs/standalone-panel/settings.json'
        if not old.is_file():
            continue
        saved = _read_json(old).get('pushplus_token')
        if saved is None:
            continue
        value['pushplus'] = _provider(saved)
        try:
            _write_json(path, value)
        except OSError:
            raise NotificationError('notification_config_write_failed') from None
        return value
    return value


def _load(root):
    with _config_lock(root):
        return _load_unlocked(root)


def _public(value):
    mode = value['pushplus']['mode']
    return {'ok': True, 'status': 'configured' if mode == 'token' else 'disabled' if mode == 'off' else 'legacy',
            'provider': 'pushplus', 'schema_version': SCHEMA, 'mode': mode,
            'configured': mode == 'token', 'token_masked': '••••••••' if mode == 'token' else '',
            'message': MESSAGES['configured' if mode == 'token' else 'disabled' if mode == 'off' else 'legacy'],
            'events': dict(value['events']),
            'event_catalog': [{'event_id': key, 'label': label, 'capability': capability}
                              for key, (label, capability) in EVENTS.items()]}


def status(root=None):
    return _public(_load(installation_root(root)))


def _seal(root, value):
    try:
        from desktop import device_identity
    except ImportError:
        from desktop.maauma_service import seal
        return seal(root, value)
    return device_identity.seal_credentials(root, value)


def _unseal(root, value):
    try:
        from desktop import device_identity
    except ImportError:
        from desktop.maauma_service import unseal
        return unseal(root, value)
    return device_identity.open_credentials(root, value)


def _checked_token(token):
    if not isinstance(token, str) or len(token) > 4096 or '\n' in token or '\r' in token:
        raise NotificationError('invalid_token')
    return token.strip()


def _change(root, change, *, recover_unreadable=False):
    root = installation_root(root)
    try:
        with _config_lock(root):
            recovered = False
            try:
                value = _load_unlocked(root)
            except NotificationError as error:
                if not recover_unreadable or str(error) != 'saved_token_unreadable':
                    raise
                # Only an explicit token save/disable can replace damaged data.
                # Never fall back to an old token or decrypt it during recovery.
                value = _defaults()
                try:
                    previous = _read_json(root / CONFIG).get('events', {})
                    if isinstance(previous, dict):
                        value['events'].update({key: item for key, item in previous.items()
                                                if key in EVENTS and type(item) is bool})
                except NotificationError:
                    pass
                recovered = True
            change(value)
            _write_json(root / CONFIG, value)
            result = _public(value)
            if recovered:
                result['recovered'] = True
            return result
    except NotificationError:
        raise
    except Exception:
        raise NotificationError('notification_config_write_failed') from None


def save(root, token):
    token = _checked_token(token)
    root = installation_root(root)
    if token:
        try:
            protected = _seal(root, {'token': token})
            provider = _provider({'mode': 'token', 'protected': protected})
        except Exception:
            raise NotificationError('notification_crypto_unavailable') from None
    else:
        provider = {'mode': 'off'}
    result = _change(root, lambda value: value.update(pushplus=provider), recover_unreadable=True)
    result['message'] = MESSAGES['saved' if token else 'disabled']
    if result.get('recovered'):
        result['message'] += ' 已修复不可读的推送配置，请核对事件开关。'
    return result


def disable(root):
    return save(root, '')


def use_legacy(root):
    return _change(root, lambda value: value.update(pushplus={'mode': 'legacy'}))


def set_events(root, events):
    _event_settings(events)
    def change(value):
        value['events'].update(events)
    result = _change(root, change)
    result.update(status='events_saved', message=MESSAGES['events_saved'])
    return result


def _saved_token(root, config):
    provider = config['pushplus']
    if provider['mode'] == 'legacy':
        return None
    if provider['mode'] == 'off':
        return ''
    try:
        token = _checked_token(_unseal(root, provider['protected']).get('token'))
        if not token:
            raise ValueError()
        return token
    except Exception:
        raise NotificationError('saved_token_unreadable') from None


def configured_token(root=None):
    """Compatibility boundary: None permits legacy; '' explicitly disables."""
    root = installation_root(root)
    return _saved_token(root, _load(root))


def _resolve_token(root, config, *, token_override=None, legacy_token_file=None):
    token = _saved_token(root, config)
    if token is not None:
        return token
    if token_override is not None:
        return _checked_token(token_override)
    token = _checked_token(os.environ.get('UMA_CUT_PUSHPLUS_TOKEN', ''))
    if not token and legacy_token_file:
        try:
            token = _checked_token(Path(legacy_token_file).read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError, ValueError):
            raise NotificationError('token_file_unreadable') from None
    return token


def _result(status, **fields):
    return {'ok': status in {'accepted', 'disabled', 'muted', 'duplicate'},
            'status': status, 'message': MESSAGES.get(status, MESSAGES['failed']), **fields}


def _deliver(token, title, content, opener=None):
    payload = json.dumps({'token': token, 'title': title, 'content': content,
                          'template': 'txt', 'channel': 'wechat'}, ensure_ascii=False).encode('utf8')
    request = urllib.request.Request('https://www.pushplus.plus/send', data=payload,
                                    headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with (opener or urllib.request.urlopen)(request, timeout=10) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            value = json.loads(raw.decode('utf-8-sig'))
        code = value.get('code')
        return _result('accepted' if type(code) is int and code == 200 else 'rejected',
                       code=code if type(code) is int else None)
    except Exception:
        # Requests/provider errors can contain a credential: never return them.
        return _result('failed')


def _claim(root, event_id, key):
    digest = hashlib.sha256((event_id + '\n' + key).encode('utf8')).hexdigest()
    path = root / 'logs/notifications' / (digest + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(descriptor, 'w', encoding='utf8') as stream:
        json.dump({'event_id': event_id, 'status': 'attempted', 'at': time.time()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def send_event(root=None, *, event_id, title, content, dedupe_key=None,
               legacy_token_file=None, token_override=None, opener=None):
    """Notification failure is a return value, never game success or a retry."""
    try:
        if (event_id not in EVENTS or not isinstance(title, str) or not 0 < len(title) <= 200
                or not isinstance(content, str) or len(content) > 20000
                or dedupe_key is not None and (not isinstance(dedupe_key, str) or not 0 < len(dedupe_key) <= 512)):
            return _result('failed', code='invalid_request')
        root = installation_root(root)
        config = _load(root)
        if config['pushplus']['mode'] == 'off':
            return _result('disabled')
        if not config['events'][event_id]:
            return _result('muted')
        token = _resolve_token(root, config, token_override=token_override, legacy_token_file=legacy_token_file)
        if not token:
            return _result('disabled' if token_override == '' else 'not_configured')
        record = None
        if dedupe_key is not None:
            try:
                record = _claim(root, event_id, dedupe_key)
            except OSError:
                return _result('failed', code='notification_ledger_unavailable')
            if record is None:
                return _result('duplicate')
        result = _deliver(token, title, content, opener)
        if record is not None:
            try:
                _write_json(record, {'event_id': event_id, 'status': result['status'],
                                     'at': time.time()})
            except OSError:
                pass  # The pre-I/O marker still prevents an automatic retry.
        return result
    except NotificationError as error:
        code = str(error)
        return _result(code if code in {'saved_token_unreadable', 'token_file_unreadable'} else 'failed', code=code)
    except Exception:
        return _result('failed')


def test_notification(root=None, *, opener=None):
    """Called only by an explicit UI/CLI test action; never at save/startup."""
    try:
        root = installation_root(root)
        config = _load(root)
        token = _saved_token(root, config)
        if config['pushplus']['mode'] == 'off':
            return _result('disabled')
        if not token:
            return _result('not_configured')
        return _deliver(token, 'MaaUma 推送测试', '这是一条由你点击按钮发送的MaaUma公共推送测试通知。', opener)
    except NotificationError:
        return _result('saved_token_unreadable')
    except Exception:
        return _result('failed')
