"""Retain unresolved choice-changing training frames, without device I/O."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil

import cv2
import numpy as np


SCHEMA = 'uma.candidate_failure_evidence.v1'
FOLDER = 'session/candidate_failures'
TABS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
IMPACT_REASONS = frozenset({'known_lower_bound_exceeds_eligible', 'unknown_primary_can_change_selection',
                            'purchased_training_quality_incomplete', 'competitive_candidate_failure_unknown'})
MAX_RECORDS = 4
MAX_IMAGES = 8
MAX_TOTAL_BYTES = 2 * 1024 * 1024
FRAME_NAMES = frozenset({'before.png', 'after.png', 'before_row.png', 'after_row.png'})


def _inside(path, root):
    result = Path(path).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise ValueError('candidate_failure_path_outside_run')
    return result


def _diagnostic(value):
    value = value if isinstance(value, dict) else {}
    result = {key: value[key] for key in ('usable', 'confidence', 'primary_gain', 'failure_rate_verified')
              if key in value and (value[key] is None or type(value[key]) in (bool, int)
                  or type(value[key]) is float and math.isfinite(value[key]))}
    result['reasons'] = [str(item)[:160] for item in value.get('reasons', [])[:12]]
    result['verified_gains'] = {key: amount for key, amount in value.get('verified_gains', {}).items()
        if key in (*TABS, 'skill_pt') and type(amount) in (int, float) and math.isfinite(amount)}
    result['gain_rows'] = [{key: row[key] for key in ('attribute', 'row_index', 'status', 'reason',
        'stage', 'segmentation_succeeded', 'style') if key in row}
        for row in value.get('gain_rows', [])[:8] if isinstance(row, dict)]
    return result


def _read_frame(path):
    if not path.is_file():
        return None
    raw = path.read_bytes()
    if not 0 < len(raw) <= 16 * 1024 * 1024:
        raise ValueError('candidate_failure_image_size_invalid')
    frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None or not 1 <= frame.shape[0] * frame.shape[1] <= 16_000_000:
        raise ValueError('candidate_failure_image_invalid')
    return frame


def _crop(frame, diagnostic):
    # Only use an explicit parser-provided row rectangle; never invent one.
    for row in diagnostic.get('gain_rows', []):
        roi = row.get('roi') or row.get('bbox') if isinstance(row, dict) else None
        if (isinstance(roi, (list, tuple)) and len(roi) == 4
                and all(type(value) is int for value in roi)):
            x1, y1, x2, y2 = roi
            if 0 <= x1 < x2 <= frame.shape[1] and 0 <= y1 < y2 <= frame.shape[0]:
                return frame[y1:y2, x1:x2]
    return None


def archive_refresh_failures(run_root, execution_root, diagnostic, *, run_id, turn_index):
    """Persist only requested bad candidates that still failed after refresh.

    This is optional evidence capture. A diagnostic failure must not replace
    an eligible game action, replay a training, or create a success receipt.
    """
    run = Path(run_root).resolve()
    capture = _inside(execution_root, run)
    if (run.name != run_id or diagnostic.get('run_session_id') != run_id
            or diagnostic.get('turn_index') != turn_index or type(turn_index) is not int
            or not 1 <= turn_index <= 999 or capture.name != 'capture'):
        raise ValueError('candidate_failure_context_mismatch')
    relative = capture.relative_to(run).parts
    if (len(relative) != 3 or not relative[0].startswith('autonomous_')
            or not relative[1].startswith('turn_')
            or any(not re.fullmatch(r'[A-Za-z0-9_-]+', part) for part in relative)):
        raise ValueError('candidate_failure_capture_context_invalid')
    after = {row.get('training_type'): row for row in diagnostic.get('after', []) if isinstance(row, dict)}
    records = []
    for request in diagnostic.get('requests', [])[:2]:
        if not isinstance(request, dict):
            continue
        tab = request.get('training_type')
        final = after.get(tab, {})
        if (tab not in TABS or final.get('usable') is not False
                or not IMPACT_REASONS.intersection(request.get('reasons', []))):
            continue
        folder = _inside(run / FOLDER / f'{relative[0]}__{relative[1]}__{tab}', run)
        folder.mkdir(parents=True, exist_ok=True)
        entry = {'schema_version': SCHEMA, 'run_id': run_id, 'turn_index': turn_index,
            'training_type': tab, 'captured_at': datetime.now(timezone.utc).isoformat(),
            'scope': 'unresolved_candidate_after_bounded_refresh; game continued',
            'choice_reasons': [str(item)[:160] for item in request.get('reasons', [])[:12]],
            'before': _diagnostic(request.get('before')), 'after': _diagnostic(final), 'frames': []}
        for phase, source in [('before', capture / 'candidate_refresh_sources' / f'{tab}.png'),
                              ('after', capture / 'raw_720' / f'{tab}.png')]:
            source = _inside(source, capture)
            target = _inside(folder / f'{phase}.png', run)
            try:
                frame = _read_frame(source)
                if frame is None:
                    entry['frames'].append({'phase': phase, 'status': 'missing'})
                    continue
                shutil.copyfile(source, target)
                entry['frames'].append({'phase': phase, 'status': 'retained', 'file': target.name,
                    'source_capture_relative': source.relative_to(run).as_posix()})
                row = _crop(frame, request.get('before', {}) if phase == 'before' else final)
                if row is not None:
                    cropped = _inside(folder / f'{phase}_row.png', run)
                    if cv2.imwrite(str(cropped), row):
                        entry['frames'].append({'phase': phase + '_row', 'status': 'retained', 'file': cropped.name})
            except (OSError, ValueError, cv2.error) as error:
                entry['frames'].append({'phase': phase, 'status': 'unavailable', 'error': type(error).__name__})
        path = _inside(folder / 'manifest.json', run)
        temporary = _inside(folder / 'manifest.tmp', run)
        temporary.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        records.append({'turn_index': turn_index, 'training_type': tab,
                        'retained_images': sum(frame['status'] == 'retained' for frame in entry['frames'])})
    return {'status': 'recorded' if records else 'no_unresolved_choice_candidate', 'records': records}


def archive_pipeline_refresh_failures(args, execution_root, diagnostic):
    """Bound evidence to the existing run session without affecting execution."""
    try:
        session = Path(args.transition_session_json)
        if session.parent.name != 'session':
            return {'status': 'context_unavailable'}
        return archive_refresh_failures(session.parent.parent, execution_root, diagnostic,
            run_id=args.run_session_id, turn_index=args.turn_index)
    except (OSError, ValueError, TypeError, AttributeError, cv2.error) as error:
        return {'status': 'unavailable', 'error': type(error).__name__}


def review_candidate_failures(run_root, *, include_previews=False):
    """Read owned saved evidence; export a bounded set of JPEG previews."""
    run = Path(run_root).resolve()
    try:
        base = _inside(run / FOLDER, run)
    except (OSError, ValueError):
        return {'schema_version': SCHEMA, 'records': [], 'available_records': 0,
                'status': 'evidence_path_unavailable'}, {}
    candidates = []
    for path in base.glob('*/manifest.json') if base.is_dir() else []:
        try:
            path = _inside(path, run)
            entry = json.loads(path.read_text(encoding='utf-8-sig'))
            if (not isinstance(entry, dict) or entry.get('schema_version') != SCHEMA or entry.get('run_id') != run.name
                    or entry.get('training_type') not in TABS or type(entry.get('turn_index')) is not int):
                continue
            candidates.append((entry['turn_index'], path.parent.name, path.parent, entry))
        except (OSError, ValueError, TypeError):
            continue
    attachments = {}
    records = []
    total_bytes = 0
    for _, _, folder, entry in sorted(candidates, key=lambda row: (row[0], row[1]), reverse=True)[:MAX_RECORDS]:
        public = {key: entry.get(key) for key in ('turn_index', 'training_type', 'scope', 'choice_reasons',
                                                 'before', 'after')}
        public['frames'] = []
        frames = entry.get('frames')
        for item in frames[:4] if isinstance(frames, list) else []:
            if not isinstance(item, dict):
                continue
            view = {key: item[key] for key in ('phase', 'status', 'error') if key in item}
            name = item.get('file')
            if item.get('status') == 'retained' and isinstance(name, str) and name in FRAME_NAMES:
                try:
                    path = _inside(folder / name, run)
                    if not path.is_relative_to(base):
                        continue
                    frame = _read_frame(path)
                    if frame is None:
                        view['status'] = 'missing'
                    elif include_previews and len(attachments) < MAX_IMAGES:
                        from core.auto_trainer.cut_stop_diagnostic import preview_jpeg
                        raw, _, _, _ = preview_jpeg(frame)
                        if total_bytes + len(raw) <= MAX_TOTAL_BYTES:
                            attachment = f'识别异常/{len(records) + 1:02d}_{entry["training_type"]}_{Path(name).stem}.jpg'
                            attachments[attachment] = raw
                            total_bytes += len(raw)
                            view['attachment'] = attachment
                        else:
                            view['preview_omitted'] = 'export_size_limit'
                    elif include_previews:
                        view['preview_omitted'] = 'export_image_limit'
                except (OSError, ValueError, TypeError, cv2.error):
                    view['status'] = 'unavailable'
            public['frames'].append(view)
        records.append(public)
    return {'schema_version': SCHEMA, 'records': records, 'available_records': len(candidates),
            'export_record_limit': MAX_RECORDS, 'export_image_limit': MAX_IMAGES,
            'note': 'Saved failed-candidate frames, not a new capture or a stopped career.'}, attachments
