"""Local OCR/CV inventory perception; never emit authentication actions or raw OCR."""
import hashlib
import re
from pathlib import Path
import cv2
import numpy as np
from core.account_login.observer import observe as observe_login
from core.account_login.observer import read_tokens as read_login_tokens
from core.page_identity import PageIdentity

def read_tokens(image, ocr):
    tokens = read_login_tokens(image, ocr)
    navigation = {t['text'] for t in tokens if t['confidence'] >= 0.9 and t['bbox'][1] > 1180}
    if '主页' not in navigation and {'强化编成', '剧情', '招募'} <= navigation:
        rows, _ = ocr(cv2.resize(image[1220:1280, 290:440], None, fx=3, fy=3))
        matches = [(box, confidence) for box, text, confidence in rows or [] if text == '主页' and confidence >= 0.9]
        if len(matches) == 1:
            box, confidence = matches[0]
            tokens.append(dict(text='主页', confidence=float(confidence), bbox=[int(min((p[0] for p in box)) / 3) + 290, int(min((p[1] for p in box)) / 3) + 1220, int(max((p[0] for p in box)) / 3) + 290, int(max((p[1] for p in box)) / 3) + 1220]))
    return tokens

def _within(token, box):
    a, b, c, d = token['bbox']
    x0, y0, x1, y1 = box
    return x0 <= a < c <= x1 and y0 <= b < d <= y1

def _stars(image, scale=1.0):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([18, 100, 180]), np.array([38, 255, 255]))
    if scale == 1.25:
        mask[240:252, 70:230] = 0
    stats = cv2.connectedComponentsWithStats(mask)[2]
    strips = []
    unit_height = 24 * scale
    for x, y, w, h, area in stats[1:]:
        if not 0.82 * unit_height <= h <= 1.15 * unit_height:
            continue
        count = round((w / unit_height - 1.08) / 0.77) + 1
        if 1 <= count <= 5 and abs(w / unit_height - (1.08 + 0.77 * (count - 1))) < 0.13 and (abs(area / unit_height ** 2 - (0.53 + 0.48 * (count - 1))) < 0.19):
            strips.append((int(x), int(y), int(w), int(h), count))
    return strips

def _portrait_key(image):
    gray = cv2.cvtColor(cv2.resize(image, (9, 8)), cv2.COLOR_BGR2GRAY)
    return np.packbits(gray[:, 1:] > gray[:, :-1]).tobytes().hex()

def observe(raw, tokens):
    result = dict(page='unknown', actions={}, frame_sha256=hashlib.sha256(raw).hexdigest())
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return result
    good = [t for t in tokens if t['confidence'] >= 0.9]
    login = observe_login(raw, tokens)
    if login.get('tutorial'):
        result.update(page='loading', tutorial=login['tutorial'])
        if 'close_notice' in login['actions']:
            result.update(page='event_notice', actions={'dismiss_event_result': login['actions']['close_notice']})
        return result
    if login['page'] in {'connection_interrupted', 'connection_conflict'}:
        result.update(page=login['page'])
        return result

    def find(text, box=(0, 0, 720, 1280)):
        matches = [t for t in good if t['text'].replace(' ', '') == text and _within(t, box)]
        return matches[0] if len(matches) == 1 else None

    def action(name, token):
        if token:
            x0, y0, x1, y1 = token['bbox']
            result['actions'][name] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    home = find('主页', (260, 1180, 460, 1280))
    if home and find('选择参赛优俊少女', (0, 0, 300, 65)) and find('场地适应性', (320, 175, 480, 240)) and find('确定', (260, 1030, 450, 1120)):
        result['page'] = 'race_selection'
        action('back_home', home)
        return result
    from core.daily_legend.listing import help_close, locate
    legend_help = help_close(tokens)
    if legend_help:
        result.update(page='daily_legend_help', actions={'close_legend_help': legend_help})
        return result
    if home and find('日常传奇赛事', (0, 0, 260, 70)) and locate(image, 1):
        result.update(page='daily_legend_list')
        action('back_home', home)
        return result
    if home and find('日常安排', (280, 730, 550, 815)) and find('日常赛事', (120, 950, 300, 1050)):
        result.update(page='daily_schedule')
        action('back_home', home)
        return result
    close = find('关闭', (200, 1100, 520, 1250))
    from core.autonomous_training.harvest_observer import observe_harvest
    completed = observe_harvest(raw, tokens, allow_animation_taps=False)
    if completed and completed['page'] == 'training_complete' and ('close_completed_training' in completed['actions']):
        result.update(page='training_complete', actions={'close_training_complete': completed['actions']['close_completed_training']})
        return result
    result['game_header_detected'] = login.get('game_header_detected', False)
    if login['page'] == 'records':
        result['page'] = 'records'
        return result
    if login['page'] in {'auth_blocked', 'password', 'terms'}:
        result['page'] = 'auth_blocked'
        return result
    if login['page'] == 'startup_terms':
        result['page'] = 'startup_terms'
        return result
    if login['page'] == 'recent':
        result['page'] = 'recent'
        names = [t for t in good if _within(t, (180, 480, 540, 560))]
        result['sdk_login_name'] = names[0]['text'] if len(names) == 1 else None
        return result
    from core.location_navigation import SETTLEMENT_ROUTES, TASK_OWNERS
    from core.jjc.observer import observe_weekly_settlement
    weekly = observe_weekly_settlement(image, tokens)
    if weekly is not None and login['page'] not in {'notice', 'story_unlock', 'connection_conflict', 'connection_interrupted'}:
        result.update(weekly)
        return result
    if login['page'] not in {'notice', 'story_unlock'}:
        history = find('自主训练养成履历', (50, 0, 670, 120)) is not None or (completed is not None and completed['page'] in {'history_summary', 'history_races'})
        if history:
            result.update(page='autonomous_history', actions={})
            control = find('确定', (220, 1120, 500, 1240))
            from core.autonomous_training.harvest_observer import _control_enabled
            if control is not None and _control_enabled(image, control):
                action('close_training_history', control)
            return result
        if completed is not None and completed['page'] in TASK_OWNERS:
            result.update(page=completed['page'], actions={}, task_owner=TASK_OWNERS[completed['page']])
            return result
    if completed and completed['page'] in SETTLEMENT_ROUTES and (login['page'] not in {'notice', 'story_unlock'}):
        page = completed['page']
        route = SETTLEMENT_ROUTES[page]
        result.update(page=page, actions={})
        if route in completed['actions']:
            result['actions'][route] = completed['actions'][route]
        return result
    if find('日期变化', (280, 380, 450, 450)) and find('日期发生了变化', (260, 610, 460, 675)) and find('确定', (300, 800, 420, 875)):
        result['page'] = 'date_change'
        action('confirm_date_change', find('确定', (300, 800, 420, 875)))
        return result
    if find('使用收集到的碎片，可以解锁养成优俊少女。', (20, 970, 500, 1030)) and find('解锁后，可以为其进行养成。', (20, 1010, 350, 1070)) and find('关闭', (200, 1160, 520, 1260)) and find('帮助', (560, 1160, 680, 1260)):
        result['page'] = 'uma_unlock_help'
        action('close_uma_help', find('关闭', (200, 1160, 520, 1260)))
        return result
    if find('可查看或更改训练员信息。', (20, 970, 320, 1030)) and find('过生日时可在主页收到来自优俊少女的祝福，', (20, 1010, 500, 1060)) and find('改变性别后部分剧情将会发生变化。', (20, 1040, 420, 1090)) and find('（性别可随时更改）', (20, 1070, 260, 1120)) and find('继续', (200, 1160, 520, 1260)):
        result['page'] = 'profile_help'
        action('continue_profile_help', find('继续', (200, 1160, 520, 1260)))
        return result
    if find('设置优俊少女代表和协助卡', (20, 970, 320, 1030)) and find('可以为被关注者的养成提供帮助。', (20, 1010, 400, 1060)) and find('根据被借用的次数次日可获得好友积分。', (20, 1040, 460, 1090)) and find('（将发放至礼物箱）', (20, 1070, 260, 1120)) and find('关闭', (200, 1160, 520, 1260)) and find('上一页', (20, 1160, 180, 1260)) and find('帮助', (560, 1160, 680, 1260)):
        result['page'] = 'profile_support_help'
        action('close_profile_help', find('关闭', (200, 1160, 520, 1260)))
        return result
    if find('养成优俊少女详情', (180, 20, 550, 110)) and close:
        result.update(page='uma_detail', character=None)
        action('close_detail', close)
        titles = [t for t in tokens if t['confidence'] >= 0.85 and _within(t, (250, 120, 580, 190)) and re.fullmatch('[\\[【][^\\[\\]【】]{2,30}[\\]】]', t['text'])]
        names = [t for t in good if t['confidence'] >= 0.98 and _within(t, (250, 171, 580, 212)) and re.fullmatch('[\\u4e00-\\u9fff·・]{2,20}', t['text'])]
        stars = [s[-1] for s in _stars(image, 1.25) if 80 <= s[0] <= 90 and 245 <= s[1] <= 260]
        if len(stars) == 1:
            name = names[0]['text'] if len(names) == 1 else None
            if len(titles) == 1:
                title = titles[0]['text'][1:-1]
                result['character'] = dict(title=title if name is not None else None, name=name, full_name=f'[{title}] {name}' if name is not None else None, stars=stars[0], version_verified=name is not None, name_verified=name is not None)
            elif name is not None:
                result['character'] = dict(name=name, stars=stars[0], title=None, full_name=None, version_verified=False, name_verified=True)
            else:
                result['character'] = dict(name=None, stars=stars[0], title=None, full_name=None, version_verified=False, name_verified=False)
        return result
    if find('个人资料', (0, 0, 250, 65)) and find('训练员账号', (270, 300, 500, 390)) and home:
        result.update(page='profile', trainer_id=None, trainer_name=None)
        ids = [t for t in good if t['confidence'] >= 0.98 and _within(t, (290, 375, 560, 425)) and re.fullmatch('\\d{12}', t['text'].replace(' ', ''))]
        names = [t for t in good if _within(t, (100, 65, 580, 120))]
        if len(ids) == 1:
            result['trainer_id'] = ids[0]['text'].replace(' ', '')
        if len(names) == 1:
            result['trainer_name'] = names[0]['text']
        action('back_home', home)
        return result
    list_title = [t for t in good if _within(t, (0, 0, 290, 65)) and t['text'].replace('·', '').replace(' ', '') == '养成优俊少女一览解锁']
    if len(list_title) == 1 and find('返回', (0, 1020, 150, 1130)) and home:
        result.update(page='uma_list', characters=[], locked_boundary_visible=False, filter_off=bool(find('筛选：关', (230, 940, 400, 1030))), sort_stars=bool(find('★数', (390, 940, 520, 1030))), sort_descending=any((t['text'].startswith('降序') and _within(t, (580, 940, 700, 1030)) for t in good)))
        boundary = find('未解锁优俊少女', (15, 85, 300, 965))
        bottom = 970
        if boundary:
            _, y0, _, y1 = boundary['bbox']
            hsv = cv2.cvtColor(image[y0:y1, 300:660], cv2.COLOR_BGR2HSV)
            if ((hsv[:, :, 0] >= 32) & (hsv[:, :, 0] <= 65) & (hsv[:, :, 1] > 120) & (hsv[:, :, 2] > 150)).mean() > 0.9:
                result['locked_boundary_visible'] = True
                bottom = y0
        strips = []
        star_strips = _stars(image)
        offsets = [x - round((x - 40) / 134.25) * 134.25 for x, y, _, h, _ in star_strips if 205 <= y and y + h < bottom and (30 <= x <= 630)]
        origin = float(np.median(offsets)) if len(offsets) >= 3 else 40
        for x, y, w, h, stars in star_strips:
            column = round((x - origin) / 134.25)
            if not (34 <= origin <= 46 and 0 <= column < 5 and (abs(x - (origin + column * 134.25)) < 4) and (205 <= y) and (y + h < bottom) and (20 <= h <= 26)):
                continue
            key = _portrait_key(image[y - 103:y - 24, x + 8:x + 94])
            strips.append((x, y, stars, key))
        strips.sort(key=lambda s: (round(s[1] / 5), s[0]))
        result['list_signature'] = [list(s) for s in strips]
        rows = {}
        for x, y, _, _ in strips:
            rows.setdefault(round(y / 5), []).append(round((x - origin) / 134.25))
        row_columns = list(rows.values())
        if any((cols != list(range(5)) for cols in row_columns[:-1])) or (row_columns and row_columns[-1] != list(range(len(row_columns[-1])))):
            result['list_signature'] = None
            action('back_home', home)
            return result
        for x, y, stars, key in strips:
            if stars < 3:
                continue
            index = len(result['characters'])
            name = f'uma_{index}'
            result['characters'].append(dict(action_id=name, portrait_key=key, stars=stars))
            result['actions'][name] = dict(point=[x + 52, y - 60])
        action('back_home', home)
        if result['filter_off'] and result['sort_stars'] and result['sort_descending']:
            result['actions']['scroll_uma_top'] = dict(swipe=[360, 300, 360, 860, 500])
            result['actions']['scroll_uma'] = dict(swipe=[360, 860, 360, 300, 500])
        return result
    if find('菜单', (250, 20, 470, 120)) and close and find('标题页面', (440, 950, 690, 1150)):
        result['page'] = 'menu'
        profile = find('个人资料/', (430, 140, 690, 220))
        if find('训练员名片', (430, 180, 690, 260)):
            action('open_profile', profile)
        action('return_title', find('标题页面', (440, 950, 690, 1150)))
        action('close_menu', close)
        return result
    if find('强化编成', (0, 140, 270, 240)) and find('协助卡', (400, 850, 650, 1000)) and home:
        result['page'] = 'enhance_menu'
        action('open_uma', find('养成优俊少女', (0, 850, 350, 1000)))
        action('back_home', home)
        return result
    if find('养成优俊少女', (300, 600, 600, 710)) and home and find('觉醒等级强化', (100, 700, 390, 810)) and find('才能开花', (100, 820, 350, 930)):
        buttons = [t for t in good if _within(t, (400, 800, 690, 960)) and t['text'].replace('·', '').replace(' ', '') == '一览解锁']
        if len(buttons) == 1:
            result['page'] = 'uma_menu'
            action('open_uma_list', buttons[0])
            action('back_home', home)
            return result
    titles = [t for t in good if _within(t, (80, 200, 650, 800)) and re.fullmatch('(是否要?|是否)?返回标题页[面]?？?', t['text'].replace(' ', ''))]
    if titles and find('确定', (380, 650, 700, 1100)) and find('取消', (30, 650, 350, 1100)):
        result['page'] = 'title_confirm'
        action('confirm_title', find('确定', (380, 650, 700, 1100)))
        return result
    if login['page'] == 'title' and set(login['actions']) == {'enter_game'}:
        result.update(page='title', actions={'enter_game': dict(login['actions']['enter_game'])})
        if login.get('title_page_verified') is True:
            result['title_page_verified'] = True
        if 'title_logo_error' in login:
            result['title_logo_error'] = login['title_logo_error']
        return result
    if login['page'] in {'title', 'unknown', 'loading'}:
        from core.title_screen import title_logo
        title = title_logo(image, tokens)
        if title:
            result.update(page='title', title_logo_error=title['error'], actions={'enter_game': {'point': title['point']}})
            return result
    from core.story_event.observer import observe as observe_event
    event = observe_event(raw, tokens)
    event_page, event_action, navigation = {'bingo': ('event_bingo', 'close_bingo' if 'close_bingo' in event['actions'] else 'advance_bingo', 'dismiss_event_result'), 'roulette_limit': ('event_notice', 'close', 'dismiss_event_result'), 'roulette_replace': ('event_notice', 'close', 'dismiss_event_result'), 'roulette_empty': ('event_notice', 'close', 'dismiss_event_result'), 'tutorial_help': ('event_notice', 'dismiss_tutorial', 'dismiss_event_result'), 'story_list': ('event_notice', 'close', 'dismiss_event_result'), 'story_next': ('event_notice', 'cancel', 'dismiss_event_result'), 'story_settings': ('event_notice', 'cancel_story', 'dismiss_event_result'), 'story_playing': ('event_story_playing', 'menu', 'open_event_story_menu'), 'story_menu': ('event_story_menu', 'skip', 'skip_event_story'), 'story_skip_confirm': ('event_story_skip_confirm', 'skip_confirm', 'confirm_event_story_skip'), 'roulette': ('event_roulette', 'back', 'back_event'), 'missions': ('event_missions', 'back', 'back_event'), 'event': ('story_event', 'home', 'back_home')}.get(event['page'], (None, None, None))
    if event_page and event_action in event['actions']:
        result.update(page=event_page, actions={navigation: event['actions'][event_action]})
        return result
    if login['page'] in {'unknown', 'notice'}:
        from core.autonomous_training.observer import observe as observe_training
        training = observe_training(raw, tokens)
        navigation = {'autonomous_running': ('open_training_menu', 'open_training_menu'), 'training_menu': ('return_home', 'back_home')}.get(training['page'])
        if navigation:
            result.update(page=training['page'], actions={})
            if navigation[0] in training['actions']:
                result['actions'][navigation[1]] = training['actions'][navigation[0]]
            return result
        if training['page'] in TASK_OWNERS:
            result.update(page=training['page'], actions={}, task_owner=TASK_OWNERS[training['page']])
            return result
    if login['page'] in {'notice', 'story_unlock', 'loading'}:
        result.update(page=login['page'], actions=login['actions'])
        return result
    setup_back = find('返回', (0, 950, 350, 1250))
    if login['page'] == 'unknown' and setup_back:
        from core.autonomous_training.observer import observe as observe_training
        training = observe_training(raw, tokens)
        if training['page'] in {'trainee', 'scenario', 'exam_mode'}:
            result.update(page='training_' + training['page'], actions={})
            action('back_training_setup', setup_back)
            return result
    if login['page'] == 'unknown':
        from core.jjc.observer import observe as observe_jjc
        owned_race = observe_jjc(raw, tokens)
        owner_page = 'jjc_' + owned_race['page']
        if TASK_OWNERS.get(owner_page) == 'jjc':
            result.update(page=owner_page, actions={}, task_owner='jjc')
            return result
    if home:
        from core.jjc.observer import observe as observe_jjc
        arena = observe_jjc(raw, tokens)
        if arena['page'] == 'arena' and (arena.get('race_in_progress') or any((t['text'].replace(' ', '') == '正在参赛' and _within(t, (80, 700, 550, 910)) for t in good))):
            result.update(page='arena_pending', actions={})
            action('back_home', home)
            return result
        if arena['page'] == 'races':
            result.update(page='races')
            action('back_home', home)
            return result
        if arena['page'] == 'opponents':
            result.update(page='arena_opponents')
            action('back_home', home)
            return result
        if arena['page'] == 'arena' and type(arena.get('rp')) is int and (arena['rp'] >= 0) and (not arena.get('race_in_progress')):
            result.update(page='arena_empty' if arena['rp'] == 0 else 'arena_idle', rp=arena['rp'])
            action('back_home', home)
            return result
    if login['page'] == 'home':
        hsv = cv2.cvtColor(image[1230:1270, 280:460], cv2.COLOR_BGR2HSV)
        selected = ((hsv[:, :, 0] >= 80) & (hsv[:, :, 0] <= 115) & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 150)).mean() > 0.25
        if selected:
            result.update(page='home', resources={})
            ui = PageIdentity(image)
            action('open_enhance', find('强化编成', (0, 1180, 160, 1280)) or ui.find('强化编成'))
            action('open_menu', find('菜单', (580, 25, 715, 105)) or ui.find('菜单'))
            for field, box, pattern in [('coins', (210, 85, 365, 125), '\\d[\\d,]*'), ('jewels', (410, 85, 530, 125), '\\d[\\d,]*'), ('team_score', (20, 108, 170, 150), '\\d+'), ('team_grade', (20, 45, 170, 116), '[A-Z]+\\d?')]:
                values = [t['text'] for t in good if t['confidence'] >= 0.95 and _within(t, box) and re.fullmatch(pattern, t['text'])]
                if len(values) == 1:
                    result['resources'][field] = values[0] if field == 'team_grade' else int(values[0].replace(',', ''))
    if result['page'] == 'unknown' and event['page'] == 'other_event' and ('leave_other' in event['actions']):
        result.update(page='other_event', actions={'back_home': event['actions']['leave_other']})
    return result
