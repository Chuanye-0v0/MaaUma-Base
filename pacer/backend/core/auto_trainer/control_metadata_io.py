"""Bounded retries for metadata operations known not to append or send input."""
from __future__ import annotations

import time

_OPERATIONS = {'mkdir', 'temp_open', 'temp_write', 'temp_flush', 'temp_fsync', 'temp_close',
               'replace', 'temp_cleanup', 'audit_open', 'audit_write', 'audit_flush', 'audit_fsync', 'audit_close'}
_TARGETS = {'control_state', 'control_audit', 'control_requests', 'control_token', 'control_metadata'}
_SCHEMA = 'uma.control_metadata_io_failure.v1'
_DELAYS = (.05, .15)


def metadata_target(path):
    return {'control_state.json': 'control_state', 'control_audit.jsonl': 'control_audit',
            'control_requests.json': 'control_requests', 'control_token.json': 'control_token'}.get(
                path.name, 'control_metadata')


def safe_metadata_failure(value):
    if (not isinstance(value, dict) or value.get('schema_version') != _SCHEMA
            or not isinstance(value.get('operation'), str) or value['operation'] not in _OPERATIONS
            or not isinstance(value.get('target'), str) or value['target'] not in _TARGETS
            or type(value.get('attempts')) is not int or not 1 <= value['attempts'] <= 3):
        return {}
    result = {key: value[key] for key in ('schema_version', 'operation', 'target', 'attempts')}
    for key in ('errno', 'winerror'):
        if type(value.get(key)) is int and 0 <= value[key] <= 2147483647:
            result[key] = value[key]
    return result


def note_metadata_failure(exc, operation, target, attempts=1):
    # Preserve the inner operation and retry count when an outer stage sees it.
    if not safe_metadata_failure(getattr(exc, 'control_metadata_io_failure', None)):
        value = dict(schema_version=_SCHEMA, operation=operation, target=target, attempts=attempts)
        value.update(errno=getattr(exc, 'errno', None), winerror=getattr(exc, 'winerror', None))
        exc.control_metadata_io_failure = safe_metadata_failure(value)


def permission_retry(operation, target, callback):
    """Only call for mkdir, exclusive temp creation, replace, or pre-append open.

    Never retry an entire heartbeat, an audit write, or a game operation.
    """
    for attempt in range(1, 4):
        try:
            return callback()
        except PermissionError as exc:
            if attempt == 3:
                note_metadata_failure(exc, operation, target, attempt)
                raise
            time.sleep(_DELAYS[attempt - 1])
        except OSError as exc:
            note_metadata_failure(exc, operation, target, attempt)
            raise
