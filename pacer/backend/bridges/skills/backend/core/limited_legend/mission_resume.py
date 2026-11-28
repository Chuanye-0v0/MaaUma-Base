"""Read-only proof of a period-claim popup already closed by the shared handler."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
from pathlib import Path

def load_mission_resume(output, endpoint):
    output = Path(output).resolve()

    def read(path):
        return json.loads(path.read_text(encoding='utf8'))
    try:
        prior = read(output / 'adapter_result.json')
        if prior.get('reason') == 'stale_frame':
            owner = read(output / 'result.json')
            outcome = prior.get('action_outcome') or {}
            folder = output / 'missions'
            final = prior.get('last_observation') or {}
            image = Path(final.get('screenshot_path', '')).resolve()
            frame = read(image.with_suffix('.json'))
            if endpoint != _public_endpoint() or prior.get('ok') is not False or owner.get('pending') is not False or (prior.get('tickets_after') != 0) or (type(prior.get('completed_races')) is not int) or (not 1 <= prior['completed_races'] <= 3) or (len(prior.get('races', [])) != prior['completed_races']) or (prior['completed_races'] != prior.get('tickets_before')) or any((owner.get(k) != prior.get(k) for k in ('reason', 'races', 'completed_races', 'tickets_before', 'tickets_after', 'action_outcome'))) or (outcome.get('action_id') not in {'missions_daily', 'missions_period', 'close_missions'}) or (outcome.get('execution_status') != 'not_executed') or (outcome.get('input_attempts') != 0) or (outcome.get('acknowledged_inputs') != 0) or (read(folder / 'recovery/last-action.json') != outcome) or (image.parent != folder) or (final.get('endpoint') != endpoint) or (final.get('page') != 'limited_missions') or (final.get('mission_state_verified') is not True) or (final.get('mission_tab') not in {'daily', 'period'}) or (type(final.get('claim_enabled')) is not bool) or any((frame.get(k) != final.get(k) for k in ('page', 'endpoint', 'mission_tab', 'mission_state_verified', 'claim_enabled', 'frame_sha256'))) or (hashlib.sha256(image.read_bytes()).hexdigest() != final.get('frame_sha256')) or any((read(p).get('execution_status') == 'uncertain' for p in output.rglob('last-action.json'))) or prior.get('passive_popup_receipts'):
                return None
            return dict(prior, mission_tabs_remaining=True)
        if prior.get('reason') != 'limited_claim_receipt_missing':
            return None
        owner = read(output / 'result.json')
        if endpoint != _public_endpoint() or prior.get('ok') is not False or prior.get('missions_checked') != ['daily'] or (prior.get('tickets_after') != 0) or (prior.get('completed_races') != prior.get('tickets_before')) or (not prior.get('races')) or (len(prior['races']) != prior['completed_races']) or any((owner.get(k) != prior.get(k) for k in ('reason', 'races', 'completed_races', 'tickets_before', 'tickets_after'))):
            return None
        folder = output / 'missions'
        if any((read(p).get('execution_status') == 'uncertain' for p in output.rglob('last-action.json'))):
            return None
        frames = [read(p) for p in folder.glob('*.json') if p.stem.isdigit()]

        def frame(digest):
            matches = [f for f in frames if f.get('frame_sha256') == digest]
            if len(matches) != 1:
                raise ValueError('frame_missing')
            f = matches[0]
            image = Path(f['screenshot_path']).resolve()
            if image.parent != folder or f.get('endpoint') != endpoint or hashlib.sha256(image.read_bytes()).hexdigest() != digest:
                raise ValueError('frame_invalid')
            return f
        claim = read(folder / 'claim_period-submitted.json')
        receipts = [read(p) for p in folder.glob('*-receipt.json')]
        if len([r for r in receipts if r.get('action_id') == 'claim_period' and r.get('source_frame_sha256') == claim.get('source_frame_sha256')]) != 1:
            return None
        before = frame(claim['source_frame_sha256'])
        final = prior['last_observation']
        saved_final = frame(final['frame_sha256'])
        if any((saved_final.get(k) != final.get(k) for k in ('page', 'mission_tab', 'mission_state_verified', 'claim_enabled', 'captured_monotonic'))):
            return None
        if claim.get('action_id') != 'claim_period' or claim.get('event') != prior.get('event') or before.get('mission_tab') != 'period' or (before.get('claim_enabled') is not True) or (before.get('mission_state_verified') is not True) or (final.get('page') != 'limited_missions') or (final.get('mission_tab') != 'period') or (final.get('mission_state_verified') is not True) or (final.get('claim_enabled') is not False):
            return None
        paths = prior.get('passive_popup_receipts', [])
        if len(paths) != 1:
            return None
        path = Path(paths[0]).resolve()
        if path.parent != folder / 'popup-actions':
            return None
        popup = read(path)
        if popup.get('kind') != 'reward_received' or popup.get('ok') is not True or popup.get('endpoint') != endpoint or (popup.get('input_status') != 'dispatched') or (popup.get('tap_count') != 1) or (popup.get('button') != '关闭') or (not before['captured_monotonic'] <= popup['captured_monotonic'] <= final['captured_monotonic']):
            return None
        frame(popup['source_frame_sha256'])
        return prior
    except (OSError, ValueError, KeyError, TypeError):
        return None
