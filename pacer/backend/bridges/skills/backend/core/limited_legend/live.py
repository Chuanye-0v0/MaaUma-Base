"""Limited-event device, retaining the existing fresh-frame race executor."""
import cv2
from core.daily_legend.live import DailyLegendDevice, crop_tokens, fast_display
from core.legend_race.live import LegendRaceDevice
from core.legend_selection.observer import read_tokens
from core.limited_legend.observer import observe
MISSION_ACTIONS = {'open_missions': 'legend', 'missions_daily': 'limited_missions', 'missions_period': 'limited_missions', 'claim_daily': 'limited_missions', 'claim_period': 'limited_missions', 'close_missions': 'limited_missions', 'close_mission_rewards': 'limited_mission_rewards'}

def read_observation(raw, image, ocr, event=None):
    tokens = read_tokens(image, ocr)
    result = observe(raw, image, tokens, event)
    if image.shape[:2] != (1280, 720):
        return result
    texts = {t['text'].replace(' ', '') for t in tokens if t['confidence'] >= 0.9}

    def line(box):
        x, y, r, b = box
        rows, _ = ocr(cv2.resize(image[y:b, x:r], None, fx=3, fy=3), use_det=False, use_cls=False)
        return [dict(text=text.replace(' ', ''), confidence=float(score), bbox=list(box)) for text, score in rows or []]
    boxes = []
    if result['page'] == 'events' and 'open_legend' not in result.get('actions', {}):
        card = result.get('event_cards', {}).get('legend')
        if card:
            x, y, r, b = card['bbox']
            center = (x + r) // 2
            boxes = [(max(0, center - 160), max(0, y - 210), min(720, center + 160), min(1280, b + 30))]
    elif result['page'] == 'legend':
        boxes = [(570, 490, 705, 610), (290, 1200, 430, 1280)]
        if result.get('tickets') is None:
            tokens += line((625, 8, 690, 42))
        icon = line((635, 557, 675, 577)) + line((636, 576, 674, 595))
        if len(icon) == 2 and [t['text'] for t in icon] == ['限定', '任务'] and (min((t['confidence'] for t in icon)) >= 0.95):
            tokens.append(dict(text='限定任务', confidence=min((t['confidence'] for t in icon)), bbox=[635, 557, 675, 595]))
    elif result['page'] != 'race_ready' and (result['page'] == 'lineup' or any(('困难' in t for t in texts))):
        boxes = [(260, 1130, 460, 1220)]
        for token in line((315, 1155, 405, 1200)):
            if token['text'] == '继续' and token['confidence'] >= 0.95:
                tokens = [t for t in tokens if not (t['text'].replace(' ', '') == '继续' and 1130 < t['bbox'][1] < 1210)]
                tokens.append(token)
    if '获得奖杯！' in texts:
        boxes = [(255, 608, 540, 665), (270, 880, 450, 960)]
    elif '恭喜获胜！' in texts or '胜利' in texts or '失败' in texts or ('平局' in texts):
        boxes = [(420, 1130, 600, 1230)]
    elif '选择道具' in texts:
        boxes = [(450, 900, 570, 970), (430, 870, 610, 925)]
    for box in boxes:
        refined = crop_tokens(image, ocr, box)
        for token in refined:
            same = [t for t in tokens if t['text'].replace(' ', '') == token['text'].replace(' ', '') and abs(t['bbox'][1] - token['bbox'][1]) < 25]
            if same and max((t['confidence'] for t in same)) >= token['confidence']:
                continue
            tokens = [t for t in tokens if t not in same]
            tokens.append(token)
        if box == (570, 490, 705, 610):
            words = [t for t in refined if t['confidence'] >= 0.95]
            if {t['text'] for t in words} == {'限定', '任务'} and (not any((t['text'] == '限定任务' for t in tokens))):
                tokens.append(dict(text='限定任务', confidence=min((t['confidence'] for t in words)), bbox=[min((t['bbox'][0] for t in words)), min((t['bbox'][1] for t in words)), max((t['bbox'][2] for t in words)), max((t['bbox'][3] for t in words))]))
    return observe(raw, image, tokens, event)

class LimitedLegendDevice(DailyLegendDevice):
    navigation_actions = DailyLegendDevice.navigation_actions | set(MISSION_ACTIONS) - {'claim_daily', 'claim_period'}

    def __init__(self, endpoint, adb_path, output_dir):
        LegendRaceDevice.__init__(self, endpoint, adb_path, output_dir)
        self.target_verified = False
        self.target = {}
        self.context = {}

    def read_observation(self, raw, image, navigation):
        if image.shape[:2] != (1280, 720):
            raise RuntimeError('portrait_720_required')
        fast = fast_display(raw, image, self.ocr, self.event, bool(self.horse and self.target_verified))
        if fast and fast['frame_sha256'] != getattr(self, '_last_display_dispatch', None):
            return fast
        if self.horse and self.target_verified:
            from core.legend_race.items import fast_items
            fast = fast_items(raw, image, self.ocr)
            if fast:
                return fast
        return read_observation(raw, image, self.ocr, self.event)

    def authorize(self, action, current, expected):
        if action in MISSION_ACTIONS:
            if any((o.get('endpoint') != self.endpoint or o.get('page') != MISSION_ACTIONS[action] for o in (current, expected))) or action not in current.get('actions', {}):
                raise RuntimeError('limited_mission_not_bound')
            if action.startswith('claim_'):
                tab = action.removeprefix('claim_')
                if any((o.get('mission_tab') != tab or o.get('claim_enabled') is not True or o.get('mission_state_verified') is not True for o in (current, expected))):
                    raise RuntimeError('limited_claim_not_bound')
            return current['actions'][action]
        return super().authorize(action, current, expected)
