"""Read-only resolution of daily attempts that failed before journal handoff."""
import json
from pathlib import Path

def account_protected(report, context):
    return any((not isinstance(value, dict) or value.get('account_conflict') is True or value.get('account_paused') is True or (isinstance(value.get('action_outcome'), dict) and value['action_outcome'].get('decision') == 'pause_account') for value in (report, context)))

def resolve_resume_source(source, endpoint):
    source = Path(source).resolve()
    journal = json.loads((source.parent / 'active.json').read_text(encoding='utf8'))
    anchor = Path(journal['output']).resolve()
    if journal['endpoint'] != endpoint or anchor.parent != source.parent:
        raise RuntimeError('daily_resume_source_mismatch')
    original = json.loads((anchor / 'result.json').read_text(encoding='utf8'))
    original_context = json.loads((anchor / 'context.json').read_text(encoding='utf8'))
    if account_protected(original, original_context):
        raise RuntimeError('daily_resume_account_protected')
    if anchor == source:
        return source
    report = json.loads((source / 'result.json').read_text(encoding='utf8'))
    context = json.loads((source / 'context.json').read_text(encoding='utf8'))
    if account_protected(report, context):
        raise RuntimeError('daily_resume_account_protected')
    progress = ('initial_tickets', 'completed_races', 'claims', 'pending_race', 'pending_exchange')
    outcome = report.get('action_outcome') or {}
    rejected_before_capture = report.get('reason') == 'daily_navigation_resume_unverified' and context == {} and (report.get('last_observation') == {}) and (report.get('initial_tickets') is None) and (report.get('completed_races') == report.get('claims') == 0) and (report.get('pending_race') is False) and (report.get('pending_exchange') is False) and (not any(source.glob('*.png'))) and (not any(source.rglob('*-submitted.json'))) and (not (source / 'recovery/last-action.json').exists())
    paused_before_capture = report.get('reason') == 'operator_stop' and report.get('last_observation') == {} and (context == original_context) and bool(context) and all((key in report and key in original and (report[key] == original[key]) for key in progress)) and (not any(source.rglob('*-submitted.json'))) and (not (source / 'recovery/last-action.json').exists())
    if report.get('ok') is False and original.get('ok') is False and (rejected_before_capture or paused_before_capture) and (type(report.get('device_actions')) is int) and (report['device_actions'] == 0) and (not any(source.rglob('*-receipt.json'))) and (outcome.get('input_attempts', 0) == 0) and (outcome.get('execution_status') not in {'executed', 'uncertain'}):
        return anchor
    if report.get('ok') is not False or original.get('ok') is not False or report.get('reason') not in {'daily_transition_timeout', 'game_stalled', 'operator_stop'} or (type(report.get('device_actions')) is not int) or (report['device_actions'] != 0) or any(source.glob('*-receipt.json')) or (outcome.get('input_attempts', 0) != 0) or (outcome.get('execution_status') in {'executed', 'uncertain'}) or (report.get('reason') == 'operator_stop' and (any(source.rglob('*-submitted.json')) or (source / 'recovery/last-action.json').exists())) or (report.get('last_observation', {}).get('endpoint') != endpoint) or (not context) or (context != original_context) or any((key not in report or key not in original or report[key] != original[key] for key in progress)):
        raise RuntimeError('daily_resume_source_mismatch')
    return anchor
