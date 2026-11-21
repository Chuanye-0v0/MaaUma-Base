"""Validated runtime permission for one Pacer host process; never persisted.

Entries are scoped to an application and the exact server-confirmed
client/binding/device/product identity. A new Python host starts with an empty
store. Normal run completion keeps the entry; explicit rejection invalidates
all controllers which borrowed that application's entry.
"""
from copy import deepcopy
from pathlib import Path
import threading


IDENTITY_FIELDS = ('client_id', 'binding_id', 'device_id', 'product_id')


def identity_key(value):
    if not isinstance(value, dict) or value.get('product_id') != 'uma-cut':
        return None
    key = tuple(value.get(name) for name in IDENTITY_FIELDS)
    return key if all(isinstance(item, str) and item for item in key) else None


class RuntimeLicenseSessions:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}
        self._versions = {}

    @staticmethod
    def _root(root):
        return str(Path(root).resolve())

    def version(self, root):
        with self._lock:
            return self._versions.get(self._root(root), 0)

    def has_entry(self, root, now):
        with self._lock:
            value = self._entries.get(self._root(root))
            return value is not None and now < max(value['online_deadline'], value['offline_deadline'])

    def remember(self, root, value, *, verified_at, online_deadline,
                 offline_deadline, offline_seconds):
        identity = identity_key(value)
        if identity is None:
            return None  # A response without bound identity cannot be shared.
        with self._lock:
            key = self._root(root)
            previous = self._entries.get(key)
            if previous is not None and (previous['identity'] != identity
                    or offline_deadline < previous['offline_deadline']):
                self._versions[key] = self._versions.get(key, 0) + 1
            version = self._versions.get(key, 0)
            self._entries[key] = dict(identity=identity, verified_at=verified_at,
                online_deadline=online_deadline, offline_deadline=offline_deadline,
                offline_seconds=offline_seconds, version=version)
            return version

    def borrow(self, root, identity, now):
        expected = identity_key(identity)
        with self._lock:
            key = self._root(root)
            value = self._entries.get(key)
            if value is None or now >= max(value['online_deadline'], value['offline_deadline']):
                return None
            if expected is None or value['identity'] != expected:
                self.invalidate(root)
                return None
            return deepcopy(value)

    def invalidate(self, root):
        with self._lock:
            key = self._root(root)
            self._entries.pop(key, None)
            self._versions[key] = self._versions.get(key, 0) + 1


PROCESS_LICENSE_SESSIONS = RuntimeLicenseSessions()
