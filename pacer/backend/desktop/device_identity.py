"""Windows device identity bridge. Private keys never leave the DPAPI helper."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

class DeviceIdentityError(RuntimeError):
    pass


def _safe_identity_diagnostic(value, *, returncode=None):
    diag = {}
    if isinstance(value, dict):
        stage = value.get('stage')
        if isinstance(stage, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,47}', stage):
            diag['stage'] = stage
        error_type = value.get('error_type')
        if isinstance(error_type, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', error_type):
            diag['error_type'] = error_type
        for key in ('win32_error', 'hresult'):
            num = value.get(key)
            if type(num) is int and num != 0:
                diag[key] = num
    if type(returncode) is int and returncode != 0:
        diag['returncode'] = returncode
    return diag


def _identity_error(code, diag=None):
    error = DeviceIdentityError(code)
    if diag:
        error.identity_diagnostic = diag
    return error


def helper(root, action, **fields):
    root = _maauma_authorization_root(root)
    executable = Path(root) / ('MaaUmaIdentity.exe' if (Path(root) / 'MaaUmaIdentity.exe').is_file() else 'UmaCutPanel.exe')
    if os.name != 'nt' or not executable.is_file():
        raise _identity_error('device_identity_unavailable', {'stage': 'helper_missing'})
    try:
        completed = subprocess.run([str(executable), '--device-identity'], cwd=str(root), input=json.dumps({'action': action, **fields}, ensure_ascii=False), encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=dict(os.environ, DOTNET_ROOT=str(Path(root) / 'dotnet'), DOTNET_MULTILEVEL_LOOKUP='0'))
    except subprocess.TimeoutExpired:
        raise _identity_error('device_identity_unavailable', {'stage': 'helper_timeout'}) from None
    except OSError:
        raise _identity_error('device_identity_unavailable', {'stage': 'helper_exec'}) from None
    if len(completed.stdout) > 4 * 1024 ** 2:
        raise _identity_error('device_identity_unavailable', {'stage': 'helper_output_too_large', 'returncode': completed.returncode} if completed.returncode else {'stage': 'helper_output_too_large'})
    try:
        response = json.loads(completed.stdout.lstrip('\ufeff').strip())
    except ValueError:
        raise _identity_error('device_identity_unavailable', _safe_identity_diagnostic({'stage': 'helper_output'}, returncode=completed.returncode)) from None
    if not isinstance(response, dict) or response.get('ok') is not True:
        code = response.get('code') if isinstance(response, dict) else None
        resolved = code if code in {'device_identity_unavailable', 'device_credentials_unreadable', 'device_key_unreadable', 'device_changed'} else 'device_identity_unavailable'
        raise _identity_error(resolved, _safe_identity_diagnostic(response, returncode=completed.returncode))
    if completed.returncode:
        raise _identity_error('device_identity_unavailable', _safe_identity_diagnostic({'stage': 'helper_exit'}, returncode=completed.returncode))
    return response

def identity(root):
    response = helper(root, 'identity')
    if not re.fullmatch('[a-f0-9]{64}', str(response.get('device_id', ''))) or not isinstance(response.get('public_key'), str):
        raise DeviceIdentityError('device_identity_unavailable')
    return {key: response[key] for key in ('device_id', 'public_key')}

def signed_headers(root, method, path, data=b'', access_token=''):
    if method not in {'GET', 'POST'} or not path.startswith('/v1/') or '\n' in path or ('\r' in path):
        raise DeviceIdentityError('device_identity_unavailable')
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = '\n'.join(('MaaUma-Device-v1', timestamp, nonce, method, path, hashlib.sha256(data or b'').hexdigest(), hashlib.sha256(access_token.encode('utf8')).hexdigest()))
    response = helper(root, 'sign', message_base64=base64.b64encode(canonical.encode('utf8')).decode('ascii'))
    signature = response.get('signature')
    try:
        if len(base64.b64decode(signature, validate=True)) != 64:
            raise ValueError()
    except (ValueError, TypeError):
        raise DeviceIdentityError('device_identity_unavailable') from None
    return {'X-MaaUma-Time': timestamp, 'X-MaaUma-Nonce': nonce, 'X-MaaUma-Signature': signature}

def seal_credentials(root, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    response = helper(root, 'seal', payload_base64=base64.b64encode(raw).decode('ascii'))
    return {'schema_version': 'uma.private_service_auth.v2', 'protection': 'windows-user-dpapi-v1', 'encrypted': response['encrypted']}

def open_credentials(root, value):
    if value.get('schema_version') != 'uma.private_service_auth.v2' or value.get('protection') != 'windows-user-dpapi-v1':
        raise DeviceIdentityError('device_credentials_unreadable')
    response = helper(root, 'unseal', encrypted=value.get('encrypted'))
    try:
        opened = json.loads(base64.b64decode(response['payload_base64'], validate=True).decode('utf8'))
        if not isinstance(opened, dict):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise DeviceIdentityError('device_credentials_unreadable') from None
    return opened

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
