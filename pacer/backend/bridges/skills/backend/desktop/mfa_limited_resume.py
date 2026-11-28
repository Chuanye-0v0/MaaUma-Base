"""Read-only evidence reconciliation for legacy pre-entry limited-event failures."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import hashlib
from pathlib import Path
from core.limited_legend.recovery import navigation_only_failure

def completed_unavailable_result(prior, account_dir, endpoint=_public_endpoint()):
    """Accept the old adapter's rejected authorized skip, not any race completion.

    This is read-only. Only the normal batch progress writer records the handled
    task; the failed job, original adapter and all original evidence remain intact.
    """
    from core.event_menu import unavailable, skipped_result_verified

    def read(path):
        return json.loads(path.read_text(encoding='utf8'))
    try:
        if prior.get('kind') != 'limited_legend' or prior.get('ok') is not False or prior.get('pending') is not True or (prior.get('game_task_complete') is not False) or (prior.get('reason') != 'limited_legend_unavailable_skipped') or prior.get('account_conflict') or prior.get('conflict_check_error') or prior.get('passive_popup_receipts') or (prior.get('action_outcome', {}).get('execution_status') == 'uncertain'):
            return None
        account_dir = Path(account_dir).resolve()
        output = Path(prior['output_dir']).resolve()
        parts = output.relative_to(account_dir).parts
        if len(parts) != 4 or not parts[0].isdigit() or parts[1] != 'tasks' or (not parts[2].isdigit()) or (parts[3] != 'limited_legend') or (not False) or (read(output / 'adapter_result.json') != prior):
            return None
        result, owner = (read(output / 'backend_result.json'), read(output / 'result.json'))
        marker = read(output.parent.parent / 'active.json')
        if not skipped_result_verified(result, 'limited_legend', endpoint) or owner.get('ok') is not True or owner.get('pending') is not False or (owner.get('reason') != result['reason']) or (owner.get('completed_races') != 0) or (owner.get('races') != []) or (marker.get('task') != 'limited_legend') or (marker.get('endpoint') != endpoint) or (Path(marker['output']).resolve() != output) or any(output.rglob('*-submitted.json')):
            return None
        frames = [read(p) for p in sorted(output.glob('*.json')) if p.stem.isdigit()]
        absent = [f for f in frames if unavailable(f, 'legend', endpoint)]
        if len(absent) < 2 or absent[-1]['captured_monotonic'] <= absent[-2]['captured_monotonic'] or absent[-1]['screenshot_path'] != result['last_observation']['screenshot_path']:
            return None
        for frame in absent[-2:]:
            image = Path(frame['screenshot_path']).resolve()
            if image.parent != output or hashlib.sha256(image.read_bytes()).hexdigest() != frame['frame_sha256']:
                return None
        receipts = [read(p) for p in output.rglob('*-receipt.json')]
        if result.get('device_actions') != len(receipts) or any((r.get('action_id') not in {'open_races', 'open_events'} or r.get('observation', {}).get('endpoint') != endpoint for r in receipts)):
            return None
        if any((read(p).get('execution_status') != 'executed' or read(p).get('action_id') not in {'open_races', 'open_events'} for p in output.rglob('last-action.json'))):
            return None
        return dict(result, ok=True, complete=False, game_task_complete=False, skip_verified=True, pending=False, output_dir=str(output), adapter_reconciled=True, retry_allowed=False, reconciled_from=str(output / 'adapter_result.json'))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None

def completed_manual_result(prior, account_dir, endpoint=_public_endpoint()):
    """Recover only the old adapter's rejected, already-completed manual result.

    Read-only: retain all original artifacts; the batch persists its verified
    result through the same normal progress writer used by other recovery.
    """
    from core.limited_legend.completion import completion_verified

    def read(p):
        return json.loads(p.read_text(encoding='utf8'))
    try:
        if prior.get('kind') != 'limited_legend' or prior.get('ok') is not False or prior.get('pending') is not True or (prior.get('complete') is not True) or (prior.get('game_task_complete') is not False) or (prior.get('reason') != 'limited_legend_manual_completion_verified') or prior.get('account_conflict') or prior.get('conflict_check_error') or prior.get('passive_popup_receipts') or (prior.get('action_outcome', {}).get('execution_status') == 'uncertain'):
            return None
        account_dir = Path(account_dir).resolve()
        output, source = (Path(prior['output_dir']).resolve(), Path(prior['resumed_from']).resolve())
        for folder in {output, source}:
            parts = folder.relative_to(account_dir).parts
            if len(parts) != 4 or not parts[0].isdigit() or parts[1] != 'tasks' or (not parts[2].isdigit()) or (parts[3] != 'limited_legend'):
                return None
            if any((read(p).get('execution_status') == 'uncertain' for p in folder.rglob('last-action.json'))):
                return None
        result = read(output / 'backend_result.json')
        owner = read(source / 'result.json')
        proof = read(source / 'manual-completion.json')
        if read(output / 'adapter_result.json') != prior or not completion_verified(result, endpoint) or result.get('manual_reconciliation') != proof or (result.get('resumed_from') != str(source)) or (owner.get('ok') is not True) or (owner.get('pending') is not False) or (source.parent / 'active.json').exists():
            return None
        for key in ('reason', 'complete', 'tickets_after', 'tickets_before', 'races', 'completed_races', 'screenshot_path'):
            if owner.get(key) != result.get(key) or prior.get(key) != result.get(key):
                return None
        if prior.get('manual_reconciliation') != proof or prior.get('last_observation') != result.get('last_observation'):
            return None

        def frame(observed):
            path = Path(observed['screenshot_path']).resolve()
            if not path.is_relative_to(source) or path.suffix != '.png':
                raise ValueError('frame_scope')
            stored = read(path.with_suffix('.json'))
            persisted = {k: v for k, v in observed.items() if k != 'unchanged_since_monotonic'}
            if stored != persisted or stored.get('endpoint') != endpoint or hashlib.sha256(path.read_bytes()).hexdigest() != stored.get('frame_sha256'):
                raise ValueError('frame_binding')
            return stored
        frame(proof['observation'])
        home = frame(result['last_observation'])
        receipts = [read(p) for p in sorted((source / 'missions').glob('*-receipt.json'))]
        last = receipts[-1]
        if last.get('action_id') != 'return_home' or frame(last['observation']).get('page') != 'home' or home['captured_monotonic'] < last['observation']['captured_monotonic']:
            return None
        return dict(result, game_task_complete=True, output_dir=str(output), adapter_reconciled=True, retry_allowed=False, reconciled_from=str(output / 'adapter_result.json'))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None

def submitted_race_source(prior, account_dir, endpoint=_public_endpoint()):
    """Bind a pending result to the current account and original paid entry."""
    from core.daily_legend.resume import load_submitted_race

    def read(path):
        return json.loads(path.read_text(encoding='utf8'))
    try:
        account_dir = Path(account_dir).resolve()
        number = int(account_dir.name.removeprefix('account-'))
        if read(account_dir.parent / 'progress.json').get('current_account') != number or prior.get('kind') != 'limited_legend' or prior.get('ok') is not False or (prior.get('pending') is not True) or prior.get('account_conflict') or prior.get('conflict_check_error') or (prior.get('action_outcome', {}).get('execution_status') == 'uncertain'):
            return None
        adapter_output = Path(prior['output_dir']).resolve()
        source = Path(prior.get('legend_output_dir') or prior.get('resumed_from') or adapter_output).resolve()
        origin = Path(prior['resumed_from']).resolve() if prior.get('resumed_from') else None
        if origin is not None:
            parts = origin.relative_to(account_dir).parts
            if len(parts) != 4 or not parts[0].isdigit() or parts[1] != 'tasks' or (not parts[2].isdigit()) or (parts[3] != 'limited_legend'):
                return None
        for output in {source, adapter_output}:
            parts = output.relative_to(account_dir).parts
            if len(parts) != 4 or not parts[0].isdigit() or parts[1] != 'tasks' or (not parts[2].isdigit()) or (parts[3] != 'limited_legend'):
                return None
            marker = read(output.parent.parent / 'active.json')
            if marker.get('task') != 'limited_legend' or marker.get('options') != {} or marker.get('endpoint') != endpoint or (Path(marker['output']).resolve() != output):
                return None
            if any((read(p).get('execution_status') == 'uncertain' for p in output.rglob('last-action.json'))):
                return None
        if read(adapter_output / 'adapter_result.json') != prior:
            return None
        from core.limited_legend.mission_resume import load_mission_resume
        if load_mission_resume(source, endpoint) is not None:
            return source
        if prior.get('passive_popup_receipts'):
            return None
        from core.daily_legend.resume import load_unsubmitted_entry
        if load_unsubmitted_entry(source, endpoint) is not None:
            if origin is not None and origin != source and (read(source / 'entry-origin.json').get('source') != str(origin)):
                return None
            return source
        try:
            load_submitted_race(source, endpoint)
            return source
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            if origin is None or origin == source:
                return None
        if read(source / 'entry-origin.json').get('source') != str(origin):
            return None
        allowed = {'open_races', 'open_events', 'open_legend', 'open_details', 'open_selection', 'open_sort', 'sort_evaluation', 'apply_sort', 'descending', 'scroll_top', 'scroll_next', 'confirm_selected', 'continue_lineup', 'close_legend_help'}

        def selection(action):
            return action in allowed or (isinstance(action, str) and action.startswith('horse_') and action[6:].isdigit())

        def unsafe_outcome(value):
            if not isinstance(value, dict):
                return True
            status = value.get('execution_status')
            if 'execution_status' in value and status not in {'executed', 'not_executed'}:
                return True
            for key in ('input_attempts', 'acknowledged_inputs'):
                if key in value and (type(value[key]) is not int or value[key] < 0):
                    return True
            attempts, acknowledged = (value.get('input_attempts'), value.get('acknowledged_inputs'))
            return attempts is not None and acknowledged is not None and (attempts != acknowledged) or (status == 'not_executed' and any((v is not None and v != 0 for v in (attempts, acknowledged))))
        for output in {source, adapter_output}:
            if any((read(p).get('action_id') not in {'confirm_selected', 'continue_lineup'} for p in output.rglob('*-submitted.json'))):
                return None
            for path in output.rglob('*-receipt.json'):
                receipt = read(path)
                if not selection(receipt.get('action_id')) or unsafe_outcome(receipt) or ('action_outcome' in receipt and unsafe_outcome(receipt['action_outcome'])):
                    return None
            for path in output.rglob('last-action.json'):
                outcome = read(path)
                status, attempts, acknowledged = (outcome.get('execution_status'), outcome.get('input_attempts'), outcome.get('acknowledged_inputs'))
                if not selection(outcome.get('action_id')) or status not in {'executed', 'not_executed'} or type(attempts) is not int or (type(acknowledged) is not int) or (attempts != acknowledged) or (attempts < 0) or (status == 'not_executed' and attempts != 0):
                    return None
        return origin if load_unsubmitted_entry(origin, endpoint) is not None else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return None

def pre_entry_failure(prior, account_dir, endpoint=_public_endpoint()):
    try:
        if prior.get('kind') != 'limited_legend' or prior.get('pending') is not True or prior.get('game_task_complete') is not False:
            return None
        output = Path(prior['output_dir']).resolve()
        parts = output.relative_to(Path(account_dir).resolve()).parts
        if len(parts) != 4 or not parts[0].isdigit() or parts[1] != 'tasks' or (not parts[2].isdigit()) or (parts[3] != 'limited_legend'):
            return None
        adapter = json.loads((output / 'adapter_result.json').read_text(encoding='utf8'))
        marker = json.loads((output.parent.parent / 'active.json').read_text(encoding='utf8'))
        if adapter != prior or marker.get('task') != 'limited_legend' or marker.get('options') != {} or (marker.get('endpoint') != endpoint) or (Path(marker['output']).resolve() != output) or (not navigation_only_failure(prior, output, endpoint)):
            return None
        return dict(prior, pending=False, pre_entry_verified=True, retry_allowed=True, reconciled_from=str(output / 'adapter_result.json'))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
