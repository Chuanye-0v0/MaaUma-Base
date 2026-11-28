"""Recover a submitted daily-legend race from its original receipt chain."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import hashlib
from pathlib import Path

def load_unsubmitted_entry(source, endpoint, target=None, *, _seen=None):
    """Prove only that the current entry was never dispatched; no race-count audit."""
    try:
        source = Path(source).resolve()
        seen = set() if _seen is None else set(_seen)
        if source in seen:
            return None
        seen.add(source)
        read = lambda p: json.loads(p.read_text(encoding='utf8'))
        journal, result = (read(source.parent / 'active.json'), read(source / 'result.json'))
        if endpoint != _public_endpoint() or journal.get('endpoint') != endpoint or Path(journal.get('run', '')).resolve() != source or (journal.get('stage') != 'enter_race') or (journal.get('state') != 'pending') or (result.get('ok') is not False) or (result.get('complete') is not False) or (result.get('reason') != 'stale_frame') or (not isinstance(journal.get('horse'), dict)) or (not journal['horse'].get('name')) or (not isinstance(journal.get('event'), dict)) or (not journal['event'].get('course')) or (not isinstance(journal.get('races'), list)):
            return None
        frame = Path(result['screenshot_path']).resolve()
        race = frame.parent
        if race.parent != source or not race.name.startswith('race-'):
            return None
        outcome = read(race / 'recovery/last-action.json')
        if outcome != result.get('action_outcome') or outcome.get('action_id') != 'enter_race' or outcome.get('execution_status') != 'not_executed' or (outcome.get('input_attempts') != 0) or (outcome.get('acknowledged_inputs') != 0) or (outcome.get('observed_page') != 'items'):
            return None
        if any((read(p).get('action_id') not in {'confirm_selected', 'continue_lineup'} for p in race.glob('*-submitted.json'))):
            return None
        receipts = [read(p) for p in race.glob('*-receipt.json')]
        if any((r.get('action_id') in {'enter_race', 'start_race', 'run_race', 'view_result'} for r in receipts)):
            return None

        def bound(observed):
            png = Path(observed['screenshot_path']).resolve()
            return png.parent == race and observed.get('endpoint') == endpoint and (hashlib.sha256(png.read_bytes()).hexdigest() == observed.get('frame_sha256'))
        observed = read(frame.with_suffix('.json'))
        if not bound(observed) or observed.get('page') != 'items' or observed.get('items_empty') is not True or ('enter_race' not in observed.get('actions', {})):
            return None
        lineup = race / 'continue_lineup-submitted.json'
        if lineup.exists():
            marker = read(lineup)
            if marker.get('action_id') != 'continue_lineup' or marker.get('horse') != journal['horse'] or marker.get('event') != journal['event'] or (not any((r.get('action_id') == 'continue_lineup' and r.get('source_frame_sha256') == marker.get('source_frame_sha256') and (r.get('observation', {}).get('page') == 'items') and bound(r['observation']) for r in receipts))):
                return None
        else:
            original = load_unsubmitted_entry(read(source / 'entry-origin.json')['source'], endpoint, target, _seen=seen)
            if original is None or original[0]['horse'] != journal['horse'] or original[0]['event'] != journal['event']:
                return None
        if target is not None:
            context = read(source / 'context.json')
            if journal['event'].get('opponent') != target['name'] or context.get('target', {}).get('number') != target['number'] or context.get('target_verified') is not True:
                return None
        return (journal, race)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None

def transition_entry_receipt(race, receipt, entry, endpoint):
    """An acknowledged entry may capture its connecting layer, not ready yet.

    This proves only the original input, not a completed race or ticket debit.
    The resumed runner must still observe a bound race-ready/result state.
    """
    try:
        observed = receipt['observation']
        path = Path(observed['screenshot_path']).resolve()
        if observed.get('page') not in {'loading', 'lineup'} or path.parent != race.resolve() or hashlib.sha256(path.read_bytes()).hexdigest() != observed.get('frame_sha256'):
            return False
        from core.legend_selection.observer import observe
        if observe(path.read_bytes(), observed.get('tokens', []))['page'] != 'loading':
            return False
        outcome = json.loads((race / 'recovery/last-action.json').read_text(encoding='utf8'))
        return outcome.get('action_id') == 'enter_race' and outcome.get('execution_status') == 'executed' and (outcome.get('source_page') == 'items') and (outcome.get('input_attempts') == outcome.get('acknowledged_inputs') == 1) and (outcome.get('source_frame_sha256') == entry['source_frame_sha256']) and (observed.get('endpoint') == endpoint)
    except (OSError, ValueError, KeyError, TypeError):
        return False

def load_pending(source, endpoint, opponent_number):
    from core.daily_legend.catalog import opponent
    return load_submitted_race(source, endpoint, opponent(opponent_number))

def load_submitted_race(source, endpoint, target=None):
    """Shared receipt proof; numbered daily target validation stays at entry."""
    source = Path(source).resolve()
    journal = json.loads((source.parent / 'active.json').read_text(encoding='utf8'))
    context = json.loads((source / 'context.json').read_text(encoding='utf8'))
    event, horse = (journal.get('event'), journal.get('horse'))
    before, remaining = (journal.get('tickets_before'), journal.get('tickets_after'))
    races = journal.get('races')
    if Path(journal.get('run', '')).resolve() != source or journal.get('endpoint') != endpoint or journal.get('state') != 'pending' or (not isinstance(horse, dict)) or (not horse.get('name')) or (not isinstance(event, dict)) or (not event.get('course')) or (not event.get('opponent')) or (target is not None and (event.get('opponent') != target['name'] or context.get('target', {}).get('number') != target['number'] or context.get('target_verified') is not True or (context.get('event') != event))) or (type(before) is not int) or (type(remaining) is not int) or (not 0 <= before <= 3) or (not 1 <= remaining <= 3) or (not isinstance(races, list)):
        raise RuntimeError('daily_legend_resume_evidence_invalid')
    race = source / f'race-{len(races) + 1:03d}'
    entry = json.loads((race / 'enter_race-submitted.json').read_text(encoding='utf8'))
    receipts = [json.loads(p.read_text(encoding='utf8')) for p in sorted(race.glob('*-receipt.json'))]
    if entry.get('action_id') != 'enter_race' or entry.get('horse') != horse or entry.get('event') != event or (entry.get('tickets') != remaining) or (entry.get('tickets_after') != remaining - 1) or (not entry.get('source_frame_sha256')) or (not any((r.get('action_id') == 'enter_race' and r.get('source_frame_sha256') == entry['source_frame_sha256'] and (r.get('observation', {}).get('endpoint') == endpoint) and (r.get('observation', {}).get('page') == 'race_ready' and r['observation'].get('race_horse') == horse['name'] and (r['observation'].get('event', {}).get('course') == event['course']) or transition_entry_receipt(race, r, entry, endpoint)) for r in receipts))):
        raise RuntimeError('daily_legend_resume_entry_unverified')
    outcome = None
    for action, value in [('continue_victory', 'victory'), ('continue_defeat', 'defeat'), ('close_trophy', 'victory')]:
        marker = race / (action + '-submitted.json')
        if marker.exists():
            submitted = json.loads(marker.read_text(encoding='utf8'))
            if action == 'continue_defeat' and submitted.get('result_outcome') == 'draw':
                value = 'draw'
            if submitted.get('action_id') == action and submitted.get('source_frame_sha256') and (submitted.get('horse') == horse) and (submitted.get('event') == event) and any((r.get('action_id') == action and r.get('source_frame_sha256') == submitted['source_frame_sha256'] and (r.get('observation', {}).get('endpoint') == endpoint) for r in receipts)):
                if outcome is not None and outcome != value:
                    raise RuntimeError('daily_legend_resume_outcome_conflict')
                outcome = value
    reward_marker = race / 'continue_rewards-submitted.json'
    rewards_submitted = reward_marker.exists()
    if rewards_submitted:
        submitted = json.loads(reward_marker.read_text(encoding='utf8'))
        if outcome is None or submitted.get('action_id') != 'continue_rewards' or (not submitted.get('source_frame_sha256')) or (submitted.get('horse') != horse) or (submitted.get('event') != event) or (not any((r.get('action_id') == 'continue_rewards' and r.get('source_frame_sha256') == submitted['source_frame_sha256'] and (r.get('observation', {}).get('endpoint') == endpoint) for r in receipts))):
            raise RuntimeError('daily_legend_resume_rewards_unverified')
    return (journal, race, outcome, rewards_submitted)
