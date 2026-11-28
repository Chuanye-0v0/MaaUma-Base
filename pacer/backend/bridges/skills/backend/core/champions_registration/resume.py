"""Prove a submitted registration stopped at rewards or the subsequent tutorial.

This grants only reward dismissal and registration verification, never another
registration submission. The original owner journal and receipt chain survive.
"""
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
from pathlib import Path
SUBMISSION_TRANSIENT_PAGES = {'home', 'races', 'events', 'champions_choose', 'champions_confirm'}
SUBMISSION_WAIT_REASONS = {'unexpected_page:' + page for page in SUBMISSION_TRANSIENT_PAGES} | {'champions_registration_unverified', 'champions_resume_page_unverified'}
REGISTERED_WAIT_REASONS = {'champions_registration_identity_unavailable', '大赛报名组别未确认或与设置不同，不自动更改联赛', 'champions_home_control_unavailable', 'champions_post_registration_transition_timeout'}
RESUME_REASONS = REGISTERED_WAIT_REASONS | SUBMISSION_WAIT_REASONS | {'action_unavailable:close_registration_rewards', 'champions_registration_unverified', 'stale_frame'}

def read(path):
    value = json.loads(Path(path).read_text(encoding='utf8'))
    if not isinstance(value, dict):
        raise ValueError('object_required')
    return value

def load_restart_pending(source, endpoint, division):
    """Only navigate/verify a submitted choice; never expose submission again."""
    source = Path(source).resolve()
    journal = read(source.parent / 'active.json')
    marker = read(source / 'confirm_registration-submitted.json')
    if endpoint != _public_endpoint() or journal.get('state') != 'pending' or Path(journal.get('run', '')).resolve() != source or (journal.get('endpoint') != endpoint) or (journal.get('division') != division) or (division not in {'intermediate', 'advanced'}) or (not (journal.get('event') or {}).get('period')) or (marker.get('action_id') != 'confirm_registration') or (marker.get('event') != journal['event']) or (marker.get('state') != 'pending') or (marker.get('horse') is not None):
        raise RuntimeError('restart_registration_submission_unverified')
    frames = [read(p) for p in source.glob('*.json') if p.stem.isdigit()]
    frames = [f for f in frames if f.get('frame_sha256') == marker.get('source_frame_sha256')]
    if not frames:
        raise RuntimeError('restart_registration_frame_missing')
    frame = next((f for f in frames if f.get('page') == 'champions_confirm' and f.get('division') == division), frames[0])
    path = Path(frame.get('screenshot_path', '')).resolve()
    if path.parent != source or hashlib.sha256(path.read_bytes()).hexdigest() != frame.get('frame_sha256'):
        raise RuntimeError('restart_registration_frame_invalid')
    import cv2, numpy as np
    from core.champions_registration.observer import observe
    parsed = observe(path.read_bytes(), cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), 1), frame.get('tokens', []))
    if frame.get('endpoint') != endpoint or parsed.get('page') != 'champions_confirm' or parsed.get('division') != division or ('confirm_registration' not in parsed.get('actions', {})):
        raise RuntimeError('restart_registration_choice_unverified')
    return (journal, len(list(source.glob('*-receipt.json'))))

def verified_passive_tail(source, result, last, snapshots):
    """A completed passive input led to this exact transient frame, not a guess.

    This permits reobservation only; the live handler must rediscover any button.
    An unfinished auxiliary action never authorizes another input on resume.
    """
    import cv2
    import numpy as np
    from core.passive_skip import detect as skip
    from core.passive_tutorial import detect as tutorial
    paths = sorted((source / 'popup-actions').glob('*.json'))
    if not paths or last.get('page') not in {'unknown', 'loading'}:
        return False
    receipts = []
    for value in paths:
        path = Path(value).resolve()
        if path.parent != source / 'popup-actions' or path.suffix != '.json':
            return False
        receipt = read(path)
        if receipt.get('ok') is not True or receipt.get('input_status') != 'dispatched' or receipt.get('tap_count') != 1 or (receipt.get('endpoint') != last.get('endpoint')) or (receipt.get('reason') != 'passive_popup_transition_observed'):
            return False
        receipts.append(receipt)
    tail = receipts[-1]
    frame = snapshots.get(tail.get('source_frame_sha256'), {})
    path = Path(frame.get('screenshot_path', '')).resolve()
    if tail.get('after_frame_sha256') != last.get('frame_sha256') or frame.get('endpoint') != last.get('endpoint') or path.parent != source or (path.suffix != '.png'):
        return False
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != tail.get('source_frame_sha256'):
        return False
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), 1)
    found = tutorial(image, frame.get('tokens', [])) or skip(image, frame.get('tokens', []))
    return bool(found and all((found.get(k) == tail.get(k) for k in ('kind', 'action_id', 'button'))) and isinstance(tail.get('point'), list) and (len(tail['point']) == 2) and (max((abs(a - b) for a, b in zip(found['point'], tail.get('point', [])))) <= 12))

def load_pending(source, endpoint, division):
    source = Path(source).resolve()
    journal = read(source.parent / 'active.json')
    result = read(source / 'result.json')
    event = journal.get('event')
    closed = (source / 'close_registration_rewards-submitted.json').exists()
    unexecuted_close = journal.get('stage') == 'close_registration_rewards' and (not closed) and (result.get('reason') == 'stale_frame')
    submitted_wait = not closed and journal.get('stage') == 'confirm_registration' and (result.get('reason') in SUBMISSION_WAIT_REASONS) and (result.get('last_page') in SUBMISSION_TRANSIENT_PAGES | {'unknown', 'loading', 'champions_rewards', 'champions_registered'})
    if endpoint != _public_endpoint() or division not in {'advanced', 'intermediate'} or Path(journal.get('run', '')).resolve() != source or (journal.get('endpoint') != endpoint) or (journal.get('division') != division) or (journal.get('state') != 'pending') or (journal.get('stage') not in {'confirm_registration', 'close_registration_rewards'}) or (not isinstance(event, dict)) or (not event.get('period')) or (journal.get('horse') is not None) or (journal.get('races') != []) or (journal.get('registration') is not None) or (result.get('kind') != 'champions_registration') or (result.get('ok') is not False) or (result.get('pending') is not True) or (not closed and (not submitted_wait) and (result.get('last_page') != 'champions_rewards' or not (unexecuted_close or (journal.get('stage') == 'confirm_registration' and result.get('reason') == 'action_unavailable:close_registration_rewards')))) or (closed and result.get('reason') not in REGISTERED_WAIT_REASONS | {'champions_registration_unverified'}) or (closed and journal.get('stage') != 'close_registration_rewards'):
        raise RuntimeError('champions_resume_evidence_invalid')
    marker = read(source / 'confirm_registration-submitted.json')
    last_marker = read(source / 'close_registration_rewards-submitted.json') if closed else marker
    outcome = read(source / 'recovery/last-action.json')
    if marker.get('action_id') != 'confirm_registration' or marker.get('event') != event or marker.get('horse') is not None or (marker.get('state') != 'pending') or (last_marker.get('action_id') != ('close_registration_rewards' if closed else 'confirm_registration')) or (last_marker.get('event') != event) or (last_marker.get('horse') is not None) or (last_marker.get('state') != 'pending'):
        raise RuntimeError('champions_resume_submission_unverified')
    if unexecuted_close:
        if outcome != result.get('action_outcome') or outcome.get('action_id') != 'close_registration_rewards' or outcome.get('execution_status') != 'not_executed' or (outcome.get('input_attempts') != 0) or (outcome.get('acknowledged_inputs') != 0) or (outcome.get('source_frame_sha256') is not None) or (outcome.get('source_page') is not None) or (outcome.get('observed_page') != 'champions_rewards'):
            raise RuntimeError('champions_resume_close_not_proven_unexecuted')
    elif outcome.get('execution_status') != 'executed' or outcome.get('action_id') != journal['stage'] or outcome.get('input_attempts') != 1 or (outcome.get('acknowledged_inputs') != 1) or (outcome.get('source_frame_sha256') != last_marker.get('source_frame_sha256')):
        raise RuntimeError('champions_resume_submission_unverified')
    receipts = [read(p) for p in sorted(source.glob('*-receipt.json'))]
    actions = [r.get('action_id') for r in receipts]
    route = ['open_races', 'open_events', 'open_champions', 'choose_' + division, 'confirm_registration']
    if closed:
        route.append('close_registration_rewards')
    if len(receipts) < (3 if closed else 2) or actions != route[-len(receipts):] or result.get('device_actions') != len(receipts) or (receipts[-1].get('source_frame_sha256') != last_marker.get('source_frame_sha256')):
        raise RuntimeError('champions_resume_receipts_invalid')
    snapshots = {s.get('frame_sha256'): s for p in source.glob('*.json') if p.stem.isdigit() for s in [read(p)]}
    confirm = snapshots.get(marker.get('source_frame_sha256'), {})
    submission = receipts[-2] if closed else receipts[-1]
    choice = snapshots.get(receipts[-3 if closed else -2].get('source_frame_sha256'), {})
    reward = submission.get('observation', {})
    last = result.get('last_observation') or {}
    closing_frame = snapshots.get(last_marker.get('source_frame_sha256'), {}) if closed else reward
    for state in (choice, confirm, reward, last, closing_frame):
        path = Path(state.get('screenshot_path', '')).resolve()
        if path.parent != source or path.suffix != '.png' or state.get('endpoint') != endpoint or (hashlib.sha256(path.read_bytes()).hexdigest() != state.get('frame_sha256')) or (read(path.with_suffix('.json')).get('frame_sha256') != state.get('frame_sha256')):
            raise RuntimeError('champions_resume_frame_unbound')
    if submission.get('source_frame_sha256') != marker.get('source_frame_sha256') or choice.get('page') != 'champions_choose' or choice.get('event_period') != event['period'] or ('choose_' + division not in choice.get('actions', {})) or (confirm.get('page') != 'champions_confirm') or (confirm.get('division') != division) or ('confirm_registration' not in confirm.get('actions', {})) or (not submitted_wait and reward.get('page') != 'champions_rewards' and (not (reward.get('page') in {'unknown', 'loading'} and (closing_frame if closed else last).get('page') == 'champions_rewards'))) or (not closed and (not submitted_wait) and (last.get('page') != 'champions_rewards')):
        raise RuntimeError('champions_resume_reward_unverified')
    if submitted_wait:
        import cv2
        import numpy as np
        from core.champions_registration.observer import observe
        raw = Path(confirm['screenshot_path']).read_bytes()
        verified = observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), confirm.get('tokens', []))
        if verified.get('page') != 'champions_confirm' or verified.get('division') != division or 'confirm_registration' not in verified.get('actions', {}) or (last.get('page') != result.get('last_page')) or (reward.get('page') not in SUBMISSION_TRANSIENT_PAGES | {'unknown', 'loading', 'champions_rewards', 'champions_registered'}):
            raise RuntimeError('champions_resume_submission_unverified')
    if closed:
        import cv2
        import numpy as np
        from core.passive_tutorial import detect
        if closing_frame.get('page') != 'champions_rewards':
            raise RuntimeError('champions_resume_close_unverified')
        image = cv2.imdecode(np.frombuffer(Path(last['screenshot_path']).read_bytes(), np.uint8), 1)
        if result.get('reason') in REGISTERED_WAIT_REASONS:
            from core.champions_registration.observer import observe
            verified = observe(Path(last['screenshot_path']).read_bytes(), image, last.get('tokens', []))
            registered = verified.get('page') == 'champions_registered' and verified.get('division') in (None, division) and (verified.get('event_period') in (None, event['period']))
            if not registered and detect(image, last.get('tokens', [])) is None and (not verified_passive_tail(source, result, last, snapshots)):
                raise RuntimeError('champions_resume_registration_unverified')
        elif detect(image, last.get('tokens', [])) is None:
            raise RuntimeError('champions_resume_tutorial_unverified')
    return (journal, len(receipts))
