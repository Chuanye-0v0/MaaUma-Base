"""Prove a failed entrance attempt stopped before any race or reward action."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
from pathlib import Path

def unsubmitted_selection_failure(result, output, endpoint):
    """A recorded roster-only failure can re-enter, never a submitted race."""
    allowed = {'open_races', 'open_events', 'open_legend', 'open_details', 'open_selection', 'open_sort', 'sort_evaluation', 'apply_sort', 'descending', 'scroll_top', 'scroll_next', 'confirm_selected', 'continue_lineup', 'close_legend_help'}

    def read(path):
        return json.loads(path.read_text(encoding='utf8'))

    def navigation(action):
        return action in allowed or (action.startswith('horse_') and action[6:].isdigit())
    try:
        output = Path(output).resolve()
        if endpoint != _public_endpoint() or result.get('ok') is not False or str(result.get('reason', '')).startswith('operator_stop') or (result.get('last_page') not in {'details', 'selection', 'sort', 'lineup', 'items'}) or (result.get('completed_races') != 0) or (result.get('races') != []) or (result.get('items_used') != 0) or (result.get('refills') != 0) or result.get('account_conflict') or result.get('conflict_check_error') or result.get('passive_popup_receipts') or (result.get('device_action_count_complete') is False) or (result.get('action_outcome', {}).get('execution_status') == 'uncertain') or (output.parent / 'active.json').exists():
            return False
        owner = read(output / 'result.json')
        if owner.get('pending') is not False or owner.get('complete') is not False or any((owner.get(k) != result.get(k) for k in ('ok', 'reason', 'last_page', 'screenshot_path', 'completed_races', 'races', 'tickets_before', 'tickets_after', 'device_actions'))):
            return False
        before = result.get('tickets_before')
        if before is not None and (type(before) is not int or not 0 <= before <= 3) or result.get('tickets_after') != before:
            return False
        for path in output.rglob('*-submitted.json'):
            marker = read(path)
            if marker.get('action_id') not in {'confirm_selected', 'continue_lineup'}:
                return False
        records = [read(p) for p in output.rglob('*-receipt.json')]
        if type(result.get('device_actions')) is not int or result['device_actions'] != len(records):
            return False
        frames = {s.get('frame_sha256'): s for p in output.rglob('*.json') if p.stem.isdigit() for s in [read(p)]}

        def bound(frame):
            image = Path(frame['screenshot_path']).resolve()
            return image.is_relative_to(output) and frame.get('endpoint') == endpoint and (hashlib.sha256(image.read_bytes()).hexdigest() == frame.get('frame_sha256'))
        for receipt in records:
            action = receipt.get('action_id', '')
            source = frames.get(receipt.get('source_frame_sha256'), {})
            if not navigation(action) or action not in source.get('actions', {}) or (not bound(source)) or (not bound(receipt.get('observation', {}))):
                return False
        for path in output.rglob('last-action.json'):
            state = read(path)
            if state.get('execution_status') not in {'executed', 'not_executed'} or not navigation(state.get('action_id', '')):
                return False
        image = Path(result['screenshot_path']).resolve()
        observed = read(image.with_suffix('.json'))
        return image.is_relative_to(output) and observed.get('endpoint') == endpoint and (observed.get('page') == result['last_page']) and (hashlib.sha256(image.read_bytes()).hexdigest() == observed.get('frame_sha256'))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False

def navigation_only_failure(result, output, endpoint):
    if result.get('reason') == 'limited_legend_unavailable_skipped':
        from desktop.mfa_limited_resume import completed_unavailable_result
        output = Path(output).resolve()
        return completed_unavailable_result(result, output.parents[3], endpoint) is not None
    if unsubmitted_selection_failure(result, output, endpoint):
        return True

    def read(path):
        data = json.loads(path.read_text(encoding='utf8'))
        if not isinstance(data, dict):
            raise ValueError('object_required')
        return data
    try:
        output = Path(output).resolve()
        route = [('home', 'open_races'), ('races', 'open_events'), ('events', 'open_legend')]
        page = result.get('last_page')
        help_timeout = page in {'unknown', 'daily_legend_help'} and result.get('reason') == 'navigation_not_observed'
        late_help = page == 'legend' and result.get('reason') == 'action_page_changed:open_details'
        index = len(route) if help_timeout or late_help else next((i for i, pair in enumerate(route) if pair[0] == page))
        tickets = result.get('tickets_before')
        tickets_valid = type(tickets) is int and 0 < tickets <= 3 and (result.get('tickets_after') == tickets) if late_help else tickets is None and result.get('tickets_after') is None
        if endpoint != _public_endpoint() or result.get('ok') is not False or (not (help_timeout or late_help) and result.get('reason') != 'action_unavailable:' + route[index][1]) or (result.get('completed_races') != 0) or (result.get('races') != []) or (not tickets_valid) or (result.get('items_used') != 0) or (result.get('refills') != 0) or (result.get('device_action_count_complete') is False) or result.get('account_conflict') or result.get('conflict_check_error') or (result.get('action_outcome', {}).get('execution_status') == 'uncertain') or (output.parent / 'active.json').exists() or any(output.rglob('*-submitted.json')):
            return False
        owner = read(output / 'result.json')
        if owner.get('pending') is not False or owner.get('complete') is not False or any((owner.get(k) != result.get(k) for k in ('ok', 'reason', 'last_page', 'screenshot_path', 'device_actions', 'completed_races', 'races', 'tickets_before', 'tickets_after', 'items_used', 'refills'))):
            return False
        path = Path(result['screenshot_path']).resolve()
        if path.parent != output or path.suffix != '.png':
            return False
        final = read(path.with_suffix('.json'))
        if final.get('endpoint') != endpoint or final.get('page') != page or final.get('frame_sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
            return False
        if help_timeout:
            from core.daily_legend.listing import help_close
            if not help_close(final.get('tokens', [])):
                return False
        zero_path = output / 'race-001' / 'recovery' / 'last-action.json'
        if late_help:
            zero = read(zero_path)
            if zero != result.get('action_outcome') or zero != owner.get('action_outcome') or zero != dict(action_id='open_details', execution_status='not_executed', input_attempts=0, acknowledged_inputs=0, source_page=None, source_frame_sha256=None, observed_page='daily_legend_help', decision='reobserve') or (final.get('tickets') != tickets):
                return False
            frames = sorted((p for p in (output / 'race-001').glob('*.json') if p.stem.isdigit()))
            if not frames:
                return False
            overlay = read(frames[-1])
            image = Path(overlay.get('screenshot_path', '')).resolve()
            from core.daily_legend.listing import help_close
            if image != frames[-1].with_suffix('.png') or overlay.get('endpoint') != endpoint or overlay.get('page') != 'daily_legend_help' or (hashlib.sha256(image.read_bytes()).hexdigest() != overlay.get('frame_sha256')) or (overlay.get('captured_monotonic', 0) < final['captured_monotonic']) or (not help_close(overlay.get('tokens', []))):
                return False
        receipts = [read(p) for p in sorted(output.rglob('*-receipt.json'))]
        count = result.get('device_actions')
        allowed = [a for _, a in route[:index]]
        if type(count) is not int or count != len(receipts) or count > len(allowed) or ([r.get('action_id') for r in receipts] != (allowed[-count:] if count else [])):
            return False
        snapshots = {s.get('frame_sha256'): s for p in output.glob('*.json') if p.stem.isdigit() for s in [read(p)]}
        for receipt in receipts:
            source = snapshots.get(receipt.get('source_frame_sha256'), {})
            action = receipt['action_id']
            expected_page = dict(((a, p) for p, a in route))[action]
            after = receipt.get('observation', {})
            if source.get('page') != expected_page or source.get('endpoint') != endpoint or action not in source.get('actions', {}) or (after.get('endpoint') != endpoint) or (final['captured_monotonic'] < after['captured_monotonic']):
                return False
        outcomes = [read(p) for p in output.rglob('last-action.json') if not (late_help and p == zero_path)]
        if any((o.get('execution_status') != 'executed' or o.get('action_id') not in allowed for o in outcomes)):
            return False
        if receipts:
            last = read(output / 'recovery/last-action.json')
            if last.get('execution_status') != 'executed' or last.get('input_attempts') != 1 or last.get('acknowledged_inputs') != 1 or (last.get('action_id') != receipts[-1]['action_id']) or (last.get('source_frame_sha256') != receipts[-1]['source_frame_sha256']):
                return False
        elif outcomes:
            return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return False
