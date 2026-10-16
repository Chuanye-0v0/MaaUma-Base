"""Replan an unfinished task when the game has retired its old event phase."""
import hashlib
import json
from pathlib import Path

def round_phase(book):
    """A missing dialog label does not erase the task's already known phase."""
    value = (book.get('active_round') or {}).get('event_phase')
    return value if value is not None else book.get('event_phase')

def phase_restart_record(book, observed, source, endpoint):
    active = book.get('active_round') or {}
    old = round_phase(book)
    new = observed.get('event_phase')
    scene_ok = observed.get('page') == 'champions_hub' and 'open_entry' in observed.get('actions', {}) and (observed.get('round_registered') is not True) or (observed.get('page') == 'champions_active_hub' and observed.get('round_registered') is True and ('open_current_round' in observed.get('actions', {})))
    if observed.get('endpoint') != endpoint or not scene_ok or type(old) is not int or (type(new) is not int) or (new <= old) or (observed.get('division') not in (None, book.get('division'))):
        raise RuntimeError('champions_task_entry_unverified')
    source = Path(source).resolve()
    outcome = source / f"round-{active.get('number', 0):03d}" / 'recovery/last-action.json'
    if outcome.exists() and json.loads(outcome.read_text(encoding='utf8')).get('execution_status') == 'uncertain':
        raise RuntimeError('champions_task_input_uncertain')
    return dict(reason='old_phase_retired', source=str(source), old_phase=old, current_phase=new, source_context_sha256=hashlib.sha256((source / 'context.json').read_bytes()).hexdigest(), completed_rounds=len(book.get('rounds', [])), resources_used=book.get('resources_used', {}), old_pending_matches=book.get('pending_matches'), observation=observed)

def carried_rounds(book, endpoint):
    total = 0
    for record in book.get('task_history', []):
        source = Path(record['source']).resolve()
        raw = (source / 'context.json').read_bytes()
        old = json.loads(raw)
        if hashlib.sha256(raw).hexdigest() != record.get('source_context_sha256') or old.get('endpoint') != endpoint or Path(old.get('run', '')).resolve() != source or (record.get('reason') != 'old_phase_retired') or (type(record.get('completed_rounds')) is not int) or (record['completed_rounds'] != len(old.get('rounds', []))):
            raise RuntimeError('champions_task_progress_unverified')
        total += record['completed_rounds']
    if not 0 <= total <= 4:
        raise RuntimeError('champions_task_progress_unverified')
    return total
