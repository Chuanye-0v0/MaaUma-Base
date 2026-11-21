"""Explicit feedback submission, including an optional bounded stop screenshot."""
import base64
import errno
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import http.client
from http.client import IncompleteRead
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import socket
import ssl
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, getproxies, proxy_bypass
import uuid
from desktop.product_updates import UpdateError, HttpsRedirects, backend_process_lease, cleanup_staged_updates, contained, read_json, service_base, write_json
from desktop import device_identity
from desktop import distribution_identity
from desktop import service_transport as transport
PRODUCT = 'uma-cut'
SCHEMA = 'uma.feedback.v1'
MAX_RAW = 48 * 1024 ** 2  # Legacy inline threshold only; multipart has no report-size quota.
# The base64 envelope fits both the CN gateway and the signed-request body.
# Keep this byte limit aligned with the feedback service's MAX_PACKED.
MAX_PACKED = 1_500_000
_ORIG_BUILD_OPENER = build_opener
_CONN_LOCK = threading.Lock()
_CONN_POOL: dict[tuple[str, str, int, tuple[str, int] | None], http.client.HTTPConnection] = {}
CONFIG = 'config/private-service.json'
AUTH = 'config/private-service-auth.json'
DROP = {'password', 'token', 'access_token', 'refresh_token', 'authorization', 'cookie', 'secret', 'api_key', 'account', 'account_id', 'account_name', 'username', 'trainer_id', 'trainer_name', 'expected_trainer_id', 'expected_trainer_name', 'device_id', 'device_name', 'device_serial', 'endpoint', 'adb_endpoint', 'adb_path', 'path', 'absolute_path', 'source_path', 'image', 'image_path', 'screenshot_path', 'raw_data', 'screenshot_bytes', 'source_manifest', 'execution_root', 'traceback', 'stack_trace', 'image_base64', 'pixel_data'}
MESSAGES = {'service_info': '已读取本机服务配置。', 'configured': '服务地址已保存，尚未联网激活。', 'activated': '邀请码已激活，可以检查更新和提交复盘。', 'service_not_configured': '请先在更新页填写服务地址。', 'activation_required': '请先激活邀请码。', 'access_revoked': '访问权限已停用，请联系维护者。', 'invite_invalid': '邀请码无效或已停用，请联系维护者。', 'invalid_service_address': '请填写服务首页的HTTPS地址；本地演练只允许localhost或127.0.0.1。', 'service_mismatch': '这个地址不是兼容的MaaUma私有服务。', 'invalid_description': '请简短说明问题发生的位置和期望结果。', 'feedback_too_large': '这份复盘超过上传大小限制，请先本地导出交给维护者。', 'feedback_invalid': '复盘格式不正确，未上传。', 'feedback_upload_unsupported':'服务暂未支持大复盘分段上传，请更新后重试；原稿保留。', 'feedback_upload_missing':'分段上传暂存已过期，请再次提交同一份复盘；原稿保留。', 'feedback_upload_incomplete':'分段上传尚未完成，可再次提交同一份复盘继续。', 'feedback_upload_invalid':'分段上传参数未通过校验，原稿已保留。', 'feedback_upload_part_invalid':'本次上传数据校验未通过，请重试原复盘。', 'feedback_upload_digest_invalid':'复盘分段校验未通过，原稿已保留。', 'feedback_storage_unavailable':'反馈存储暂时不可用，原稿已保留，请稍后重试。', 'feedback_not_found': '没有找到这份反馈。', 'feedback_prepared': '复盘预览已准备好，确认后才会上传。', 'feedback_submitted': '复盘已提交，仅维护者可以读取内容。', 'feedback_status': '已取得这份反馈的处理状态。', 'upload_limit': '24小时内的提交数量已达上限，原复盘仍保存在本机。', 'idempotency_conflict': '同一提交标识对应了不同内容，请重新生成预览。', 'service_unavailable': '暂时无法连接服务，可以稍后重试或本地导出。', 'license_valid': '本机授权有效。', 'device_binding_required': '授权需要绑定本机，请在更新页重新激活原邀请码。', 'device_already_bound': '此邀请码已绑定另一台电脑，请联系维护者解绑换机。', 'device_key_mismatch': '本机授权密钥与绑定记录不同，请联系维护者解绑后重新激活。', 'device_signature_invalid': '本机授权验证失败，请重新激活或联系维护者。', 'device_clock_skew': '电脑时间与服务相差过大，请同步 Windows 时间后重试。', 'device_request_replayed': '授权请求已过期或重复，请重试。', 'device_identity_unavailable': '无法读取本机授权组件，请安装完整更新包后重试。', 'device_credentials_unreadable': '此授权无法在当前 Windows 用户或电脑解密，请重新激活；换机需联系维护者解绑。', 'device_key_unreadable': '本机授权密钥无法解密，请联系维护者解绑后重新激活。', 'device_changed': '授权来自另一台电脑，请联系维护者解绑后重新激活。', 'service_redirect_rejected': '授权服务返回了不支持的跳转，请检查服务地址或联系维护者。', 'scoring_list': '已读取群友权重方案。', 'scoring_loaded': '方案已读取并通过本机校验。', 'scoring_published': '权重方案已发布，已激活的群友可以查看和下载。', 'scoring_withdrawn': '方案已下架，群友交换站不再显示。', 'score_profile_invalid': '权重方案格式或内容不正确，请检查后再试。', 'score_profile_incompatible': '评分模型或语义版本不兼容，未导入此方案。请使用适配当前评分模型的方案。', 'score_profile_not_found': '方案不存在或已下架，请刷新列表。', 'score_profile_not_owner': '只能修改和下架自己发布的方案。', 'score_profile_blocked': '此方案已由维护者下架，请联系维护者。', 'score_profile_conflict': '方案已更新，请刷新后再修改，原内容未被覆盖。', 'score_profile_limit': '最多同时发布20份方案，请先下架不再使用的方案。'}
MESSAGES.update(wrong_product='此授权不包含当前功能，请联系维护者。', service_rejected='授权服务拒绝了本次请求，请检查授权或联系维护者。')
MESSAGES.update(
    service_rate_limited='授权服务暂时限流，将稍后重试。',
    service_tls_verification_failed='授权连接的 TLS 证书验证失败，未接受该连接或回包。请检查电脑时间、网络或证书设置。',
    service_html_intercepted='授权连接返回了 HTML 网页，未取得授权服务回包。请检查网络或网页拦截。',
    service_response_incomplete='授权回包在传输中断开，未收到完整结果。恢复连接后会重新验证。')
STATUS_LABELS = {'open': '待查看', 'triaged': '已确认问题', 'fixed': '已修复', 'closed': '已关闭'}

class SupportError(RuntimeError):

    def __init__(self, code):
        super().__init__(code)
        self.code = code
        self.message = MESSAGES.get(code, '授权未完成，当前育成和本地记录已保留。')
_LICENSE_CACHE = {}

def _device_call(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except device_identity.DeviceIdentityError as error:
        raise SupportError(str(error)) from None

def result(status, **fields):
    return {'ok': True, 'status': status, 'message': MESSAGES.get(status, status), **fields}

def _transport_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except transport.TransportError as error:
        raise SupportError(str(error)) from None


def _config_value(raw):
    value = json.loads(raw.decode('utf-8-sig')) if raw else {}
    if not value.get('base_url'):
        raise SupportError('service_not_configured')
    value['base_url'] = service_base(value['base_url'])
    if value.get('channel') not in {'preview', 'stable'}:
        raise SupportError('service_mismatch')
    if value.get('api_transport', 'cn') not in {'cn', 'overseas'}:
        raise SupportError('invalid_transport_preference')
    return value


def config(root):
    root = _maauma_authorization_root(root)
    return _config_value(_transport_call(transport.snapshot, root)[CONFIG])


def credentials(root, cfg, *, migrate=True, timeout=45, product=PRODUCT):
    root = _maauma_authorization_root(root)
    state = _transport_call(transport.snapshot, root)
    actual = _config_value(state[CONFIG])
    if actual['base_url'] != cfg['base_url']:
        raise SupportError('service_migration_state_changed')
    value = json.loads(state[AUTH].decode('utf-8-sig')) if state[AUTH] else {}
    sealed = value.get('schema_version') == 'uma.private_service_auth.v2'
    if sealed:
        value = _device_call(device_identity.open_credentials, root, value)
    if value.get('base_url') != cfg['base_url'] or product not in value.get('products', []) or (not value.get('access_token')):
        raise SupportError('activation_required')
    token = value['access_token']
    if not isinstance(token, str):
        raise SupportError('activation_required')
    if sealed:
        current = _device_call(device_identity.identity, root)
        if value.get('device_id') != current['device_id']:
            raise SupportError('device_changed')
        if value.get('public_key') != current['public_key']:
            raise SupportError('device_key_mismatch')
        if not token.startswith('md_'):
            raise SupportError('device_credentials_unreadable')
    elif token.startswith('mc_'):
        if migrate:
            current = _device_call(device_identity.identity, root)
            response = _wire_request(root, cfg, '/v1/device/bind', {'product_id': product, **current}, token=token, signed=True, timeout=timeout)
            value = _save_activation(root, cfg, response, current, product=product)
    else:
        raise SupportError('device_credentials_unreadable')
    return value

def _transport_unavailable(error):
    if transport.proxy_tunnel_unavailable(error):
        return True
    if isinstance(error, URLError):
        error = error.reason
    # Certificate/configuration/local-file errors must not become permission.
    if isinstance(error, (TimeoutError, ConnectionError, socket.gaierror,
            ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return True
    return isinstance(error, OSError) and (error.errno in {
        errno.ETIMEDOUT, errno.ENETDOWN, errno.ENETUNREACH,
        errno.EHOSTUNREACH, errno.ECONNRESET, errno.ECONNREFUSED,
        errno.ECONNABORTED} or getattr(error, 'winerror', None) in {
        10050, 10051, 10052, 10053, 10054, 10060, 10061, 10064, 10065, 11001, 11002})


def _html_intercept(raw):
    if not isinstance(raw, bytes):
        return False
    # Require an actual HTML document/root, not arbitrary invalid JSON or a
    # JSON error whose text happens to mention HTML. No webpage is accepted.
    text = raw[:8192].decode('utf-8-sig', errors='replace').lstrip()
    return re.match(r'(?is)(?:<!doctype\s+html\b|<html(?:\s|>))', text) is not None


def _grace_transport_error(cfg, code):
    error = SupportError(code)
    # Only failures observed while connecting to the two published aliases
    # may retain an existing host grant. This flag is never read from JSON.
    error.maauma_license_grace_eligible = cfg.get('base_url') in transport.policy().approved_origins
    error.maauma_transport_retryable = False  # TLS/HTML failures never switch origin.
    return error


def _rate_limit_error(cfg, headers):
    error = _grace_transport_error(cfg, 'service_rate_limited')
    delay = 60.
    value = headers.get('Retry-After') if headers is not None else None
    if value is not None:
        try:
            delay = float(int(value))
        except (TypeError, ValueError, OverflowError):
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    error.maauma_retry_after_seconds = min(300., max(60., delay))
    return error


def _wire_request(root, cfg, path, body=None, *, token='', signed=False, timeout=45):
    origins = _transport_call(transport.api_origins, cfg, anonymous=not bool(token))
    return _transport_call(transport.attempt, origins, lambda base: _wire_origin(
        root, dict(cfg, base_url=base), path, body, token=token, signed=signed, timeout=timeout))


def _resolve_http_proxy(scheme, host, direct):
    if direct or not host or host.lower() in {'127.0.0.1', 'localhost', '::1'} or proxy_bypass(host):
        return None
    proxies = {k.lower(): v for k, v in getproxies().items() if v}
    if scheme not in proxies:
        return None
    raw_proxy = proxies[scheme].strip()
    if '://' not in raw_proxy:
        raw_proxy = 'http://' + raw_proxy
    parsed_proxy = urlsplit(raw_proxy)
    if (scheme == 'https' and parsed_proxy.scheme.lower() == 'http' and parsed_proxy.hostname
            and parsed_proxy.username is None and parsed_proxy.password is None):
        return (parsed_proxy.hostname, parsed_proxy.port or 80)
    return _REDIRECT_FALLBACK


def _pop_pooled_connection(key, timeout):
    with _CONN_LOCK:
        conn = _CONN_POOL.pop(key, None)
    if conn is not None:
        conn.timeout = timeout
        if conn.sock is not None:
            try:
                conn.sock.settimeout(timeout)
            except OSError:
                pass
        return conn, True
    scheme, host, port, proxy_target = key
    if proxy_target is not None:
        proxy_host, proxy_port = proxy_target
        conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=timeout, context=ssl.create_default_context())
        conn.set_tunnel(host, port)
        return conn, False
    if scheme == 'https':
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context()), False
    return http.client.HTTPConnection(host, port, timeout=timeout), False


def _return_pooled_connection(key, conn, response):
    if response is not None and getattr(response, 'will_close', True):
        try:
            conn.close()
        except OSError:
            pass
        return
    if conn.sock is None:
        try:
            conn.close()
        except OSError:
            pass
        return
    with _CONN_LOCK:
        old = _CONN_POOL.pop(key, None)
        _CONN_POOL[key] = conn
    if old is not None and old is not conn:
        try:
            old.close()
        except OSError:
            pass


_REDIRECT_FALLBACK = object()


def _send_keepalive_origin(cfg, path, data, headers, timeout):
    parsed = urlsplit(cfg['base_url'])
    scheme = parsed.scheme.lower()
    if scheme not in {'http', 'https'} or not parsed.hostname:
        return _REDIRECT_FALLBACK
    direct = transport.direct_cn_api(cfg)
    proxy_target = _resolve_http_proxy(scheme, parsed.hostname, direct)
    if proxy_target is _REDIRECT_FALLBACK:
        return _REDIRECT_FALLBACK
    port = parsed.port or (443 if scheme == 'https' else 80)
    key = (scheme, parsed.hostname, port, proxy_target)
    base_prefix = parsed.path.rstrip('/') if parsed.path and parsed.path != '/' else ''
    req_path = (base_prefix + path) or '/'
    method = 'POST' if data is not None else 'GET'
    req_headers = dict(headers)
    req_headers['Connection'] = 'keep-alive'
    if parsed.netloc and 'Host' not in req_headers:
        req_headers['Host'] = parsed.netloc
    for attempt in range(2):
        conn, reused = _pop_pooled_connection(key, timeout)
        keep = False
        response = None
        try:
            conn.request(method, req_path, body=data, headers=req_headers)
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                return _REDIRECT_FALLBACK
            if response.status >= 400:
                err_raw = response.read(8192)
                raise HTTPError(cfg['base_url'] + path, response.status, response.reason, response.headers, io.BytesIO(err_raw))
            raw = response.read(2 * 1024 ** 2 + 1)
            keep = not response.will_close
            return raw
        except HTTPError:
            raise
        except http.client.IncompleteRead:
            try:
                conn.close()
            except OSError:
                pass
            raise
        except (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError, BrokenPipeError, EOFError) as err:
            try:
                conn.close()
            except OSError:
                pass
            if reused and attempt == 0:
                continue
            raise URLError(err) from err
        except (http.client.HTTPException, OSError) as err:
            try:
                conn.close()
            except OSError:
                pass
            raise URLError(err) from err
        finally:
            if keep:
                _return_pooled_connection(key, conn, response)
            else:
                try:
                    conn.close()
                except OSError:
                    pass
    return _REDIRECT_FALLBACK


def _wire_origin(root, cfg, path, body=None, *, token='', signed=False, timeout=45):
    headers = {'User-Agent': 'MaaUma-Support/1', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf8') if body is not None else None
    if data is not None:
        headers['Content-Type'] = 'application/json'
    if signed:
        headers.update(_device_call(device_identity.signed_headers, root, 'POST' if data is not None else 'GET', path, data or b'', token))
    req = Request(cfg['base_url'] + path, data=data, headers=headers, method='POST' if data is not None else 'GET')
    raw = None
    try:
        pooled_raw = _REDIRECT_FALLBACK
        if build_opener is _ORIG_BUILD_OPENER:
            pooled_raw = _send_keepalive_origin(cfg, path, data, headers, timeout)
        if pooled_raw is _REDIRECT_FALLBACK:
            handlers = (ProxyHandler({}),) if transport.direct_cn_api(cfg) else ()
            with build_opener(HttpsRedirects(cfg['base_url'].startswith('http://')), *handlers).open(req, timeout=timeout) as response:
                raw = response.read(2 * 1024 ** 2 + 1)
        else:
            raw = pooled_raw
        if len(raw) > 2 * 1024 ** 2:
            raise SupportError('service_mismatch')
        value = json.loads(raw.decode('utf8'))
    except HTTPError as error:
        error_raw = None
        try:
            error_raw = error.read(8192)
            details = json.loads(error_raw)
            code = details.get('code') if isinstance(details, dict) else None
        except (IncompleteRead, ValueError, OSError):
            code = None
        if isinstance(code, str) and code in MESSAGES and code not in {'service_unavailable', 'service_rate_limited'}:
            raise transport.mark(SupportError(code), error, code) from None
        # Only a rate-limit status without an auth/product rejection may retain
        # an already verified host window. JSON alone cannot grant this flag.
        if error.code == 429 and (code is None or isinstance(code, str) and code in {
                'rate_limited', 'too_many_requests', 'service_rate_limited'}):
            raise _rate_limit_error(cfg, error.headers) from None
        if error.code == 403 and _html_intercept(error_raw):
            raise _grace_transport_error(cfg, 'service_html_intercepted') from None
        # An unknown auth/product rejection is not a network outage, even when
        # its body incorrectly says service_unavailable.
        if error.code == 408 or 500 <= error.code <= 599:
            raise transport.mark(SupportError('service_unavailable'), error, code) from None
        raise transport.mark(SupportError('service_rejected'), error, code) from None
    except (IncompleteRead, URLError, OSError) as error:
        cause = error.reason if isinstance(error, URLError) else error
        if isinstance(cause, IncompleteRead):
            raise _grace_transport_error(cfg, 'service_response_incomplete') from None
        if isinstance(cause, (ssl.SSLCertVerificationError, ssl.CertificateError)):
            if cfg.get('base_url') in transport.policy().approved_origins:
                raise _grace_transport_error(cfg, 'service_tls_verification_failed') from None
        raise transport.mark(SupportError('service_unavailable' if _transport_unavailable(error)
            else 'service_mismatch'), error) from None
    except ValueError:
        if _html_intercept(raw):
            raise _grace_transport_error(cfg, 'service_html_intercepted') from None
        raise SupportError('service_mismatch') from None
    if not isinstance(value, dict) or value.get('ok') is not True:
        raise SupportError('service_mismatch')
    return value

def request(root, path, body=None, *, authenticated=True, signed=False, timeout=45):
    if authenticated:
        _ensure_transport(root, timeout=timeout)
    cfg = config(root)
    token = credentials(root, cfg, timeout=timeout)['access_token'] if authenticated else ''
    return _wire_request(root, cfg, path, body, token=token, signed=signed or authenticated, timeout=timeout)

def api_path(root, value):
    return _transport_call(transport.api_path, config(root), value)


def migrate_service_transport(root, target=None, *, timeout=45):
    root = _maauma_authorization_root(root)
    value = _transport_call(transport.migrate, root, target, config_loader=config,
        credentials_loader=lambda location: credentials(location, config(location), migrate=False),
        sealer=lambda location, auth: _device_call(device_identity.seal_credentials, location, auth),
        request_info=lambda base: _wire_origin(root, {'base_url': base}, '/v1/info', timeout=timeout),
        request_license=lambda base, auth, product: _wire_origin(root, {'base_url': base},
            '/v1/license/check?' + urlencode({'product_id': product}),
            token=auth['access_token'], signed=True, timeout=timeout),
        receipt_validator=lambda receipt, product: distribution_identity.validate_receipt(
            receipt, kind='activation', product=product))
    _LICENSE_CACHE.pop(str(Path(root).resolve()), None)
    return value


def _ensure_transport(root, *, timeout=45):
    root = _maauma_authorization_root(root)
    selected = transport.policy()
    if selected.cn_origin is None:
        return
    cfg = config(root)
    if cfg['base_url'] != selected.legacy_origin:
        return
    state = _transport_call(transport.snapshot, root)
    if not state[AUTH] or json.loads(state[AUTH].decode('utf-8-sig')) == {}:
        return
    try:
        migrate_service_transport(root, selected.cn_origin, timeout=timeout)
    except SupportError as error:
        if getattr(error, 'maauma_transport_retryable', False) is not True:
            raise


def _save_activation(root, cfg, response, current, *, product=PRODUCT):
    root = _maauma_authorization_root(root)
    if not re.fullmatch('md_[A-Za-z0-9._~-]{32,252}', str(response.get('access_token', ''))) or product not in response.get('products', []) or (not response.get('binding_id')):
        raise SupportError('service_mismatch')
    if response.get('device_id') != current['device_id']:
        raise SupportError('service_mismatch')
    value = {key: response[key] for key in ('access_token', 'client_id', 'products', 'allow_preview', 'binding_id', 'device_id')}
    value.update(schema_version='uma.private_service_auth_payload.v2', base_url=cfg['base_url'], public_key=current['public_key'])
    receipt = distribution_identity.validate_receipt(response.get('distribution_receipt'), kind='activation')
    if receipt is not None:
        value['distribution_receipt'] = receipt
    write_json(root / AUTH, _device_call(device_identity.seal_credentials, root, value))
    if receipt is None:
        distribution_identity.clear_activation_receipt(root)
    else:
        distribution_identity.save_activation_receipt(root, receipt)
    _LICENSE_CACHE.pop(str(Path(root).resolve()), None)
    return value

def ensure_runtime_license(root, force=False):
    """Online validation with a short, decaying, process-memory helper cache.

    Offline permission belongs only to LicensedRunControl, never to a disk
    receipt. Startup callers force a real request; cache hits do not refresh it.
    """
    try:
        return _runtime_license(root, force)
    except SupportError:
        raise
    except UpdateError as error:
        code = str(error)
        raise SupportError(code if code in MESSAGES and code != 'service_unavailable'
            else 'service_mismatch') from None
    except (OSError, ValueError, KeyError, TypeError):
        # These originate outside _wire_request, e.g. malformed local config or
        # credentials. Only the transport boundary may report a network outage.
        raise SupportError('service_mismatch') from None


def runtime_license_identity(root):
    """Check local binding only; never request a service or expose credentials."""
    cfg = config(root)
    auth = credentials(root, cfg, migrate=False, product=PRODUCT)
    identity = {name: auth.get(name) for name in ('client_id', 'binding_id', 'device_id')}
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise SupportError('device_binding_required')
    return dict(identity, product_id=PRODUCT)


def _runtime_license(root, force):
    root = Path(root)
    key = str(_maauma_authorization_root(root).resolve())
    cfg = config(root)
    now = time.monotonic()
    cached = _LICENSE_CACHE.get(key)
    if not force and cached and now < cached[0] and len(cached) > 3 and cached[3] == cfg['base_url']:
        clean = dict(cached[1])
        age = max(0., now - cached[2])
        clean.update(valid_for_seconds=max(0, int(clean['valid_for_seconds'] - age)),
            offline_grace_seconds=max(0, int(clean['offline_grace_seconds'] - age)),
            license_cache_hit=True, verified_age_seconds=age)
        return clean
    _LICENSE_CACHE.pop(key, None)
    # Signed runtime checks can use either fixed, trusted alias of the same
    # authority. Config/DPAPI migration remains an explicit separate operation;
    # a migration receipt failure must not force an existing user onto Workers.
    auth = credentials(root, cfg, timeout=12)
    response = _wire_request(root, cfg, '/v1/license/check?' + urlencode({'product_id': PRODUCT}), token=auth['access_token'], signed=True, timeout=12)
    if any((response.get(field) != auth.get(field) for field in ('client_id', 'binding_id', 'device_id'))) or response.get('product_id') != PRODUCT:
        raise SupportError('service_mismatch')
    duration = response.get('valid_for_seconds')
    # Compatibility for the old successful license contract. An explicit zero
    # or any explicit rejection is still honored, never treated as an outage.
    grace = response.get('offline_grace_seconds', 86400)
    if type(duration) is not int or duration <= 0:
        raise SupportError('service_mismatch')
    if type(grace) is not int or not 0 <= grace <= 86400:
        raise SupportError('service_mismatch')
    try:
        distribution_identity.save_activation_receipt(root, response.get('distribution_receipt'))
    except (OSError, UpdateError):
        # Public provenance is optional. Its persistence is independent of the
        # online identity/product/duration checks completed immediately above.
        pass
    verified = time.monotonic()
    clean = {field: response[field] for field in ('client_id', 'binding_id', 'device_id', 'product_id')}
    clean.update(ok=True, valid_for_seconds=min(duration, 300),
        offline_grace_seconds=grace, license_cache_hit=False, verified_age_seconds=0.)
    _LICENSE_CACHE[key] = (verified + min(duration, 300), clean, verified, cfg['base_url'])
    return dict(clean)

def service_info(root):
    try:
        cfg = config(root)
    except SupportError:
        from desktop.cut_package_profile import profile
        cfg = {'base_url': '', 'channel': profile(root)['channel']}
    auth = {}
    try:
        auth = credentials(root, cfg, migrate=False)
        active = True
    except SupportError:
        active = False
    last = root / 'updates/last-result.json'
    return result('service_info', configured=bool(cfg['base_url']), activated=active, base_url=cfg['base_url'], channel=cfg['channel'], last_update=read_json(last) if last.exists() else None, cache_cleanup=cleanup_staged_updates(root), **distribution_identity.distribution_info(root, activation_receipt=auth.get('distribution_receipt')))

def refresh_distribution(root):
    """Fetch a missing public marker only as part of an explicit user action."""
    info = service_info(root)
    if info['distribution_source'] == 'activation':
        distribution_identity.save_activation_receipt(root, info['distribution_receipt'])
    if info['distribution_source'] != 'activation':
        response = request(root, '/v1/distribution/receipt?' + urlencode({'product_id': PRODUCT}))
        receipt = distribution_identity.save_activation_receipt(root, response.get('distribution_receipt'))
        if receipt is None:
            raise SupportError('service_mismatch')
        info = service_info(root)
    return info

def configure_service(root, base_url, channel, api_transport=None):
    base = service_base(base_url)
    if channel not in {'preview', 'stable'}:
        raise SupportError('service_mismatch')
    update_root = Path(root)
    root = _maauma_authorization_root(root)
    with transport.guard(root):
        _transport_call(transport.recover, root)
        previous = root / CONFIG
        old = read_json(previous) if previous.exists() else {}
        preference = old.get('api_transport', 'cn') if api_transport is None else api_transport
        if preference not in {'cn', 'overseas'}:
            raise SupportError('invalid_transport_preference')
        write_json(previous, dict(schema_version='uma.private_service_config.v1', base_url=base,
            channel=channel, api_transport=preference))
        if old.get('base_url') != base:
            write_json(root / AUTH, {})
            distribution_identity.clear_activation_receipt(root)
    _LICENSE_CACHE.pop(str(Path(root).resolve()), None)
    write_json(update_root / 'config/update-source.json', dict(product_id=PRODUCT, channel=channel, private_service=True, feed_url=base + '/v1/updates/' + PRODUCT + '/' + channel))
    return result('configured', **{key: value for key, value in service_info(root).items() if key not in {'ok', 'status', 'message'}})

def activate(root, invite_code):
    if not isinstance(invite_code, str) or not 12 <= len(invite_code.strip()) <= 160:
        raise SupportError('invite_invalid')
    info = request(root, '/v1/info', authenticated=False)
    if info.get('schema_version') != 'uma.private_service.v1' or info.get('report_visibility') != 'maintainer-only':
        raise SupportError('service_mismatch')
    if info.get('device_binding') != 'one-computer-v1':
        raise SupportError('service_mismatch')
    current = _device_call(device_identity.identity, root)
    response = request(root, '/v1/activate', {'invite_code': invite_code.strip(), 'product_id': PRODUCT, **current}, authenticated=False, signed=True)
    cfg = config(root)
    _save_activation(root, cfg, response, current)
    return result('activated', activated=True, channel=cfg['channel'])

def sanitize(value, counts, depth=0):
    if depth > 64:
        raise SupportError('feedback_invalid')
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).casefold()
            if lowered in DROP or any((part in lowered for part in ('password', 'access_token', 'refresh_token', 'secret', 'cookie', 'base64'))):
                counts['removed_fields'] += 1
                continue
            cleaned[str(key)] = sanitize(item, counts, depth + 1)
        return cleaned
    if isinstance(value, list):
        return [sanitize(item, counts, depth + 1) for item in value]
    if isinstance(value, str):
        if value.startswith(('data:image/', 'iVBORw0KGgo', '/9j/')):
            counts['removed_fields'] += 1
            return '<image-omitted>'
        original = value
        value = re.sub('(?:[A-Za-z]:[\\\\/]|\\\\\\\\)[^\\r\\n\\"\\\']+', '<local-path>', value)
        value = re.sub('https?://[^\\s\\"\\\']+', '<url>', value)
        value = re.sub('\\b(?:\\d{1,3}\\.){3}\\d{1,3}(?::\\d{1,5})?\\b', '<device-address>', value)
        value = re.sub('(?i)((?:trainer[_ ]?id|account[_ ]?id|训练员(?:ID|编号)|账号(?:ID|编号))\\s*[:=：]?\\s*)\\d+', '\\1<identity-omitted>', value)
        value = re.sub('(?i)((?:token|password|secret|authorization)\\s*[=:]\\s*)[^\\s,;]+', '\\1<redacted>', value)
        value = re.sub('(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|\\bsk-[A-Za-z0-9_-]{24,})', '<credential-omitted>', value)
        if value != original:
            counts['redacted_strings'] += 1
        return value
    if value is None or type(value) in (bool, int, float):
        return value
    raise SupportError('feedback_invalid')

def prepare_feedback(root, run_id, description, *, review=None, include_screenshot=True):
    if not isinstance(description, str) or not description.strip() or len(description) > 4000:
        raise SupportError('invalid_description')
    if type(include_screenshot) is not bool:
        raise SupportError('feedback_invalid')
    if review is not None:
        return _prepare_feedback(root,run_id,description,review=review,include_screenshot=include_screenshot)
    from core.auto_trainer.career_review_export import checked_review_run
    from core.auto_trainer.pacer_storage import read_access_for_run
    run=checked_review_run(root/'backend',run_id)
    with read_access_for_run(run):
        return _prepare_feedback(root,run_id,description,include_screenshot=include_screenshot)


def _prepare_feedback(root, run_id, description, *, review=None, include_screenshot=True):
    batch_context = None
    run = None
    if review is None:
        from core.auto_trainer.career_review_export import build_career_review, checked_review_run, _pick, _owned_file
        run = checked_review_run(root / 'backend', run_id)
        review = build_career_review(run, package_root=root)
        cut_origin = (review.get('run') or {}).get('cut_origin')
        cut_record = (review.get('run') or {}).get('mode') == 'cut' or bool(cut_origin)
        batch_file = run.parent.parent / 'cut_farming.json'
        if cut_record and batch_file.is_file() and _owned_file(batch_file, run.parent.parent):
            batch = read_json(batch_file)
            batch_context = {**_pick(batch, 'status', 'stop', 'resource_usage', 'resource_ledger', 'reserved_resources'), 'configured_limits': _pick(batch.get('config'), 'max_attempts', 'max_tp_items', 'max_carrots'), 'scope': 'Latest batch stop may be in the restart after this selected career.'}
            attempt = next((a for a in batch.get('attempts', []) if a.get('run_id') == run.name), {})
            batch_context['selected_attempt'] = _pick(attempt, 'run_id', 'status', 'active_seconds', 'restart_elapsed_seconds', 'restart_started_at_unix_ms', 'restart_stopped_at_unix_ms', 'recovery_checks')
            batch_context['selected_attempt']['automatic_recoveries'] = [_pick(item, 'kind', 'status', 'ok', 'reason', 'restart_performed', 'race_entry_replayed', 'new_career_started', 'elapsed_seconds') for item in attempt.get('auto_recoveries', [])]
            if cut_origin:
                batch_context['scope'] = '原刷切批次来源；这一局已在原目录转入自动育成，批次用量不代表续养的新消费。'
        restart_file = run / 'cut_restart_report.json'
        if cut_record and restart_file.is_file() and _owned_file(restart_file, run):
            restart = read_json(restart_file)
            batch_context = batch_context or {}
            fields = ('state', 'reason', 'next_phase', 'ordinary_start_verified', 'support_action', 'trainee_action', 'pending_navigation', 'observed_state', 'observed_reason', 'recognition_seconds', 'resource_kind', 'resource_amount', 'observed_tp_before', 'observed_tp_cost', 'configuration_reused', 'navigation_retry', 'abandon_confirmation_verified', 'abandon_confirmation_retry')
            details = {**_pick(restart, 'ok', 'reason', 'readiness_timeout'), 'observation': _pick(restart.get('observation'), *fields)}
            frames = sorted((run / 'cut_restart').glob('frame_*.json'))[-8:]
            details['last_observations'] = [_pick(read_json(p), *fields) for p in frames if _owned_file(p, run)]
            batch_context['selected_career_restart'] = details
    if review.get('schema_version') != 'uma.career_review.v1':
        raise SupportError('feedback_invalid')
    counts = {'removed_fields': 0, 'redacted_strings': 0}
    clean = sanitize(review, counts)
    manifest = root / 'product-package-manifest.json'
    version = read_json(manifest).get('version', 'unknown') if manifest.exists() else 'unknown'
    payload = dict(schema_version=SCHEMA, product_id=PRODUCT, app_version=version, created_at=datetime.now(timezone.utc).isoformat(), description=sanitize(description.strip(), counts), review=clean, redaction=counts, export_distribution=distribution_identity.export_distribution(root))
    if batch_context:
        payload['batch_context'] = sanitize(batch_context, counts)
    attachment = None
    if include_screenshot and run is not None:
        from core.auto_trainer.career_review_export import review_feedback_image
        attachment = review_feedback_image(run)
        if attachment:
            payload['diagnostic_images'] = [attachment]
    identifier = uuid.uuid4().hex
    folder = contained(root, 'exports/feedback/drafts/' + identifier)
    folder.mkdir(parents=True)
    raw_size=0;raw_digest=hashlib.sha256()
    with (folder/'feedback.json.gz').open('wb') as stream:
        with gzip.GzipFile(fileobj=stream,mode='wb',compresslevel=6,mtime=0) as compressed:
            for text in json.JSONEncoder(ensure_ascii=False,separators=(',', ':'),allow_nan=False).iterencode(payload):
                chunk=text.encode('utf8');raw_size+=len(chunk);raw_digest.update(chunk);compressed.write(chunk)
    packed_size=(folder/'feedback.json.gz').stat().st_size
    packed_digest=hashlib.sha256()
    with (folder/'feedback.json.gz').open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):packed_digest.update(chunk)
    preview_payload = dict(payload)
    image_preview = None
    if attachment:
        image_preview = folder / 'stop.jpg'
        image_preview.write_bytes(base64.b64decode(attachment['data_base64'], validate=True))
        preview_payload['diagnostic_images'] = [{k: v for k, v in attachment.items() if k != 'data_base64'}]
        preview_payload['diagnostic_images'][0]['preview_note'] = '图片随这份复盘上传；请在面板中查看停机画面。'
    preview_chunks=[];preview_length=0;preview_truncated=False
    with (folder/'preview.json').open('w',encoding='utf8') as stream:
        for text in json.JSONEncoder(ensure_ascii=False,indent=2,allow_nan=False).iterencode(preview_payload):
            stream.write(text)
            if preview_length<40000:
                piece=text[:40000-preview_length];preview_chunks.append(piece);preview_length+=len(piece)
            if len(text)>max(0,40000-preview_length):preview_truncated=True
    coverage = clean.get('coverage') or {}
    run = clean.get('run') or {}
    summary = f"程序版本：{version}\n记录：{run.get('run_id', run_id)}\n训练观察：{coverage.get('training_observations', 0)}；事件选择：{coverage.get('event_choices', 0)}\n附带异常停机游戏截图：{(1 if attachment else 0)} 张。文字记录已脱敏，不读取账号登录配置。"
    if attachment:
        summary += '\n截图为压缩预览，包含画面上可见的游戏信息；原始 PNG 保留在本机。'
        diagnostic = clean.get('stop_diagnostic') or {}
        if diagnostic.get('scope') == 'game_screen_before_abnormal_stop':
            summary += '\n' + str(diagnostic.get('note', '实时停机截图失败，此图是本局停机前缓存，不是实时画面。'))
            summary += ' 原画面时间：' + str(diagnostic.get('captured_at', '未知'))
    elif include_screenshot:
        summary += '\n本局没有可用的停机截图（旧记录无法补回已清理的画面）。'
    source = payload['export_distribution']
    summary += '\n本次导出来源编号：' + (source['trace_id'] or '未知') + '；仅标记本次导出，不代表历史育成的运行者。'
    draft = dict(schema_version='uma.feedback_draft.v1', draft_id=identifier, summary=summary, sha256=packed_digest.hexdigest(), size_bytes=packed_size, raw_bytes=raw_size, multipart_required=bool(image_preview and image_preview.stat().st_size>512*1024), submitted=None)
    write_json(folder / 'draft.json', draft)
    preview=''.join(preview_chunks)+('\n……详细内容请打开完整预览文件。' if preview_truncated else '')
    return result('feedback_prepared', draft_id=identifier, summary=summary, preview_text=preview, preview_path=str(folder / 'preview.json'), image_preview_path=str(image_preview) if image_preview else None, screenshot_count=1 if attachment else 0, size_bytes=packed_size, redaction=counts)

def draft_folder(root, identifier):
    if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
        raise SupportError('feedback_invalid')
    return contained(root, 'exports/feedback/drafts/' + identifier)

def submit_feedback(root, draft_id):
    folder = draft_folder(root, draft_id)
    draft = read_json(folder / 'draft.json')
    if draft.get('submitted'):
        return result('feedback_submitted', feedback_id=draft['submitted'], feedback_status='open')
    archive=folder/'feedback.json.gz';file_digest=hashlib.sha256();raw_size=0
    with archive.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):file_digest.update(chunk)
    if file_digest.hexdigest()!=draft.get('sha256'):raise SupportError('feedback_invalid')
    with gzip.open(archive,'rb') as stream:
        for chunk in iter(lambda:stream.read(64*1024),b''):raw_size+=len(chunk)
    if archive.stat().st_size<=MAX_PACKED and raw_size<=MAX_RAW and draft.get('multipart_required') is not True:
        data=archive.read_bytes()
        response=request(root,'/v1/feedback',dict(idempotency_key=draft_id,payload_gzip_base64=base64.b64encode(data).decode('ascii'),sha256=draft['sha256']))
    else:
        from desktop.feedback_multipart import upload_gzip,UploadError
        info=request(root,'/v1/info',authenticated=False)
        if info.get('feedback_upload')!='maauma.feedback_upload.v1':raise SupportError('feedback_upload_unsupported')
        def progress(done,total):
            print(json.dumps({'schema_version':'maauma.feedback_upload_progress.v1','stage':'upload','completed_parts':done,'total_parts':total}),file=sys.stderr,flush=True)
        try:response=upload_gzip(archive,draft_id,lambda value:request(root,'/v1/feedback',value),state_path=folder/'upload-state.json',progress=progress)
        except UploadError as error:raise SupportError(error.code) from None
    identifier = response.get('feedback_id')
    if not isinstance(identifier, str) or not re.fullmatch('MF-[A-Fa-f0-9-]{36}', identifier):
        raise SupportError('service_mismatch')
    draft['submitted'] = identifier
    write_json(folder / 'draft.json', draft)
    (folder/'upload-state.json').unlink(missing_ok=True)
    return result('feedback_submitted', feedback_id=identifier, feedback_status=response.get('status', 'open'))

def feedback_status(root, identifier):
    if not isinstance(identifier, str) or not re.fullmatch('MF-[A-Fa-f0-9-]{36}', identifier):
        raise SupportError('feedback_invalid')
    value = request(root, '/v1/feedback/' + identifier)
    status = value.get('status', 'open')
    return result('feedback_status', feedback_id=identifier, feedback_status=status, status_label=STATUS_LABELS.get(status, status), fixed_version=value.get('fixed_version'), note=value.get('note', ''))

def dispatch(root, value):
    action = value.get('action')
    if action in {'scoring_list', 'scoring_get', 'scoring_publish', 'scoring_withdraw'}:
        from desktop.scoring_exchange import dispatch as exchange
        return exchange(root, value)
    if action == 'service_info':
        return service_info(root)
    if action == 'refresh_distribution':
        return refresh_distribution(root)
    if action == 'configure_service':
        from desktop.cut_package_profile import profile
        return configure_service(root, value.get('base_url'), value.get('channel', profile(root)['channel']), value.get('api_transport'))
    if action == 'migrate_service_transport':
        return migrate_service_transport(root, value.get('base_url'))
    if action == 'rollback_service_transport':
        location = _maauma_authorization_root(root)
        value = _transport_call(transport.rollback, location)
        _LICENSE_CACHE.pop(str(location.resolve()), None)
        return value
    if action == 'activate':
        return activate(root, value.get('invite_code'))
    if action == 'license_check':
        return result('license_valid', **{k: v for k, v in ensure_runtime_license(root, force=True).items() if k != 'ok'})
    if action == 'prepare_feedback':
        return prepare_feedback(root, value.get('run_id'), value.get('description'), include_screenshot=value.get('include_screenshot', True))
    if action == 'submit_feedback':
        return submit_feedback(root, value.get('draft_id'))
    if action == 'feedback_status':
        return feedback_status(root, value.get('feedback_id'))
    raise SupportError('unsupported_action')

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    try:
        raw = sys.stdin.read(100000)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise SupportError('unsupported_action')
        with backend_process_lease(root):
            response = dispatch(root, value)
    except Exception as error:
        code = str(error) if isinstance(error, (SupportError, UpdateError)) else 'service_unavailable'
        response = dict(ok=False, status='error', code=code, message=MESSAGES.get(code, '操作未完成；本地复盘和配置仍保留。'))
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response['ok'] else 2
if __name__ == '__main__':
    raise SystemExit(main())

def _maauma_authorization_root(value):
    root = Path(value).resolve()
    if root.name.lower() == 'pacer':
        marker = root.parent / 'maauma-suite.json'
        if marker.is_file():
            try:
                if json.loads(marker.read_text(encoding='utf-8-sig')).get('schema_version') == 'maauma.suite.v1':
                    return root.parent
            except (OSError, ValueError, AttributeError):
                pass
    return root
