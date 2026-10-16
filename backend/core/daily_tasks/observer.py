"""Local observations for the user-taught daily route, in 720x1280 portrait."""
import re
import cv2
import numpy as np
from core.legend_selection.observer import observe as observe_selection, read_tokens as read_selection_tokens, radio_choice
from core.legend_race.observer import selected_horse
from core.jjc.formation import _control_state
TABS = ('日常', '主要', '称号', '限定')

def deduplicate_tokens(tokens):
    """Merge repeated readings of one physical label, never different values."""
    result = []
    for original in tokens:
        token = dict(original, text=original['text'].replace(' ', ''))
        a, b, c, d = token['bbox']
        duplicate = None
        for index, prior in enumerate(result):
            if prior['text'] != token['text']:
                continue
            x, y, right, bottom = prior['bbox']
            overlap = max(0, min(c, right) - max(a, x)) * max(0, min(d, bottom) - max(b, y))
            smaller = min((c - a) * (d - b), (right - x) * (bottom - y))
            if smaller > 0 and overlap / smaller >= 0.8 and (abs(a + c - (x + right)) / 2 <= max(4, min(c - a, right - x) * 0.3)) and (abs(b + d - (y + bottom)) / 2 <= max(4, min(d - b, bottom - y) * 0.3)):
                duplicate = index
                break
        if duplicate is None:
            result.append(token)
        else:
            prior = result[duplicate]
            chosen, other = (token, prior) if token.get('confidence', 0) > prior.get('confidence', 0) else (prior, token)
            if not chosen.get('source') and other.get('source'):
                chosen = dict(chosen, source=other['source'])
            result[duplicate] = chosen
    return result

def supplement_missing_fields(image, tokens, observed, ocr):
    """One same-frame enlarged read, only for absent fields on known pages.

    A conflicting candidate is not an absent field. This never captures a new
    frame or changes its capture timestamp; callers rebuild observation/actions.
    """
    page = observed.get('page')
    specs = {'daily_races': [('tickets', (580, 0, 720, 65), '\\d+/\\d+')], 'multi_confirm': [('count', (270, 590, 460, 690), '\\d+/\\d+'), ('tickets', (350, 690, 655, 750), '\\d+/\\d+(?:/\\d+)?')], 'items': [('tickets', (300, 800, 610, 870), '\\d+/\\d+(?:/\\d+)?')], 'daily_details': [('multi_enabled', (270, 1030, 480, 1130), '多次参赛[:：][开关]')]}
    result = list(tokens)
    for field, box, pattern in specs.get(page, []):
        if observed.get(field) is not None:
            continue
        a, b, c, d = box
        candidates = [t for t in result if a <= t['bbox'][0] < t['bbox'][2] <= c and b <= t['bbox'][1] < t['bbox'][3] <= d and re.fullmatch(pattern, t['text'].replace(' ', ''))]
        if candidates:
            continue
        rows, _ = ocr(cv2.resize(image[b:d, a:c], None, fx=3, fy=3))
        for points, text, confidence in rows or []:
            text = text.replace(' ', '')
            if confidence >= 0.9 and re.fullmatch(pattern, text):
                bounds = [min((p[0] for p in points)), min((p[1] for p in points)), max((p[0] for p in points)), max((p[1] for p in points))]
                target = [int(bounds[0] / 3) + a, int(bounds[1] / 3) + b, int(bounds[2] / 3) + a, int(bounds[3] / 3) + b]
                if a <= target[0] < target[2] <= c and b <= target[1] < target[3] <= d:
                    result.append(dict(text=text, confidence=float(confidence), bbox=target, source='daily_same_frame_' + field))
    return deduplicate_tokens(result)

def _selected_tab(image, token):
    """Selected tabs show light label ink on their filled surface, in any hue."""
    a, b, c, d = token['bbox']
    if not (10 <= a < c <= image.shape[1] - 10 and 0 <= b < d <= image.shape[0]):
        return None
    sides = np.concatenate((image[b:d, a - 10:a - 3], image[b:d, c + 3:c + 10]), axis=1)
    background = float(np.median(sides.max(axis=2)))
    label = image[b:d, a:c].max(axis=2).astype(float)
    light = float((label > background + 8).mean())
    dark = float((label < background - 20).mean())
    if light >= 0.15 and dark < 0.1 and (float(label.max()) >= 170):
        return True
    return False if dark >= 0.15 else None

def read_tokens(image, ocr):
    tokens = deduplicate_tokens(read_selection_tokens(image, ocr))
    if image.shape[:2] != (1280, 720):
        return tokens

    def refine(box, pattern, source=None):
        a, b, c, d = box
        rows, _ = ocr(cv2.resize(image[b:d, a:c], None, fx=4, fy=4), use_det=False)
        for text, confidence in rows or []:
            text = text.replace(' ', '')
            if confidence >= 0.9 and re.fullmatch(pattern, text):
                token = dict(text=text, confidence=float(confidence), bbox=list(box))
                if source:
                    token['source'] = source
                tokens.append(token)
    if any(('选择参赛' in t['text'] and t['bbox'][1] < 55 for t in tokens)):
        for field, box, pattern in [('surface', (475, 193, 584, 234), '(草地|泥地)[SABCDEFG]'), ('distance', (463, 238, 584, 281), '(短距离|英里|中距离|长距离)[SABCDEFG]')]:
            if not any((t.get('source') == field + '_grade' and t['confidence'] >= 0.9 for t in tokens)):
                refine(box, pattern, field + '_label_grade')
    for token in list(tokens):
        if token['text'] in ('简单', '普通', '困难') and token['confidence'] < 0.9:
            a, b, c, d = token['bbox']
            refine((max(0, a - 5), max(0, b - 5), min(720, c + 5), min(1280, d + 5)), '简单|普通|困难')
    if any(('排名奖励' in t['text'] for t in tokens)) and (not any((t['text'] == '下一步' for t in tokens))):
        refine((474, 1190, 644, 1238), '下一步|下步')
        if not any((t['text'] == '下一步' and t['confidence'] >= 0.9 for t in tokens)):
            refine((463, 1199, 551, 1235), '下一步')
    if any(('请选择要兑换的道具' in t['text'] for t in tokens)) and (not any((re.fullmatch('[\\d,]+', t['text']) and 175 <= t['bbox'][0] < t['bbox'][2] <= 365 and (975 <= t['bbox'][1] < t['bbox'][3] <= 1025) for t in tokens))):
        rows, _ = ocr(cv2.resize(image[980:1025, 175:365], None, fx=3, fy=3))
        for _, text, confidence in rows or []:
            if text == '0' and confidence >= 0.8:
                tokens.append(dict(text='0', confidence=float(confidence), bbox=[175, 980, 365, 1025], source='zero_shop_cost'))
    if any((t['text'] == '强化编成' and t['bbox'][1] > 1200 for t in tokens)) and (not any((t['text'] == '主页' for t in tokens))):
        refine((330, 1230, 394, 1268), '主页')
        if not any((t['text'] == '主页' and t['confidence'] >= 0.9 for t in tokens)):
            refine((320, 1240, 393, 1270), '主页')
    return deduplicate_tokens(tokens)

def observe_claim_fast(raw, image, ocr):
    """Full-height claim receipt: title and close button, not every reward item."""
    from core.ocr_regions import read_regions
    if image.shape[:2] != (1280, 720):
        return None
    tokens = read_regions(image, ocr, [(220, 20, 530, 100), (270, 1130, 470, 1230)])
    if not any((t['text'].replace(' ', '') == '领取成功' and t['confidence'] >= 0.95 for t in tokens)):
        return None
    result = observe(raw, tokens)
    if result['page'] == 'claim_success' and 'close_claim' in result['actions']:
        from core.jjc.observer import observe_loading_fast
        return observe_loading_fast(raw, image, ocr) or result
    return None

def ticket_pair(tokens, box):
    """Parse either two counts or the observed merged 6/65/6, without guessing."""
    a, b, c, d = box
    texts = [t for t in tokens if a <= t['bbox'][0] < t['bbox'][2] <= c and b <= t['bbox'][1] < t['bbox'][3] <= d and ('/' in t['text'])]
    texts.sort(key=lambda t: t['bbox'][0])
    if len(texts) == 2:
        first, second = (t['bbox'] for t in texts)
        if min(first[2], second[2]) > max(first[0], second[0]) and min(first[3], second[3]) > max(first[1], second[1]):
            return None
        pairs = [re.fullmatch('(\\d+)/(\\d+)', t['text']) for t in texts]
        if all(pairs) and pairs[0][2] == pairs[1][2]:
            before, after, maximum = (int(pairs[0][1]), int(pairs[1][1]), int(pairs[0][2]))
            return (before, after) if 0 <= after <= before <= maximum else None
    if len(texts) == 1:
        parts = texts[0]['text'].split('/')
        if len(parts) == 3 and all((p.isdigit() for p in parts)):
            options = [(int(parts[0]), int(parts[1][i:])) for i in range(1, len(parts[1])) if parts[1][:i] == parts[2] and 0 <= int(parts[1][i:]) <= int(parts[0]) <= int(parts[2])]
            return options[0] if len(options) == 1 else None
    return None

def observe(raw, tokens):
    tokens = deduplicate_tokens(tokens)
    result = observe_selection(raw, tokens)
    if result['page'] in {'loading', 'blocked', 'connection_interrupted', 'connection_conflict'}:
        return result
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return result
    for field in ('surface', 'distance'):
        values = [t['text'][-1] for t in tokens if t.get('source') == field + '_label_grade' and t['confidence'] >= 0.9]
        if len(set(values)) == 1:
            result[field] = values[0] if result.get(field) in (None, values[0]) else None
    selection = result.copy()
    result['actions'] = {}
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.9]

    def inside(t, box):
        x0, y0, x1, y1 = box
        a, b, c, d = t['bbox']
        return x0 <= a < c <= x1 and y0 <= b < d <= y1

    def matches(pattern, box=(0, 0, 720, 1280)):
        return [(t, m) for t in good if inside(t, box) for m in [re.fullmatch(pattern, t['text'])] if m]

    def find(text, box=(0, 0, 720, 1280)):
        found = [t for t in good if t['text'] == text and inside(t, box)]
        return found[0] if len(found) == 1 else None

    def has(text, box=(0, 0, 720, 1280)):
        return any((text in t['text'] and inside(t, box) for t in good))

    def action(name, token):
        if token:
            a, b, c, d = token['bbox']
            result['actions'][name] = dict(point=[(a + c) // 2, (b + d) // 2])

    def pink(box):
        a, b, c, d = box
        hsv = cv2.cvtColor(image[b:d, a:c], cv2.COLOR_BGR2HSV)
        return int(((hsv[:, :, 0] >= 155) & (hsv[:, :, 0] < 179) & (hsv[:, :, 1] > 140) & (hsv[:, :, 2] > 160)).sum()) >= 60

    def number(box):
        found = matches('(?:\\d+|\\d{1,3}(?:,\\d{3})+)', box)
        return int(found[0][0]['text'].replace(',', '')) if len(found) == 1 else None

    def button(name, text, box=(0, 1000, 720, 1260)):
        action(name, find(text, box))

    def claim_button(name, box):
        from core.autonomous_training.harvest_observer import _control_enabled
        token = find('一键领取', box)
        if token and _control_enabled(image, token):
            action(name, token)

    def home():
        button('return_home', '主页', (280, 1200, 440, 1280))
    reward_headings = find('奖励总计', (260, 505, 465, 580)) and has('参赛奖励') and has('排名奖励')
    reward_closes = [t for t in tokens if t['text'].replace(' ', '') in {'关闭', '结束'} and t['confidence'] >= (0.85 if reward_headings and t['text'].replace(' ', '') == '结束' else 0.9) and inside(t, (270, 1130, 470, 1230))]
    reward_close = reward_closes[0] if len(reward_closes) == 1 else None
    fixed_reward_frame = find('参赛结果', (220, 20, 530, 100)) and reward_close
    courses = matches('(?:特殊)?(月光奖|木星杯)(简单|普通|困难)?', (0, 0, 720, 1100))
    names = {m[1] for _, m in courses}
    if len(names) == 1:
        result['course'] = next(iter(names))
    difficulties = {m[2] for _, m in courses if m[2]}
    difficulties.update((t['text'] for t in good if t['text'] in ('简单', '普通', '困难') and inside(t, (0, 0, 720, 1100))))
    if len(difficulties) == 1:
        result['difficulty'] = next(iter(difficulties))
    if selection['page'] in ('blocked', 'loading'):
        return result
    if has('秋川理事长', (20, 890, 330, 1010)):
        result['page'] = 'mission_dialog'
        region = image[1170:1255, 615:705]
        skip = find('跳过', (580, 1130, 720, 1280)) or find('快进', (580, 1130, 720, 1280))
        if skip:
            action('skip_mission_dialog', skip)
        elif (region.min(axis=2) > 225).mean() > 0.3:
            result['actions']['skip_mission_dialog'] = dict(point=[658, 1213])
    elif find('领取成功', (220, 350, 530, 470)) and (find('已获得以上奖励', (220, 650, 510, 760)) or find('已领取指定礼物', (220, 650, 510, 760))):
        result['page'] = 'claim_success'
        button('close_claim', '关闭', (270, 780, 470, 880))
    elif find('领取成功', (220, 20, 530, 100)):
        result['page'] = 'claim_success'
        button('close_claim', '关闭', (270, 1130, 470, 1230))
    elif find('礼物箱', (220, 20, 510, 100)):
        result['page'] = 'gifts'
        result['gifts_empty'] = has('没有可领取') or has('没有礼物') or has('无可领取的礼物')
        if not result['gifts_empty']:
            claim_button('claim_gifts', (385, 1130, 665, 1240))
        button('close_gifts', '关闭', (90, 1130, 345, 1240))
    elif find('兑换确认', (200, 20, 540, 100)) or find('兑换成功', (200, 20, 540, 100)):
        success = find('兑换成功', (200, 20, 540, 100)) is not None
        result['page'] = 'shop_success' if success else 'shop_confirm'
        if success:
            button('close_shop_success', '关闭', (270, 1130, 470, 1230))
        elif has('是否使用金币兑换指定道具', (30, 1010, 695, 1080)):
            before, after = (number((240, 1070, 440, 1125)), number((440, 1070, 660, 1125)))
            result.update(coins_before=before, coins_after=after, currency='金币')
            if type(before) is int and type(after) is int and (0 <= after < before):
                result['coin_cost'] = before - after
                button('exchange_shop', '兑换', (400, 1130, 670, 1235))
    elif find('比赛播放确认', (200, 395, 550, 460)):
        result['page'] = 'orientation'
        portrait = radio_choice(image, ((69, 543), (407, 543)), radius=12, extent=28)
        result['portrait_selected'] = None if portrait is None else portrait == 1
        action('select_portrait', find('竖屏', (475, 490, 610, 580)))
        if result['portrait_selected']:
            button('confirm_portrait', '确定', (410, 775, 655, 885))
    elif find('限时特卖正在准备', (170, 350, 560, 470)):
        result['page'] = 'shop_unavailable'
        button('close_shop_unavailable', '关闭', (270, 760, 470, 900))
    elif find('限时特卖开售', (180, 350, 550, 470)):
        result['page'] = 'limited_sale'
        counts = matches('(?:今日开售数)?剩余(\\d+)次', (200, 695, 580, 800))
        result['sale_remaining'] = int(counts[0][1][1]) if len(counts) == 1 else None
        if result['sale_remaining'] == 0:
            button('open_limited_shop', '前往限时特卖', (385, 770, 680, 900))
        button('cancel_limited_sale', '取消', (80, 770, 350, 900))
    elif find('一键参赛', (200, 350, 540, 460)):
        result['page'] = 'multi_confirm'
        count = matches('(\\d+)/(\\d+)', (270, 590, 460, 690))
        ticket = ticket_pair(good, (350, 690, 655, 750))
        if len(count) == 1 and ticket is not None:
            result.update(count=int(count[0][1][1]), maximum=int(count[0][1][2]), tickets=ticket[0], tickets_after=ticket[1])
            if 0 < result['count'] <= result['maximum'] == result['tickets']:
                if result['count'] < result['maximum']:
                    result['actions']['increase_count'] = dict(point=[529, 647])
                if result['tickets_after'] == result['tickets'] - result['count']:
                    button('enter_multi', '参赛！', (390, 790, 660, 870))
    elif find('选择道具', (200, 290, 530, 365)) and has('请选择道具'):
        result['page'] = 'items'
        slots = image[693:805, 230:475]
        hsv = cv2.cvtColor(slots, cv2.COLOR_BGR2HSV)
        result['items_empty'] = float((hsv[:, :, 1] > 65).mean()) < 0.02
        counts = ticket_pair(good, (300, 800, 610, 870))
        if counts is not None:
            result.update(tickets=counts[0], tickets_after=counts[1])
            if result['items_empty'] and 0 < result['tickets'] and (result['tickets_after'] == result['tickets'] - 1):
                button('enter_race', '参赛！', (390, 865, 660, 930))
    elif reward_headings or fixed_reward_frame:
        result['page'] = 'multi_rewards'
        action('close_multi_rewards', reward_close)
    elif find('商店', (0, 0, 200, 70)) and has('今日开售数'):
        result['page'] = 'limited_shop'
        result['shop_empty'] = has('似乎没货了')
        result['shop_exchanged_rows'] = len(matches('已兑换', (20, 450, 690, 920)))
        result.update(coin_cost=number((175, 975, 365, 1025)), coins=number((495, 975, 705, 1025)))
        if result['coin_cost'] is None and any((t.get('source') == 'zero_shop_cost' for t in tokens)):
            result['coin_cost'] = 0
        if has('请选择要兑换的道具'):
            button('select_all_shop', '全部选择', (560, 910, 715, 980))
        if type(result['coin_cost']) is int and type(result['coins']) is int and (0 < result['coin_cost'] <= result['coins']):
            button('confirm_shop', '确定', (245, 1030, 470, 1130))
        home()
    elif find('商店', (0, 0, 200, 70)) and has('兑换', (20, 460, 700, 1030)):
        result['page'] = 'shop_menu'
        button('open_sale_from_menu', '限时特卖', (20, 460, 700, 1030))
        result['actions']['scroll_shop_menu'] = dict(swipe=[355, 940, 355, 530, 350])
        home()
    elif find('任务', (0, 0, 200, 70)) and all((find(t, (0, 450, 720, 510)) for t in TABS)):
        result['page'] = 'missions'
        claimable, selected = ([], [])
        for index, tab in enumerate(TABS):
            token = find(tab, (0, 450, 720, 510))
            a, b, c, d = token['bbox']
            if pink((max(0, c - 3), 440, min(720, c + 58), 480)):
                claimable.append(tab)
                action('tab_' + tab, token)
            if _selected_tab(image, token) is True:
                selected.append(tab)
        result.update(claimable_tabs=claimable, selected_tab=selected[0] if len(selected) == 1 else None)
        if result['selected_tab'] in claimable:
            claim_button('claim_missions', (230, 1030, 520, 1130))
        home()
    elif has('挑战日常赛事') and has('金币') and has('协助积分') and find('关闭', (270, 1150, 475, 1240)):
        result['page'] = 'race_help'
        button('close_race_help', '关闭', (270, 1150, 475, 1240))
    elif find('赛事详情', (200, 20, 540, 100)) and has('日常赛事', (100, 100, 610, 210)):
        result['page'] = 'daily_details'
        off = find('多次参赛:关') or find('多次参赛：关')
        on = find('多次参赛:开') or find('多次参赛：开')
        toggle = on or off
        toggle_state = None
        if toggle and (not (on and off)):
            a, b, c, d = toggle['bbox']
            toggle_state = _control_state(image, (a - 20, b - 8, c + 20, d + 8))
        result['multi_enabled'] = True if on and (not off) else False if off and (not on) else None
        result['multi_available'] = toggle_state
        if toggle_state is True and off and (not on):
            action('enable_multi', off)
        button('open_selection', '参赛！', (385, 1130, 670, 1240))
        button('close_details', '取消', (75, 1130, 350, 1240))
    elif selection['page'] in ('selection', 'sort'):
        result['actions'] = dict(selection['actions'])
        if selection['page'] == 'selection' and result.get('course') in ('月光奖', '木星杯'):
            result['horse'] = selected_horse(result)
            if result['horse']:
                button('confirm_selected', '确定', (230, 1020, 510, 1140))
        if selection['page'] == 'selection':
            home()
    elif find('日常赛事', (0, 0, 250, 70)):
        result['page'] = 'daily_races'
        counts = matches('(\\d+)/(\\d+)', (580, 0, 720, 65))
        result['tickets'] = int(counts[0][1][1]) if len(counts) == 1 else None
        moon = find('月光奖', (250, 610, 460, 740))
        if type(result['tickets']) is int and result['tickets'] > 0 and moon and find('木星杯', (250, 760, 460, 870)) and has('草地1600米', (250, 680, 570, 760)):
            action('open_moon_course', moon)
        available = []
        for token, match in matches('月光奖', (20, 600, 265, 1030)):
            a, b, c, d = token['bbox']
            if _control_state(image, (max(20, a - 20), max(600, b - 20), min(260, c + 20), min(1040, d + 40))) is True:
                labels = [t['text'] for t in good if t['text'] in ('简单', '普通', '困难') and inside(t, (20, d, 265, min(1100, d + 60)))]
                if len(labels) == 1:
                    available.append((token, labels[0]))
        if available:
            for token, difficulty in available:
                action('course_' + difficulty, token)
                result['actions']['course_' + difficulty].update(course='月光奖', difficulty=difficulty)
            token, difficulty = min(available, key=lambda item: item[0]['bbox'][1])
            result.update(course='月光奖', difficulty=difficulty)
            if result['tickets'] is not None and result['tickets'] > 0:
                action('open_daily_details', token)
                result['actions']['open_daily_details'].update(course='月光奖', difficulty=difficulty)
                result['actions']['scroll_courses'] = dict(swipe=[620, 1000, 620, 750, 350])
            else:
                result['actions'] = {}
        home()
    elif result.get('course') == '月光奖' and find('继续比赛', (200, 350, 540, 450)) and find('参赛中的优俊少女', (20, 510, 220, 580)):
        result['page'] = 'race_resume'
        names = [t['text'] for t in good if inside(t, (250, 610, 510, 650))]
        result['resume_horse_name'] = names[0] if len(names) == 1 else None
        result['resume_horse_score'] = number((380, 650, 520, 720))
        button('resume_race', '继续', (390, 760, 680, 900))
    elif result.get('course') == '月光奖' and find('前往赛事', (370, 1110, 650, 1250)):
        result['page'] = 'race_ready'
        names = [t['text'] for t in good if inside(t, (280, 810, 510, 880))]
        result['race_horse'] = names[0] if len(names) == 1 else None
        button('start_race', '前往赛事', (370, 1110, 650, 1250))
    elif result.get('course') == '月光奖' and find('继续', (260, 1110, 490, 1240)):
        result['page'] = 'lineup'
        result['lineup_names'] = [t['text'] for t in good if inside(t, (110, 110, 460, 1060))]
        button('continue_lineup', '继续', (260, 1110, 490, 1240))
    elif result.get('course') == '月光奖' and find('参赛！', (235, 1120, 510, 1250)):
        result['page'] = 'race_grid'
        result['lineup_names'] = [t['text'] for t in good if inside(t, (110, 100, 475, 1070))]
        button('run_race', '参赛！', (235, 1120, 510, 1250))
        result['actions']['scroll_lineup_top'] = dict(swipe=[350, 300, 350, 950, 450])
    elif result.get('course') == '月光奖' and find('下一页', (235, 1120, 510, 1250)):
        result['page'] = 'race_result'
        result['won'] = has('第1名', (0, 130, 190, 430))
        button('next_result', '下一页', (235, 1120, 510, 1250))
    elif result.get('course') == '月光奖' and has('排名奖励') and (find('下一步', (400, 1110, 670, 1250)) or find('下步', (474, 1190, 644, 1238))):
        result['page'] = 'race_rewards'
        action('next_rewards', find('下一步', (400, 1110, 670, 1250)) or find('下步', (474, 1190, 644, 1238)))
    elif find('拍摄', (425, 1210, 525, 1270)):
        result['page'] = 'animation'
        circles = image[1180:1250, 540:592]
        skip = find('跳过', (530, 1150, 650, 1280)) or find('快进', (530, 1150, 650, 1280))
        if skip:
            action('skip_race', skip)
        elif (circles.min(axis=2) > 225).mean() > 0.3:
            result['actions']['skip_race'] = dict(point=[567, 1220])
    elif has('日常安排', (200, 725, 550, 830)) and has('日常赛事', (70, 900, 330, 1070)):
        result['page'] = 'schedule'
        button('open_daily_races', '日常赛事', (70, 900, 330, 1070))
    elif find('团队赛事', (200, 760, 540, 920)) and find('团队编成', (200, 900, 600, 1030)):
        result['page'] = 'arena'
        home()
    elif selection['page'] == 'races':
        result['page'] = 'races'
        button('open_schedule', '日常安排', (75, 1010, 365, 1140))
    elif selection['page'] == 'home':
        result['page'] = 'home'
        result['actions'] = dict(selection['actions'])
        result['missions_badge'] = pink((659, 770, 719, 821))
        result['gifts_badge'] = pink((673, 878, 694, 901))
        if has('已开放', (215, 1100, 295, 1160)):
            result['actions']['open_shop'] = dict(point=[251, 1090])
        if result['missions_badge']:
            result['actions']['open_missions'] = dict(point=[655, 818])
        if result['gifts_badge']:
            result['actions']['open_gifts'] = dict(point=[654, 913])
    return result
