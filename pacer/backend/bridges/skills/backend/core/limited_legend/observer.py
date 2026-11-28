"""Recognize event entry and missions; reuse the shared race/selection gates."""
import re
import cv2
from core.legend_race.observer import observe as observe_race
from core.daily_legend.live import daily_observation
from core.legend_selection.observer import _green

def observe(raw, image, tokens, event=None):
    result = observe_race(raw, tokens, navigation=True, event=event)
    if image.shape[:2] != (1280, 720) or result['page'] in ('blocked', 'loading', 'connection_interrupted', 'connection_conflict'):
        return result
    if result['page'] == 'rewards' and 'continue_rewards' in result['actions']:
        result['actions']['continue_sale_rewards'] = result['actions']['continue_rewards']
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.95]

    def rows(box):
        x, y, r, b = box
        return [t for t in good if x <= t['bbox'][0] < t['bbox'][2] <= r and y <= t['bbox'][1] < t['bbox'][3] <= b]

    def find(text, box):
        found = [t for t in rows(box) if t['text'].rstrip('!！') == text.rstrip('!！')]
        return found[0] if len(found) == 1 else None

    def add(name, token):
        if token:
            x, y, r, b = token['bbox']
            result['actions'][name] = dict(point=[(x + r) // 2, (y + b) // 2])

    def green_text(token):
        if not token:
            return False
        x, y, r, b = token['bbox']
        crop = image[max(0, y - 5):b + 5, max(0, x - 16):r + 16]
        return (_green(crop) & (cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[:, :, 2] > 180)).mean() > 0.35
    if find('获得奖杯！', (180, 250, 580, 380)):
        label = [t for t in tokens if t['confidence'] >= 0.9 and t['text'].replace(' ', '') == '传奇赛事' and (250 <= t['bbox'][0] < t['bbox'][2] <= 390) and (590 <= t['bbox'][1] < t['bbox'][3] <= 680)]
        opponents = [t['text'][2:] for t in rows((370, 590, 540, 680)) if t['text'].startswith('VS') and len(t['text']) > 2]
        close = find('关闭', (270, 870, 450, 970))
        if len(label) == 1 and close:
            result.update(page='trophy', victory=True, actions={})
            if len(opponents) == 1:
                result['event'] = {'opponent': opponents[0]}
            add('close_trophy', close)
            return result
    from core.event_menu import menu_entries
    menu = menu_entries(image, tokens)
    if menu is not None:
        result.update(menu)
        result['actions'].pop('open_champions', None)
        return result
    if find('限定任务', (240, 20, 490, 100)) and find('日常', (100, 90, 300, 160)) and find('期间限定', (400, 90, 660, 160)):
        result.update(page='limited_missions', actions={})
        daily = find('日常', (100, 90, 300, 160))
        period = find('期间限定', (400, 90, 660, 160))
        tabs = [name for name, t in [('daily', daily), ('period', period)] if green_text(t)]
        result['mission_tab'] = tabs[0] if len(tabs) == 1 else None
        claim = find('一键领取', (380, 1120, 670, 1240))
        result['claim_enabled'] = bool(claim and green_text(claim))
        result['mission_state_verified'] = bool(claim and result['mission_tab'])
        add('missions_daily', daily)
        add('missions_period', period)
        add('close_missions', find('关闭', (90, 1120, 310, 1240)))
        if result['claim_enabled'] and result['mission_tab']:
            add('claim_' + result['mission_tab'], claim)
        return result
    if find('领取成功', (240, 20, 490, 100)) and find('已获得以上奖励', (210, 1080, 530, 1140)):
        close = find('关闭', (240, 1140, 490, 1230))
        result.update(page='limited_mission_rewards', actions={})
        add('close_mission_rewards', close)
        return result
    if result['page'] == 'legend':
        if not find('传奇赛事', (0, 0, 260, 70)):
            return dict(result, page='unknown', actions={})
        courses = [re.fullmatch('(.+?)\\d{3,4}米', t['text']) for t in rows((50, 330, 500, 405))]
        courses = [m.group(1) for m in courses if m]
        if len(courses) == 1:
            result['event'] = dict(result.get('event', {}), course=courses[0])
        add('open_missions', find('限定任务', (540, 440, 710, 620)))
        if 'return_home' not in result['actions']:
            from core.page_identity import PageIdentity
            add('return_home', PageIdentity(image).find('主页', (290, 1200, 430, 1280)))
        return result
    if result['page'] == 'unknown':
        playback = daily_observation(raw, image, tokens, 1, event, listing={})
        if playback['page'] in {'rank_overlay', 'orientation', 'animation', 'limited_sale', 'daily_legend_help'}:
            result = playback
    return result
