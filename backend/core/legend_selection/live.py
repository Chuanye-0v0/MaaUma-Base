"""Fresh-frame allowlist. Selection confirmation is intentionally unsupported."""
import json
import time
import cv2
import numpy as np
from core.legend_selection.observer import observe, read_tokens, same_list, grid_ready
from core.support_library.live import SupportDevice
from core.action_recovery import action_boundary, require_fresh_observation
ACTION_PAGES = dict(open_races='home', open_events='races', open_legend='events', open_details='legend', open_selection='details', open_sort='selection', sort_evaluation='sort', apply_sort='sort', descending='selection', scroll_top='selection', scroll_next='selection')

def authorize_action(action, current, expected):
    candidate = current['actions'].get(action)
    horse = action.startswith('horse_') and action[6:].isdigit()
    page = 'selection' if horse else ACTION_PAGES.get(action)
    if not candidate or page is None or current['page'] != page or (expected['page'] != page):
        raise RuntimeError('action_not_allowed:' + action)
    if action == 'apply_sort' and current.get('evaluation_selected') is not True:
        raise RuntimeError('sort_not_selected')
    if horse or action in ('scroll_top', 'scroll_next'):
        if current.get('sort_field') != '评价' or current.get('direction') != '降序':
            raise RuntimeError('selection_not_sorted')
        if current.get('filter_off') is not True:
            raise RuntimeError('filter_not_off')
        if not grid_ready(current):
            raise RuntimeError('candidate_list_uncertain')
        if not same_list(expected, current):
            raise RuntimeError('candidate_list_changed')
        if horse and int(action[6:]) >= len(current['candidates']):
            raise RuntimeError('candidate_not_present')
    return candidate

class LegendSelectionDevice(SupportDevice):

    def capture(self):
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('screenshot_invalid')
        observed = observe(raw, read_tokens(image, self.ocr))
        stem = str(time.time_ns())
        path = self.output_dir / (stem + '.png')
        path.write_bytes(raw)
        observed.update(screenshot_path=str(path), captured_monotonic=captured, endpoint=self.endpoint)
        (self.output_dir / (stem + '.json')).write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf-8')
        return observed

    @action_boundary
    def step(self, action_id, stop_event, expected):
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        before = self.capture()
        candidate = authorize_action(action_id, before, expected)
        require_fresh_observation(before, action_id)
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        if 'swipe' in candidate:
            self.command('-s', self.endpoint, 'shell', 'input', 'swipe', *map(str, candidate['swipe']))
        else:
            self.command('-s', self.endpoint, 'shell', 'input', 'tap', *map(str, candidate['point']))
        if stop_event.wait(0.6):
            raise RuntimeError('operator_stop')
        result = dict(action_id=action_id, source_frame_sha256=before['frame_sha256'], observation=self.capture())
        (self.output_dir / (str(time.time_ns()) + '-receipt.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
