"""Reuse the shared fresh-frame executor, receipts and screenshot retention."""
import cv2
from core.legend_race.live import LegendRaceDevice
from core.legend_selection.observer import read_tokens
from core.ocr_regions import read_regions
from core.champions_registration.observer import observe, DIVISIONS, REGISTERED_RIBBON
ACTION_PAGES = {'open_races': 'home', 'open_events': 'races', 'open_champions': 'events', 'choose_intermediate': 'champions_choose', 'choose_advanced': 'champions_choose', 'close_league_choice': 'champions_choose', 'confirm_registration': 'champions_confirm', 'close_registration_rewards': 'champions_rewards', 'registration_home': 'champions_registered'}

def read_observation(raw, image, ocr):
    if image.shape[:2] != (1280, 720):
        return observe(raw, image, [])
    anchors = read_regions(image, ocr, [(0, 0, 720, 105), (210, 285, 530, 365)])
    labels = {t['text'].replace(' ', '') for t in anchors if t['confidence'] >= 0.95}
    if '确认联赛选择' in labels:
        boxes = [(30, 470, 690, 655), (60, 800, 670, 970)]
    elif '选择要参加的联赛' in labels:
        boxes = [(70, 400, 700, 800), (30, 810, 700, 1110), (230, 1140, 490, 1230)]
    elif '领取奖励' in labels:
        boxes = [(140, 1060, 590, 1140), (230, 1140, 490, 1230)]
    elif '群英联赛' in labels:
        boxes = [REGISTERED_RIBBON, (90, 440, 710, 645), (290, 1200, 430, 1280)]
        anchors = [t for t in anchors if t['bbox'][1] < 105]
    else:
        return observe(raw, image, read_tokens(image, ocr))
    tokens = anchors + read_regions(image, ocr, boxes)
    result = observe(raw, image, tokens)
    if result['page'] == 'champions_choose' and (not result.get('event_period')):
        box = (145, 450, 585, 485)
        x, y, r, b = box
        rows, _ = ocr(cv2.resize(image[y:b, x:r], None, fx=3, fy=3), use_det=False, use_cls=False)
        tokens += [dict(text=text, confidence=float(score), bbox=list(box)) for text, score in rows or []]
        result = observe(raw, image, tokens)
    return result

class ChampionsRegistrationDevice(LegendRaceDevice):
    navigation_actions = {'open_races', 'open_events', 'open_champions', 'registration_home', 'close_league_choice'}
    additional_predecessors = {'confirm_registration': ('choose_intermediate', 'choose_advanced'), 'close_registration_rewards': ('confirm_registration',)}

    def __init__(self, endpoint, adb_path, output_dir, division):
        if division not in DIVISIONS:
            raise ValueError('请先选择大赛报名组别：中级或高级')
        super().__init__(endpoint, adb_path, output_dir)
        self.division = division
        self.context = {}

    def read_observation(self, raw, image, navigation):
        return read_observation(raw, image, self.ocr)

    def authorize(self, action, current, expected):
        if action not in ACTION_PAGES or action not in current.get('actions', {}) or any((o.get('page') != ACTION_PAGES[action] or o.get('endpoint') != self.endpoint for o in (current, expected))):
            raise RuntimeError('champions_action_not_bound')
        if action.startswith('choose_'):
            if not self.event or any((o.get('event_period') != self.event.get('period') for o in (current, expected))):
                raise RuntimeError('champions_period_not_bound')
        if action.startswith('choose_') and action != 'choose_' + self.division:
            raise RuntimeError('champions_wrong_division')
        if action == 'confirm_registration':
            if not self.event or any((o.get('division') != self.division for o in (current, expected))):
                raise RuntimeError('champions_wrong_division')
        return current['actions'][action]
