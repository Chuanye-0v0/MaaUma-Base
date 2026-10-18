"""Published service aliases, bounded transport retry, and recoverable config migration.

No origin supplied in a runtime request becomes trusted.  The deployment owner
must publish the reviewed CN origin in this module before enabling CN routing.
Only the test context below can substitute loopback origins.  No device key or
plaintext credential is stored here or in the recovery journal.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import base64
import errno
import json
import os
from pathlib import Path
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
import uuid

LEGACY_ORIGIN = 'https://maauma-private.2234198378.workers.dev'
APPROVED_CN_ORIGIN = 'https://api.maauma.top'  # Reviewed release-owner origin.
CONFIG = 'config/private-service.json'
AUTH = 'config/private-service-auth.json'
JOURNAL = 'config/private-service-origin-migration.json'
_FIXTURE_POLICY = ContextVar('maauma_service_fixture_policy', default=None)
_LOCAL = threading.local()
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_TRANSIENT_CODES = {None, 'service_unavailable', 'gateway_unavailable',
    'upstream_unavailable', 'gateway_timeout', 'cn_gateway_unavailable',
    'gateway_upstream_unavailable', 'gateway_configuration_invalid',
    'gateway_client_ip_unavailable'}


class TransportError(RuntimeError):
    pass


def origin(value, *, loopback=False):
    if not isinstance(value, str):
        raise TransportError('invalid_service_address')
    value = value.strip().rstrip('/')
    parsed = urlsplit(value)
    local = loopback and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
    if (not (parsed.scheme == 'https' or local) or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment
            or '\r' in value or '\n' in value):
        raise TransportError('invalid_service_address')
    return parsed.scheme.lower() + '://' + parsed.netloc.lower()


@dataclass(frozen=True)
class TrustedPolicy:
    cn_origin: str | None
    legacy_origin: str = LEGACY_ORIGIN
    loopback_fixture: bool = False

    def __post_init__(self):
        values = [self.legacy_origin] + ([self.cn_origin] if self.cn_origin is not None else [])
        for value in values:
            parsed = urlsplit(value)
            if self.loopback_fixture and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
                raise TransportError('fixture_origin_must_be_loopback')
            if origin(value, loopback=self.loopback_fixture) != value:
                raise TransportError('trusted_origin_must_be_canonical')

    @property
    def approved_origins(self):
        return tuple(dict.fromkeys(([self.cn_origin] if self.cn_origin else []) + [self.legacy_origin]))


def policy():
    return _FIXTURE_POLICY.get() or TrustedPolicy(APPROVED_CN_ORIGIN)


@contextmanager
def fixture_policy(cn_origin, legacy_origin):
    """In-process loopback fixture only; never exposed by a client dispatch."""
    value = TrustedPolicy(cn_origin, legacy_origin, loopback_fixture=True)
    token = _FIXTURE_POLICY.set(value)
    try:
        yield value
    finally:
        _FIXTURE_POLICY.reset(token)


def approved_target(value):
    selected = policy()
    target = origin(value, loopback=selected.loopback_fixture)
    if target not in selected.approved_origins:
        raise TransportError('service_origin_not_trusted')
    return target


def api_origins(cfg, *, binary=False, anonymous=False):
    selected = policy()
    base = origin(cfg['base_url'], loopback=True)  # Existing explicit localhost fixtures stay isolated.
    if base not in selected.approved_origins:
        return (base,)  # Explicit custom services retain their old isolated boundary.
    if binary:
        return (selected.legacy_origin,)  # Small JSON gateways are not large-ZIP proxies.
    if anonymous and selected.cn_origin is not None:
        return ((selected.legacy_origin, selected.cn_origin) if cfg.get('api_transport') == 'overseas'
                else (selected.cn_origin, selected.legacy_origin))
    if selected.cn_origin is None or base != selected.cn_origin:
        return (base,)  # Existing credentials migrate explicitly before becoming CN-first.
    if cfg.get('api_transport') == 'overseas':
        return (selected.legacy_origin, selected.cn_origin)
    return (selected.cn_origin, selected.legacy_origin)


def api_path(cfg, value):
    selected = policy()
    if not isinstance(value, str) or '\r' in value or '\n' in value:
        raise TransportError('invalid_service_path')
    parsed = urlsplit(value)
    if parsed.fragment or parsed.username or parsed.password:
        raise TransportError('invalid_service_path')
    if parsed.scheme or parsed.netloc:
        candidate = origin(parsed.scheme + '://' + parsed.netloc, loopback=True)
        current = origin(cfg['base_url'], loopback=True)
        allowed = selected.approved_origins if current in selected.approved_origins else (current,)
        if candidate not in allowed:
            raise TransportError('service_origin_not_trusted')
    if not parsed.path.startswith('/v1/'):
        raise TransportError('invalid_service_path')
    return parsed.path + ('?' + parsed.query if parsed.query else '')


def transient(cause, code=None):
    if code not in _TRANSIENT_CODES:
        return False
    if isinstance(cause, HTTPError):
        return cause.code in {502, 503, 504}
    if isinstance(cause, URLError):
        cause = cause.reason
    if isinstance(cause, (ssl.SSLCertVerificationError, ssl.CertificateError)):
        return False
    if isinstance(cause, (TimeoutError, ConnectionError, socket.gaierror,
            ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return True
    return isinstance(cause, OSError) and (cause.errno in {
        errno.ETIMEDOUT, errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH,
        errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED} or
        getattr(cause, 'winerror', None) in {10050,10051,10052,10053,10054,10060,10061,10064,10065,11001,11002})


def mark(error, cause, code=None):
    error.maauma_transport_retryable = transient(cause, code)
    return error


def attempt(origins, once):
    values = tuple(dict.fromkeys(origins))
    if not 1 <= len(values) <= 2:
        raise TransportError('invalid_transport_policy')
    for index, base in enumerate(values):
        try:
            return once(base)  # The client constructs a fresh nonce/signature on every call.
        except Exception as error:
            if index + 1 == len(values) or getattr(error, 'maauma_transport_retryable', False) is not True:
                raise


def _path(root, name):
    root = Path(root).resolve()
    path = root / name
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise TransportError('service_configuration_path_invalid')
    return path


def _raw(path):
    return path.read_bytes() if path.is_file() else None


def _atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf8')


@contextmanager
def guard(root):
    """One short configuration transaction; never held while making a request."""
    root = Path(root).resolve()
    key = str(root)
    with _LOCKS_GUARD:
        local_lock = _LOCKS.setdefault(key, threading.RLock())
    with local_lock:
        held = getattr(_LOCAL, 'held', set())
        if key in held:
            yield
            return
        lock_path = _path(root, 'config/private-service-origin.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a+b') as stream:
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b'0');stream.flush()
            deadline = time.monotonic() + 1.
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
                        raise TransportError('service_configuration_busy') from None
                    time.sleep(.01)
            _LOCAL.held = held | {key}
            try:
                yield
            finally:
                _LOCAL.held = held
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _encode(raw):
    return base64.b64encode(raw).decode('ascii') if raw is not None else None


def _decode(value):
    return base64.b64decode(value, validate=True) if value is not None else None


def _journal(root):
    path = _path(root, JOURNAL)
    if not path.is_file():
        return None
    if path.stat().st_size > 4 * 1024**2:
        raise TransportError('service_migration_journal_invalid')
    value = json.loads(path.read_text('utf-8-sig'))
    if (not isinstance(value, dict) or value.get('schema_version') != 'maauma.service_origin_migration.v1'
            or value.get('phase') not in {'prepared','committed','rolled_back'}
            or set(value.get('before', {})) != {CONFIG, AUTH}
            or set(value.get('after', {})) != {CONFIG, AUTH}):
        raise TransportError('service_migration_journal_invalid')
    return value


def recover(root):
    with guard(root):
        value = _journal(root)
        if value is not None and value['phase'] == 'prepared':
            for name, raw in value['before'].items():
                _atomic(_path(root, name), _decode(raw))
            value['phase'] = 'rolled_back'
            _atomic(_path(root, JOURNAL), _json_bytes(value))
            return True
    return False


def snapshot(root):
    with guard(root):
        recover(root)
        return {name: _raw(_path(root, name)) for name in (CONFIG, AUTH)}


def commit(root, before, config, sealed_auth):
    """Commit only already-verified, already-sealed data; receipt/key are untouched."""
    with guard(root):
        recover(root)
        if before != {name: _raw(_path(root, name)) for name in (CONFIG, AUTH)}:
            raise TransportError('service_migration_state_changed')
        after = {CONFIG: _json_bytes(config), AUTH: _json_bytes(sealed_auth) if sealed_auth is not None else before[AUTH]}
        value = {'schema_version':'maauma.service_origin_migration.v1','phase':'prepared',
                 'before':{name:_encode(raw) for name,raw in before.items()},
                 'after':{name:_encode(raw) for name,raw in after.items()}}
        _atomic(_path(root, JOURNAL), _json_bytes(value))
        try:
            _atomic(_path(root, AUTH), after[AUTH])
            _atomic(_path(root, CONFIG), after[CONFIG])
            value['phase'] = 'committed'
            _atomic(_path(root, JOURNAL), _json_bytes(value))
        except OSError:
            recover(root)
            raise TransportError('service_migration_commit_failed') from None


def rollback(root):
    with guard(root):
        recover(root)
        value = _journal(root)
        if value is None or value['phase'] != 'committed':
            raise TransportError('service_migration_not_committed')
        current = {name:_encode(_raw(_path(root,name))) for name in (CONFIG,AUTH)}
        if current != value['after']:
            raise TransportError('service_migration_state_changed')
        value['phase'] = 'prepared'
        _atomic(_path(root,JOURNAL), _json_bytes(value))
        recover(root)
    return {'ok':True,'status':'service_migration_rolled_back'}


def migrate(root, target, *, config_loader, credentials_loader, sealer,
            request_info, request_license, receipt_validator):
    """Verify this exact target without fallback before changing local origin.

    Callbacks are the real client's existing identity/sign/seal boundaries.
    Tests substitute synthetic callbacks, never Windows credential material.
    """
    if target is None:
        target = policy().cn_origin
    if target is None:
        raise TransportError('cn_origin_not_configured')
    target = approved_target(target)
    before = snapshot(root)
    cfg = config_loader(root)
    source = origin(cfg['base_url'], loopback=True)
    if source not in policy().approved_origins:
        raise TransportError('migration_source_not_trusted')
    if source == target:
        return {'ok':True,'status':'service_transport_already_current','base_url':target}
    info = request_info(target)  # Explicit target only: fallback cannot prove it works.
    if info.get('schema_version') != 'uma.private_service.v1' or info.get('device_binding') != 'one-computer-v1':
        raise TransportError('service_mismatch')
    old_auth = json.loads(before[AUTH].decode('utf-8-sig')) if before[AUTH] else {}
    sealed = None
    authenticated = bool(old_auth)
    if authenticated:
        if (not isinstance(old_auth, dict)
                or old_auth.get('schema_version') != 'uma.private_service_auth.v2'
                or old_auth.get('protection') != 'windows-user-dpapi-v1'
                or not isinstance(old_auth.get('encrypted'), str)
                or 'access_token' in old_auth):
            # Legacy plaintext-token binding remains the existing client's
            # separate flow, never an origin-migration journal entry.
            raise TransportError('device_credentials_unreadable')
        auth = credentials_loader(root)
        products = auth.get('products', [])
        product = 'umaa-public' if 'umaa-public' in products else 'uma-cut' if 'uma-cut' in products else None
        if product is None:
            raise TransportError('wrong_product')
        response = request_license(target, auth, product)
        if (any(response.get(name) != auth.get(name) or not auth.get(name)
                for name in ('client_id','binding_id','device_id'))
                or response.get('product_id') != product
                or not isinstance(response.get('products'), list) or product not in response['products']
                or type(response.get('valid_for_seconds')) is not int or response['valid_for_seconds'] <= 0):
            raise TransportError('service_mismatch')
        receipt = receipt_validator(response.get('distribution_receipt'), product)
        if not isinstance(receipt, dict):
            raise TransportError('distribution_receipt_invalid')
        old_receipt = auth.get('distribution_receipt')
        if isinstance(old_receipt, dict) and old_receipt.get('trace_id') != receipt.get('trace_id'):
            raise TransportError('service_migration_trace_mismatch')
        value = dict(auth, base_url=target, products=response['products'],
                     allow_preview=response.get('allow_preview') is True)
        sealed = sealer(root, value)  # Keep token, key binding and original receipt.
        if (not isinstance(sealed, dict) or sealed.get('schema_version') != 'uma.private_service_auth.v2'
                or sealed.get('protection') != 'windows-user-dpapi-v1'
                or not isinstance(sealed.get('encrypted'), str) or 'access_token' in sealed):
            raise TransportError('device_credentials_unreadable')
    updated = dict(cfg, base_url=target, api_transport='cn')
    commit(root, before, updated, sealed)
    return {'ok':True,'status':'service_transport_migrated','base_url':target,
            'authenticated':authenticated,'identity_preserved':authenticated,
            'activation_receipt_changed':False,'new_activation_required':False}
