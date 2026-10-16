"""TP-only execution of U2's foreground parser; never starts/abandons training."""
import hashlib
import json
from pathlib import Path
import re
import time
import cv2
import numpy as np
from core.tp_refill_bridge import ocr_tokens
from core.tp_refill_perception import analyze_tp_refill

def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(path)

def hud_plus(image):
    """Locate the live TP gauge's adjacent green plus, not a fixed tap point."""
    gauges = [t for t in ocr_tokens(image) if re.fullmatch('\\d{1,3}/\\d{2,3}', t['text']) and 140 < t['bbox'][0] < 380 and (t['bbox'][1] < 85)]
    if len(gauges) != 1:
        return None
    gauge = gauges[0]
    before, maximum = map(int, gauge['text'].split('/'))
    if not 0 <= before <= maximum:
        return None
    cy = round((gauge['bbox'][1] + gauge['bbox'][3]) / 2)
    x0 = round(gauge['bbox'][2])
    x1 = min(430, x0 + 90)
    y0 = max(0, cy - 32)
    y1 = min(100, cy + 33)
    if x1 <= x0:
        return None
    crop = image[y0:y1, x0:x1]
    circles = cv2.HoughCircles(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.HOUGH_GRADIENT, 1, 20, param1=70, param2=15, minRadius=12, maxRadius=28)
    if circles is None:
        return None
    candidates = []
    for cx, yy, radius in circles[0]:
        cx, yy, radius = (round(float(cx)), round(float(yy)), round(float(radius)))
        patch = crop[max(0, yy - radius + 3):yy + radius - 2, max(0, cx - radius + 3):cx + radius - 2]
        if not patch.size:
            continue
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        green = ((hsv[:, :, 0] > 30) & (hsv[:, :, 0] < 90) & (hsv[:, :, 1] > 85) & (hsv[:, :, 2] > 100)).mean()
        white = ((hsv[:, :, 1] < 80) & (hsv[:, :, 2] > 220)).astype('uint8')
        _, _, stats, _ = cv2.connectedComponentsWithStats(white)
        cross = any((8 <= w <= 28 and 8 <= h <= 30 and (0.2 < area / (w * h) < 0.8) for _, _, w, h, area in stats[1:]))
        if green > 0.35 and cross:
            candidates.append((x0 + cx, y0 + yy))
    if len(candidates) != 1:
        return None
    return dict(state='open_tp_menu', center=candidates[0], tp=before)

class TpRefill:

    def __init__(self, device, output, *, clock=time.monotonic, parser=analyze_tp_refill):
        self.device, self.root = (device, Path(output))
        self.clock, self.parser = (clock, parser)

    def capture(self, state):
        raw = self.device.command('-s', self.device.endpoint, 'exec-out', 'screencap', '-p')
        captured = self.clock()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[1::-1] not in {(720, 1280), (1440, 2560)}:
            raise RuntimeError('tp_refill_frame_invalid')
        scale = image.shape[1] / 720
        image = cv2.resize(image, (720, 1280))
        result = self.parser(image, remaining={'tp_items': 8 - state['tp_items'], 'carrots': 9999 - state['carrots']}, selected_resource_kind=state.get('selected'), returning=state.get('returning', False), medicine_enabled=True)
        if result is None:
            result = hud_plus(image)
        if result is None:
            result = dict(state='unknown', reason='tp_refill_entry_unverified')
        source = self.root / (str(time.time_ns()) + '.png')
        self.root.mkdir(parents=True, exist_ok=True)
        source.write_bytes(raw)
        result.update(frame_sha256=hashlib.sha256(raw).hexdigest(), screenshot_path=str(source), captured_monotonic=captured, scale=scale, endpoint=self.device.endpoint)
        return result

    @staticmethod
    def signature(packet):
        return {k: packet.get(k) for k in ('state', 'center', 'resource_kind', 'resource_amount', 'preview_resource_before', 'preview_resource_after', 'selected_units', 'observed_item_stock', 'reported_resource_kind', 'reported_resource_before', 'reported_resource_after', 'reported_resource_delta', 'reported_training_points_after', 'tp')}

    def run(self, needed, emit, stop):
        if type(needed) is not int or not 0 < needed <= 200:
            raise RuntimeError('tp_refill_required_cost_unverified')
        journal = self.root / 'state.json'
        state = json.loads(journal.read_text(encoding='utf8')) if journal.exists() else dict(endpoint=self.device.endpoint, needed=needed, tp_items=0, carrots=0, records=[], returning=False)
        if state['endpoint'] != self.device.endpoint or state['needed'] != needed:
            raise RuntimeError('tp_refill_owner_changed')
        try:
            for _ in range(40):
                if stop.is_set():
                    raise RuntimeError('operator_stop')
                packet = self.capture(state)
                kind = packet['state']
                if kind == 'open_tp_menu' and packet['tp'] >= needed:
                    if state.get('pending'):
                        raise RuntimeError('tp_refill_result_unverified')
                    _save(journal, state)
                    return dict(ok=True, reason='tp_refill_verified', **state)
                if kind == 'unknown' and state.get('result_closed') and (state.get('verified_tp', 0) >= needed):
                    return dict(ok=True, reason='tp_refill_verified', **state)
                pending = state.get('pending')
                if pending and kind != 'close_tp_result':
                    if kind in {'unknown', 'confirm_tp_refill', 'select_one_tp_refill'} and time.time() - pending.get('at', 0) < 15:
                        if stop.wait(0.5):
                            raise RuntimeError('operator_stop')
                        continue
                    raise RuntimeError('tp_refill_result_unverified')
                if kind == 'close_tp_result':
                    if not pending or packet.get('reported_resource_kind') != pending['kind'] or packet.get('reported_resource_delta') != pending['amount'] or (packet.get('reported_resource_before') != pending['before']) or (type(packet.get('reported_training_points_after')) is not int):
                        raise RuntimeError('tp_refill_result_unverified')
                    state['verified_tp'] = packet['reported_training_points_after']
                allowed = {'open_tp_menu', 'choose_tp_item', 'choose_tp_carrots', 'select_one_tp_refill', 'confirm_tp_refill', 'close_tp_result', 'close_tp_menu'}
                if kind not in allowed or not packet.get('center'):
                    raise RuntimeError(packet.get('reason') or 'tp_refill_page_unverified')
                fresh = self.capture(state)
                if self.signature(fresh) != self.signature(packet):
                    continue
                if self.clock() - fresh['captured_monotonic'] > 20:
                    raise RuntimeError('tp_refill_frame_stale')
                if stop.is_set():
                    raise RuntimeError('operator_stop')
                if kind == 'confirm_tp_refill':
                    resource, amount = (fresh.get('resource_kind'), fresh.get('resource_amount'))
                    if state.get('pending') or resource not in {'tp_items', 'carrots'} or type(amount) is not int or (amount <= 0) or (fresh.get('selected_units') != 1):
                        raise RuntimeError('tp_refill_cost_unverified')
                    state[resource] += amount
                    state['pending'] = dict(kind=resource, amount=amount, before=fresh['preview_resource_before'], at=time.time())
                elif kind in {'choose_tp_item', 'choose_tp_carrots'}:
                    state['selected'] = 'tp_items' if kind == 'choose_tp_item' else 'carrots'
                intent = dict(action_id=kind, endpoint=self.device.endpoint, performed=None, source_frame_sha256=fresh['frame_sha256'])
                state['records'].append(intent)
                _save(journal, state)
                x, y = fresh['center']
                scale = fresh['scale']
                self.device.command('-s', self.device.endpoint, 'shell', 'input', 'tap', str(round(x * scale)), str(round(y * scale)))
                intent['performed'] = True
                if kind == 'close_tp_result':
                    state.pop('pending')
                    state.pop('selected', None)
                    state['result_closed'] = True
                    state['returning'] = state['verified_tp'] >= needed
                if kind == 'close_tp_menu':
                    state['menu_closed'] = True
                _save(journal, state)
                emit(dict(type='progress', message='补TP：' + {'choose_tp_item': '优先使用TP药', 'choose_tp_carrots': '已核实无TP药，使用萝卜', 'confirm_tp_refill': '确认一份恢复，费用已记录', 'close_tp_result': '恢复扣减已核实'}.get(kind, '处理已识别恢复窗口')))
                if stop.wait(0.5):
                    raise RuntimeError('operator_stop')
            raise RuntimeError('tp_refill_step_limit')
        except (RuntimeError, OSError, ValueError) as error:
            state['reason'] = str(error)
            _save(journal, state)
            return dict(ok=False, **state)
