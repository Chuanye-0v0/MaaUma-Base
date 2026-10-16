"""Local, structured observations and bounded support-maintenance actions."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
import time
import weakref
from pathlib import Path
import cv2
import numpy as np
from core.page_identity import PageIdentity
from desktop.adb_probe import AdbProbe
from core.support_library.card_inventory import read_card

def observe(raw: bytes, tokens: list[dict]) -> dict:
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    result = dict(frame_sha256=hashlib.sha256(raw).hexdigest(), page='unknown', actions={}, tokens=tokens)
    if image is None or image.shape[:2] != (1280, 720):
        result['reason'] = 'portrait_720_required'
        return result
    good = [t for t in tokens if t['confidence'] >= 0.9]
    inventory_card = read_card(image, tokens)
    names = [t['text'] for t in good if 20 < t['bbox'][0] < 80 and 550 < t['bbox'][1] < 620 and (t['bbox'][2] < 350)]
    result['card_name'] = ' '.join(names)
    portrait = cv2.resize(cv2.cvtColor(image[250:500, 65:295], cv2.COLOR_BGR2GRAY), (9, 8))
    result['card_key'] = ''.join(('1' if v else '0' for v in (portrait[:, 1:] > portrait[:, :-1]).flat))

    def find(text, region):
        x0, y0, x1, y1 = region
        matches = [t for t in good if t['text'].replace(' ', '') == text and x0 <= t['bbox'][0] and (y0 <= t['bbox'][1]) and (t['bbox'][2] <= x1) and (t['bbox'][3] <= y1)]
        return matches[0] if len(matches) == 1 else None

    def action(name, target):
        if target:
            x0, y0, x1, y1 = target['bbox']
            result['actions'][name] = dict(point=[int((x0 + x1) / 2), int((y0 + y1) / 2)], text=target['text'])

    def duplicate_matches():
        orb = cv2.ORB_create(nfeatures=1500, edgeThreshold=8, fastThreshold=10)
        _, source = orb.detectAndCompute(image[218:550, 42:316], None)
        _, material = orb.detectAndCompute(image[825:952, 299:421], None)
        if source is None or material is None or len(source) < 2:
            return False
        matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(material, source, k=2)
        return sum((pair[0].distance < 0.7 * pair[1].distance for pair in matches if len(pair) == 2)) >= 20

    def number(region):
        x0, y0, x1, y1 = region
        found = [t for t in good if re.fullmatch('\\d[\\d,]*', t['text']) and x0 <= t['bbox'][0] and (y0 <= t['bbox'][1]) and (t['bbox'][2] <= x1) and (t['bbox'][3] <= y1)]
        return int(found[0]['text'].replace(',', '')) if len(found) == 1 else None

    def max_selector(name):
        mask = cv2.inRange(cv2.cvtColor(image[700:785, 500:700], cv2.COLOR_BGR2HSV), np.array([25, 100, 180]), np.array([90, 255, 255]))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        circles = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) > 1500]
        if len(circles) == 2:
            x, y, w, h = max(circles)
            result['actions'][name] = dict(point=[500 + x + w // 2, 700 + y + h // 2], text='max_stage')
    if any(('连接已断开' in t['text'] for t in good)):
        result['page'] = 'disconnected'
        result['reason'] = 'device_disconnected'
        return result
    if find('等级强化', (250, 20, 470, 100)) and find('需要材料', (0, 930, 200, 990)):
        result['page'] = 'upgrade_confirm'
        action('cancel', find('取消', (130, 1140, 290, 1240)))
        old = [re.fullmatch('等级(\\d+)', t['text']) for t in good if 170 < t['bbox'][0] < 340 and 100 < t['bbox'][1] < 175]
        new = [re.fullmatch('等级(\\d+)', t['text']) for t in good if 340 < t['bbox'][0] < 590 and 100 < t['bbox'][1] < 175]
        old, new = ([m for m in old if m], [m for m in new if m])
        values = [number((200, 990, 370, 1040)), number((500, 990, 700, 1040)), number((200, 1040, 370, 1090)), number((500, 1040, 700, 1090))]
        if find('是否消耗协助积分和金币进行等级强化？', (100, 1090, 650, 1140)) and len(old) == len(new) == 1 and all((v is not None for v in values)):
            sp, sp_owned, coin, coin_owned = values
            if 0 < sp <= sp_owned and 0 < coin <= coin_owned and (0 < int(old[0][1]) < int(new[0][1]) <= 50):
                result['resources'] = dict(sp_cost=sp, sp_owned=sp_owned, coin_cost=coin, coin_owned=coin_owned)
                result['upgrade'] = dict(old_level=int(old[0][1]), new_level=int(new[0][1]))
                action('confirm_upgrade', find('等级强化', (430, 1140, 620, 1230)))
    elif find('使用重复的协助卡解锁上限吧。', (0, 970, 600, 1040)):
        result['page'] = 'breakthrough_help'
        action('close_help', find('关闭', (280, 1170, 450, 1260)))
    elif find('使用协助积分和金币进行等级强化吧。', (0, 970, 600, 1040)):
        result['page'] = 'upgrade_help'
        action('close_help', find('关闭', (280, 1170, 450, 1260)))
    elif find('上限解锁完成！', (40, 900, 690, 1100)):
        result['page'] = 'breakthrough_success'
        action('dismiss_success', find('上限解锁完成！', (40, 900, 690, 1100)))
    elif find('上限解锁', (250, 290, 470, 370)) and find('需要材料', (0, 550, 180, 620)):
        result['page'] = 'breakthrough_confirm'
        action('cancel', find('取消', (130, 870, 290, 960)))
        cost = [re.fullmatch('是否消耗([1-4])张协助卡解锁上限[？?]', t['text']) for t in good if 810 < t['bbox'][1] < 875]
        caps = [re.fullmatch('最大等级(\\d+)最大等级(\\d+)', t['text'].replace(' ', '')) for t in good if 500 < t['bbox'][1] < 555]
        cost, caps = ([m for m in cost if m], [m for m in caps if m])
        if len(cost) == len(caps) == 1:
            needed = int(cost[0][1])
            old, new = (int(caps[0][1]), int(caps[0][2]))
            if new - old == needed * 5 and new <= 50:
                result['breakthrough'] = dict(needed=needed, old_cap=old, new_cap=new)
                action('confirm_breakthrough', find('上限解锁', (430, 870, 610, 960)))
    elif find('显示设置', (250, 30, 470, 100)) and find('显示顺序', (40, 180, 220, 240)):
        result['page'] = 'support_sort'
        action('sort_breakthrough', find('可解锁次数', (380, 540, 550, 620)))
        action('sort_rarity', find('稀有度', (90, 240, 210, 310)))
        action('confirm_sort', find('确定', (440, 1140, 600, 1230)))
    elif find('上限解锁', (0, 140, 250, 210)) and find('上限解锁阶段', (350, 210, 550, 260)):
        if find('选择上限解锁阶段', (250, 620, 550, 710)):
            result['page'] = 'breakthrough_materials'
            result['breakthrough_maxed'] = bool(find('上限解锁已达上限', (200, 860, 550, 950)))
            result['insufficient_duplicates'] = bool(find('数量不足', (280, 980, 470, 1050)))
            action('back', find('返回', (0, 1030, 160, 1130)))
            counts = [t for t in good if re.fullmatch('\\d+/\\d+', t['text']) and 300 < t['bbox'][0] < 450 and (940 < t['bbox'][1] < 990)]
            if len(counts) == 1 and duplicate_matches():
                owned, needed = map(int, counts[0]['text'].split('/'))
                result['duplicate_material'] = dict(owned=owned, needed=needed)
                if 0 < needed <= owned and (not result['insufficient_duplicates']):
                    action('request_breakthrough', find('上限解锁', (250, 1030, 500, 1130)))
                    max_selector('max_breakthrough')
        else:
            result['page'] = 'breakthrough_list'
            action('select_card', find('选择', (280, 1030, 450, 1130)))
            action('open_upgrade', find('等级强化', (350, 140, 530, 210)))
            action('open_sort', find('稀有度', (390, 950, 570, 1030)))
            action('open_sort', find('可解锁次数', (390, 950, 570, 1030)))
    elif find('等级强化', (0, 140, 250, 210)) and find('协助效果', (350, 210, 550, 350)):
        result['page'] = 'upgrade_materials' if find('选择等级强化阶段', (250, 620, 550, 710)) else 'upgrade_list'
        if result['page'] == 'upgrade_materials':
            result['upgrade_maxed'] = bool(find('已强化至最大等级', (200, 780, 550, 860)))
            current = [re.fullmatch('等级(\\d+)', t['text'].replace(' ', '')) for t in good if 200 < t['bbox'][0] < 480 and 680 < t['bbox'][1] < 740]
            current = [m for m in current if m]
            if len(current) == 1:
                result['current_level'] = int(current[0][1])
            action('back', find('返回', (0, 1030, 160, 1130)))
            max_selector('max_upgrade')
            labels = [find('消耗SP', (40, 840, 190, 910)), find('SP', (380, 840, 500, 910)), find('消耗金币', (40, 910, 190, 980)), find('金币', (380, 910, 510, 980))]
            values = [number((230, 850, 360, 910)), number((510, 850, 700, 910)), number((200, 915, 360, 980)), number((510, 915, 700, 980))]
            if all(labels) and all((v is not None for v in values)):
                sp, sp_owned, coin, coin_owned = values
                result['resources'] = dict(sp_cost=sp, sp_owned=sp_owned, coin_cost=coin, coin_owned=coin_owned)
                if 0 < sp <= sp_owned and 0 < coin <= coin_owned:
                    action('request_upgrade', find('等级强化', (250, 1030, 500, 1130)))
            if result['upgrade_maxed']:
                result['actions'].pop('request_upgrade', None)
            result['inventory_card'] = inventory_card
            return result
        action('select_card', find('选择', (280, 1030, 450, 1130)))
        action('open_sort', find('稀有度', (390, 950, 570, 1030)))
        action('open_breakthrough', find('上限解锁', (550, 140, 710, 210)))
    elif find('协助卡', (300, 600, 500, 700)) and find('等级强化', (140, 700, 370, 820)) and find('上限解锁', (430, 700, 680, 820)):
        result['page'] = 'support_menu'
        action('open_breakthrough', find('上限解锁', (430, 700, 680, 820)))
        action('open_upgrade', find('等级强化', (140, 700, 370, 820)))
    elif find('强化编成', (0, 140, 250, 240)) and find('养成优俊少女', (0, 850, 350, 1000)):
        result['page'] = 'enhance_menu'
        action('open_support', find('协助卡', (350, 850, 700, 1000)))
    elif (ui := PageIdentity(image)).home(tokens):
        result['page'] = 'home'
        action('open_enhance', find('强化编成', (0, 1180, 150, 1280)) or ui.find('强化编成'))
    if result['page'] in ('upgrade_list', 'breakthrough_list'):
        rows = []
        for item in sorted(good, key=lambda t: t['bbox'][1]):
            y = item['bbox'][1]
            if re.fullmatch('等级\\d+', item['text'].replace(' ', '')) and 690 < y < 1000:
                if not rows or y - rows[-1] > 40:
                    rows.append(y)
        for row, y in enumerate(rows):
            for column in range(5):
                result['actions'][f'card_{row * 5 + column}'] = dict(point=[85 + 137 * column, y - 65], text='visible_card')
        result['sort_descending'] = any(('降序' in t['text'] and t['bbox'][1] > 950 for t in good))
        result['sort_mode'] = 'breakthrough' if find('可解锁次数', (390, 950, 570, 1030)) else 'rarity' if find('稀有度', (390, 950, 570, 1030)) else 'unknown'
        result['filter_off'] = any((t['text'].replace(' ', '').replace(':', '：') == '筛选：关' and t['bbox'][1] > 950 for t in good))
        result['list_signature'] = cv2.resize(cv2.cvtColor(image[665:780, 25:690], cv2.COLOR_BGR2GRAY), (40, 8)).tolist()
        if 'open_sort' in result['actions'] and any((k.startswith('card_') for k in result['actions'])):
            result['actions']['scroll_library'] = dict(point=[360, 920], end=[360, 735], gesture='swipe', text='next_row')
            result['actions']['scroll_library_top'] = dict(point=[360, 735], end=[360, 920], gesture='swipe', text='previous_row')
    if result['page'] in ('upgrade_list', 'breakthrough_list', 'breakthrough_materials'):
        result['inventory_card'] = inventory_card
    return result

class SupportDevice:

    def __init__(self, endpoint: str, adb_path: str, output_dir: Path):
        AdbProbe._validate_endpoint(endpoint)
        self.endpoint = endpoint
        self.adb = AdbProbe(adb_path, endpoint)._resolve_adb_path()
        if not self.adb:
            raise RuntimeError('adb_not_found')
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        from core.ocr_runtime import create_ocr
        from core.incremental_ocr import IncrementalOCR
        self.ocr = IncrementalOCR(create_ocr())
        from core.screenshot_retention import install_capture_retention
        install_capture_retention(self)
        from core.stall_watch import watch_device, install_screen_identity
        install_screen_identity(self)
        watch_device(self)
        from core.passive_popup_runtime import install_passive_popup_recovery
        install_passive_popup_recovery(self)

    def command(self, *args):
        return subprocess.run([self.adb, *args], capture_output=True, timeout=15, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout

    def close(self):
        finalizer = getattr(self, '_native_frame_finalizer', None)
        if finalizer is not None:
            finalizer()

    def connect(self):
        self.command('connect', self.endpoint)
        if self.command('-s', self.endpoint, 'get-state').strip() != b'device':
            raise RuntimeError('device_not_ready')

    def capture(self):
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured_monotonic = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('screenshot_invalid')
        rows, _ = self.ocr(image)
        tokens = []
        for box, text, confidence in rows or []:
            tokens.append(dict(text=text, confidence=float(confidence), bbox=[int(min((p[0] for p in box))), int(min((p[1] for p in box))), int(max((p[0] for p in box))), int(max((p[1] for p in box)))]))
        if any((t['text'] == '选择上限解锁阶段' for t in tokens)):
            tabbed = any((t['text'] == '道具' and 780 < t['bbox'][1] < 850 for t in tokens))
            rows, _ = self.ocr(cv2.resize(image[940:994, 300:440], None, fx=3, fy=3))
            direct, _ = self.ocr(cv2.resize(image[953:981, 356:418], None, fx=4, fy=4), use_det=False, use_cls=False)
            if direct and re.fullmatch('\\d+/[1-4]', direct[0][0]) and (direct[0][1] >= 0.95):
                rows = [[[[168, 39], [354, 39], [354, 123], [168, 123]], direct[0][0], direct[0][1]]]
            for box, text, confidence in rows or []:
                if re.fullmatch('\\d+/\\d+', text):
                    tokens = [t for t in tokens if not (940 < t['bbox'][1] < 990 and re.fullmatch('\\d+/\\d+', t['text']))]
                    tokens.append(dict(text=text, confidence=float(confidence), bbox=[300 + int(min((p[0] for p in box)) / 3), 940 + int(min((p[1] for p in box)) / 3), 300 + int(max((p[0] for p in box)) / 3), 940 + int(max((p[1] for p in box)) / 3)]))
            if not any((re.fullmatch('\\d+/\\d+', t['text']) and t['confidence'] >= 0.9 and (940 < t['bbox'][1] < 990) for t in tokens)):
                count_boxes = [t['bbox'] for t in tokens if re.fullmatch('[\\d/]+', t['text']) and 300 < t['bbox'][0] < 383 and (940 < t['bbox'][1] < 990)]
                left_edge = max(300, count_boxes[0][0] - 2) if len(count_boxes) == 1 else 330
                left, _ = self.ocr(cv2.resize(image[953:981, left_edge:383], None, fx=4, fy=4), use_det=False, use_cls=False)
                right, _ = self.ocr(cv2.resize(image[953:981, 395:418], None, fx=4, fy=4), use_det=False, use_cls=False)
                if left and right and left[0][0].isdigit() and (right[0][0] in ('1', '2', '3', '4')) and (min(left[0][1], right[0][1]) >= 0.9):
                    tokens.append(dict(text=left[0][0] + '/' + right[0][0], confidence=min(left[0][1], right[0][1]), bbox=[330, 953, 418, 981], source='material_digits'))
            if tabbed:
                tokens = [t for t in tokens if not (940 < t['bbox'][1] < 990 and re.fullmatch('\\d+/\\d+', t['text']))]
                left, _ = self.ocr(cv2.resize(image[958:985, 330:376], None, fx=4, fy=4), use_det=False, use_cls=False)
                right, _ = self.ocr(cv2.resize(image[958:985, 387:405], None, fx=4, fy=4), use_det=False, use_cls=False)
                full, _ = self.ocr(cv2.resize(image[958:985, 354:406], None, fx=4, fy=4), use_det=False, use_cls=False)
                if full and re.fullmatch('\\d+/[1-4]', full[0][0]) and (full[0][1] >= 0.9):
                    tokens.append(dict(text=full[0][0], confidence=full[0][1], bbox=[354, 958, 406, 985]))
                elif left and right and left[0][0].isdigit() and (right[0][0] in ('1', '2', '3', '4')) and (min(left[0][1], right[0][1]) >= 0.9):
                    tokens.append(dict(text=left[0][0] + '/' + right[0][0], confidence=min(left[0][1], right[0][1]), bbox=[330, 958, 405, 985]))
        observed = observe(raw, tokens)
        stem = str(time.time_ns())
        path = self.output_dir / (stem + '.png')
        path.write_bytes(raw)
        observed.update(screenshot_path=str(path), captured_monotonic=captured_monotonic, endpoint=self.endpoint)
        (self.output_dir / (stem + '.json')).write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf-8')
        return observed

    def step(self, action_id: str, stop_event=None):
        observed = self.capture()
        candidate = observed['actions'].get(action_id)
        expected_page = {'request_breakthrough': 'breakthrough_materials', 'request_upgrade': 'upgrade_materials'}.get(action_id)
        for _ in range(2):
            if candidate is not None or not expected_page or observed.get('page') != expected_page:
                break
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(0.3)
            observed = self.capture()
            candidate = observed['actions'].get(action_id)
        if candidate is None:
            return dict(ok=False, reason='action_not_allowed', observation=observed, tap_count=0)
        if action_id == 'confirm_breakthrough':
            pending = getattr(self, 'pending_breakthrough', None)
            confirmation = observed.get('breakthrough', {})
            if not pending or confirmation.get('needed') != pending['needed'] or time.monotonic() - pending['time'] > 30:
                return dict(ok=False, reason='confirmation_not_bound', observation=observed, tap_count=0)
            self.pending_breakthrough = None
        if action_id == 'confirm_upgrade':
            pending = getattr(self, 'pending_upgrade', None)
            if not pending or observed.get('resources') != pending['resources'] or time.monotonic() - pending['time'] > 30:
                return dict(ok=False, reason='confirmation_not_bound', observation=observed, tap_count=0)
            self.pending_upgrade = None
        if stop_event is not None and stop_event.is_set():
            return dict(ok=False, reason='operator_stop', tap_count=0)
        if time.monotonic() - observed['captured_monotonic'] > 3:
            return dict(ok=False, reason='stale_frame', tap_count=0)
        x, y = candidate['point']
        if action_id == 'request_breakthrough':
            self.pending_breakthrough = {**observed['duplicate_material'], 'time': time.monotonic(), 'frame_sha256': observed['frame_sha256']}
        if action_id == 'request_upgrade':
            self.pending_upgrade = dict(resources=observed['resources'], time=time.monotonic(), frame_sha256=observed['frame_sha256'])
        if candidate.get('gesture') == 'swipe':
            end_x, end_y = candidate['end']
            self.command('-s', self.endpoint, 'shell', 'input', 'swipe', str(x), str(y), str(end_x), str(end_y), '700')
        else:
            self.command('-s', self.endpoint, 'shell', 'input', 'tap', str(x), str(y))
        time.sleep(1)
        after = self.capture()
        result = dict(ok=after['page'] != 'disconnected', reason='device_disconnected' if after['page'] == 'disconnected' else 'step_observed', action_id=action_id, source_frame_sha256=observed['frame_sha256'], tap_count=1, observation=after)
        (self.output_dir / (str(time.time_ns()) + '-receipt.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
