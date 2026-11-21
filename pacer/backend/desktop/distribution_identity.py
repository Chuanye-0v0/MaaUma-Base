"""Public distribution provenance. This module does not read private credentials.

The signature is kept for maintainer verification; local format validation is
not signature verification and never grants a licence.
"""
from datetime import datetime
from pathlib import Path
import re
import uuid

from desktop.product_updates import contained, read_json, write_json, UpdateError

SCHEMA = 'uma.distribution_receipt.v1'
ACTIVATION_FILE = 'config/distribution-receipt.json'
PACKAGE_FILE = 'distribution-receipt.json'
FIELDS = ('schema_version', 'receipt_id', 'trace_id', 'product_id', 'kind',
          'version', 'issued_at', 'base_sha256', 'signature')


def validate_receipt(value, *, kind=None, product='uma-cut'):
    """Return only public, schema-defined fields, or None for damaged receipts."""
    if not isinstance(value, dict):
        return None
    if value.get('schema_version') != SCHEMA or value.get('product_id') != product:
        return None
    if not isinstance(value.get('kind'), str) or value['kind'] not in {'activation', 'package'} or kind and value['kind'] != kind:
        return None
    try:
        if str(uuid.UUID(value['receipt_id'])) != value['receipt_id']:
            return None
        issued = datetime.fromisoformat(value['issued_at'].replace('Z', '+00:00'))
        if issued.tzinfo is None:
            return None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(value.get('trace_id'), str) or not re.fullmatch(r'MU-(?:[0-9A-F]{4}-){3}[0-9A-F]{4}', value['trace_id']):
        return None
    if not isinstance(value.get('signature'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['signature']):
        return None
    version = value.get('version')
    digest = value.get('base_sha256')
    if version is not None and (not isinstance(version, str) or not re.fullmatch(r'[0-9A-Za-z.+_-]{1,80}', version)):
        return None
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        return None
    if value['kind'] == 'package' and (not version or not digest):
        return None
    return {key: value.get(key) for key in FIELDS}


def _read_receipt(root, name, kind):
    try:
        path = contained(Path(root), name)
        if not path.is_file() or path.stat().st_size > 16384:
            return None
        return validate_receipt(read_json(path), kind=kind)
    except (OSError, ValueError, UpdateError):
        return None


def save_activation_receipt(root, value):
    receipt = validate_receipt(value, kind='activation')
    if receipt is not None:
        previous = _read_receipt(root, ACTIVATION_FILE, 'activation')
        if previous != receipt:
            write_json(contained(Path(root), ACTIVATION_FILE), receipt)
    return receipt


def clear_activation_receipt(root):
    """A different successful activation/service must not retain the old owner."""
    contained(Path(root), ACTIVATION_FILE).unlink(missing_ok=True)


def distribution_info(root, *, activation_receipt=None):
    activation = (validate_receipt(activation_receipt, kind='activation') or
                  _read_receipt(root, ACTIVATION_FILE, 'activation'))
    package = _read_receipt(root, PACKAGE_FILE, 'package')
    receipt = activation or package
    return {
        'trace_id': receipt['trace_id'] if receipt else None,
        'distribution_source': receipt['kind'] if receipt else 'unknown',
        'distribution_receipt': receipt,
        'initial_package_receipt': package,
        'distribution_verification': 'not_verified_locally',
    }


def export_distribution(root):
    """Mark who exported, without retroactively attributing the recorded run."""
    info = distribution_info(root)
    return {
        'schema_version': 'uma.export_distribution.v1',
        'attribution': 'export_source_only',
        'runtime_identity': 'unknown',
        **info,
    }
