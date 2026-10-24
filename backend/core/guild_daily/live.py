"""Project-native capture and a single guarded input for guild daily actions."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
from pathlib import Path
import time
import cv2
import numpy as np
from core.action_recovery import action_boundary, await_candidate, require_fresh_observation, ReobserveRequired
from core.support_library.live import SupportDevice
from core.guild_daily.observer import observe, refine_sender_names
from core.guild_daily import ITEMS
PAGES = {'open_guild': {'home'}, 'open_requests': {'guild_hub'}, 'return_home': {'guild_hub', 'guild_unjoined'}, 'open_menu': {'guild_hub'}, 'open_donations': {'guild_hub'}, 'open_filter': {'guild_hub'}, 'scroll_chat': {'guild_hub'}, 'continue_guild_intro': {'guild_unjoined'}, 'close_guild_intro': {'guild_unjoined'}, 'close_unjoined': {'guild_unjoined'}, 'open_request': {'guild_requests'}, 'scroll_requests': {'guild_requests'}, 'scroll_requests_top': {'guild_requests'}, 'claim_received': {'guild_requests'}, 'confirm_request': {'guild_request_picker', 'guild_request_confirm'}, 'confirm_donation': {'guild_donation_confirm'}, 'close': {'guild_requests', 'guild_donation_confirm', 'guild_request_picker', 'guild_request_active', 'guild_donation_limit', 'guild_request_success', 'guild_donation_success', 'guild_claim_success', 'guild_locked_dialog', 'guild_menu', 'guild_request_confirm', 'guild_rank_reward'}}

def authorize(action, current, expected, state, options):
    if current.get('endpoint') != expected.get('endpoint') or current.get('endpoint') != _public_endpoint():
        raise RuntimeError('guild_endpoint_not_bound')
    candidate = current.get('actions', {}).get(action)
    prior = expected.get('actions', {}).get(action)
    pages = {'guild_requests', 'guild_hub'} if action.startswith('donate_') else {'guild_request_picker'} if action.startswith('select_') else PAGES.get(action, set())
    if current.get('page') not in pages:
        raise RuntimeError('guild_action_not_allowed')
    if current.get('page') != expected.get('page') or not candidate or (not prior):
        raise ReobserveRequired(action, current)
    for key in ('effect', 'key', 'item'):
        if candidate.get(key) != prior.get(key):
            raise ReobserveRequired(action, current)
    effect = {'confirm_donation': 'donation', 'confirm_request': 'request', 'claim_received': 'claim'}.get(action, 'navigation')
    if candidate.get('effect') != effect:
        raise RuntimeError('guild_action_effect_changed')
    if action.startswith('select_') and action.removeprefix('select_') != options['request_item']:
        raise RuntimeError('guild_request_item_not_selected')
    if action.startswith('donate_') and (not options['donate']):
        raise RuntimeError('guild_donation_disabled')
    if action == 'confirm_donation':
        selected = state.get('selected_request') or {}
        if not options['donate'] or not selected.get('key') or current.get('item') not in ITEMS or (selected.get('item') != current.get('item')):
            raise RuntimeError('guild_donation_not_bound')
    selected = current.get('selected_item') if current.get('page') == 'guild_request_picker' else current.get('item')
    if action == 'confirm_request' and (options['request_item'] is None or selected != options['request_item'] or state['request_done'] or (current.get('page') == 'guild_request_confirm' and state.get('selection_attempted') is not True)):
        raise RuntimeError('guild_request_not_bound')
    if candidate.get('effect') in {'donation', 'request', 'claim'}:
        pending = state.get('pending') or {}
        if pending.get('action_id') != action or pending.get('status') != 'intent':
            raise RuntimeError('guild_transaction_not_reserved')
    if candidate.get('effect') not in {'navigation', 'donation', 'request', 'claim'}:
        raise RuntimeError('guild_action_not_allowed')
    return candidate

class GuildDailyDevice(SupportDevice):

    def capture(self):
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('screenshot_invalid')
        rows, _ = self.ocr(image)
        tokens = [dict(text=text, confidence=float(conf), bbox=[int(min((p[0] for p in box))), int(min((p[1] for p in box))), int(max((p[0] for p in box))), int(max((p[1] for p in box)))]) for box, text, conf in rows or []]
        tokens = refine_sender_names(image, tokens, self.ocr)
        observed = observe(raw, image, tokens)
        path = self.output_dir / (str(time.time_ns()) + '.png')
        path.write_bytes(raw)
        observed.update(endpoint=self.endpoint, captured_monotonic=captured, screenshot_path=str(path), screen_size=[720, 1280], scale=1)
        path.with_suffix('.json').write_text(json.dumps(observed, ensure_ascii=False), encoding='utf8')
        self._latest = observed
        return observed

    @action_boundary
    def step(self, action, stop, expected):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        before = self.capture()
        if action.startswith('donate_') and before.get('page') in {'guild_requests', 'guild_hub'} and (action not in before.get('actions', {})):
            raise ReobserveRequired(action, before)
        if action not in before.get('actions', {}):
            before = await_candidate(self.capture, stop, action, before, expected['page'])
        candidate = authorize(action, before, expected, self.guild_state, self.guild_options)
        require_fresh_observation(before, action)
        point = candidate.get('point')
        swipe = candidate.get('swipe')
        values = swipe if swipe is not None else point
        if not isinstance(values, (tuple, list)) or len(values) != (4 if swipe is not None else 2) or any((type(v) is not int or not 0 <= v < (720 if i % 2 == 0 else 1280) for i, v in enumerate(values))):
            raise RuntimeError('guild_target_invalid')
        args = ['-s', self.endpoint, 'shell', 'input', 'swipe' if swipe is not None else 'tap', *map(str, values)]
        if swipe is not None:
            args.append(str(candidate['duration_ms']))
        self.command(*args)
        from core.navigation_wait import wait_transition
        after = wait_transition(self.capture, stop, before, same_page_ready=lambda frame: action.startswith('select_') and frame.get('selected_item') == action.removeprefix('select_') or (action in {'scroll_requests', 'scroll_requests_top', 'scroll_chat'} and frame.get('list_signature') != before.get('list_signature')) or (action == 'open_filter' and frame.get('chat_filter') != before.get('chat_filter')))
        path = self.output_dir / (str(time.time_ns()) + '-receipt.json')
        receipt = dict(action_id=action, performed=True, endpoint=self.endpoint, source_frame_sha256=before['frame_sha256'], source_screenshot_path=before['screenshot_path'], observation=after, receipt_path=str(path))
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')
        return receipt
