"""Video-grounded role recognition. Team and opponent names are decorative."""
import hashlib
import re
import unicodedata
import cv2
import numpy as np
from core.champions_registration.observer import observe as registration_observe, DIVISIONS
from core.legend_selection.observer import _green

def _control_enabled(image, token):
    """Check the control surface, without treating its hue as identity."""
    x, y, r, b = token['bbox']
    area = image[max(0, y - 6):min(1280, b + 6), max(0, x - 30):min(720, r + 30)]
    hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV)
    colored = (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 100)
    white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 220)
    return bool(colored.mean() > 0.3 or white.mean() > 0.65)

def observe(raw, image, tokens, *, outcome_expected=False):
    result = dict(page='unknown', actions={}, tokens=tokens, frame_sha256=hashlib.sha256(raw).hexdigest())
    if image is None or image.shape[:2] != (1280, 720):
        return result
    good = [dict(t, text=re.sub('\\s+', '', unicodedata.normalize('NFKC', t['text']))) for t in tokens if t['confidence'] >= 0.95]
    common = registration_observe(raw, image, tokens)
    if common['page'] in {'blocked', 'loading', 'connection_interrupted', 'connection_conflict'}:
        return common
    if common['page'] in {'champions_choose', 'champions_confirm'}:
        return common
    if any((t['text'] == '连接中' or '加载中' in t['text'] for t in good)):
        return dict(result, page='loading')

    def rows(box):
        x, y, r, b = box
        return [t for t in good if x <= t['bbox'][0] < t['bbox'][2] <= r and y <= t['bbox'][1] < t['bbox'][3] <= b]

    def exact(text, box):
        found = [t for t in rows(box) if t['text'] == text]
        return found[0] if len(found) == 1 else None

    def has(text, box):
        return any((text in t['text'] for t in rows(box)))

    def add(action, token, enabled=False):
        if token:
            x, y, r, b = token['bbox']
            if not enabled or _control_enabled(image, token):
                result['actions'][action] = dict(point=[(x + r) // 2, (y + b) // 2])

    def control(action, box, orange=False):
        x, y, r, b = box
        area = image[y:b, x:r]
        hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV)
        mask = (hsv[:, :, 0] >= 12) & (hsv[:, :, 0] <= 32) & (hsv[:, :, 1] > 110) & (hsv[:, :, 2] > 100) if orange else _green(area)
        count, _, stats, centers = cv2.connectedComponentsWithStats(mask.astype('uint8'))
        if count > 1:
            index = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])
            if stats[index, cv2.CC_STAT_AREA] > 0.35 * mask.size:
                cx, cy = centers[index]
                result['actions'][action] = dict(point=[int(x + cx), int(y + cy)])
    header = (0, 0, 720, 160)
    modal_title = (200, 370, 550, 455)
    body = (90, 450, 650, 795)
    division = next((k for k, (_, label) in DIVISIONS.items() if has(label, (180, 50, 550, 420))), None)
    if division:
        result['division'] = division
    phase = next((int(m.group(1)) for t in rows((150, 20, 600, 650)) if (m := re.fullmatch('第([1-9])轮', t['text']))), None)
    if phase:
        result['event_phase'] = phase
    promoted = [t for t in rows((70, 560, 650, 740)) if t['text'] == '决赛轮晋级成功!']
    tier = [t for t in rows((200, 300, 530, 620)) if t['text'] in {'A', 'B'} and t['bbox'][2] - t['bbox'][0] > 90 and (t['bbox'][3] - t['bbox'][1] > 90)]
    if len(promoted) == len(tier) == 1 and has('分级联赛', (180, 180, 560, 350)) and exact('组', (250, 430, 500, 640)) and (_green(image[200:365, 10:710]).mean() > 0.05) and (_green(image[710:850, 10:710]).mean() > 0.05) and (not any((any((word in t['text'] for word in ('消耗', '购买', '兑换', '放弃', '报名确认'))) for t in good))):
        result.update(page='champions_promotion', promotion_group=tier[0]['text'])
        add('dismiss_group_promotion', promoted[0])
        return result
    outcomes = [dict(t, text=re.sub('\\s+', '', unicodedata.normalize('NFKC', t['text']))) for t in tokens if t.get('confidence', 0) >= 0.9 and re.sub('\\s+', '', t['text']) in {'胜利', '失败', '平局'} and (100 <= t['bbox'][0] < t['bbox'][2] <= 620) and (600 <= t['bbox'][1] < t['bbox'][3] <= 950) and (t['bbox'][2] - t['bbox'][0] > 220) and (t['bbox'][3] - t['bbox'][1] > 90)]
    click = exact('点击', (250, 1000, 470, 1110))
    outcome_modal = exact('报名确认', modal_title) or exact('自动编成确认', modal_title)
    if click and (not outcome_modal) and (len(outcomes) == 1 or outcome_expected):
        result.update(page='champions_outcome', result_outcome={'胜利': 'victory', '失败': 'defeat', '平局': 'draw'}[outcomes[0]['text']] if len(outcomes) == 1 else None, result_basis='owned_result_click' if outcome_expected else 'outcome_banner_click')
        add('dismiss_match_outcome', click)
        return result
    if exact('自动编成确认', modal_title):
        result.update(page='champions_auto_confirm')
        if has('是否重置当前编成', body) and has('对优俊少女进行统一编成', body) and exact('取消', (90, 795, 350, 915)):
            add('confirm_auto_lineup', exact('确定', (380, 795, 660, 915)), True)
        return result
    if exact('报名确认', modal_title):
        result.update(page='champions_entry_confirm', entry_resource={'kind': 'unknown'})
        free = has('每日可免费报名1次', body) and has('今日可报名次数不会减少', body)
        if free and has('是否报名参赛', body):
            result['entry_resource'] = dict(kind='free', cost=0, free_verified=True)
        elif has('使用报名券进行报名', body) and exact('持有', (120, 630, 250, 725)):
            result['entry_resource'] = dict(kind='ticket', cost=1, ticket_verified=True)
        elif has('30个宝石报名参赛', body) and has('即将', body) and has('是否确认执行', body):
            result['entry_resource'] = dict(kind='jewels', cost=30, jewels_verified=True)
        remaining = next((int(m.group(1)) for t in rows(body) if (m := re.fullmatch('剩余([0-3])次', t['text']))), None)
        if remaining is not None:
            result['entry_resource']['today_remaining'] = remaining
        add('cancel_entry', exact('取消', (90, 795, 350, 915)))
        add('confirm_entry', exact('确定', (380, 795, 660, 915)), True)
        if 'confirm_entry' not in result['actions'] and 'cancel_entry' in result['actions'] and (result['entry_resource'].get('kind') in {'free', 'ticket', 'jewels'}):
            control('confirm_entry', (380, 800, 660, 885))
        return result
    if exact('参赛登记确认', modal_title):
        result.update(page='champions_team_confirm')
        if has('按该成员、战术配置进行登记', body) and has('无法更改成员、战术配置', body):
            occupied = []
            for x in (120, 310, 500):
                area = image[460:630, x:x + 95]
                occupied.append(float(cv2.cvtColor(area, cv2.COLOR_BGR2HSV)[:, :, 1].mean()) > 35)
            result['team_complete'] = all(occupied)
            if result['team_complete']:
                add('confirm_team', exact('确定', (380, 795, 660, 915)), True)
        return result
    if has('请选择3名', (250, 300, 620, 405)) and exact('报名信息', (0, 450, 170, 525)):
        result.update(page='champions_lineup', lineup_count=None)
        counts = []
        empty = []
        empty_labels = []
        for top in (530, 675, 820):
            vacant = exact('请选择参与报名的优俊少女', (230, top + 55, 690, top + 115)) is not None
            empty_labels.append(vacant)
            portrait = image[top:top + 90, 40:145]
            counts.append(float((cv2.cvtColor(portrait, cv2.COLOR_BGR2HSV)[:, :, 1] > 55).mean()) > 0.08)
            empty.append(vacant or (float(portrait.std()) < 8 and float(portrait.mean()) > 210))
        if all(counts) and (not any(empty_labels)):
            result['lineup_count'] = 3
            add('confirm_lineup', exact('确定', (200, 1020, 510, 1140)), True)
        elif all((bool(full) != bool(vacant) for full, vacant in zip(counts, empty))):
            result['lineup_count'] = sum(counts)
            add('auto_lineup', exact('自动编成', (400, 925, 680, 1010)), True)
        return result
    if any(('正在进行匹配' in t['text'] for t in good)):
        return dict(result, page='champions_matching')
    if exact('领取成功', modal_title) and exact('已获得以上奖励', (200, 660, 560, 760)):
        result.update(page='champions_claim_receipt')
        result['ticket_reward'] = has('报名券', (130, 490, 400, 590))
        add('close_claim', exact('关闭', (200, 780, 510, 915)))
        return result
    if has('轮次奖励', (120, 0, 600, 180)):
        result.update(page='champions_round_reward')
        continuing = exact('继续', (200, 1120, 530, 1240))
        if continuing:
            add('continue_round_reward', continuing)
            return result
        if _green(image[1145:1215, 240:480]).mean() > 0.35:
            return result
        chest = image[790:1100, 230:490]
        if float(chest.mean()) > 140 and float(chest.std()) > 25:
            result.update(page='champions_round_reward')
            result['actions']['advance_round_reward'] = dict(point=[360, 1000])
        return result
    if exact('排名表', (100, 1120, 370, 1240)) and exact('继续', (400, 1120, 650, 1240)):
        add('continue_match_result', exact('继续', (400, 1120, 650, 1240)))
        if 'continue_match_result' in result['actions']:
            result.update(page='champions_match_result')
            outcome = next((t['text'] for t in good if t['text'] in {'失败', '胜利', '平局'}), None)
            if outcome:
                result['result_outcome'] = {'失败': 'defeat', '胜利': 'victory', '平局': 'draw'}[outcome]
        return result
    match = next((re.fullmatch('第(\\d+)轮第([1-5])场比赛', t['text']) for t in rows(header) if re.fullmatch('第(\\d+)轮第([1-5])场比赛', t['text'])), None)
    if match and exact('查看结果', (150, 1120, 350, 1240)) and exact('前往赛事', (370, 1120, 570, 1240)) and exact('属性', (340, 165, 500, 225)) and exact('技能', (520, 165, 690, 225)):
        from core.public_race_geometry import RaceRoutePlanner
        preview = RaceRoutePlanner._race_preview_action_points(image)
        if isinstance(preview, dict):
            result.update(page='champions_preview', event_phase=int(match.group(1)), match_index=int(match.group(2)), preview=preview)
            point = preview.get('view_result')
            if preview.get('view_result_locked') is False and isinstance(point, (list, tuple)) and (len(point) == 2) and all((type(v) is int for v in point)) and (0 <= point[0] < 720) and (0 <= point[1] < 1280):
                result['actions']['view_match_result'] = dict(point=list(point))
            return result
    if match:
        result.update(page='champions_standings', event_phase=int(match.group(1)), match_index=int(match.group(2)))
        add('continue_standings', exact('继续', (200, 1120, 530, 1240)))
        if 'continue_standings' not in result['actions']:
            control('continue_standings', (220, 1140, 510, 1225))
        return result
    statuses = []
    for i in range(5):
        cell = rows((15 + i * 138, 525, min(720, 155 + i * 138), 590))
        found = [t['text'] for t in cell if t['text'] in {'正在参赛', '参赛前', '失败', '胜利', '平局'}]
        statuses.append(found[0] if len(found) == 1 else None)
    unmatched = has('尚未匹配', (180, 525, 570, 590))
    header_count = sum((has(f'第{i}场比', (15 + (i - 1) * 138, 480, min(720, 155 + (i - 1) * 138), 535)) for i in range(1, 6)))
    semantic_table = all(statuses) and has('群英联赛', header) and (phase is not None or has('比赛结果', (550, 570, 720, 640)))
    finished_table = all(statuses) and phase is not None and has('比赛结果', (550, 570, 720, 640))
    if (header_count == 5 or semantic_table or finished_table) and (has('获得奖励', (0, 630, 230, 685)) or finished_table):
        result.update(page='champions_board', table_verified=True, match_states=statuses, unmatched=unmatched, completed_matches=sum((s in {'失败', '胜利', '平局'} for s in statuses)))
        active = [i + 1 for i, s in enumerate(statuses) if s == '正在参赛']
        completed = result['completed_matches']
        contiguous = all((s in {'失败', '胜利', '平局'} for s in statuses[:completed]))
        result['next_match'] = active[0] if len(active) == 1 and contiguous and (active[0] == completed + 1) else None
        if unmatched:
            add('match_round', exact('匹配', (200, 1000, 540, 1160)))
            if 'match_round' not in result['actions']:
                control('match_round', (240, 1050, 470, 1145), True)
        elif result['next_match'] is not None:
            add('launch_match', exact('参加赛事', (200, 1000, 540, 1160)) or exact('赛事', (200, 1000, 540, 1160)))
        elif result['completed_matches'] == 5:
            add('claim_round', exact('获得奖励', (200, 1030, 540, 1170)), True)
            if 'claim_round' not in result['actions']:
                control('claim_round', (230, 1030, 500, 1155))
        return result
    if common['page'] in {'home', 'races', 'events', 'champions_choose', 'profile', 'menu', 'uma_list', 'uma_detail'}:
        return common
    current_round = exact('赛事', (210, 875, 500, 1010))
    registered = any((has(label, (90, 810, 260, 900)) for label in ('正在参赛', '正在报名')))
    if has('群英联赛', header) and registered and current_round:
        result.update(page='champions_active_hub', round_registered=True, event_period=common.get('event_period'))
        add('open_current_round', current_round)
        return result
    if has('群英联赛', header) and exact('报名参赛', (210, 875, 470, 960)):
        result.update(page='champions_hub')
        add('open_entry', exact('报名参赛', (210, 875, 470, 960)))
        add('return_home', exact('主页', (290, 1200, 430, 1280)))
        result['event_period'] = common.get('event_period')
        return result
    return result
