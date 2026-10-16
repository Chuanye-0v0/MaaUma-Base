"""Recognize event entry and missions; reuse the shared race/selection gates."""
import re
import cv2
import numpy as np
from core.legend_race.observer import observe as observe_race
from core.daily_legend.live import daily_observation

def _claim_control_state(image, token):
    if type(token.get('enabled')) is bool:
        return token['enabled']
    x, y, r, b = token['bbox']
    hsv = cv2.cvtColor(image[y:b, x:r], cv2.COLOR_BGR2HSV)
    white_ink = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 235)
    if white_ink.mean() > 0.1:
        return True
    if np.percentile(hsv[:, :, 2], 95) < 210:
        return False
    return None

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
        if any((t['confidence'] >= 0.75 and any((word in t['text'].replace(' ', '') for word in ('宝石', '付费', '购买', '充值', '支付', '消耗'))) for t in tokens)):
            return dict(result, page='unknown', actions={})
        result.update(page='limited_missions', actions={})
        daily = find('日常', (100, 90, 300, 160))
        period = find('期间限定', (400, 90, 660, 160))
        body = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.9 and 150 <= t['bbox'][1] < t['bbox'][3] <= 1040]
        titles = [(tab, t) for t in body for tab, label in [('daily', '日常任务'), ('period', '期间限定任务')] if 120 <= t['bbox'][0] < 550 and re.match('[【\\[]?' + label, t['text'])]
        tabs = sorted({tab for tab, t in titles})
        result['mission_tab'] = tabs[0] if len(tabs) == 1 else None
        selected_tabs = [name for name, t in [('daily', daily), ('period', period)] if t.get('selected') is True]
        if selected_tabs and selected_tabs != tabs:
            result['mission_tab'] = None
        claim = find('一键领取', (380, 1120, 670, 1240))
        result['claim_present'] = True if claim else None
        result['claim_enabled'] = _claim_control_state(image, claim) if claim else None
        if claim is None:
            footer = image[1145:1228, 378:661]
            surround = image[1234:1244, 378:661]
            if float(footer.std()) < 5 and abs(float(footer.mean()) - float(surround.mean())) < 10:
                result['claim_present'] = False
        statuses = []
        for tab, title in titles:
            labels = [t['text'] for t in body if t['text'] in {'领取', '挑战', '已领取'} and 550 <= t['bbox'][0] and (title['bbox'][1] <= t['bbox'][1] < title['bbox'][1] + 110)]
            if len(labels) != 1:
                statuses = []
                break
            statuses.append(labels[0])
        result['claimable_rows'] = statuses.count('领取') if statuses and len(tabs) == 1 else None
        result['mission_row_states'] = statuses
        result['reward_state'] = 'unclaimed' if result['claimable_rows'] else 'already_claimed' if statuses and all((value == '已领取' for value in statuses)) else 'unmet' if statuses and all((value == '挑战' for value in statuses)) else 'none_claimable' if result['claimable_rows'] == 0 else None
        state, count = (result['claim_enabled'], result['claimable_rows'])
        result['mission_state_verified'] = bool(claim and result['mission_tab'] and (type(state) is bool) and (count is not None) and (state is True and count > 0 or (state is False and count == 0)))
        result['claim_state'] = 'missing_button' if result['claim_present'] is False else 'unreadable' if state is None else 'available' if state is True else 'no_claimable_rewards' if count == 0 else 'disabled_unclaimed'
        add('missions_daily', daily)
        add('missions_period', period)
        add('close_missions', find('关闭', (90, 1120, 310, 1240)))
        if result['claim_enabled'] is True and result['mission_state_verified']:
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
