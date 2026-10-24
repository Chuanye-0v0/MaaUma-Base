"""Observe named guild controls; reference coordinates are never input targets."""
import hashlib
from pathlib import Path
import re
import cv2
import numpy as np
from core.guild_daily import ITEMS

def refine_sender_names(image, tokens, ocr):
    """Read the nickname before the visual space in a merged name/age line."""
    from core.ocr_regions import read_regions
    targets = []
    for t in tokens:
        if not re.search('\\d+(?:分钟|分|小时|秒|天)(?:前|以内)$', t['text']):
            continue
        x0, y0, x1, y1 = t['bbox']
        if not 100 <= y0 < y1 <= 1040:
            continue
        roi = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        dark = cv2.inRange(roi, (3, 55, 25), (40, 255, 215))
        blank = (dark > 0).sum(axis=0) == 0
        gaps = []
        start = None
        for i, value in enumerate(blank.tolist() + [False]):
            if value and start is None:
                start = i
            elif not value and start is not None:
                if 3 < start and i < x1 - x0 - 3 and (i - start >= 4):
                    gaps.append((start, i, i - start))
                start = None
        if gaps:
            cut = max(gaps, key=lambda g: g[2])[0]
            targets.append((t, (max(0, x0 - 2), max(0, y0 - 2), min(720, x0 + cut + 2), min(1280, y1 + 2))))
    if not targets:
        return tokens
    refined = read_regions(image, ocr, [box for _, box in targets])
    for t, (x0, y0, x1, y1) in targets:
        matches = sorted([r for r in refined if r['confidence'] >= 0.65 and x0 <= r['bbox'][0] < r['bbox'][2] <= x1 and (y0 <= r['bbox'][1] < r['bbox'][3] <= y1)], key=lambda r: r['bbox'][0])
        if matches:
            t['member_name'] = ''.join((r['text'].replace(' ', '') for r in matches))
    return tokens

def observe(raw, image, tokens):
    result = dict(page='unknown', actions={}, requests=[], request_active=False, donation_exhausted=False, tokens=tokens, frame_sha256=hashlib.sha256(raw).hexdigest())
    if image is None or image.shape[:2] != (1280, 720):
        return result
    good = [dict(t, text=re.sub('\\s+', '', t['text'])) for t in tokens if t.get('confidence', 0) >= 0.65 and len(t.get('bbox', [])) == 4]

    def find(names, box=(0, 0, 720, 1280), contains=False):
        names = (names,) if isinstance(names, str) else names
        x0, y0, x1, y1 = box
        found = [t for t in good if x0 <= t['bbox'][0] < t['bbox'][2] <= x1 and y0 <= t['bbox'][1] < t['bbox'][3] <= y1 and any((name in t['text'] if contains else name == t['text'] for name in names))]
        return found[0] if len(found) == 1 else None

    def any_text(names):
        return any((any((name in t['text'] for name in names)) for t in good))

    def enabled(token):
        if token is None:
            return False
        x0, y0, x1, y1 = token['bbox']
        roi = image[max(0, y0 - 15):min(1280, y1 + 15), max(0, x0 - 30):min(720, x1 + 30)]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        return float((cv2.inRange(hsv, (25, 65, 80), (100, 255, 255)) > 0).mean()) >= 0.1

    def ordinary_enabled(token):
        if token is None:
            return False
        x0, y0, x1, y1 = token['bbox']
        hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        brown = cv2.inRange(hsv, (3, 55, 25), (40, 255, 215))
        return enabled(token) or float((brown > 0).mean()) >= 0.04

    def offer(name, token, *, effect='navigation', green=False, **fields):
        if token is not None and (not green or enabled(token)):
            x0, y0, x1, y1 = token['bbox']
            result['actions'][name] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2], effect=effect, label=token['text'], **fields)

    def item_in(rows):
        found = {key for key, names in ITEMS.items() for t in rows if ('鞋' in t['text'] or t['text'] in names) and any((n in t['text'] for n in names))}
        return next(iter(found)) if len(found) == 1 else None

    def close():
        offer('close', find(('关闭', '取消'), (20, 700, 700, 1250)))

    def scan_donations(ymin, ymax):
        buttons = sorted([t for t in good if t['text'] in {'捐赠', '寄付', '已捐赠'} and 450 <= t['bbox'][0] and (ymin <= t['bbox'][1] < ymax)], key=lambda t: t['bbox'][1])
        for button in buttons:
            cy = (button['bbox'][1] + button['bbox'][3]) // 2
            row = [t for t in good if 20 <= t['bbox'][0] < 700 and cy - 115 <= (t['bbox'][1] + t['bbox'][3]) // 2 <= cy + 110]
            item = item_in(row)
            item_tokens = [t for t in row if item and any((n in t['text'] for n in ITEMS[item]))]
            item_y = min((t['bbox'][1] for t in item_tokens), default=cy)
            names = [t['text'] for t in row if t['bbox'][3] <= item_y and t['text'] not in {'请求', '道具', '道具捐赠请求', '请求道具', '当前捐赠数', '持有数'} and (not re.search('小时|分钟|秒|剩余|持有|数量|\\d+/\\d+', t['text']))]
            below = [t for t in row if cy + 25 <= t['bbox'][1] <= cy + 100 and t.get('member_name')]
            if len(below) == 1:
                names = [below[0]['member_name']]
            if item is None or len(names) != 1:
                if button['text'] != '已捐赠' and ordinary_enabled(button):
                    result['unresolved_requests'] = result.get('unresolved_requests', 0) + 1
                continue
            key = hashlib.sha256((names[0] + '|' + item).encode('utf8')).hexdigest()[:24]
            donated = button['text'] == '已捐赠'
            counts = [re.fullmatch('已捐赠(\\d+)个?', t['text']) for t in row]
            counts = [int(m[1]) for m in counts if m]
            viewer = counts[0] if len(counts) == 1 else None
            x0, y0, x1, y1 = button['bbox']
            position = [(x0 + x1) // 2, (y0 + y1) // 2]
            result['requests'].append(dict(key=key, item=item, donated=donated, viewer_donated=viewer, position=position))
            if not donated and ordinary_enabled(button):
                offer('donate_' + key, button, key=key, item=item, viewer_donated=viewer)
    if find(('加载中', '连接中'), (420, 1130, 720, 1280), contains=True):
        result['page'] = 'loading'
        return result
    if find('社团菜单', (200, 300, 520, 500)) and any_text(('社团信息', '社团名片', '排行榜')):
        result['page'] = 'guild_menu'
        close()
        return result
    if find('社团排行榜', (100, 150, 620, 340)) and any_text(('结果公布',)) and find('获得奖励', (200, 590, 550, 750)):
        result['page'] = 'guild_rank_reward'
        close()
        return result
    if any_text(('购买', '补充库存', '消耗宝石', '消耗萝卜', '退出社团', '踢出成员')):
        result['page'] = 'guild_forbidden'
        return result
    if any_text(('社团',)) and any_text(('尚未解锁', '功能未解锁', '解锁后可使用')):
        result['page'] = 'guild_locked_dialog'
        offer('close', find(('关闭', '确定'), (200, 700, 650, 1250)))
        return result
    if any_text(('捐赠次数已达上限', '捐赠次数达到上限', '今日无法再捐赠', '持有道具不足', '持有数量不足')):
        result.update(page='guild_donation_limit', donation_exhausted=True)
        close()
        return result
    if any_text(('请求成功', '已发出道具捐赠请求', '已发送道具捐赠请求')):
        result.update(page='guild_request_success', item=item_in(good))
        close()
        return result
    if any_text(('捐赠完成', '捐赠成功', '已捐赠道具')):
        result.update(page='guild_donation_success', item=item_in(good))
        close()
        return result
    if any_text(('获得道具', '获得了以下养成道具', '已领取以下道具', '领取完成')):
        quantities = [re.search('[×xX](\\d+)', t['text']) for t in good if '养成道具' in t['text']]
        quantities = [int(m[1]) for m in quantities if m]
        result.update(page='guild_claim_success', item=item_in(good), received_count=quantities[0] if len(quantities) == 1 else None)
        close()
        return result
    request_confirm_title = find('请求确认', (100, 20, 650, 470))
    if not request_confirm_title and any_text(('8小时内无法', '小时后可再次请求', '下次请求时间', '正在请求道具')):
        result.update(page='guild_request_active', request_active=True, item=item_in(good))
        close()
        return result
    request_title = request_confirm_title or find(('请求道具', '道具请求'), (100, 20, 650, 470))
    if request_title and item_in(good) is not None and find(('确定', '请求'), (350, 700, 700, 1250)):
        result.update(page='guild_request_confirm', item=item_in(good))
        offer('confirm_request', find(('确定', '请求'), (350, 700, 700, 1250)), effect='request', green=True, item=result['item'])
        close()
        return result
    title = find(('捐赠确认', '捐赠道具', '道具捐赠'), (100, 20, 650, 470))
    if title and any_text(('捐赠数量', '捐赠以下', '是否捐赠', '持有数', '所持数')):
        result.update(page='guild_donation_confirm', item=item_in(good))
        values = {int(t['text']) for t in good if re.fullmatch('\\d+', t['text']) and 280 <= t['bbox'][0] < t['bbox'][2] <= 440 and (650 <= t['bbox'][1] < t['bbox'][3] <= 755)}
        result['quantity'] = next(iter(values)) if len(values) == 1 else None
        remaining = []
        for t in good:
            if '今日剩余捐赠次数' not in t['text']:
                continue
            line = ''.join((r['text'] for r in sorted(good, key=lambda r: r['bbox'][0]) if abs(r['bbox'][1] - t['bbox'][1]) < 15))
            match = re.search('今日剩余捐赠次数(?:为|:|：)?(\\d+)', line)
            if match:
                remaining.append(int(match[1]))
        result['remaining_donations'] = remaining[0] if len(remaining) == 1 else None
        held = find('持有数', (200, 730, 390, 830))
        stock = {int(t['text']) for t in good if held and re.fullmatch('\\d+', t['text']) and (held['bbox'][2] < t['bbox'][0] < 650) and (abs(t['bbox'][1] - held['bbox'][1]) < 18)}
        result['owned'] = next(iter(stock)) if len(stock) == 1 else None
        if result['quantity'] != 0 and result['remaining_donations'] != 0 and (result['owned'] != 0):
            offer('confirm_donation', find(('捐赠', '确定'), (350, 700, 700, 1250)), effect='donation', green=True, item=result['item'])
        close()
        return result
    picker_title = find(('道具捐赠请求', '选择请求道具', '请求道具', '道具请求'), (80, 20, 650, 105)) or find(('选择请求道具', '请求道具', '道具请求'), (80, 320, 650, 470))
    item_rows = [(key, find(names, (20, 180, 690, 1100))) for key, names in ITEMS.items()]
    donation_rows = any((t['text'] in {'捐赠', '寄付'} and 180 <= t['bbox'][1] < 1040 for t in good))
    if picker_title and (not donation_rows) and (sum((t is not None for _, t in item_rows)) >= 3):
        result.update(page='guild_request_picker', selected_item=None)
        for key, t in item_rows:
            if t is None:
                continue
            offer('select_' + key, t, item=key)
            cy = (t['bbox'][1] + t['bbox'][3]) // 2
            roi = cv2.cvtColor(image[max(0, cy - 28):min(1280, cy + 28), 540:705], cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(roi, (25, 80, 90), (95, 255, 255))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            marks = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) >= 100]
            if any((10 <= w <= 55 and 10 <= h <= 55 and (0.6 <= w / h <= 1.6) for x, y, w, h in marks)):
                if result['selected_item'] is not None:
                    result['selected_item'] = 'ambiguous'
                else:
                    result['selected_item'] = key
        offer('confirm_request', find(('确定', '请求'), (350, 700, 700, 1250)), effect='request', green=True, item=result['selected_item'])
        close()
        return result
    if picker_title:
        result['page'] = 'guild_requests'
        result['view_kind'] = 'donation_list' if donation_rows or find('一键捐赠', (350, 1020, 710, 1150)) else 'own_status'
        result['request_active'] = any_text(('当前道具捐赠请求状况', '当前的道具捐赠请求状况', '正在请求', '请求中', '请求已发送'))
        if result['request_active']:
            result['item'] = item_in(good)
        result['donation_exhausted'] = any_text(('今日捐赠次数已达上限', '今日捐赠已达上限', '没有可捐赠的请求', '暂无道具捐赠请求'))
        result['list_signature'] = cv2.resize(cv2.cvtColor(image[180:1050, 25:695], cv2.COLOR_BGR2GRAY), (16, 24)).tolist()
        offer('open_request', find(('请求', '发起请求', '请求道具'), (20, 1010, 700, 1230)), green=True)
        if any_text(('收到的道具', '可领取的道具', '请求已结束')):
            offer('claim_received', find(('领取', '全部领取'), (20, 180, 700, 1100)), effect='claim', green=True)
        scan_donations(180, 1040)
        if not result['donation_exhausted']:
            result['actions']['scroll_requests'] = dict(swipe=[350, 970, 350, 360], duration_ms=350, effect='navigation')
            result['actions']['scroll_requests_top'] = dict(swipe=[350, 360, 350, 970], duration_ms=350, effect='navigation')
        if result['view_kind'] == 'donation_list':
            rail = cv2.cvtColor(image[105:1055, 691:699], cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(rail, (0, 0, 55), (179, 95, 180))
            rows = (mask > 0).sum(axis=1) >= 2
            spans = []
            start = None
            for i, value in enumerate(rows.tolist() + [False]):
                if value and start is None:
                    start = i
                elif not value and start is not None:
                    if i - start >= 15:
                        spans.append((start + 105, i + 104))
                    start = None
            if spans:
                lo, hi = max(spans, key=lambda s: s[1] - s[0])
                result.update(list_top=lo <= 125, list_bottom=hi >= 1035)
        close()
        return result
    chat_hub = (find('社团', (580, 10, 710, 110)) and find('菜单', (580, 10, 710, 110)) or find('社团菜单', (570, 10, 710, 115))) and find('请输入信息', (200, 1030, 580, 1150))
    named_hub = find('社团', (0, 0, 350, 100)) and (find(('聊天', '成员'), (0, 80, 720, 430)) or find(('道具请求', '道具捐赠请求', '请求道具'), (0, 500, 720, 1180)))
    if (chat_hub or named_hub) and (not any_text(('邀请列表', '申请列表', '搜索社团'))):
        result.update(page='guild_hub', chat_hub=bool(chat_hub))
        if not chat_hub:
            offer('open_requests', find(('道具请求', '道具捐赠请求', '请求道具'), (0, 500, 720, 1180)))
        if chat_hub:
            badge_path = Path(__file__).parent / 'assets/guild-donation-list-icon.png'
            if badge_path.is_file():
                badge = cv2.imdecode(np.frombuffer(badge_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                match = cv2.minMaxLoc(cv2.matchTemplate(image[570:645, 65:190], badge, cv2.TM_CCOEFF_NORMED))
                if match[1] >= 0.94:
                    x, y = match[3]
                    h, w = badge.shape[:2]
                    result['actions']['open_donations'] = dict(point=[65 + x + w // 2, 570 + y + h // 2], effect='navigation')
            path = Path(__file__).parent / 'assets/guild-request-icon.png'
            if path.is_file():
                template = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                score = cv2.minMaxLoc(cv2.matchTemplate(image[1050:1140, 75:155], template, cv2.TM_CCOEFF_NORMED))
                if score[1] >= 0.94:
                    x, y = score[3]
                    h, w = template.shape[:2]
                    result['actions']['open_requests'] = dict(point=[75 + x + w // 2, 1050 + y + h // 2], effect='navigation')
        offer('return_home', find('主页', (250, 1180, 470, 1280)))
        filter_token = find(('全部', '信息', '贴图', '请求', '捐赠请求', '道具请求', '道具捐赠请求', '自定义对战'), (430, 960, 680, 1070))
        if filter_token is None and chat_hub:
            labels = [t for t in good if 470 <= t['bbox'][0] < t['bbox'][2] <= 625 and 990 <= t['bbox'][1] < t['bbox'][3] <= 1050]
            if len(labels) == 1:
                filter_token = labels[0]
        offer('open_filter', filter_token)
        if filter_token:
            result['chat_filter'] = filter_token['text']
        if chat_hub:
            scan_donations(650, 990)
            chat = image[650:985, 25:695]
            result['list_signature'] = cv2.resize(cv2.cvtColor(chat, cv2.COLOR_BGR2GRAY), (16, 24)).tolist()
            result['chat_empty'] = int((np.min(chat, axis=2) < 210).sum()) < 20
            result['actions']['scroll_chat'] = dict(swipe=[350, 710, 350, 960], duration_ms=350, effect='navigation')
        menu = find('社团菜单', (570, 10, 710, 115))
        if menu is None:
            top = [t for t in good if 580 <= t['bbox'][0] < t['bbox'][2] <= 710 and 10 <= t['bbox'][1] < t['bbox'][3] <= 110 and (t['text'] in {'社团', '菜单'})]
            if len(top) == 2 and {t['text'] for t in top} == {'社团', '菜单'}:
                menu = dict(text='社团菜单', bbox=[min((t['bbox'][0] for t in top)), min((t['bbox'][1] for t in top)), max((t['bbox'][2] for t in top)), max((t['bbox'][3] for t in top))])
        offer('open_menu', menu)
        return result
    from core.page_identity import PageIdentity
    if PageIdentity(image).home(tokens):
        result.update(page='home', guild_locked=None)
        scores = {}
        matches = {}
        for name in ('locked', 'unlocked'):
            path = Path(__file__).parent / 'assets' / (name + '.png')
            if path.is_file():
                template = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                if template is not None:
                    match = cv2.minMaxLoc(cv2.matchTemplate(image[1040:1145, 10:116], template, cv2.TM_CCOEFF_NORMED))
                    scores[name] = float(match[1])
                    matches[name] = (match[3], template.shape)
        if len(scores) == 2:
            winner = max(scores, key=scores.get)
            if scores[winner] >= 0.94 and scores[winner] - min(scores.values()) >= 0.08:
                result['guild_locked'] = winner == 'locked'
                if winner == 'unlocked':
                    (x, y), (h, w, _) = matches[winner]
                    result['actions']['open_guild'] = dict(point=[10 + x + w // 2, 1040 + y + h // 2], effect='navigation')
        if 'open_guild' not in result['actions'] and result['guild_locked'] is not True:
            label = find('社团', (15, 1100, 120, 1160))
            if label is None:
                parts = sorted([t for t in good if 15 <= t['bbox'][0] < t['bbox'][2] <= 120 and 1100 <= t['bbox'][1] < t['bbox'][3] <= 1160], key=lambda t: t['bbox'][0])
                if ''.join((t['text'] for t in parts)) == '社团' and max((t['bbox'][1] for t in parts)) - min((t['bbox'][1] for t in parts)) <= 8:
                    label = dict(text='社团', bbox=[min((t['bbox'][0] for t in parts)), min((t['bbox'][1] for t in parts)), max((t['bbox'][2] for t in parts)), max((t['bbox'][3] for t in parts))])
            offer('open_guild', label)
    elif find('社团', (0, 0, 350, 100)) and any_text(('邀请列表', '申请列表', '搜索社团')):
        result.update(page='guild_unjoined', membership='not_joined')
        offer('return_home', find('主页', (250, 1180, 470, 1280)) or find('返回', (0, 1000, 200, 1150)))
    elif any_text(('大家共同合作',)) and any_text(('加入社团吧', '成员之间可以进行交流')):
        result.update(page='guild_unjoined', membership='not_joined')
        offer('continue_guild_intro', find('继续', (250, 1100, 470, 1280)))
    elif any_text(('有效利用道具捐赠请求和',)) and any_text(('借用享受一定折扣',)):
        result.update(page='guild_unjoined', membership='not_joined')
        offer('close_guild_intro', find('关闭', (250, 1100, 470, 1280)))
    elif find('社团信息', (180, 0, 550, 100)) and any_text(('取消申请', '申请加入该社团')):
        result.update(page='guild_unjoined', membership='not_joined')
        offer('close_unjoined', find('取消', (20, 1080, 350, 1250)))
    return result
