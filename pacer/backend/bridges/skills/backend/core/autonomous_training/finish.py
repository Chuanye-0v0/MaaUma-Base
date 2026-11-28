"""Single-entry submission for the end-of-training control."""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
import os
import time
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any
from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN
from core.autonomous_training.success_close import _child_purchase_ids
_ENDPOINT = _public_endpoint()
_ACTION = 'finish_training'
_INTENT = 'finish_entry_intent.json'
_REPORT = 'finish_entry_report.json'

def finish_control_visible(observed: Mapping[str, Any]) -> bool:
    actions = observed.get('actions')
    control = actions.get(_ACTION) if isinstance(actions, Mapping) else None
    point = control.get('point') if isinstance(control, Mapping) else None
    if not (isinstance(point, list) and len(point) == 2 and all((type(item) is int for item in point)) and (0 <= point[0] < 720) and (0 <= point[1] < 1280)):
        return False
    if observed.get('page') not in (None, 'finish_ready') or control.get('enabled') is False:
        return False
    label = control.get('text', control.get('label'))
    if label is not None and str(label).replace(' ', '') not in {'养成结束', '养成结束！', '养成结束!'}:
        return False
    box = control.get('bbox')
    if box is not None and (not (isinstance(box, list) and len(box) == 4 and all((type(v) is int for v in box)) and (0 <= box[0] < box[2] <= 720) and (0 <= box[1] < box[3] <= 1280) and (box[0] <= point[0] <= box[2]) and (box[1] <= point[1] <= box[3]))):
        return False
    return True

def canonical_finish_evidence(observed: Mapping[str, Any]) -> dict[str, Any] | None:
    summary = observed.get('finish_summary')
    if not (isinstance(summary, Mapping) and isinstance(summary.get('name'), str) and summary['name'] and isinstance(summary.get('run_styles'), Mapping) and summary['run_styles'] and (set(summary['run_styles']) <= {'领跑', '跟前', '居中', '后追'}) and all((isinstance(grade, str) and grade in {'G', 'F', 'E', 'D', 'C', 'B', 'A', 'S', 'SS'} for grade in summary['run_styles'].values())) and finish_control_visible(observed)):
        return None
    return {'name': summary['name'], 'run_styles': dict(summary['run_styles'])}

def submit_finish_entry_once(*, device: Any, child_plan_path: str | Path | None=None, expected_child_plan_sha256: str | None=None, return_after_learning_report_path: str | Path | None=None, expected_return_after_learning_report_sha256: str | None=None, no_purchase_evidence_path: str | Path | None=None, expected_no_purchase_evidence_sha256: str | None=None, no_purchase_return_report_path: str | Path | None=None, expected_no_purchase_return_report_sha256: str | None=None, finish_ready_observation_path: str | Path, expected_finish_ready_observation_sha256: str, completion_authorization: Mapping[str, Any], output_dir: str | Path, mode: str='dry-run', execute: bool=False, safety_token: str | None=None, expected_endpoint: str=_ENDPOINT, skip_skills: bool=False) -> dict[str, Any]:
    """Submit at most one observed finish-entry control, then stop."""
    root = Path(output_dir)
    base = _result(mode, execute)
    if expected_endpoint not in {_ENDPOINT, _public_endpoint()} or getattr(device, 'endpoint', None) != expected_endpoint:
        return _write(root, {**base, 'stop_reason': 'device_endpoint_not_allowed'})
    if mode not in {'dry-run', 'live'} or type(execute) is not bool:
        return _write(root, {**base, 'stop_reason': 'execution_mode_invalid'})
    observed_path = Path(finish_ready_observation_path)
    observed = _read(observed_path)
    if observed is None:
        return _write(root, {**base, 'stop_reason': 'finish_entry_artifact_unreadable'})
    observed_sha = _sha(observed_path)
    if not _hex(expected_finish_ready_observation_sha256) or observed_sha != expected_finish_ready_observation_sha256:
        return _write(root, {**base, 'stop_reason': 'finish_entry_sha256_mismatch'})
    standard_requested = any((value is not None for value in (child_plan_path, expected_child_plan_sha256, return_after_learning_report_path, expected_return_after_learning_report_sha256)))
    zero_requested = any((value is not None for value in (no_purchase_evidence_path, expected_no_purchase_evidence_sha256, no_purchase_return_report_path, expected_no_purchase_return_report_sha256)))
    if type(skip_skills) is not bool or sum((standard_requested, zero_requested, skip_skills)) != 1:
        return _write(root, {**base, 'stop_reason': 'finish_entry_chain_ambiguous'})
    if skip_skills:
        remaining = observed.get('current_skill_points')
        if expected_endpoint != _ENDPOINT or type(remaining) is not int or remaining < 0 or (completion_authorization.get('skip_skills') is not True) or (completion_authorization.get('discard_remaining_skill_points_accepted') is not True) or (type(completion_authorization.get('accepted_remaining_skill_points')) is not int) or (completion_authorization['accepted_remaining_skill_points'] != remaining):
            return _write(root, {**base, 'stop_reason': 'completion_authorization_invalid'})
        returned_path = observed_path
        chain = {'skip_skills_observation_sha256': observed_sha, 'plan_id': 'skip-skills-' + observed_sha[:16], 'session_id': str(observed_path.resolve())}
    elif standard_requested:
        if None in (child_plan_path, expected_child_plan_sha256, return_after_learning_report_path, expected_return_after_learning_report_sha256):
            return _write(root, {**base, 'stop_reason': 'finish_entry_chain_incomplete'})
        child_path, returned_path = (Path(child_plan_path), Path(return_after_learning_report_path))
        child, returned = (_read(child_path), _read(returned_path))
        if child is None or returned is None:
            return _write(root, {**base, 'stop_reason': 'finish_entry_artifact_unreadable'})
        child_sha, returned_sha = (_sha(child_path), _sha(returned_path))
        if not _hex(expected_child_plan_sha256) or not _hex(expected_return_after_learning_report_sha256) or (child_sha, returned_sha) != (expected_child_plan_sha256, expected_return_after_learning_report_sha256):
            return _write(root, {**base, 'stop_reason': 'finish_entry_sha256_mismatch'})
        remaining = _valid_child(child)
        if remaining is None or not _valid_return(returned, child, child_sha, remaining):
            return _write(root, {**base, 'stop_reason': 'finish_entry_return_chain_invalid'})
        chain = {'child_plan_sha256': child_sha, 'return_after_learning_report_sha256': returned_sha, 'plan_id': child['plan_id'], 'session_id': child['execution_promotion']['session_id']}
    else:
        if None in (no_purchase_evidence_path, expected_no_purchase_evidence_sha256, no_purchase_return_report_path, expected_no_purchase_return_report_sha256):
            return _write(root, {**base, 'stop_reason': 'finish_entry_chain_incomplete'})
        evidence_path, returned_path = (Path(no_purchase_evidence_path), Path(no_purchase_return_report_path))
        evidence, returned = (_read(evidence_path), _read(returned_path))
        if evidence is None or returned is None:
            return _write(root, {**base, 'stop_reason': 'finish_entry_artifact_unreadable'})
        evidence_sha, returned_sha = (_sha(evidence_path), _sha(returned_path))
        if not _hex(expected_no_purchase_evidence_sha256) or not _hex(expected_no_purchase_return_report_sha256) or (evidence_sha, returned_sha) != (expected_no_purchase_evidence_sha256, expected_no_purchase_return_report_sha256):
            return _write(root, {**base, 'stop_reason': 'finish_entry_sha256_mismatch'})
        remaining = _valid_no_purchase_return(evidence, returned, evidence_sha, observed_path, observed_sha)
        if remaining is None:
            return _write(root, {**base, 'stop_reason': 'finish_entry_no_purchase_chain_invalid'})
        chain = {'no_purchase_evidence_sha256': evidence_sha, 'no_purchase_return_report_sha256': returned_sha, 'plan_id': f'no-purchase-{evidence_sha[:16]}', 'session_id': evidence['turn_id']}
    if not _valid_finish_ready(observed, observed_path, expected_endpoint=expected_endpoint):
        return _write(root, {**base, 'stop_reason': 'finish_entry_identity_or_control_invalid'})
    if not skip_skills and (not _valid_authorization(completion_authorization, remaining)):
        return _write(root, {**base, 'stop_reason': 'completion_authorization_invalid'})
    prepared = {**base, **chain, 'finish_ready_observation_sha256': observed_sha, 'finish_ready_frame_sha256': observed['frame_sha256'], 'accepted_remaining_skill_points': remaining, 'plan_id': chain['plan_id'], 'session_id': chain['session_id']}
    if mode == 'dry-run' or execute is not True:
        return _write(root, {**prepared, 'stop_reason': 'dry_run_no_device_actions'})
    if safety_token != SKILL_PURCHASE_SAFETY_TOKEN:
        return _write(root, {**prepared, 'stop_reason': 'execution_gate_rejected'})
    intent_key = hashlib.sha256(json.dumps([chain['plan_id'], chain['session_id'], observed_sha], separators=(',', ':')).encode()).hexdigest()
    intent_path = returned_path.parent / '.finish_entry_intents' / f'{intent_key}.json'
    if os.name == 'nt' and len(str(intent_path.resolve())) >= 260:
        intent_path = Path('\\\\?\\' + str(intent_path.resolve()))
    context = getattr(device, 'context', None)
    if not isinstance(context, MutableMapping):
        return _write(root, {**prepared, 'stop_reason': 'explicit_device_context_required'})
    intent = {'schema_version': 'uma.autonomous_finish_entry_intent.v1', 'status': 'dispatch_intent', 'action_id': _ACTION, **{key: prepared[key] for key in ('finish_ready_observation_sha256', 'finish_ready_frame_sha256', 'accepted_remaining_skill_points', 'plan_id', 'session_id')}, 'recommended_skills_complete': not skip_skills, 'skip_skills': skip_skills, 'discard_remaining_skill_points_accepted': True}
    intent['finish_chain_kind'] = 'skip_skills' if skip_skills else 'no_purchase' if 'no_purchase_evidence_sha256' in chain else 'purchase'
    for key in ('child_plan_sha256', 'return_after_learning_report_sha256', 'no_purchase_evidence_sha256', 'no_purchase_return_report_sha256', 'skip_skills_observation_sha256'):
        if key in chain:
            intent[key] = chain[key]
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with intent_path.open('x', encoding='utf-8') as handle:
            json.dump(intent, handle, ensure_ascii=False, indent=2)
    except FileExistsError:
        return _write(root, {**prepared, 'stop_reason': 'finish_entry_already_submitted'})
    intent_sha = _sha(intent_path)
    context.update(mode='harvest', finish_submission={**intent, 'intent_path': str(intent_path), 'intent_sha256': intent_sha})
    try:
        receipt = device.step(_ACTION)
    except Exception:
        return _write(root, {**prepared, 'intent_path': str(intent_path), 'finish_entry_submitted': True, 'stop_reason': 'finish_entry_dispatch_uncertain'})
    source = receipt.get('source_screenshot_path') if isinstance(receipt, Mapping) else None
    source_path = Path(source) if isinstance(source, str) else None
    source_observed = {'finish_summary': receipt.get('source_finish_summary'), 'actions': {_ACTION: {'point': receipt.get('source_finish_action_point')}}} if isinstance(receipt, Mapping) else {}
    if not (isinstance(receipt, Mapping) and receipt.get('action_id') == _ACTION and (receipt.get('performed') is True) and (receipt.get('finish_intent_path') == str(intent_path)) and (source_path is not None) and _hex(receipt.get('source_frame_sha256')) and (_sha(source_path) == receipt['source_frame_sha256']) and (type(receipt.get('source_captured_at_unix_ms')) is int) and finish_control_visible(source_observed) and finish_control_visible(observed)):
        return _write(root, {**prepared, 'intent_path': str(intent_path), 'intent_sha256': intent_sha, 'finish_entry_submitted': True, 'stop_reason': 'finish_entry_dispatch_uncertain'})
    observation = receipt.get('observation') if isinstance(receipt, Mapping) else None
    post = observation if isinstance(observation, Mapping) else {}
    post_path = Path(post['screenshot_path']) if isinstance(post.get('screenshot_path'), str) else None
    if post_path is None or not _hex(post.get('frame_sha256')) or type(post.get('captured_at_unix_ms')) is not int or (_sha(post_path) != post['frame_sha256']):
        return _write(root, {**prepared, 'intent_path': str(intent_path), 'intent_sha256': intent_sha, 'finish_entry_submitted': True, 'stop_reason': 'finish_entry_dispatch_uncertain'})
    return _write(root, {**prepared, 'intent_path': str(intent_path), 'intent_sha256': intent_sha, 'finish_entry_submitted': True, 'stop_reason': 'finish_entry_probe_complete', 'actions': [_ACTION], 'device_action_count': 1, 'post_state': 'observed_unclassified', 'post_frame_path': post.get('screenshot_path'), 'post_frame_sha256': post.get('frame_sha256'), 'post_captured_at_unix_ms': post.get('captured_at_unix_ms')})

def _valid_child(child: Mapping[str, Any]) -> int | None:
    remaining = child.get('expected_remaining_skill_points')
    promotion = child.get('execution_promotion')
    if _child_purchase_ids(child) is None or not isinstance(child.get('plan_id'), str) or (not isinstance(promotion, Mapping)) or (not isinstance(promotion.get('session_id'), str)) or (type(remaining) is not int) or (remaining < 0):
        return None
    return remaining

def _valid_return(report: Mapping[str, Any], child: Mapping[str, Any], child_sha: str, remaining: int) -> bool:
    return report.get('schema_version') == 'uma.autonomous_return_after_learning.v1' and report.get('mode') == 'live' and (report.get('execute') is True) and (report.get('stop_reason') == _return_complete_stop(child)) and (report.get('plan_id') == child['plan_id']) and (report.get('child_plan_sha256') == child_sha) and (report.get('return_performed') is True) and (report.get('actions') == ['skill_purchase.return_after_purchase']) and (report.get('device_action_count') == 1) and (type(report.get('verified_current_skill_points')) is int) and (report['verified_current_skill_points'] == remaining) and _hex(report.get('source_frame_sha256')) and (report.get('verified_skill_points_frame_sha256') == report['source_frame_sha256']) and _hex(report.get('final_confirmation_report_sha256')) and _hex(report.get('success_close_report_sha256'))

def _valid_no_purchase_return(evidence: Mapping[str, Any], returned: Mapping[str, Any], evidence_sha: str, observed_path: Path, observed_sha: str | None) -> int | None:
    from core.autonomous_training.harvest_skills import _valid_no_purchase_evidence
    remaining = evidence.get('current_skill_points')
    if not _valid_no_purchase_evidence(evidence) or evidence.get('schema_version') != 'uma.harvest_no_purchase_evidence.v1' or evidence.get('status') != 'complete_no_harvest_eligible_purchase' or (evidence.get('selected_action_ids') != []) or (type(remaining) is not int) or (remaining < 0) or (not isinstance(evidence.get('turn_id'), str)) or (not evidence['turn_id']) or (returned.get('schema_version') != 'uma.harvest_no_purchase_return.v1') or (returned.get('mode') != 'live') or (returned.get('execute') is not True) or (returned.get('stop_reason') != 'return_harvest_skills_probe_complete') or (returned.get('return_performed') is not True) or (returned.get('actions') != ['return_harvest_skills']) or (returned.get('device_action_count') != 1) or (returned.get('no_purchase_evidence_sha256') != evidence_sha) or (returned.get('verified_current_skill_points') != remaining) or (returned.get('finish_ready_observation_path') != str(observed_path)) or (returned.get('finish_ready_observation_sha256') != observed_sha):
        return None
    for name in ('catalog', 'batch_plan', 'inventory', 'planner_report', 'capture_manifest'):
        path, sha = (evidence.get(f'{name}_path'), evidence.get(f'{name}_sha256'))
        if not isinstance(path, str) or not _hex(sha) or _sha(Path(path)) != sha:
            return None
    return remaining

def _return_complete_stop(child: Mapping[str, Any]) -> str:
    purchases = child.get('purchases')
    return 'return_after_purchase_probe_complete' if isinstance(purchases, list) and len(purchases) == 1 else 'batch_return_after_purchase_probe_complete'

def _valid_finish_ready(observed: Mapping[str, Any], source: Path, *, expected_endpoint: str=_ENDPOINT) -> bool:
    now = int(time.time() * 1000)
    return observed.get('page') == 'finish_ready' and observed.get('endpoint') == expected_endpoint and _hex(observed.get('frame_sha256')) and (_sha(Path(observed.get('screenshot_path', ''))) == observed['frame_sha256']) and (_sha(source) is not None) and finish_control_visible(observed) and (type(observed.get('captured_at_unix_ms')) is int) and (type(observed.get('expires_at_unix_ms')) is int) and (observed['captured_at_unix_ms'] <= now <= observed['expires_at_unix_ms'])

def _valid_authorization(value: Mapping[str, Any], remaining: int) -> bool:
    return isinstance(value, Mapping) and value.get('recommended_skills_complete') is True and (value.get('discard_remaining_skill_points_accepted') is True) and (type(value.get('accepted_remaining_skill_points')) is int) and (value['accepted_remaining_skill_points'] == remaining)

def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None

def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None

def _hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all((char in '0123456789abcdef' for char in value))

def _result(mode: str, execute: bool) -> dict[str, Any]:
    return {'schema_version': 'uma.autonomous_finish_entry.v1', 'mode': mode, 'execute': execute, 'finish_entry_submitted': False, 'actions': [], 'device_action_count': 0}

def _write(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / _REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
