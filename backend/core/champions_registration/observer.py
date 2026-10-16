"""Region-bound registration recognition, independent of course and event dates."""
import re
import unicodedata
import cv2
from core.legend_selection.observer import observe as observe_navigation
from core.jjc.formation import _control_state
from core.page_identity import PageIdentity
from core.passive_popup import RISK
DIVISIONS = {'intermediate': ('中级', '公开联赛'), 'advanced': ('高级', '分级联赛')}
REGISTERED_RIBBON = (160, 300, 560, 440)

def observe(raw, image, tokens):
    result = observe_navigation(raw, tokens)
    if image.shape[:2] != (1280, 720):
        return dict(result, page='unknown', actions={})
    good = [dict(t, text=re.sub('\\s+', '', unicodedata.normalize('NFKC', t['text']))) for t in tokens if t['confidence'] >= 0.95]
    if any((any((w in t['text'] for w in ('连接冲突', '其他设备登录'))) for t in good)):
        return dict(result, page='connection_conflict', actions={})
    if result['page'] in {'blocked', 'loading', 'connection_interrupted'}:
        return result
    if any((t['text'] == '连接中' for t in good)):
        return dict(result, page='loading', actions={})

    def rows(box):
        x, y, r, b = box
        return [t for t in good if x <= t['bbox'][0] < t['bbox'][2] <= r and y <= t['bbox'][1] < t['bbox'][3] <= b]

    def find(text, box):
        found = [t for t in rows(box) if t['text'] == text]
        return found[0] if len(found) == 1 else None

    def add(action, token):
        if token:
            x, y, r, b = token['bbox']
            result['actions'][action] = dict(point=[(x + r) // 2, (y + b) // 2])

    def close_button():
        box = (230, 1140, 490, 1230)
        exact = find('关闭', box)
        if exact:
            return exact
        parts = sorted(rows(box), key=lambda t: t['bbox'][0])
        if len(parts) != 2 or [t['text'] for t in parts] != ['关', '闭']:
            return None
        a, b = (t['bbox'] for t in parts)
        if abs(a[1] + a[3] - (b[1] + b[3])) > 20 or not -20 <= b[0] - a[2] <= 45:
            return None
        return dict(text='关闭', bbox=[a[0], min(a[1], b[1]), b[2], max(a[3], b[3])])

    def period(box):
        lines = []
        for token in sorted(rows(box), key=lambda t: (t['bbox'][1], t['bbox'][0])):
            center = (token['bbox'][1] + token['bbox'][3]) / 2
            line = next((line for line in lines if abs(line[0] - center) <= 12), None)
            if line is None:
                line = [center, []]
                lines.append(line)
            line[1].append(token)
        matches = set()
        pattern = '(?<!\\d)(\\d{2}/\\d{2}\\d{2}:\\d{2})[~\\-—–至](\\d{2}/\\d{2}\\d{2}:\\d{2})(?!\\d)'
        for _, tokens_in_line in lines:
            text = ''.join((t['text'] for t in sorted(tokens_in_line, key=lambda t: t['bbox'][0])))
            matches.update(('~'.join(m) for m in re.findall(pattern, text)))
        return next(iter(matches)) if len(matches) == 1 else None
    if find('确认联赛选择', (220, 280, 520, 370)):
        result.update(page='champions_confirm', actions={}, division=None)
        groups = [key for key, (label, name) in DIVISIONS.items() if find('参加' + name, (200, 470, 430, 540)) and find(label, (410, 470, 510, 540))]
        confirm = find('确定', (410, 865, 620, 965))
        cancel = find('取消', (90, 865, 310, 965))
        foreground = rows((20, 260, 700, 980))
        risk = RISK.search(''.join((t['text'] for t in foreground if t['text'] != '是否确认执行?')))
        controls = [t for t in foreground if t['text'] in {'确定', '取消'}]
        if len(groups) == 1 and cancel and (len(controls) == 2) and (not risk):
            result['division'] = groups[0]
            if confirm:
                x, y, r, b = confirm['bbox']
                cx, cy = ((x + r) // 2, (y + b) // 2)
                if _control_state(image, (cx - 120, cy - 40, cx + 120, cy + 40)) is True:
                    add('confirm_registration', confirm)
        return result
    if find('领取奖励', (230, 20, 500, 100)):
        result.update(page='champions_rewards', actions={})
        add('close_registration_rewards', close_button())
        return result
    if find('选择要参加的联赛', (180, 20, 540, 100)):
        result.update(page='champions_choose', actions={}, event_period=period((90, 440, 650, 495)))
        add('close_league_choice', close_button())
        if result['event_period'] and find('联赛报名期间', (170, 475, 550, 560)) and find('报名联赛', (20, 720, 230, 810)):
            for key, (label, name) in DIVISIONS.items():
                box = (30, 800, 345, 1110) if key == 'intermediate' else (370, 800, 695, 1110)
                if find(label, box):
                    add('choose_' + key, find(name, box))
        return result
    if find('群英联赛', (0, 0, 250, 70)):
        result.update(page='champions_registered', actions={}, division=None, event_period=period((100, 475, 660, 530)))
        groups = [key for key, (_, name) in DIVISIONS.items() if find(name, REGISTERED_RIBBON)]
        active_round = any((re.fullmatch('第[1-9]轮进行中', t['text']) for t in rows((150, 520, 580, 640))))
        final_phase = find('决赛轮参赛登记期间', (90, 510, 650, 650)) or find('决赛轮', (90, 510, 650, 650))
        modern_hub = active_round or final_phase is not None
        if len(groups) == 1 and (find('更改联赛', (550, 510, 710, 600)) or modern_hub):
            result['division'] = groups[0]
            add('registration_home', find('主页', (290, 1200, 430, 1280)) or PageIdentity(image).find('主页', (290, 1200, 430, 1280)) or PageIdentity(image).find('主页', (290, 1160, 430, 1250), group='navigation'))
            if 'registration_home' not in result['actions']:
                footer = [t for t in tokens if t.get('confidence', 0) >= 0.9 and re.sub('\\s+', '', t['text']) == '主页' and (290 <= t['bbox'][0] < t['bbox'][2] <= 430) and (1200 <= t['bbox'][1] < t['bbox'][3] <= 1280)]
                if len(footer) == 1:
                    add('registration_home', footer[0])
        return result
    from core.event_menu import menu_entries
    menu = menu_entries(image, tokens)
    if menu is not None:
        result.update(menu)
        result['actions'].pop('open_legend', None)
        return result
    if result['page'] not in {'home', 'races'}:
        result.update(page='unknown', actions={})
    return result
