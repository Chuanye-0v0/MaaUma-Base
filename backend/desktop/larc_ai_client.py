"""Explicit local AI connection; only structured text choices cross the network.

config/larc-ai.json and config/larc-ai-auth.json are installation-local data,
never release-package or feedback contents. The latter uses the existing Windows
DPAPI helper. Saving/status/clearing a connection never contacts its provider.
"""
import ipaddress
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
from desktop import device_identity
CONFIG = 'config/larc-ai.json'
AUTH = 'config/larc-ai-auth.json'
TEST = 'config/larc-ai-test.json'
PROVIDER = 'openai-compatible'
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
_CONFIG_SCHEMA = 'uma.larc_ai_connection.v1'
_AUTH_PURPOSE = 'uma.larc_ai_credentials.v1'
_PAYLOAD_KEYS = {'schema_version', 'decision_id', 'node', 'run_id', 'context', 'candidates', 'allowed_action_ids', 'local_action_id', 'trigger_reasons'}
_MESSAGES = {'ai_not_configured': '尚未配置 AI 接口，请先填写服务地址、模型和 API Key。', 'ai_invalid_base_url': '接口地址需为 HTTPS 基址，例如 https://服务地址/v1，不能包含账号、查询参数或片段。', 'ai_invalid_model': '请填写服务商提供的模型名称，不能包含空格或控制字符。', 'ai_key_required': '首次保存或更换接口、模型时，请填写 API Key。', 'ai_invalid_key': 'API Key 格式无效，请检查是否误填了换行或其他内容。', 'ai_credentials_unreadable': '无法读取这台电脑保存的 AI 密钥，请重新填写并保存。', 'ai_config_read_failed': '无法读取本机 AI 配置，请重新保存接口设置。', 'ai_config_write_failed': '无法保存本机 AI 配置，请检查程序目录是否可写。', 'ai_invalid_payload': 'AI 决策数据格式或大小不符合要求，本次未发送。', 'ai_invalid_timeout': 'AI 请求等待时间无效。', 'ai_request_timeout': 'AI 接口响应超时。', 'ai_connection_failed': '暂时无法连接 AI 接口，请检查地址和网络。', 'ai_http_auth': 'AI 接口拒绝授权，请检查 API Key 和模型使用权限。', 'ai_http_rate_limit': 'AI 接口当前请求过多，请稍后重试。', 'ai_http_error': 'AI 服务返回错误，请检查服务状态和接口配置。', 'ai_redirect_blocked': 'AI 接口要求跳转地址；为避免泄露密钥，请填写最终的服务基址。', 'ai_response_too_large': 'AI 响应过大，本次结果未采用。', 'ai_response_invalid': 'AI 未返回有效的结构化决策，本次结果未采用。'}

class AIClientError(RuntimeError):

    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'ai_connection_failed'
        self.message = _MESSAGES[self.code]
        super().__init__(self.code)

class AIConfigurationError(AIClientError):
    pass

def _root(root):
    path = Path(root).resolve()
    return path.parent if path.name.casefold() == 'backend' else path

def _base(value):
    try:
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise ValueError()
        value = value.strip()
        if any((ord(c) < 33 or ord(c) > 126 for c in value)) or any((c in value for c in ('?', '#', '\\'))):
            raise ValueError()
        part = urlsplit(value)
        if not part.hostname or part.username is not None or part.password is not None:
            raise ValueError()
        if part.scheme not in {'https', 'http'} or part.port == 0:
            raise ValueError()
        loopback = part.hostname.lower() == 'localhost'
        try:
            loopback = loopback or ipaddress.ip_address(part.hostname).is_loopback
        except ValueError:
            pass
        if part.scheme == 'http' and (not loopback):
            raise ValueError()
        path = part.path.rstrip('/')
        if path.endswith('/chat/completions'):
            path = path[:-len('/chat/completions')]
        if not re.fullmatch('(?:/[A-Za-z0-9._~-]+)*', path):
            raise ValueError()
        if any((segment in {'.', '..'} for segment in path.split('/'))):
            raise ValueError()
        host = part.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        if part.port is not None and part.port != (443 if part.scheme == 'https' else 80):
            host += ':' + str(part.port)
        return urlunsplit((part.scheme, host, path, '', ''))
    except (ValueError, TypeError):
        raise AIConfigurationError('ai_invalid_base_url') from None

def _model(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:/@+\\-]{0,199}', value.strip()):
        raise AIConfigurationError('ai_invalid_model')
    return value.strip()

def _key(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 8192:
        raise AIConfigurationError('ai_invalid_key')
    value = value.strip()
    if any((not 33 <= ord(c) <= 126 for c in value)):
        raise AIConfigurationError('ai_invalid_key')
    return value

def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result

def _json(data):
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))

def _read(path, limit):
    try:
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError()
        value = _json(data.decode('utf8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, UnicodeError):
        raise AIConfigurationError('ai_config_read_failed') from None

def _configuration(root):
    if not (root / CONFIG).is_file():
        raise AIConfigurationError('ai_not_configured')
    value = _read(root / CONFIG, 16 * 1024)
    if value.get('schema_version') != _CONFIG_SCHEMA or value.get('provider') != PROVIDER:
        raise AIConfigurationError('ai_config_read_failed')
    return {'schema_version': _CONFIG_SCHEMA, 'provider': PROVIDER, 'base_url': _base(value.get('base_url')), 'model': _model(value.get('model'))}

def _credential(root, config):
    try:
        value = device_identity.open_credentials(root, _read(root / AUTH, 64 * 1024))
        if value.get('purpose') != _AUTH_PURPOSE or value.get('base_url') != config['base_url'] or value.get('model') != config['model']:
            raise ValueError()
        return _key(value.get('api_key'))
    except (device_identity.DeviceIdentityError, AIClientError, ValueError, TypeError, KeyError):
        raise AIConfigurationError('ai_credentials_unreadable') from None

def _write(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf8')
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        temp.replace(path)
    except (OSError, ValueError, TypeError):
        raise AIConfigurationError('ai_config_write_failed') from None
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass

def provider_status(root):
    """Local validation only. Never return the sealed blob or decrypted key."""
    root = _root(root)
    result = {'configured': False, 'provider': PROVIDER, 'model': '', 'base_url': ''}
    try:
        config = _configuration(root)
        result.update(model=config['model'], base_url=config['base_url'])
        _credential(root, config)
        test = _test_status(root, config)
        return dict(result, configured=True, test=test, message=test['message'])
    except AIClientError as error:
        return dict(result, test={'status': 'not_configured', 'message': error.message}, message=error.message)

def configure(root, base_url, model, api_key=None):
    root = _root(root)
    config = {'schema_version': _CONFIG_SCHEMA, 'provider': PROVIDER, 'base_url': _base(base_url), 'model': _model(model)}
    if api_key is None or (isinstance(api_key, str) and (not api_key.strip())):
        try:
            old = _configuration(root)
        except AIConfigurationError:
            raise AIConfigurationError('ai_key_required') from None
        if old != config:
            raise AIConfigurationError('ai_key_required')
        key = _credential(root, old)
    else:
        key = _key(api_key)
    try:
        sealed = device_identity.seal_credentials(root, {'purpose': _AUTH_PURPOSE, 'base_url': config['base_url'], 'model': config['model'], 'api_key': key})
    except device_identity.DeviceIdentityError:
        raise AIConfigurationError('ai_credentials_unreadable') from None
    _write(root / AUTH, sealed)
    _write(root / CONFIG, config)
    return {'configured': True, 'provider': PROVIDER, 'base_url': config['base_url'], 'model': config['model'], 'message': 'AI 接口已保存；尚未进行联网测试。'}

def clear(root):
    root = _root(root)
    try:
        for name in (CONFIG, AUTH):
            (root / name).unlink(missing_ok=True)
    except OSError:
        raise AIConfigurationError('ai_config_write_failed') from None
    return {'configured': False, 'provider': PROVIDER, 'base_url': '', 'model': '', 'message': '本机 AI 接口和密钥已清除。'}

class _NoRedirect(HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AIClientError('ai_redirect_blocked')

def _read_response(response, deadline):
    length = response.headers.get('Content-Length')
    if length is not None:
        try:
            if int(length) > MAX_RESPONSE_BYTES:
                raise AIClientError('ai_response_too_large')
        except ValueError:
            raise AIClientError('ai_response_invalid') from None
    chunks = []
    size = 0
    read = getattr(response, 'read1', response.read)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AIClientError('ai_request_timeout')
        connection = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if connection is not None:
            connection.settimeout(remaining)
        block = read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
        if not block:
            break
        chunks.append(block)
        size += len(block)
        if size > MAX_RESPONSE_BYTES:
            raise AIClientError('ai_response_too_large')
    return b''.join(chunks)

def _credential_version(root):
    return hashlib.sha256((root / AUTH).read_bytes()).hexdigest()

def _test_status(root, config):
    unknown = {'status': 'not_tested', 'message': 'AI 接口已保存；尚未对当前地址、模型和密钥进行连接测试。'}
    try:
        value = _read(root / TEST, 16 * 1024)
        if value.get('base_url') != config['base_url'] or value.get('model') != config['model'] or value.get('credential_version') != _credential_version(root) or (value.get('status') not in ('success', 'failed')):
            return unknown
        code = value.get('code')
        message = '连接测试成功；这不代表当前育成已启用 AI。' if value['status'] == 'success' else _MESSAGES.get(code, 'AI 连接测试未成功。')
        return {'status': value['status'], 'code': code if code in _MESSAGES else None, 'tested_at': value.get('tested_at'), 'elapsed_seconds': value.get('elapsed_seconds'), 'message': message}
    except (AIClientError, OSError):
        return unknown
