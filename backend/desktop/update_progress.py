"""Best-effort, credential-free update telemetry; no update policy lives here."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import time
import uuid

SCHEMA = 'maauma.update_progress.v1'
STAGES = {'checking', 'connecting', 'downloading', 'verifying', 'preparing',
          'prepared', 'current', 'failed'}
ITEMS = {'base': '基础程序', 'authorized_extension': '已授权扩展'}


class ProgressReporter:
    """Reports only values owned by this class, never request or error objects."""

    def __init__(self, sink, *, heartbeat_seconds=1.0, waiting_seconds=8.0):
        self._sink = sink
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._heartbeat_seconds = heartbeat_seconds
        self._waiting_seconds = waiting_seconds
        self._started = time.monotonic()
        self._last_emit = 0.0
        self._sequence = 0
        self._op_id = uuid.uuid4().hex
        self._stage = 'checking'
        self._item_id = None
        self._items = {}
        self._item_started = self._started
        self._last_byte = None
        self._last_byte_at = None
        self._samples = []
        self._verification_bytes = 0
        self._verification_total = None
        self._verification_step = None

    def __enter__(self):
        self._emit(force=True)
        self._thread = threading.Thread(target=self._heartbeat, daemon=True,
                                        name='update-progress')
        self._thread.start()
        return self

    def __exit__(self, *unused):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._heartbeat_seconds + 0.2)

    def _heartbeat(self):
        while not self._stop.wait(self._heartbeat_seconds):
            self._emit(force=True)

    def set_items(self, items):
        """items is a sequence of (public safe item id, exact length or None)."""
        with self._lock:
            self._items = {
                key: {'received': 0, 'total': self._size(total),
                      'total_source': 'manifest' if self._size(total) else 'unknown',
                      'complete': False, 'failed': False}
                for key, total in items if key in ITEMS
            }
            self._emit(force=True)

    @staticmethod
    def _size(value):
        return value if type(value) is int and value > 0 else None

    def stage(self, stage):
        if stage not in STAGES:
            return
        with self._lock:
            self._stage = stage
            self._emit(force=True)

    def start_item(self, item_id):
        if item_id not in self._items:
            return
        with self._lock:
            self._item_id = item_id
            self._item_started = time.monotonic()
            self._last_byte = None
            self._last_byte_at = None
            self._samples = [(self._item_started, 0)]
            self._verification_bytes = 0
            self._verification_total = None
            self._verification_step = None
            self._stage = 'connecting'
            self._emit(force=True)

    def connected(self, content_length=None):
        with self._lock:
            if self._item_id is None:
                return
            total = self._size(content_length)
            if total is not None:
                self._items[self._item_id].update(total=total, total_source='response')
            self._stage = 'downloading'
            self._emit(force=True)

    def received(self, count):
        if type(count) is not int or count <= 0:
            return
        with self._lock:
            if self._item_id is None:
                return
            self._items[self._item_id]['received'] += count
            self._last_byte = time.monotonic()
            self._last_byte_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            self._samples.append((self._last_byte, self._items[self._item_id]['received']))
            cutoff = self._last_byte - 5.0
            while len(self._samples) > 2 and self._samples[1][0] < cutoff:
                self._samples.pop(0)
            self._emit()

    def transferred(self):
        with self._lock:
            if self._item_id is not None:
                item = self._items[self._item_id]
                if item['total'] is None:
                    item.update(total=item['received'], total_source='completed')
            self._stage = 'verifying'
            self._emit(force=True)

    def verification(self, step, processed, total):
        if step not in {'archive_integrity', 'file_integrity', 'personalized_integrity', 'archive_metadata', 'receipt'}:
            return
        with self._lock:
            self._stage = 'verifying'
            self._verification_step = step
            self._verification_bytes = max(0, int(processed))
            self._verification_total = self._size(total)
            self._emit(force=processed == 0 or processed == total)

    def complete_item(self):
        with self._lock:
            if self._item_id is not None:
                self._items[self._item_id]['complete'] = True
            self._emit(force=True)

    def fail_item(self):
        with self._lock:
            if self._item_id is not None:
                self._items[self._item_id]['failed'] = True
            self._stage = 'failed'
            self._emit(force=True)

    def finish(self, response):
        with self._lock:
            status = response.get('status') if isinstance(response, dict) else None
            if isinstance(response, dict) and response.get('ok') is True:
                component = response.get('component')
                if isinstance(component, dict):
                    status = component.get('status')
                    if component.get('ok') is not True:
                        self._stage = 'failed'
                        self._emit(force=True)
                        return
                self._stage = 'prepared' if status == 'prepared' else 'current'
            else:
                self._stage = 'failed'
            self._emit(force=True)

    def _emit(self, *, force=False):
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_emit < 0.25:
                return
            self._last_emit = now
            self._sequence += 1
            item = self._items.get(self._item_id, {})
            received = item.get('received', 0)
            total = item.get('total')
            idle = now - (self._last_byte if self._last_byte is not None else self._item_started)
            speed = 0.0
            if self._stage == 'downloading' and self._samples and idle < 3.0:
                sample_time, sample_bytes = self._samples[0]
                if now > sample_time:
                    speed = max(0.0, (received - sample_bytes) / (now - sample_time))
            totals = [row['total'] for row in self._items.values()]
            overall_total = sum(totals) if totals and all(value is not None for value in totals) else None
            value = {
                'schema_version': SCHEMA, 'op_id': self._op_id, 'sequence': self._sequence,
                'elapsed_seconds': round(now - self._started, 2), 'stage': self._stage,
                'item_id': self._item_id, 'item_label': ITEMS.get(self._item_id),
                'item_index': list(self._items).index(self._item_id) + 1 if self._item_id in self._items else 0,
                'item_count': len(self._items),
                'completed_items': sum(row['complete'] for row in self._items.values()),
                'failed_items': sum(row['failed'] for row in self._items.values()),
                'item_status': ('failed' if item.get('failed') else
                                'verified' if item.get('complete') else
                                'active' if self._item_id else None),
                'received_bytes': received, 'total_bytes': total,
                'total_source': item.get('total_source', 'unknown'),
                'overall_received_bytes': sum(row['received'] for row in self._items.values()),
                'overall_total_bytes': overall_total,
                'bytes_per_second': round(speed, 1),
                'eta_seconds': round(max(0, total - received) / speed, 1) if total is not None and speed > 0 else None,
                'last_received_at': self._last_byte_at, 'idle_seconds': round(idle, 2),
                'waiting': self._stage in {'connecting', 'downloading'} and idle >= self._waiting_seconds,
                'verification_step': self._verification_step,
                'verification_bytes': self._verification_bytes,
                'verification_total_bytes': self._verification_total,
            }
            try:
                self._sink(value)
            except Exception:
                # A closed GUI progress pipe never changes an update transaction.
                pass


def jsonl_sink(stream):
    def send(value):
        stream.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
        stream.flush()
    return send


def content_length(stream):
    try:
        value = stream.headers.get('Content-Length')
        size = int(value)
        return size if 0 < size <= 2 * 1024 ** 3 else None
    except (AttributeError, TypeError, ValueError):
        return None


def read_chunk(stream, size=64 * 1024):
    # HTTPResponse.read(n) may wait for all n bytes. read1 reports real incoming
    # chunks promptly; file/local test streams retain their ordinary read path.
    return getattr(stream, 'read1', stream.read)(size)
