"""Read the last saved adaptation fields from this summary's own reports only.

Candidate for host integration. Standard library only, no devices, glob, recursive
search, source/config reads, writes or process calls. Output has no paths or OCR.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

_CODE = re.compile(r'[a-z][a-z0-9_]{0,79}\Z')
_STAGES = {'selecting', 'submission_pending', 'awaiting_result', 'complete'}


def _last_dict(value):
    return value[-1] if isinstance(value, list) and value and isinstance(value[-1], dict) else {}


def _bound(raw, root, relative_to=None):
    if not isinstance(raw, (str, Path)) or not str(raw):
        return None
    try:
        path = Path(raw)
        if not path.is_absolute():
            path = (relative_to or root) / path
        path = path.resolve()
        return path if path.is_relative_to(root) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _read(path, root, names):
    path = _bound(path, root)
    if path is None or path.name not in names:
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _fields(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    if isinstance(value.get('errors'), list):
        result['errors'] = [item for item in value['errors']
            if isinstance(item, str) and _CODE.fullmatch(item)]
    levels = value.get('levels')
    if isinstance(levels, list) and len(levels) == 10 and all(item is None or type(item) is int for item in levels):
        result['levels'] = list(levels)
    for key in ('points', 'pending_cost', 'selected_slot'):
        if key in value and (value[key] is None or type(value[key]) is int):
            result[key] = value[key]
    return result


def _observation(report, fallback_stage=None):
    steps = report.get('step_reports')
    if not isinstance(steps, list):
        return {}
    for step in reversed(steps):
        if not isinstance(step, dict) or not isinstance(step.get('result'), dict):
            continue
        result = step['result']
        decision = result.get('decision')
        if not isinstance(decision, dict) or not isinstance(decision.get('evidence'), dict):
            continue
        fields = _fields(decision['evidence'].get('larc_adaptation'))
        if not fields:
            continue
        session = result.get('larc_adaptation_session')
        stage = session.get('stage') if isinstance(session, dict) else fallback_stage
        value = {'larc_adaptation': fields}
        if stage in _STAGES:
            value['stage'] = stage
        reason = decision.get('reason')
        if isinstance(reason, str) and reason.startswith('larc_adaptation_') and _CODE.fullmatch(reason):
            value['reason'] = reason
        return value
    return {}


_SUCCESS = {'larc_adaptation_verified', 'larc_adaptation_already_ready',
    'larc_adaptation_partial_race_ready', 'larc_adaptation_inspection_complete'}


def _diagnostic(value, summary, source_turn=None):
    progress = summary.get('final_progress')
    stop_turn = progress.get('next_turn') if isinstance(progress, dict) else None
    source_turn = source_turn if type(source_turn) is int else None
    stop_turn = stop_turn if type(stop_turn) is int else None
    same = source_turn == stop_turn if source_turn is not None and stop_turn is not None else None
    prior = source_turn < stop_turn if source_turn is not None and stop_turn is not None else None
    result = {'diagnostic_only': True, 'not_recorded': not bool(value) or same is not True
        or value.get('reason') in _SUCCESS, 'source_is_prior_turn': prior}
    if source_turn is not None:
        result['source_turn_index'] = source_turn
    if stop_turn is not None:
        result['stop_turn_index'] = stop_turn
    result.update(value)
    return result


def extract_stop_diagnostics(summary, summary_path):
    """Return a small whitelist or {} when the bound report is unavailable.

    Adaptation outer report -> exact safe_advance_report_path is preferred.
    A failed growth observation may return before growth_adaptations is added;
    in that case the last turn's exact output/growth/grid/report.json is used.
    All resolved reports must remain inside summary_path.parent (autonomous_*).
    """
    if not isinstance(summary, dict):
        return {}
    reason = summary.get('stop_reason')
    if not isinstance(reason, str) or not (reason.startswith('larc_adaptation_')
            or reason in {'larc_growth_stopped', 'growth_adaptation_stopped'}):
        return {}
    try:
        path = Path(summary_path).resolve()
    except (OSError, ValueError, RuntimeError, TypeError):
        return {}
    root = path.parent
    if path.name != 'summary.json' or not root.name.startswith('autonomous_'):
        return {}
    resume = summary.get('adaptation_resume_context')
    if reason == 'larc_adaptation_resume_requires_unique_session' and isinstance(resume, dict):
        count, phases = resume.get('candidate_count'), resume.get('candidate_phases')
        if (type(count) is int and count >= 0 and isinstance(phases, list)
                and len(phases) == count and all(isinstance(phase, str) and _CODE.fullmatch(phase) for phase in phases)):
            return _diagnostic({'resume_candidate_count': count, 'resume_candidate_phases': phases,
                                'not_recorded': True}, summary, resume.get('turn_index'))
    adaptation = _last_dict(summary.get('adaptation_reports'))
    row_reason = adaptation.get('reason')
    failed = (isinstance(row_reason, str) and row_reason.startswith('larc_adaptation_')
        and row_reason not in {'larc_adaptation_verified', 'larc_adaptation_already_ready',
            'larc_adaptation_partial_race_ready'})
    if failed and adaptation.get('reused') is not True:
        outer_path = _bound(adaptation.get('path'), root)
        outer = _read(outer_path, root, {'report.json'}) if outer_path is not None else None
        if outer:
            child_path = _bound(outer.get('safe_advance_report_path'), root, outer_path.parent)
            child = _read(child_path, root, {'safe_advance_report.json'}) if child_path is not None else None
            session = outer.get('session')
            stage = session.get('stage') if isinstance(session, dict) else None
            value = _observation(child or outer, stage)
            if value:
                return _diagnostic(value, summary, outer.get('turn_index'))
    growth = _last_dict(summary.get('growth_adaptations'))
    growth_paths = []
    if growth.get('ok') is False:
        folder = _bound(growth.get('path'), root)
        if folder is not None:
            growth_paths.append((folder / 'grid/report.json', growth.get('turn_index')))
    turn = _last_dict(summary.get('turns'))
    folder = _bound(turn.get('output'), root)
    if folder is not None:
        growth_paths.append((folder / 'growth/grid/report.json', turn.get('turn_index')))
    seen = set()
    for report_path, source_turn in growth_paths:
        if report_path in seen:
            continue
        seen.add(report_path)
        report = _read(report_path, root, {'report.json'})
        value = _observation(report) if report else {}
        if value:
            return _diagnostic(value, summary, source_turn)
    failure = summary.get('growth_failure')
    if (isinstance(failure, dict) and isinstance(failure.get('reason'), str)
            and _CODE.fullmatch(failure['reason'])
            and failure.get('wrapper_status') == 'larc_growth_stopped'):
        return _diagnostic({'growth_failure_reason': failure['reason'],
                            'wrapper_status': 'larc_growth_stopped', 'not_recorded': True},
                           summary, failure.get('turn_index'))
    return _diagnostic({}, summary)
