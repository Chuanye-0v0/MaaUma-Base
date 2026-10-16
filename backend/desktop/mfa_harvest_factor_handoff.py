"""Read-only proof of an acknowledged factor-window opening interrupted by Stop.

This never settles a receipt or authorizes a replay. The original harvest owner
may continue only after two fresh reads of the same factor-confirmation list.
"""
import hashlib
import json
from pathlib import Path
import re
import cv2

def _read(path):
    return json.loads(path.read_text(encoding='utf8'))

def _image(observed, root):
    path = Path(observed.get('screenshot_path', '')).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError('factor_handoff_frame_outside_owner')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != observed.get('frame_sha256'):
        raise RuntimeError('factor_handoff_frame_hash_mismatch')
    image = cv2.imread(str(path))
    if image is None or image.shape[1::-1] not in {(720, 1280), (1440, 2560)}:
        raise RuntimeError('factor_handoff_frame_size_invalid')
    return cv2.resize(image, (720, 1280))

def factor_list_signature(image):
    return hashlib.sha256(image[135:940, 40:680].tobytes()).hexdigest()

def inspect(source, endpoint):
    """Return a narrow proof or None. Existing generic uncertain guards remain."""
    source = Path(source).resolve()
    try:
        if source.name != 'harvest' or source.parent.parent.name != 'tasks' or (not source.parent.name.isdigit()):
            return None
        prior = _read(source / 'adapter_result.json')
        context = _read(source / 'context.json')
        outcome = prior.get('action_outcome') or {}
        expected = dict(action_id='confirm_factors', execution_status='uncertain', input_attempts=1, acknowledged_inputs=1, source_page='factor_result', decision='stop')
        if prior.get('ok') is not False or prior.get('current_account_only') is not True or prior.get('reason') != 'operator_stop' or (context.get('mode') != 'harvest') or (context.get('completion_submitted') is not True) or (context.get('factors_requested') is not True) or context.get('factor_choice_submitted') or context.get('factor_selection') or prior.get('account_conflict') or prior.get('account_paused') or any((outcome.get(k) != v for k, v in expected.items())) or (_read(source / 'recovery/last-action.json') != outcome):
            return None
        uncertainty = []
        for path in source.rglob('last-action.json'):
            data = _read(path)
            if data.get('execution_status') == 'uncertain':
                uncertainty.append(path.resolve())
        if uncertainty != [(source / 'recovery/last-action.json').resolve()]:
            return None
        forbidden = {'confirm_factor_choice', 'factor_reroll', 'confirm_factor_reroll', 'select_original_factors', 'select_new_factors'}
        receipts = []
        for path in source.glob('*-receipt.json'):
            data = _read(path)
            if data.get('action_id') in forbidden:
                return None
            if data.get('action_id') == 'confirm_factors':
                receipts.append((path, data))
        if len(receipts) != 1:
            return None
        receipt_path, receipt = receipts[0]
        if receipt.get('performed') is not None or receipt.get('status') != 'dispatch_intent' or receipt.get('endpoint') != endpoint or (receipt.get('source_frame_sha256') != outcome.get('source_frame_sha256')) or (Path(receipt.get('receipt_path', '')).resolve() != receipt_path.resolve()):
            return None
        if not re.fullmatch('\\d+-receipt', receipt_path.stem):
            return None
        sent_at = int(receipt_path.stem.split('-')[0]) // 1000000
        frames = [_read(path) for path in source.glob('*.json') if path.stem.isdigit()]
        before = [row for row in frames if row.get('frame_sha256') == outcome.get('source_frame_sha256')]
        if len(before) != 1:
            return None
        before = before[0]
        if before.get('page') != 'factor_result' or before.get('endpoint') != endpoint or type(before.get('captured_at_unix_ms')) is not int or (not 0 <= sent_at - before['captured_at_unix_ms'] <= 30000):
            return None
        _image(before, source)
        after = [row for row in frames if row.get('page') == 'factor_confirm' and row.get('endpoint') == endpoint and (type(row.get('captured_at_unix_ms')) is int) and (0 <= row['captured_at_unix_ms'] - sent_at <= 30000) and (set(row.get('actions', {})) == {'confirm_factor_choice'})]
        if not after:
            return None
        after = max(after, key=lambda row: row['captured_at_unix_ms'])
        signature = factor_list_signature(_image(after, source))
        from desktop.mfa_harvest_restart import completion_receipt
        completion = completion_receipt(prior, source, endpoint)
        return dict(source=str(source), endpoint=endpoint, action_id='confirm_factors', receipt_path=str(receipt_path), completion_receipt=completion, original_outcome=outcome, saved_confirmation=after, factor_list_signature=signature, unresolved_action_path=str(source / 'recovery/last-action.json'))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None

def verify_current(proof, device, stop):
    """Two local captures, no input. A different/new list does not resume."""
    source = Path(device.output_dir).resolve()
    observed = []
    for _ in range(2):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        fresh = device.capture()
        if fresh.get('endpoint') != proof['endpoint'] or fresh.get('page') != 'factor_confirm' or set(fresh.get('actions', {})) != {'confirm_factor_choice'} or device.context.get('factor_choice_submitted') or device.context.get('factor_selection') or (factor_list_signature(_image(fresh, source)) != proof['factor_list_signature']):
            raise RuntimeError('factor_handoff_current_confirmation_unverified')
        observed.append(fresh)
    record = dict(proof, fresh_confirmations=observed, input_count=0, reason='acknowledged_factor_window_postcondition_verified')
    (source / 'factor-handoff.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
    device.context['factor_handoff_dispatch'] = dict(endpoint=proof['endpoint'], owner=str(source), factor_list_signature=proof['factor_list_signature'])
    return observed[-1]

def verify_dispatch(observed, context):
    """Bind the executor's actual pre-input frame, not just earlier captures."""
    binding = context.get('factor_handoff_dispatch')
    if binding is None:
        return
    if observed.get('endpoint') != binding['endpoint'] or observed.get('page') != 'factor_confirm' or set(observed.get('actions', {})) != {'confirm_factor_choice'} or context.get('factor_selection') or context.get('factor_choice_submitted') or (factor_list_signature(_image(observed, Path(binding['owner']).resolve())) != binding['factor_list_signature']):
        raise RuntimeError('factor_handoff_dispatch_confirmation_changed')
