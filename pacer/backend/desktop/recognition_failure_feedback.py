"""Best-effort private upload of already archived, terminal recognition failures.

No screenshot capture, run-log scan, game action or request happens on import.
The queue and settings belong to the outer MaaUma installation, not app/pacer.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

DEFAULT_ENDPOINT = 'https://api.maauma.top/v1/bug-feedback'
QUEUE = Path('logs/recognition-feedback')
SETTINGS = Path('config/maauma-recognition-feedback.json')
SETTINGS_SCHEMA = 'maauma.recognition_feedback.settings.v1'
REPORT_SCHEMA = 'maauma.recognition_failure.v1'
TERMINAL_REASONS = {'completed', 'abandoned', 'operator_stop'}
MAX_IMAGE = 512 * 1024
MAX_BODY = 800 * 1024
MAX_BATCH = 2
_CODE = re.compile(r'[a-z][a-z0-9_]{0,159}\Z')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+\-]{0,99}\Z')
_FEEDBACK_ID = re.compile(r'BF-[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\Z', re.I)
_THREAD_LOCK = threading.Lock()
_ACTIVE = set()


def application_root(value):
    root = Path(value).resolve()
    return next((path for path in (root, *root.parents)
                 if (path / 'maauma-suite.json').is_file()), root)


def _read(path, maximum=64 * 1024):
    if not path.is_file() or path.stat().st_size > maximum:
        return {}
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    return value if isinstance(value, dict) else {}


def _write(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _inside(path, root):
    return path.resolve().is_relative_to(root)


def _enabled(root):
    path = root / SETTINGS
    if not _inside(path, root):
        return False
    if not path.exists():
        return True
    value = _read(path)
    return (value.get('schema_version') == SETTINGS_SCHEMA
            and value.get('enabled') is True)


def _uuid(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        return None
    return parsed if parsed == value.lower() else None


def _code(value):
    if (isinstance(value, str) and _CODE.fullmatch(value)
            and not any(part in value for part in
                        ('account', 'username', 'password', 'secret', 'api_key', 'token'))
            and not value.startswith(('md_', 'mc_', 'ghp_', 'sk_', 'akid'))):
        return value
    return None


def _number(value):
    return (value is None or type(value) is int and abs(value) <= 10**9
            or type(value) is float and math.isfinite(value) and abs(value) <= 10**9)


def _diagnostics(value):
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in ('decision_reason', 'stage', 'reason', 'field', 'status', 'error_type'):
        code = _code(value.get(key))
        if code:
            output[key] = code
    for key in ('points', 'pending_cost', 'selected_slot', 'confidence', 'score',
                'value', 'threshold', 'source_turn_index', 'stop_turn_index'):
        if key in value and _number(value[key]):
            output[key] = value[key]
    for key in ('not_recorded', 'source_is_prior_turn', 'abstained'):
        if type(value.get(key)) is bool:
            output[key] = value[key]
    for key in ('errors', 'levels'):
        if isinstance(value.get(key), list):
            output[key] = [item for item in value[key][:30]
                           if _number(item) or _code(item)]
    # Archive-owned groups only, one level deep; never paths or configuration.
    for key in ('larc_adaptation', 'recognition', 'numeric_ocr', 'observation'):
        if isinstance(value.get(key), dict):
            nested = {name: item for name, item in value[key].items()
                      if name not in ('larc_adaptation', 'recognition', 'numeric_ocr', 'observation')}
            cleaned = _diagnostics(nested)
            if cleaned:
                output[key] = cleaned
    return output


def _trim(value, limit):
    return value.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')


def _version(root, report):
    version = report.get('app_version')
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        version = _read(root / 'maauma-edition.json').get('version')
    return version if isinstance(version, str) and _VERSION.fullmatch(version) else 'unknown'


def _payload(root, report):
    reason = _code(report.get('reason')) or 'recognition_unreadable'
    detail = {'reason': reason, 'terminal_reason': report['terminal_reason']}
    for key in ('run_id','segment_id','segment_status','report_kind','stop_reason','frame_scope'):
        value=report.get(key)
        if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',value):detail[key]=value
    for key in ('stop_recorded_at','captured_at'):
        value=report.get(key)
        if isinstance(value,str) and re.fullmatch(r'[0-9T:.+Z-]{1,48}',value):detail[key]=value
    if isinstance(report.get('frame_sha256'),str) and re.fullmatch(r'[0-9a-f]{64}',report['frame_sha256']):detail['frame_sha256']=report['frame_sha256']
    if type(report.get('turn_index')) is int and 0 <= report['turn_index'] <= 10**6:
        detail['turn_index'] = report['turn_index']
    detail['diagnostics'] = _diagnostics(report.get('diagnostics'))
    return {'submission_id': report['report_id'], 'app_version': _version(root, report),
            'area': '育成识别', 'title': _trim('育成识别未完成：' + reason, 160),
            'steps': '', 'expected': '识别当前游戏画面并继续育成。',
            'actual': _trim(json.dumps(detail, ensure_ascii=False, sort_keys=True), 4000), 'contact': ''}


def _jpeg(path):
    if path.stat().st_size<=0:
        raise ValueError('invalid_image')
    raw = path.read_bytes()
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != 'JPEG':
            raise ValueError('invalid_image')
        image.load()
    return raw


def _freeze(folder, root, report, raw):
    path = folder / 'submission.json'
    digest = hashlib.sha256(raw).hexdigest()
    if not _inside(path, root):
        raise ValueError('invalid_queue_path')
    if not path.exists():
        frozen = {'report_id': report['report_id'], 'image_sha256': digest,
                  'payload': _payload(root, report)}
        # Exclusive creation freezes the first attempt even across processes.
        try:
            with path.open('x', encoding='utf8') as output:
                json.dump(frozen, output, ensure_ascii=False, indent=2)
        except FileExistsError:
            pass
    frozen = _read(path)
    if (frozen.get('report_id') != report['report_id'] or frozen.get('image_sha256') != digest
            or not isinstance(frozen.get('payload'), dict)):
        raise ValueError('frozen_submission_changed')
    payload = frozen['payload']
    limits = {'app_version': 100, 'area': 80, 'title': 160, 'steps': 4000,
              'expected': 2000, 'actual': 4000, 'contact': 200}
    if (set(payload) != {'submission_id', *limits} or payload['submission_id'] != report['report_id']
            or not payload['actual'] or any(not isinstance(payload[key], str)
                or len(payload[key].encode('utf-16-le')) > limit * 2 for key, limit in limits.items())):
        raise ValueError('invalid_frozen_submission')
    body = {**payload, 'attachment': {'name': 'recognition-failure.jpg',
            'media_type': 'image/jpeg', 'data_base64': base64.b64encode(raw).decode('ascii')}}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')
    return encoded


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _endpoint(value):
    if value == DEFAULT_ENDPOINT:
        return value
    parsed = urlsplit(value)
    try:
        loopback = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = False
    if (not loopback or parsed.scheme not in {'http', 'https'} or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('only_test_loopback_endpoint_allowed')
    return value


def _post(value, endpoint):
    body=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf8')
    request = Request(endpoint, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'User-Agent': 'MaaUma-RecognitionFeedback/1'})
    with build_opener(_NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(16 * 1024 + 1)
    if len(raw) > 16 * 1024:
        raise ValueError('invalid_response')
    result = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(result,dict) or result.get('ok') is not True:
        raise ValueError('invalid_response')
    return result


def _send(body, endpoint, *, state_path=None, progress=None):
    value=json.loads(body)
    attachment=value.get('attachment') or {}
    large_image=len(str(attachment.get('data_base64','')))>((512*1024+2)//3)*4
    if len(body)<=MAX_BODY and not large_image:
        answer=_post(value,endpoint)
        if not _FEEDBACK_ID.fullmatch(str(answer.get('feedback_id',''))):raise ValueError('invalid_response')
        return answer['feedback_id']
    from desktop.feedback_multipart import upload_bytes
    answer=upload_bytes(body,value['submission_id'],lambda part:_post(part,endpoint),state_path=state_path,progress=progress)
    if not _FEEDBACK_ID.fullmatch(str(answer.get('feedback_id',''))):raise ValueError('invalid_response')
    return answer['feedback_id']


def _defer(folder, queue, now, code):
    previous = _read(folder / 'retry.json')
    attempts = min(previous.get('attempts', 0), 16) + 1 if type(previous.get('attempts', 0)) is int else 1
    delay = 86400 if code == 'rate_limited' else min(6 * 3600, 60 * 2**min(attempts - 1, 9))
    value = {'attempts': attempts, 'not_before': now + delay, 'code': code}
    _write(folder / 'retry.json', value)
    # One queue-wide backoff prevents subsequent reports bypassing a 429 or outage.
    _write(queue / 'backoff.json', {'not_before': value['not_before'], 'code': code})


def flush(app_root, *, endpoint=DEFAULT_ENDPOINT):
    """Send at most two ready frames; failures never escape to the game caller."""
    summary = {'attempted': 0, 'sent': 0, 'skipped': 0, 'failed': 0}
    try:
        root = application_root(app_root)
        endpoint = _endpoint(endpoint)
        if not _enabled(root):
            return {**summary, 'disabled': True}
        queue = root / QUEUE
        if not queue.is_dir() or not _inside(queue, root):
            return summary
        now = time.time()
        if _read(queue / 'backoff.json').get('not_before', 0) > now:
            return {**summary, 'backoff': True}
        for folder in sorted(queue.iterdir(), key=lambda path: path.name):
            report_id = _uuid(folder.name)
            if not report_id or not folder.is_dir() or not _inside(folder, queue.resolve()):
                continue
            report_path, image_path = folder / 'report.json', folder / 'failure.jpg'
            if not all(_inside(path, queue.resolve()) for path in
                       (report_path, image_path, folder / 'ack.json', folder / 'retry.json')):
                continue
            try:
                report = _read(report_path)
                if (report.get('schema_version') != REPORT_SCHEMA or report.get('report_id') != report_id
                        or report.get('state') != 'ready' or report.get('terminal_reason') not in TERMINAL_REASONS
                        or report.get('image') != 'failure.jpg'):
                    summary['skipped'] += 1
                    continue
                ack = _read(folder / 'ack.json')
                if ack.get('report_id') == report_id and _FEEDBACK_ID.fullmatch(str(ack.get('feedback_id', ''))):
                    summary['skipped'] += 1
                    continue
                if _read(folder / 'retry.json').get('not_before', 0) > now:
                    continue
                body = _freeze(folder, root, report, _jpeg(image_path))
            except Exception:
                summary['skipped'] += 1
                continue
            summary['attempted'] += 1
            try:
                feedback_id = _send(body, endpoint, state_path=folder/'upload-state.json')
                _write(folder / 'ack.json', {'report_id': report_id, 'feedback_id': feedback_id,
                    'uploaded_at': datetime.now(timezone.utc).isoformat()})
                summary['sent'] += 1
            except HTTPError as error:
                summary['failed'] += 1
                _defer(folder, queue, now, 'rate_limited' if error.code == 429 else
                       'redirect_rejected' if 300 <= error.code < 400 else 'http_error')
                break
            except Exception:
                summary['failed'] += 1
                _defer(folder, queue, now, 'network_or_response_error')
                break
            if summary['attempted'] >= MAX_BATCH:
                break
    except Exception:
        summary['unavailable'] = True
    return summary


def flush_async(app_root):
    """Start one daemon per installation without waiting or raising to the game."""
    try:
        root = application_root(app_root)
        key = str(root).casefold()
        with _THREAD_LOCK:
            if key in _ACTIVE:
                return None
            _ACTIVE.add(key)
        def worker():
            try:
                flush(root)
            finally:
                with _THREAD_LOCK:
                    _ACTIVE.discard(key)
        thread = threading.Thread(target=worker, name='MaaUmaRecognitionFeedback', daemon=True)
        try:
            thread.start()
        except Exception:
            with _THREAD_LOCK:
                _ACTIVE.discard(key)
            return None
        return thread
    except Exception:
        return None
