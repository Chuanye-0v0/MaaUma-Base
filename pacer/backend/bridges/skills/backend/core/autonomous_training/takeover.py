"""Explicit U1 skill takeover; never starts training or changes team formation."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import threading
import time
import cv2
import numpy as np
from core.autonomous_training.live import AutonomousDevice
from core.support_library.live import SupportDevice
U1_ENDPOINT = _public_endpoint()

def authorize_identity_return(before, current, *, already_submitted=False):
    """An empty, disabled cart may be left to inspect the actual trainee."""
    if already_submitted:
        raise RuntimeError('identity_return_already_submitted')
    for observed in (before, current):
        if observed.get('endpoint') != U1_ENDPOINT or observed.get('page') != 'skills' or type(observed.get('current_skill_points')) is not int or (observed['current_skill_points'] < 0) or ('open_skill_confirmation' in observed.get('actions', {})) or ('return_harvest_skills' not in observed.get('actions', {})) or (observed.get('skill_cart_disabled') is not True) or (not observed.get('frame_sha256')) or (time.monotonic() - observed.get('captured_monotonic', 0) > 10):
            raise RuntimeError('empty_skill_cart_not_verified')
    if before['current_skill_points'] != current['current_skill_points'] or before['actions']['return_harvest_skills'] != current['actions']['return_harvest_skills']:
        raise RuntimeError('identity_return_frames_disagree')

class SkillTakeoverDevice(SupportDevice):
    capture_skill_scroll = AutonomousDevice.capture_skill_scroll

    def __init__(self, endpoint, adb_path, output_dir, *, expected_endpoint):
        if endpoint != U1_ENDPOINT or endpoint != expected_endpoint:
            raise ValueError('skill_takeover_endpoint_not_authorized')
        super().__init__(endpoint, adb_path, output_dir)
        self.context = dict(mode='harvest')

    def capture(self, *, skill_scroll_only=False):
        observed = AutonomousDevice.capture(self, skill_scroll_only=skill_scroll_only)
        if observed.get('page') == 'skills':
            image = cv2.imread(observed['screenshot_path'])
            hsv = cv2.cvtColor(image[1050:1115, 230:490], cv2.COLOR_BGR2HSV)
            observed['skill_cart_disabled'] = bool(np.mean((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] >= 130) & (hsv[:, :, 2] >= 90) & (hsv[:, :, 2] < 165)) > 0.7)
        return observed

    def step(self, action_id, stop_event=None):
        from core.autonomous_training.harvest import RESULT_ACTIONS
        allowed = set(RESULT_ACTIONS.values()) - {'open_completed_formation'}
        allowed.update({'scroll_skill_list', 'scroll_skill_reveal_bottom', 'scroll_skill_overlap', 'scroll_skill_up', 'open_skills', 'return_harvest_skills', 'finish_training', 'complete_training', 'select_skill_evolution', 'scroll_skill_evolution', 'confirm_skill_evolution_selection', 'confirm_skill_evolution', 'advance_skill_evolution', 'confirm_training_rating', 'confirm_factors', 'confirm_factor_choice'})
        if action_id not in allowed:
            raise RuntimeError('skill_takeover_action_forbidden')
        return AutonomousDevice.step(self, action_id, stop_event)

    def close_completed(self, action_id, stop_event=None):
        stop = stop_event or threading.Event()
        expected = {'close_completed_training': 'training_complete', 'close_character_story_unlock': 'character_story_unlock'}.get(action_id)
        observed = self.capture()
        if not expected or observed['page'] != expected or action_id not in observed['actions'] or (not self.context.get('completion_submitted')) or (not self.context.get('factor_choice_submitted')) or (action_id == 'close_completed_training' and self.context.get('completion_home_submitted')) or (action_id == 'close_character_story_unlock' and (not self.context.get('completion_home_submitted'))):
            raise RuntimeError('completion_home_not_bound')
        if stop.is_set() or time.monotonic() - observed['captured_monotonic'] > 3:
            raise RuntimeError('completion_home_stopped_or_stale')
        path = self.output_dir / (action_id + '.receipt.json')
        receipt = dict(action_id=action_id, endpoint=self.endpoint, source=observed, performed=None, status='dispatch_intent')
        with path.open('x', encoding='utf-8') as handle:
            json.dump(receipt, handle, ensure_ascii=False)
        self.context['completion_home_submitted'] = True
        x, y = observed['actions'][action_id]['point']
        self.command('-s', self.endpoint, 'shell', 'input', 'tap', str(x), str(y))
        receipt.update(performed=True, status='dispatched')
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        for _ in range(12):
            if stop.wait(0.5):
                raise RuntimeError('operator_stop')
            after = self.capture()
            if after['page'] in {'home', 'character_story_unlock'} and after['page'] != expected:
                break
        receipt['observation'] = after
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        if after['page'] not in {'home', 'character_story_unlock'}:
            raise RuntimeError('completion_home_destination_unverified')
        return receipt

    def inspect_finish_identity(self, stop_event=None):
        stop = stop_event or threading.Event()
        before, current = (self.capture(), self.capture())
        authorize_identity_return(before, current, already_submitted=self.context.get('identity_return_submitted', False))
        if stop.is_set() or time.monotonic() - current['captured_monotonic'] > 3:
            raise RuntimeError('identity_return_stopped_or_stale')
        path = self.output_dir / 'identity_return.receipt.json'
        receipt = dict(action_id='inspect_finish_identity', endpoint=self.endpoint, sources=[before, current], status='dispatch_intent', performed=None)
        with path.open('x', encoding='utf-8') as handle:
            json.dump(receipt, handle, ensure_ascii=False)
        self.context['identity_return_submitted'] = True
        x, y = current['actions']['return_harvest_skills']['point']
        self.command('-s', self.endpoint, 'shell', 'input', 'tap', str(x), str(y))
        receipt.update(performed=True, status='dispatched')
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        for _ in range(10):
            if stop.wait(0.5):
                raise RuntimeError('operator_stop')
            after = self.capture()
            if after['page'] == 'finish_ready':
                break
        receipt['observation'] = after
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        if after['page'] != 'finish_ready' or after.get('current_skill_points') != current['current_skill_points']:
            raise RuntimeError('identity_return_destination_not_verified')
        return receipt
