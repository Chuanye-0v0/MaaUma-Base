"""Fresh-frame, navigation-only executor for one account inventory."""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import time
import re
from pathlib import Path
import cv2
import numpy as np
from core.support_library.live import SupportDevice
from core.action_recovery import action_boundary, ReobserveRequired, require_fresh_observation
from core.location_navigation import SETTLEMENT_ROUTES
READ_ONLY_ACTIONS = {*SETTLEMENT_ROUTES.values(), 'open_training_menu', 'back_training_setup', 'dismiss_event_result', 'back_event', 'open_event_story_menu', 'skip_event_story', 'confirm_event_story_skip', 'close_legend_help', 'close_training_complete', 'open_enhance', 'open_uma', 'open_uma_list', 'open_menu', 'open_profile', 'back_home', 'close_detail', 'close_menu', 'return_title', 'confirm_title', 'enter_game', 'close_notice', 'close_story', 'close_uma_help', 'continue_profile_help', 'close_profile_help', 'confirm_date_change', 'scroll_uma_top', 'scroll_uma'}
ACTION_PAGES = {**{action: {page} for page, action in SETTLEMENT_ROUTES.items()}, 'open_training_menu': {'autonomous_running'}, 'back_training_setup': {'training_trainee', 'training_scenario', 'training_exam_mode'}, 'dismiss_event_result': {'event_bingo', 'event_notice'}, 'back_event': {'event_roulette', 'event_missions'}, 'open_event_story_menu': {'event_story_playing'}, 'skip_event_story': {'event_story_menu'}, 'confirm_event_story_skip': {'event_story_skip_confirm'}, 'close_training_complete': {'training_complete'}, 'open_enhance': {'home'}, 'open_uma': {'enhance_menu'}, 'open_uma_list': {'uma_menu'}, 'open_menu': {'home'}, 'open_profile': {'menu'}, 'back_home': {'uma_list', 'profile', 'enhance_menu', 'uma_menu', 'arena_empty', 'arena_idle', 'arena_pending', 'races', 'arena_opponents', 'daily_legend_list', 'daily_schedule', 'race_selection', 'story_event', 'other_event', 'training_menu'}, 'close_legend_help': {'daily_legend_help'}, 'close_detail': {'uma_detail'}, 'close_menu': {'menu'}, 'return_title': {'menu'}, 'confirm_title': {'title_confirm'}, 'enter_game': {'title'}, 'close_notice': {'notice'}, 'close_story': {'story_unlock'}, 'close_uma_help': {'uma_unlock_help'}, 'continue_profile_help': {'profile_help'}, 'close_profile_help': {'profile_support_help'}, 'confirm_date_change': {'date_change'}, 'scroll_uma_top': {'uma_list'}, 'scroll_uma': {'uma_list'}}

class InventoryDevice(SupportDevice):

    def __init__(self, endpoint: str, adb_path: str, output_dir: Path):
        if endpoint != _public_endpoint():
            raise RuntimeError('test_device_only')
        super().__init__(endpoint, adb_path, output_dir)

    def capture(self) -> dict:
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured_monotonic = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('screenshot_invalid')
        from .observer import observe, read_tokens
        observed = observe(raw, read_tokens(image, self.ocr))
        observed['captured_monotonic'] = captured_monotonic
        observed['endpoint'] = self.endpoint
        if observed.get('frame_sha256'):
            if getattr(self, '_last_frame_sha', None) != observed['frame_sha256']:
                self._last_frame_sha = observed['frame_sha256']
                self._unchanged_since = captured_monotonic
            observed['unchanged_since_monotonic'] = self._unchanged_since
        from core.unknown_page_evidence import save_unknown_page
        save_unknown_page(self, image, observed)
        return observed

    @action_boundary
    def step(self, action_id: str, stop_event=None, expected_character=None) -> dict:
        if stop_event is not None and stop_event.is_set():
            return {'ok': False, 'reason': 'operator_stop', 'tap_count': 0}
        observed = self.capture()
        character_action = re.fullmatch('uma_\\d+', action_id) is not None
        if character_action and isinstance(expected_character, dict) and (observed.get('page') == 'unknown'):
            for _ in range(3):
                if stop_event is not None:
                    if stop_event.wait(0.3):
                        return {'ok': False, 'reason': 'operator_stop', 'observation': observed, 'tap_count': 0}
                else:
                    time.sleep(0.3)
                observed = self.capture()
                if observed.get('page') != 'unknown':
                    break
        candidate = observed.get('actions', {}).get(action_id)
        permitted = action_id in READ_ONLY_ACTIONS or character_action
        if not permitted or candidate is None:
            return {'ok': False, 'reason': 'action_not_allowed', 'observation': observed, 'tap_count': 0}
        expected_pages = {'uma_list'} if character_action else ACTION_PAGES[action_id]
        if observed.get('page') not in expected_pages:
            return {'ok': False, 'reason': 'action_page_mismatch', 'observation': observed, 'tap_count': 0}
        try:
            require_fresh_observation(observed, action_id)
        except ReobserveRequired:
            raise
        except RuntimeError:
            return {'ok': False, 'reason': 'stale_frame', 'observation': observed, 'tap_count': 0}
        if character_action:
            if not isinstance(expected_character, dict):
                return {'ok': False, 'reason': 'expected_character_required', 'observation': observed, 'tap_count': 0}
            matches = [item for item in observed.get('characters', []) if isinstance(item, dict) and item.get('action_id') == action_id]
            if len(matches) != 1 or matches[0].get('portrait_key') != expected_character.get('portrait_key') or matches[0].get('stars') != expected_character.get('stars'):
                return {'ok': False, 'reason': 'expected_character_changed', 'observation': observed, 'tap_count': 0}
        if stop_event is not None and stop_event.is_set():
            return {'ok': False, 'reason': 'operator_stop', 'observation': observed, 'tap_count': 0}
        swipe = candidate.get('swipe') if isinstance(candidate, dict) else None
        if swipe is not None:
            if not isinstance(swipe, list) or len(swipe) != 5 or (not all((isinstance(value, int) for value in swipe))) or (not (0 <= swipe[0] < 720 and 0 <= swipe[2] < 720 and (0 <= swipe[1] < 1280) and (0 <= swipe[3] < 1280) and (1 <= swipe[4] <= 2000))):
                return {'ok': False, 'reason': 'action_candidate_invalid', 'observation': observed, 'tap_count': 0}
            self.command('-s', self.endpoint, 'shell', 'input', 'swipe', *map(str, swipe))
        else:
            point = candidate.get('point') if isinstance(candidate, dict) else None
            if not isinstance(point, list) or len(point) != 2 or (not all((isinstance(value, int) for value in point))) or (not (0 <= point[0] < 720 and 0 <= point[1] < 1280)):
                return {'ok': False, 'reason': 'action_candidate_invalid', 'observation': observed, 'tap_count': 0}
            self.command('-s', self.endpoint, 'shell', 'input', 'tap', *map(str, point))
        if action_id in {'scroll_uma_top', 'scroll_uma'}:
            previous_signature = None
            after = observed
            for settle_reads in range(1, 5):
                if stop_event is not None:
                    if stop_event.wait(0.3):
                        return {'ok': False, 'reason': 'operator_stop_after_tap', 'tap_count': 1, 'settle_reads': settle_reads - 1, 'source_frame_sha256': observed.get('frame_sha256')}
                else:
                    time.sleep(0.3)
                after = self.capture()
                signature = after.get('list_signature')
                if after.get('page') == 'uma_list' and isinstance(signature, list) and signature:
                    if signature == previous_signature:
                        return {'ok': True, 'reason': 'step_observed', 'action_id': action_id, 'tap_count': 1, 'settle_reads': settle_reads, 'source_frame_sha256': observed.get('frame_sha256'), 'observation': after}
                    previous_signature = signature
                else:
                    previous_signature = None
            return {'ok': False, 'reason': 'list_motion_not_settled', 'action_id': action_id, 'tap_count': 1, 'settle_reads': 4, 'source_frame_sha256': observed.get('frame_sha256'), 'observation': after}
        if stop_event is not None and stop_event.wait(0.5):
            return {'ok': False, 'reason': 'operator_stop_after_tap', 'tap_count': 1, 'source_frame_sha256': observed.get('frame_sha256')}
        if stop_event is None:
            time.sleep(0.5)
        after = self.capture()
        return {'ok': after.get('page') not in {'disconnected', 'auth_blocked'}, 'reason': 'step_observed' if after.get('page') not in {'disconnected', 'auth_blocked'} else 'device_blocked', 'action_id': action_id, 'tap_count': 1, 'source_page': observed.get('page'), 'source_frame_sha256': observed.get('frame_sha256'), 'observation': after}
