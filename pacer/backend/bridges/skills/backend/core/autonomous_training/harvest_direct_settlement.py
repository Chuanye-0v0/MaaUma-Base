"""Prove a mature Forward arrival at settlement, not a fabricated finish tap.

This is a read-only ownership proof. Rating/factor actions remain in the normal
local harvest executor, with its existing costs, pending-input and stop guards.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
ENTRY_PAGES = {'training_rating', 'factor_result'}
SCHEMA = 'umaa.harvest_direct_settlement.v1'
PRECOMPLETION_PAGES = {'training_status', 'autonomous_running', 'training_menu', 'new_scenario', 'scenario', 'history', 'history_summary', 'history_races', 'finish_ready', 'skills', 'skill_tutorial', 'skill_confirmation', 'skill_success', 'finish_confirm'}
PRECOMPLETION_ACTIONS = {'open_training', 'forward_harvest', 'confirm_history', 'history_next', 'history_scroll', 'open_skills', 'finish_training', 'complete_training', 'start_autonomous', 'confirm_autonomous_start', 'return_harvest_skills', 'open_skill_confirmation', 'reset_skill_selection', 'scroll_skill_list', 'scroll_skill_up', 'scroll_skill_overlap', 'scroll_skill_reveal_bottom', 'continue_skill_tutorial'}

def _read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))

def _owned(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise RuntimeError('direct_settlement_source_outside_owner')
    return path

def _acknowledged(receipt):
    outcome = receipt.get('action_outcome') or {}
    return receipt.get('performed') is True and receipt.get('execution_status') in (None, 'executed') and (not outcome or (isinstance(outcome, dict) and outcome.get('execution_status') == 'executed' and (outcome.get('action_id') == 'forward_harvest') and (outcome.get('source_frame_sha256') == receipt.get('source_frame_sha256')) and (type(outcome.get('input_attempts')) is int) and (outcome['input_attempts'] == 1) and (type(outcome.get('acknowledged_inputs')) is int) and (outcome['acknowledged_inputs'] == 1)))

@lru_cache(maxsize=1)
def _reader():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()

@lru_cache(maxsize=8)
def _reparse(path, frame_sha256):
    import cv2
    from core.autonomous_training.observer import observe, read_tokens
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != frame_sha256:
        raise RuntimeError('direct_settlement_frame_changed')
    image = cv2.imread(str(path))
    if image is None or image.shape[:2] != (1280, 720):
        raise RuntimeError('direct_settlement_frame_invalid')
    return observe(raw, read_tokens(image, _reader()))

def _frame(observed, root, endpoint):
    if observed.get('endpoint') != endpoint:
        raise RuntimeError('direct_settlement_endpoint_changed')
    image = _owned(observed.get('screenshot_path', ''), root)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    if digest != observed.get('frame_sha256'):
        raise RuntimeError('direct_settlement_frame_changed')
    parsed = _reparse(str(image), digest)
    if parsed.get('page') != observed.get('page'):
        raise RuntimeError('direct_settlement_page_unverified')
    return (image, digest, parsed)

def prove_forward_receipt(receipt_path, endpoint, root=None):
    """Recheck the one acknowledged Forward, zero timer and result frame."""
    receipt_path = Path(receipt_path).resolve()
    root = Path(root or receipt_path.parent).resolve()
    receipt_path = _owned(receipt_path, root)
    receipt = _read(receipt_path)
    if not isinstance(receipt, dict):
        raise RuntimeError('direct_settlement_forward_unverified')
    after = receipt.get('observation') or {}
    if not isinstance(after, dict):
        raise RuntimeError('direct_settlement_forward_unverified')
    outcome = receipt.get('action_outcome') or {}
    if receipt.get('action_id') != 'forward_harvest' or receipt.get('performed') is not True or receipt.get('endpoint') != endpoint or (after.get('page') not in ENTRY_PAGES) or (receipt.get('status') != 'observed') or (not _acknowledged(receipt)):
        raise RuntimeError('direct_settlement_forward_unverified')
    sources = []
    for path in receipt_path.parent.glob('*.json'):
        try:
            value = _read(path)
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get('frame_sha256') == receipt.get('source_frame_sha256') and value.get('screenshot_path'):
            sources.append((path, value))
    if not sources:
        raise RuntimeError('direct_settlement_forward_source_missing')
    source_paths = []
    for path, before in sources:
        image, digest, parsed = _frame(before, root, endpoint)
        if before.get('page') != 'training_status' or type(before.get('remaining_seconds')) is not int or before['remaining_seconds'] != 0 or (parsed.get('remaining_seconds') != 0) or ('forward_harvest' not in parsed.get('actions', {})):
            raise RuntimeError('direct_settlement_mature_source_unverified')
        source_paths.append((path, image, digest))
    arrival, arrival_digest, _ = _frame(after, root, endpoint)
    source_path, source_image, source_digest = source_paths[-1]
    return dict(schema_version=SCHEMA, endpoint=endpoint, owner_root=str(root), receipt_path=str(receipt_path), receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(), source_observation_path=str(source_path.resolve()), source_screenshot_path=str(source_image), source_frame_sha256=source_digest, arrival_screenshot_path=str(arrival), arrival_frame_sha256=arrival_digest, input_ack_basis='executed_ack' if outcome else 'legacy_performed_observed')

def completion_is_bound(context, endpoint=None):
    """A server-owned arrival does not claim that complete_training was sent."""
    if context.get('completion_submitted') is True:
        return True
    proof = context.get('direct_settlement_entry')
    if not isinstance(proof, dict) or proof.get('schema_version') != SCHEMA:
        return False
    if endpoint is not None and proof.get('endpoint') != endpoint:
        return False
    try:
        return proof == prove_forward_receipt(proof['receipt_path'], proof['endpoint'], proof['owner_root'])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False

def bind_forward_settlement(device, receipt, stop):
    after = receipt.get('observation') or {}
    if after.get('page') not in ENTRY_PAGES:
        return False
    if stop.is_set():
        raise RuntimeError('operator_stop')
    if not _acknowledged(receipt):
        raise RuntimeError('direct_settlement_forward_unverified')
    saved = _read(_owned(receipt.get('receipt_path', ''), Path(device.output_dir).resolve()))
    if not isinstance(saved, dict) or any((saved.get(key) != receipt.get(key) for key in ('action_id', 'performed', 'endpoint', 'source_frame_sha256', 'status'))):
        raise RuntimeError('direct_settlement_receipt_changed')
    if any((key in saved and saved[key] != receipt.get(key) for key in ('execution_status', 'action_outcome'))):
        raise RuntimeError('direct_settlement_receipt_changed')
    proof = prove_forward_receipt(receipt.get('receipt_path', ''), device.endpoint, device.output_dir)
    if stop.is_set():
        raise RuntimeError('operator_stop')
    device.context['direct_settlement_entry'] = proof
    return True

def is_direct_candidate(prior):
    if prior.get('ok') is not False:
        return False
    if prior.get('direct_settlement_entry'):
        return True
    return (prior.get('last_observation') or {}).get('page') in ENTRY_PAGES and any((r.get('action_id') == 'forward_harvest' for r in prior.get('records', [])))

def ensure_settlement_scope(observed, context, action=None):
    if not context.get('direct_settlement_entry'):
        return
    if not isinstance(context['direct_settlement_entry'], dict) or observed.get('endpoint') != context['direct_settlement_entry'].get('endpoint') or (not completion_is_bound({'direct_settlement_entry': context['direct_settlement_entry']}, observed.get('endpoint'))):
        raise RuntimeError('direct_settlement_source_changed')
    if observed.get('page') in PRECOMPLETION_PAGES or action in PRECOMPLETION_ACTIONS:
        raise RuntimeError('direct_settlement_precompletion_forbidden')

def inspect_owner(prior, source, endpoint):
    source = Path(source).resolve()
    owner = Path(prior.get('harvest_output_dir') or source).resolve()
    if source.name != 'harvest' or len(source.parents) < 4 or source.parent.parent.name != 'tasks' or (not source.parent.name.isdigit()) or (owner.name != 'harvest') or (not owner.is_relative_to(source.parents[3])) or (prior.get('ok') is not False) or (prior.get('current_account_only') is not True) or (prior.get('last_observation', {}).get('endpoint') != endpoint) or prior.get('account_conflict') or prior.get('account_paused') or (prior.get('action_outcome', {}).get('execution_status') == 'uncertain'):
        raise RuntimeError('direct_settlement_owner_unverified')
    records = [r for r in prior.get('records', []) if r.get('action_id') == 'forward_harvest']
    if len(records) != 1:
        raise RuntimeError('direct_settlement_forward_unverified')
    record = records[0]
    if not _acknowledged(record):
        raise RuntimeError('direct_settlement_forward_unverified')
    path = _owned(record.get('receipt_path', ''), owner)
    receipt = _read(path)
    if any((receipt.get(key) != record.get(key) for key in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
        raise RuntimeError('direct_settlement_receipt_changed')
    if any((key in receipt and receipt[key] != record.get(key) for key in ('execution_status', 'action_outcome'))):
        raise RuntimeError('direct_settlement_receipt_changed')
    return prove_forward_receipt(path, endpoint, owner)

def restore_direct_settlement(prior, source, device, stop):
    proof = inspect_owner(prior, source, device.endpoint)
    if stop.is_set():
        raise RuntimeError('operator_stop')
    fresh = device.capture()
    if stop.is_set():
        raise RuntimeError('operator_stop')
    context = device.context
    from core.autonomous_training.harvest import RESULT_PAGES, HARVEST_ACTION_PAGES, FORMATION_ACTIONS
    allowed = ENTRY_PAGES
    if context.get('factors_requested') is True:
        allowed = allowed | {'factor_confirm', 'factor_reroll_confirm'}
    if context.get('factor_choice_submitted') is True:
        allowed = allowed | RESULT_PAGES | {'character_story_unlock', 'activity_story_unlock'}
        if context.get('harvest_update_team') is True:
            allowed = allowed | {HARVEST_ACTION_PAGES[action] for action in FORMATION_ACTIONS}
    if fresh.get('page') == 'home':
        from desktop.mfa_harvest_restart import _completed_at_home
        if context.get('factor_choice_submitted') is not True or not _completed_at_home(prior, Path(proof['owner_root']), device.endpoint):
            raise RuntimeError('direct_settlement_home_unverified')
        allowed = allowed | {'home'}
    if fresh.get('endpoint') != device.endpoint or fresh.get('page') not in allowed or (context.get('factor_selection') or {}).get('pending'):
        raise RuntimeError('direct_settlement_current_page_unverified')
    context['direct_settlement_entry'] = proof
    return fresh

def settled_unmet(prior, endpoint):
    """Terminal unmet skill requirement, not permission to accept it as done."""
    if prior.get('ok') is not False or prior.get('missed_skill_phase') is not True or prior.get('settlement_verified') is not True or (prior.get('requirement_state') != 'settled_unmet') or (prior.get('pending') is not False) or (prior.get('game_task_complete') is not False) or (prior.get('last_observation', {}).get('page') != 'home') or (prior.get('last_observation', {}).get('endpoint') != endpoint) or (prior.get('action_outcome', {}).get('execution_status') == 'uncertain'):
        return False
    proof = prior.get('direct_settlement_entry')
    if not completion_is_bound({'direct_settlement_entry': proof}, endpoint):
        return False
    from desktop.mfa_harvest_restart import _completed_at_home
    try:
        return bool(_completed_at_home(prior, Path(proof['owner_root']), endpoint))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False
