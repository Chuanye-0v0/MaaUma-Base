"""Archive a failed reader's own frame; enqueue only after that career closes.

This module never captures a device, chooses an action or sends a request.  A
reader/report without a bound image is reported as unavailable, not replaced
with a newer screenshot or the existing post-stop diagnostic capture.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

MAX_IMAGE_BYTES = 512 * 1024
SCHEMA = 'maauma.recognition_failure.v1'
QUEUE = Path('logs/recognition-feedback')
_CODE = re.compile(r'[a-zA-Z0-9_.:-]{1,160}\Z')
_FAILURES = ('unreadable', 'unrecognized', 'unknown', 'recognition', 'perception',
    'capture_failed', 'sidecar_failed', 'sidecar_quality_failed',
    'training_page_observation_failed', 'larc_ss_information_failed',
    'current_scene_unverified', 'main_menu_state_unverified')


def _recognition_reason(value):
    return isinstance(value, str) and any(marker in value for marker in _FAILURES)


def _app_root(explicit=None):
    if explicit is not None:
        return Path(explicit).resolve()
    return installed_application_root()


def installed_application_root(start=None):
    """Find the outer MaaUma app, including when invoked from app/pacer."""
    start = Path(start).resolve() if start is not None else Path(__file__).resolve().parent
    return next((path for path in (start, *start.parents)
        if (path / 'maauma-suite.json').is_file()), None)


def _inside(value, root, *, relative_to=None):
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    path = Path(value)
    if not path.is_absolute():
        path = (relative_to or root) / path
    path = path.resolve()
    return path if path.is_relative_to(root) else None


def _read(path):
    try:
        if path is None or path.stat().st_size > 4 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _last(value):
    return value[-1] if isinstance(value, list) and value and isinstance(value[-1], dict) else {}


def _diagnostics(result, reason):
    decision = result.get('decision') if isinstance(result.get('decision'), dict) else {}
    evidence = decision.get('evidence') if isinstance(decision.get('evidence'), dict) else {}
    output = {'decision_reason': decision.get('reason', reason)}
    for name in ('larc_adaptation', 'recognition', 'numeric_ocr', 'observation'):
        data = evidence.get(name)
        if not isinstance(data, dict):
            continue
        fields = {}
        for key in ('points', 'pending_cost', 'selected_slot', 'confidence', 'score'):
            if data.get(key) is None or type(data.get(key)) in (int, float):
                if key in data:
                    fields[key] = data[key]
        for key in ('errors', 'levels'):
            if isinstance(data.get(key), list):
                fields[key] = [item for item in data[key][:30]
                    if item is None or type(item) in (int, float)
                    or (isinstance(item, str) and _CODE.fullmatch(item))]
        if fields:
            output[name] = fields
    session = result.get('larc_adaptation_session')
    if isinstance(session, dict) and session.get('stage') in {
            'selecting', 'submission_pending', 'awaiting_result', 'complete'}:
        output['stage'] = session['stage']
    return output


def _status_fields(data, keys):
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in keys if key in data and
        (data[key] is None or type(data[key]) in (int, float, bool)
         or isinstance(data[key], str) and _CODE.fullmatch(data[key]))}


def _reader_diagnostics(report):
    """Finite reader state only; no OCR, arbitrary errors, account or paths."""
    ss = report.get('schema_version') == 'larc_ss_information_probe.v1'
    fallback = 'larc_ss_information_failed' if ss else 'mei_outing_inspection_failed'
    diagnostics = {'decision_reason': fallback, 'stage': 'larc_ss_information' if ss else 'unknown'}
    for key in ('reason', 'stage'):
        value = report.get(key)
        if isinstance(value, str) and _CODE.fullmatch(value):
            diagnostics['decision_reason' if key == 'reason' else key] = value
    failure_stage = report.get('failure_stage')
    if ss and isinstance(failure_stage, str) and failure_stage in {
            'entry', 'open', 'collect', 'scroll_up', 'scroll_down', 'close', 'return', 'cleanup'}:
        diagnostics['stage'] = failure_stage
    observation = _status_fields(report, ('status', 'probe_status', 'returned_to_training',
        'turn_consumed', 'turn_consuming_action_executed', 'same_training_hud', 'decision_safe',
        'performed', 'tap_performed', 'has_pending_operation'))
    for name, keys in (
            ('pending_operation', ('action_id', 'stage', 'turn_index', 'tap_performed')),
            ('failure_cleanup', ('status', 'original_reason', 'reason', 'close_input_performed', 'same_training_hud')),
            ('return_readiness', ('reads', 'seconds', 'stable_reads', 'ready')),
            ('hud_comparison', ('status', 'same', 'method', 'pixel_same', 'semantic_same', 'same_training_hud',
                'pixel_similarity', 'semantic_verified', 'selected_tab_same'))):
        fields = _status_fields(report.get(name), keys)
        if fields:
            observation[name] = fields
    hud = report.get('hud_comparison')
    if isinstance(hud, dict):
        fields = observation.setdefault('hud_comparison', {})
        conflicts = hud.get('conflict_fields')
        if isinstance(conflicts, list):
            fields['conflict_fields'] = [key for key in conflicts[:8]
                if isinstance(key, str) and _CODE.fullmatch(key)]
        scores = _status_fields(hud.get('pixel_scores'), ('remaining_turns', 'calendar',
            'speed', 'stamina', 'power', 'guts', 'wisdom', 'skill_pt'))
        if scores:
            fields['pixel_scores'] = {key: value for key, value in scores.items() if type(value) in (int, float)}
        semantic = hud.get('semantic_fields')
        if isinstance(semantic, dict):
            comparisons = {}
            for key in ('remaining_turns', 'speed', 'stamina', 'power', 'guts', 'wisdom', 'skill_pt'):
                row = semantic.get(key)
                if not isinstance(row, dict):
                    continue
                values = {name: row[name] for name in ('before', 'after') if name in row
                    and (row[name] is None or type(row[name]) is int
                        or type(row[name]) is float and math.isfinite(row[name]))}
                values.update({name: row[name] for name in ('before_accepted', 'after_accepted')
                    if type(row.get(name)) is bool})
                if values:
                    comparisons[key] = values
            if comparisons:
                fields['semantic_fields'] = comparisons
    for name in ('chooser_observations', 'confirmation_observations'):
        rows = report.get(name)
        if isinstance(rows, list):
            observation[name] = [_status_fields(row, ('status', 'screen', 'reason', 'completed'))
                for row in rows[-6:] if isinstance(row, dict)]
    from core.auto_trainer.stop_screenshot_evidence import safe_menu_identity_comparison
    saved_comparison = report.get('menu_identity_comparison')
    comparison_source = _reader_source(report)
    comparison = (safe_menu_identity_comparison(saved_comparison)
        if isinstance(saved_comparison,dict) and isinstance(comparison_source,dict)
        and saved_comparison.get('run_session_id') == comparison_source.get('run_session_id')
        and saved_comparison.get('turn_index') == comparison_source.get('turn_index') else {})
    if comparison:
        observation['menu_identity_comparison'] = comparison
    diagnostics['observation'] = observation
    return diagnostics


def _reader_source(report):
    # An explicit failed source owns the frame even when it is invalid/missing;
    # never substitute a successful cleanup frame or an earlier step for it.
    if 'failure_source' in report:
        return report['failure_source']
    if 'last_source' in report:
        return report['last_source']
    row = _last(report.get('steps'))
    source = row.get('source')
    if isinstance(source, dict):
        return {**source, 'path': source.get('path') or source.get('absolute_path'),
            'sha256': source.get('sha256') or row.get('source_sha256')}
    return None


def _reader_frame(report, path, segment, run_id, turn):
    if type(turn) is not int or not isinstance(run_id, str):
        return None
    if (report.get('run_session_id', run_id) != run_id
            or report.get('turn_index', turn) != turn):
        return None
    source = _reader_source(report)
    if (not isinstance(source, dict) or source.get('run_session_id') != run_id
            or type(source.get('turn_index')) is not int or source['turn_index'] != turn):
        return None
    frame = _inside(source.get('path') or source.get('absolute_path'), segment, relative_to=path.parent)
    digest = source.get('sha256')
    if (frame is None or not frame.is_file() or not isinstance(digest, str)
            or re.fullmatch(r'[a-fA-F0-9]{64}', digest) is None
            or hashlib.sha256(frame.read_bytes()).hexdigest() != digest.lower()):
        return None
    return frame, _reader_diagnostics(report), path, dict(source, path=str(frame), sha256=digest.lower())


def _pipeline_reader_failure(reason):
    return reason == 'larc_ss_information_failed' or (
        isinstance(reason, str) and reason.startswith('mei_outing_') and bool(_CODE.fullmatch(reason)))


def bound_reader_failure(summary):
    """Recognize owned failures even when their frame/report is unavailable."""
    return isinstance(summary, dict) and (_outing_failure_reference(summary)
        or _pipeline_reader_failure(summary.get('stop_reason')))


def _pipeline_reader_report(run_root, segment, summary):
    reason = summary.get('stop_reason')
    row = _last(summary.get('turns'))
    turn = (summary.get('final_progress') or {}).get('next_turn')
    output = _inside(row.get('output'), segment)
    if type(turn) is not int or row.get('turn_index') != turn or output is None:
        return None
    path = _inside(output / 'pipeline_report.json', output)
    if path is None:
        return None
    data = _read(path)
    if data.get('run_session_id', run_root.name) != run_root.name or data.get('turn_index', turn) != turn:
        return None
    report = data.get('report') if isinstance(data.get('report'), dict) else data
    stages = report.get('stages') if isinstance(report.get('stages'), dict) else {}
    if reason == 'larc_ss_information_failed':
        leaf = stages.get('larc_ss_information')
        if (not isinstance(leaf, dict) or leaf.get('schema_version') != 'larc_ss_information_probe.v1'
                or report.get('status') != reason or leaf.get('status') != 'stopped'):
            return None
    else:
        live = stages.get('live_loop')
        leaf = live.get('execution') if isinstance(live, dict) else None
        if (not isinstance(leaf, dict) or leaf.get('schema_version') != 'larc_outing_route.v1'
                or leaf.get('is_valid') is not False or leaf.get('reason') != reason
                or leaf.get('page_handoff') is not None):
            return None
    if (leaf.get('run_session_id', run_root.name) != run_root.name
            or leaf.get('turn_index', turn) != turn):
        return None
    source = _reader_source(leaf)
    if source is not None and (not isinstance(source, dict)
            or source.get('run_session_id') != run_root.name
            or type(source.get('turn_index')) is not int or source['turn_index'] != turn
            or _inside(source.get('path') or source.get('absolute_path'), output, relative_to=path.parent) is None):
        return None
    return leaf, path, output, turn


def _pipeline_reader_frame(run_root, segment, summary):
    owned = _pipeline_reader_report(run_root, segment, summary)
    if owned is None:
        return None
    leaf, path, output, turn = owned
    return _frame_from_report(leaf, path, output, summary.get('stop_reason'), run_id=run_root.name, turn=turn)


def _frame_from_report(report, path, run_root, reason, *, run_id=None, turn=None):
    """Only the last observation of the actual failed report owns its image."""
    if report.get('schema_version') in {'larc_ss_information_probe.v1', 'larc_outing_route.v1'}:
        return _reader_frame(report, path, run_root, run_id, turn)
    child = _inside(report.get('safe_advance_report_path'), run_root, relative_to=path.parent)
    if child is not None:
        return _frame_from_report(_read(child), child, run_root, reason)
    row = _last(report.get('step_reports'))
    result = row.get('result') if isinstance(row.get('result'), dict) else report
    decision = result.get('decision') if isinstance(result.get('decision'), dict) else {}
    evidence = decision.get('evidence') if isinstance(decision.get('evidence'), dict) else {}
    # An earlier failed wrapper must not relabel a later successful observation
    # (or its image) as the failed reader's frame.
    if decision.get('reason') and not (_recognition_reason(decision['reason'])
            or _recognition_reason(result.get('reason'))):
        return None
    source = row.get('screenshot_path') or report.get('source_screenshot_path')
    if not source:
        source = evidence.get('image')
    frame = _inside(source, run_root, relative_to=path.parent)
    if frame is not None and frame.is_file():
        safety = result.get('frame_safety') if isinstance(result.get('frame_safety'), dict) else {}
        recorded_sha = safety.get('source_image_sha256')
        if (isinstance(recorded_sha, str) and re.fullmatch(r'[a-fA-F0-9]{64}', recorded_sha)
                and hashlib.sha256(frame.read_bytes()).hexdigest() != recorded_sha.lower()):
            return None
        return frame, _diagnostics(result, reason), path
    return None


def _outing_failure_reference(summary):
    reference=summary.get('recognition_failure_source')
    return (summary.get('stop_reason')=='execution_exception' and isinstance(reference,dict)
        and reference.get('kind')=='mei_outing_inspection')


def _outing_failure_frame(run_root, segment, summary):
    """Read only the failed outing report's same-run, same-turn saved frame."""
    reference=summary['recognition_failure_source']
    path=_inside(reference.get('report_path'),segment)
    if path is None or path.name!='report.json':return None
    report=_read(path)
    if (report.get('schema_version')!='larc_outing_route.v1' or report.get('is_valid') is not False
            or report.get('page_handoff') is not None):
        return None
    source=report.get('failure_source') or report.get('last_source')
    bound=reference.get('source')
    turn=(summary.get('final_progress') or {}).get('next_turn')
    if (not isinstance(source,dict) or not isinstance(bound,dict) or source!=bound
            or source.get('run_session_id')!=run_root.name or type(turn) is not int
            or type(source.get('turn_index')) is not int
            or source.get('turn_index')!=turn
            or report.get('stage')!=reference.get('stage') or report.get('reason')!=reference.get('reason')):
        return None
    frame=_inside(source.get('path'),segment)
    digest=source.get('sha256')
    if (frame is None or not frame.is_file() or not isinstance(digest,str)
            or re.fullmatch(r'[a-f0-9]{64}',digest) is None
            or hashlib.sha256(frame.read_bytes()).hexdigest()!=digest):
        return None
    # Direct preparation failures use the same finite projection as pipeline
    # failures. Otherwise their already-recorded menu difference and input
    # status disappear before the saved-stop report reaches the feedback queue.
    return frame,_reader_diagnostics(report),path


def _failure_frame(run_root, summary):
    reason = summary.get('stop_reason', '')
    segment = _inside(summary.get('output'), run_root)
    if segment is None:
        return None
    if _outing_failure_reference(summary):
        # Invalid binding is unavailable; never fall back to an old pipeline.
        return _outing_failure_frame(run_root,segment,summary)
    if _pipeline_reader_failure(reason):
        # This report either owns its exact same-turn source or is unavailable.
        # Capture stages and successful older readers cannot replace it.
        return _pipeline_reader_frame(run_root, segment, summary)
    provider_reference = summary.get('recognition_failure_source')
    if reason == 'main_menu_state_unverified' and isinstance(provider_reference, dict):
        path = _inside(provider_reference.get('report_path'), segment)
        provider = _read(path)
        source = provider.get('source')
        if isinstance(source, dict) and path is not None:
            frame = _inside(source.get('path'), segment)
            digest = source.get('sha256')
            if (frame is not None and frame.is_file() and isinstance(digest, str)
                    and hashlib.sha256(frame.read_bytes()).hexdigest() == digest):
                analyzer = provider.get('analyzer') or {}
                errors = [code for code in analyzer.get('errors', [])
                    if isinstance(code, str) and _CODE.fullmatch(code)]
                diagnostics = {'decision_reason': reason, 'numeric_ocr': {'errors': errors}}
                return frame, diagnostics, path
    references = []
    for name in ('adaptation_reports', 'route_reports', 'race_reports'):
        row = _last(summary.get(name))
        if row.get('reason') == reason and row.get('reused') is not True:
            references.append(row.get('path'))
    # Growth can return before its row is appended to growth_adaptations.
    if reason in {'larc_growth_stopped', 'growth_adaptation_stopped'} or reason.startswith('larc_adaptation_'):
        growth = _last(summary.get('growth_adaptations'))
        if growth.get('ok') is False and growth.get('path'):
            references.append(str(Path(growth['path']) / 'grid/report.json'))
        turn = _last(summary.get('turns'))
        if turn.get('output'):
            references.append(str(Path(turn['output']) / 'growth/grid/report.json'))
    # Pipeline failures carry a specific stage's source; never pick a random
    # training tab from raw_720_dir when that stage did not identify one.
    turn = _last(summary.get('turns'))
    if turn.get('pipeline_status') == reason and turn.get('output'):
        references.append(str(Path(turn['output']) / 'pipeline_report.json'))
    for raw in references:
        path = _inside(raw, segment)
        if path is None or path.name not in {'report.json', 'safe_advance_report.json', 'pipeline_report.json'}:
            continue
        data = _read(path)
        stop_turn = (summary.get('final_progress') or {}).get('next_turn')
        if (type(data.get('turn_index')) is int and type(stop_turn) is int
                and data['turn_index'] != stop_turn):
            continue
        if isinstance(data.get('report'), dict):
            data = data['report']
        found = _frame_from_report(data, path, segment, reason)
        if found:
            return found
        stages = data.get('stages') if isinstance(data.get('stages'), dict) else {}
        for name in ('live_loop', 'larc_ss_information', 'training_page_observation', 'sidecar', 'capture'):
            stage = stages.get(name)
            if isinstance(stage, dict) and path:
                found = _frame_from_report(stage, path, segment, reason)
                if found:
                    return found
    # A live-takeover rejection has one explicit observed scene source.
    scene = (summary.get('live_takeover') or {}).get('current_scene')
    if isinstance(scene, dict) and reason == 'new_cycle_current_scene_unverified':
        source = _inside(scene.get('source'), segment)
        if source is not None and source.is_file():
            return source, {'decision_reason': reason}, None
    return None


def verified_outing_failure_frame(run_root, summary, *, app_root=None):
    """Read an owned SS/outing frame for stop diagnostics; no capture or writes.

    The controller calls this before disposable PNG cleanup. Invalid binding
    cannot select any newer frame, previous pipeline image or game cache.
    """
    try:
        run = Path(run_root).resolve()
        if not bound_reader_failure(summary):
            return {'status': 'not_outing_failure'}
        if summary.get('run_session_id') != run.name:
            return {'status': 'bound_frame_unavailable'}
        segment = _inside(summary.get('output'), run)
        if (segment is None or segment.parent != run
                or not re.fullmatch(r'autonomous_[A-Za-z0-9_-]{1,130}', segment.name)):
            return {'status': 'bound_frame_unavailable'}
        found = _failure_frame(run, summary)
        if found is None:
            owned = _pipeline_reader_report(run, segment, summary) if _pipeline_reader_failure(summary.get('stop_reason')) else None
            return {'status': 'bound_frame_unavailable', **({'diagnostics': _reader_diagnostics(owned[0]),
                'segment_id': segment.name} if owned is not None else {})}
        source, diagnostics, report_path = found[:3]
        raw = source.read_bytes()
        bound = found[3] if len(found) > 3 else summary['recognition_failure_source']['source']
        if hashlib.sha256(raw).hexdigest() != bound['sha256']:
            return {'status': 'bound_frame_changed'}
        image, width, height = _jpeg(raw)
        result = {'status': 'available', 'image_bytes': image, 'width': width, 'height': height,
            'source': dict(bound), 'report_path': str(report_path), 'segment_id': segment.name,
            'captured_at_unix': source.stat().st_mtime, 'diagnostics': diagnostics,
            'has_pending_operation': diagnostics.get('observation', {}).get('has_pending_operation') is True}
        # Reuse the freshly queued JPEG only if it is byte-identical to this
        # same verified source's encoding. Recognition and abnormal-stop UUIDs
        # remain distinct; the existing abnormal archive links the latter.
        queued = summary.get('recognition_failure_archive') or {}
        identifier = queued.get('report_id')
        root = _app_root(app_root)
        if (root is not None and isinstance(identifier, str)
                and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', identifier)):
            folder = root / QUEUE / identifier
            record = _read(folder / 'report.json')
            candidate = folder / 'failure.jpg'
            if (folder.resolve().is_relative_to(root) and candidate.resolve().is_relative_to(root)
                    and record.get('run_id') == run.name and record.get('report_id') == identifier
                    and record.get('source_sha256') == bound['sha256']
                    and candidate.is_file() and candidate.read_bytes() == image):
                result['source_recognition_archive_id'] = identifier
        return result
    except Exception:
        return {'status': 'bound_frame_unavailable'}


def _jpeg(raw):
    import cv2
    import numpy as np
    if not 0 < len(raw) <= 16 * 1024 * 1024:
        raise ValueError('recognition_frame_unavailable')
    frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError('recognition_frame_invalid')
    height, width = frame.shape[:2]
    if width < 360 or height < 640 or not 1.6 <= height / width <= 2.3:
        raise ValueError('recognition_frame_not_full_game_screen')
    for maximum in (1600, 1280, 960):
        scale = min(1., maximum / max(height, width))
        image = cv2.resize(frame, (round(width * scale), round(height * scale)),
            interpolation=cv2.INTER_AREA) if scale < 1 else frame
        for quality in (92, 85, 75, 60):
            ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok and len(encoded) <= MAX_IMAGE_BYTES:
                return encoded.tobytes(), image.shape[1], image.shape[0]
    raise ValueError('recognition_frame_too_large')


def archive_segment_failure(run_root, summary, *, app_root=None):
    """Best-effort archive before working-image cleanup; no network or capture."""
    try:
        reason = summary.get('stop_reason', '')
        if (summary.get('status') not in {'stopped', 'handoff'}
                or not isinstance(reason, str) or not (_recognition_reason(reason)
                    or reason in {'larc_growth_stopped', 'growth_adaptation_stopped'}
                    or bound_reader_failure(summary))):
            return {'status': 'not_recognition_failure'}
        root = _app_root(app_root)
        if root is None:
            return {'status': 'application_root_unavailable'}
        run_root = Path(run_root).resolve()
        if summary.get('run_session_id') != run_root.name:
            return {'status': 'run_mismatch'}
        found = _failure_frame(run_root, summary)
        if found is None:
            return {'status': 'bound_frame_unavailable'}
        source, diagnostics, report_path = found[:3]
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if len(found) > 3 and digest != found[3]['sha256']:
            return {'status': 'bound_frame_unavailable'}
        progress = summary.get('final_progress') or {}
        turn = progress.get('next_turn')
        if type(turn) is not int:
            turn = _last(summary.get('turns')).get('turn_index')
        identity = f'{run_root.name}:{turn}:{reason}:{digest}'
        report_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'maauma-recognition:' + identity))
        folder = root / QUEUE / report_id
        if not folder.resolve().is_relative_to(root) or not (folder / 'report.json').resolve().is_relative_to(root):
            return {'status': 'queue_path_unavailable'}
        if (folder / 'report.json').exists():
            return {'status': 'already_archived', 'report_id': report_id}
        image, width, height = _jpeg(raw)  # Re-encoding strips EXIF/ancillary data.
        report = {'schema_version': SCHEMA, 'report_id': report_id,
            'run_id': run_root.name, 'turn_index': turn, 'reason': reason,
            'diagnostics': diagnostics, 'source_relative_path': source.relative_to(run_root).as_posix(),
            'source_sha256': digest, 'image': 'failure.jpg', 'width': width, 'height': height,
            'state': 'pending', 'created_at': datetime.now(timezone.utc).isoformat()}
        version = _read(root / 'maauma-edition.json').get('version')
        if isinstance(version, str) and 1 <= len(version) <= 40:
            report['app_version'] = version
        if report_path is not None:
            report['source_report_relative_path'] = report_path.relative_to(run_root).as_posix()
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / 'failure.jpg').resolve().is_relative_to(root):
            return {'status': 'queue_path_unavailable'}
        (folder / 'failure.jpg').write_bytes(image)
        temporary = folder / 'report.json.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        temporary.replace(folder / 'report.json')
        return {'status': 'archived', 'report_id': report_id}
    except Exception as error:
        return {'status': 'archive_unavailable', 'error': type(error).__name__}


def complete_run_feedback(run_root, terminal_reason, *, app_root=None):
    """Make this run's evidence uploadable only on a confirmed terminal event."""
    if terminal_reason not in {'completed', 'abandoned', 'operator_stop'}:
        return {'status': 'not_terminal', 'ready': 0}
    try:
        root = _app_root(app_root)
        if root is None:
            return {'status': 'application_root_unavailable', 'ready': 0}
        run_id = Path(run_root).resolve().name
        ready = 0
        for path in (root / QUEUE).glob('*/report.json'):
            if not path.resolve().is_relative_to(root):
                continue
            report = _read(path)
            if report.get('schema_version') != SCHEMA or report.get('run_id') != run_id:
                continue
            if report.get('state') == 'pending':
                report.update(state='ready', terminal_reason=terminal_reason,
                    ended_at=datetime.now(timezone.utc).isoformat())
                temporary = path.with_suffix('.json.tmp')
                temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
                temporary.replace(path)
                ready += 1
        try:
            from desktop.recognition_failure_feedback import flush_async
            flush_async(root)  # Client owns enabled setting, daemon and retries.
        except Exception:
            pass  # Keep the ready queue even if the optional client is absent.
        return {'status': 'ready', 'ready': ready}
    except Exception as error:
        return {'status': 'queue_unavailable', 'ready': 0, 'error': type(error).__name__}


def archive_abnormal_stop(run_root, session_state, *, summary=None,capture_started_at_ms=None,operator_stopping=False,app_root=None):
    """Queue a career/cut stop's saved frame or small failure; no capture/send."""
    try:
        from core.auto_trainer.stop_screenshot_evidence import load_stop_evidence, safe_reader_failure, safe_capture_failure, safe_technical_wait
        if (not isinstance(session_state,dict)or session_state.get('status')not in {'unknown','route_failed'}
                or operator_stopping is True):return {'status':'not_abnormal_stop'}
        stop=session_state.get('stop')or {}
        if not isinstance(stop,dict)or stop.get('normal_stop')is True or session_state.get('normal_stop')is True:
            return {'status':'not_abnormal_stop'}
        run=Path(run_root).resolve();reason=stop.get('reason',session_state['status'])
        if stop.get('run_id')not in (None,run.name):return {'status':'run_mismatch'}
        evidence=load_stop_evidence(run,expected_reason=reason,stopped_at_unix_ms=capture_started_at_ms)
        has_image=evidence.get('status')=='available'
        if not has_image and not isinstance(evidence.get('context'),dict):
            return {'status':'no_saved_stop_frame'}
        context=dict(evidence['context']);summary=summary if isinstance(summary,dict)else {}
        if summary.get('run_session_id')==run.name and isinstance(summary.get('output'),str):
            segment=Path(summary['output']).resolve()
            if segment.parent==run and segment.is_dir()and re.fullmatch(r'autonomous_[A-Za-z0-9_-]{1,130}',segment.name):
                context.update(segment_id=segment.name,segment_status='verified')
        root=_app_root(app_root)
        if root is None:return {'status':'application_root_unavailable'}
        identity=':'.join(str(context.get(k,''))for k in ('run_id','segment_id','stop_reason','stop_recorded_at','frame_sha256'))
        report_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'maauma-abnormal-stop:'+identity))
        folder=root/QUEUE/report_id
        if not folder.resolve().is_relative_to(root):return {'status':'queue_path_unavailable'}
        if not (folder/'report.json').exists():
            folder.mkdir(parents=True,exist_ok=True)
            if any(not(folder/name).resolve().is_relative_to(root)for name in ('failure.jpg','report.json')):
                return {'status':'queue_path_unavailable'}
            raw=evidence.get('image_bytes');meta=evidence.get('metadata')or {}
            report={'schema_version':SCHEMA,'report_id':report_id,'run_id':run.name,'reason':reason,'stop_reason':reason,
                'report_kind':'abnormal_flow_stop','segment_id':context.get('segment_id'),
                'segment_status':context['segment_status'],'stop_recorded_at':context['stop_recorded_at'],
                'captured_at':context['captured_at'],'frame_scope':context['frame_scope'],
                'frame_sha256':context['frame_sha256'],'source_relative_path':context['frame_relative_path'],
                'source_sha256':context['frame_sha256'],'image':'failure.jpg'if has_image else None,
                'width':meta.get('width'),'height':meta.get('height'),
                'diagnostics':{'decision_reason':reason,'observation':{'status':'abnormal_stop'}},
                'state':'pending','created_at':datetime.now(timezone.utc).isoformat()}
            leaf=safe_reader_failure(context.get('reader_failure'))
            if leaf:report.update(reader_failure=leaf,diagnostics=leaf)
            capture=safe_capture_failure(context.get('capture_failure'))
            if capture:report['capture_failure']=capture
            wait=safe_technical_wait(context.get('technical_wait'))
            if wait:report['technical_wait']=wait
            for key in ('frame_observation','frame_relation','frame_warning','frame_age_seconds'):
                if key in context:report[key]=context[key]
            if context.get('frame_unavailable_reason'):report['frame_unavailable_reason']=context['frame_unavailable_reason']
            if not has_image:report.update(frame_status='unavailable',state='local_only')
            version=_read(root/'maauma-edition.json').get('version')
            if isinstance(version,str)and 1<=len(version)<=40:report['app_version']=version
            if has_image:(folder/'failure.jpg').write_bytes(raw)
            temporary=folder/'report.json.tmp';temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            temporary.replace(folder/'report.json')
        # Link exact immutable archive identity to this same saved stop, without
        # replacing the actual stop or selecting a different current frame.
        manifest=run/'session/stop_diagnostic/manifest.json';current=_read(manifest)
        if (current.get('run_id')==run.name and current.get('stop_recorded_at')==context['stop_recorded_at']
                and current.get('reason')==reason):
            current.update(image_sha256=context['frame_sha256'],archive_report_id=report_id)
            if context.get('segment_id'):
                current.update(segment_id=context['segment_id'],source_run_session_id=run.name)
            temporary=manifest.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(current,ensure_ascii=False,indent=2),encoding='utf8');temporary.replace(manifest)
        return {'status':'archived','report_id':report_id,'run_id':run.name,'segment_id':context.get('segment_id'),
            'stop_reason':reason,'stop_recorded_at':context['stop_recorded_at']}
    except Exception as error:return {'status':'archive_unavailable','error':type(error).__name__}
