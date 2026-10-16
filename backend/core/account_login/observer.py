"""Region-anchored login decisions. Raw OCR never leaves the perception layer."""
import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
import cv2
import numpy as np
from core.page_identity import PageIdentity
from core.passive_skip import continue_point

def native_record(xml, target):
    """Read only the exact saved-record label; never return other account text."""
    try:
        start = xml.index('<hierarchy')
        end = xml.index('</hierarchy>') + len('</hierarchy>')
        nodes = list(ET.fromstring(xml[start:end]).iter('node'))
    except (ValueError, ET.ParseError):
        return None
    if not any((n.get('text') == '登录记录' for n in nodes)):
        return None
    matches = [n for n in nodes if n.get('text') == target]
    if len(matches) != 1:
        return None
    bounds = re.fullmatch('\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]', matches[0].get('bounds', ''))
    if bounds:
        x0, y0, x1, y1 = map(int, bounds.groups())
        if 180 <= x0 < x1 <= 540 and 480 <= y0 < y1 <= 940:
            return [(x0 + x1) // 2, (y0 + y1) // 2]

def _password_edit_state(xml):
    """Return only focused, empty and masked booleans for the unique password field."""
    try:
        start = xml.index('<hierarchy')
        end = xml.index('</hierarchy>') + len('</hierarchy>')
        nodes = list(ET.fromstring(xml[start:end]).iter('node'))
    except (ValueError, ET.ParseError):
        return (False, False, False)
    fields = [node for node in nodes if node.get('class') == 'android.widget.EditText' and node.get('password') == 'true']
    if len(fields) != 1:
        return (False, False, False)
    field = fields[0]
    text = field.get('text', '')
    return (field.get('focused') == 'true', text in {'', '请输入密码'}, bool(text) and all((char in '•●*·' for char in text)))

def _password_layout(tokens):
    titles = [t for t in tokens if t['confidence'] >= 0.9 and t['text'].replace(' ', '') == '账号密码登录' and (150 <= t['bbox'][0]) and (t['bbox'][2] <= 600) and (200 <= t['bbox'][1]) and (t['bbox'][3] <= 550)]
    if len(titles) != 1:
        return None
    bottom = titles[0]['bbox'][3]
    return ((82, bottom + 30, 644, bottom + 140), (82, bottom + 145, 644, bottom + 250))

def native_username_bounds(xml, username, account_box):
    """Return only stable bounds for the exact, enabled native account field."""
    try:
        start = xml.index('<hierarchy')
        end = xml.index('</hierarchy>') + len('</hierarchy>')
        nodes = list(ET.fromstring(xml[start:end]).iter('node'))
    except (ValueError, ET.ParseError):
        return None
    if len([node for node in nodes if node.get('text') == '账号密码登录']) != 1:
        return None
    fields = [node for node in nodes if node.get('class') == 'android.widget.EditText' and node.get('password') != 'true' and (node.get('enabled') == 'true')]
    if len(fields) != 1 or fields[0].get('text') != username:
        return None
    bounds = re.fullmatch('\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]', fields[0].get('bounds', ''))
    if not bounds:
        return None
    x0, y0, x1, y1 = map(int, bounds.groups())
    ax0, ay0, ax1, ay1 = account_box
    if ax0 - 4 <= x0 < x1 <= ax1 + 4 and ay0 - 4 <= y0 < y1 <= ay1 + 4:
        return (x0, y0, x1, y1)

def read_tokens(image, ocr):

    def read(part, x=0, y=0, scale=1):
        rows, _ = ocr(part)
        return [dict(text=text, confidence=float(confidence), bbox=[int(min((p[0] for p in box)) / scale) + x, int(min((p[1] for p in box)) / scale) + y, int(max((p[0] for p in box)) / scale) + x, int(max((p[1] for p in box)) / scale) + y]) for box, text, confidence in rows or []]
    tokens = read(image)
    if any((t['text'] == '登录记录' for t in tokens)):
        for token in list(tokens):
            x0, y0, x1, y1 = token['bbox']
            if 0.6 <= token['confidence'] < 0.9 and 180 < x0 < 300 and (480 < y0 < 920):
                left, top, right, bottom = (max(0, x0 - 3), max(0, y0 - 3), min(540, x1 + 3), min(940, y1 + 3))
                refined, _ = ocr(cv2.resize(image[top:bottom, left:right], None, fx=4.5, fy=3), use_det=False)
                if refined and len(refined) == 1 and (refined[0][0] == token['text']) and (refined[0][1] >= 0.9):
                    tokens.remove(token)
                    tokens.append(dict(token, confidence=float(refined[0][1])))
    return tokens

def read_record_tokens(image, ocr):
    """Second reading of this frame's list text only, never guessed identities."""
    x, y, r, b = (180, 500, 550, 935)
    rows, _ = ocr(cv2.resize(image[y:b, x:r], None, fx=2, fy=2))
    return [dict(text=text, confidence=float(score), bbox=[x + int(min((p[0] for p in box)) / 2), y + int(min((p[1] for p in box)) / 2), x + int(max((p[0] for p in box)) / 2), y + int(max((p[1] for p in box)) / 2)]) for box, text, score in rows or []]

def _saved_record_template_matches(image, candidates, templates):
    """Optional local name->image mapping; no bundled or inferred identities.

    A template must identify one location and one record row. Repeated matches,
    conflicting configured names, and contradictory confident OCR add no identity.
    """
    if not isinstance(templates, Mapping) or not templates:
        return {}
    gray = cv2.cvtColor(image[500:940, 180:540], cv2.COLOR_BGR2GRAY)
    verified, ambiguous = {}, set()
    for name, path in templates.items():
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            template = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
        except (OSError, TypeError, ValueError, cv2.error):
            continue
        if template is None or not template.size or template.std() < 1:
            continue
        h, w = template.shape
        if h > gray.shape[0] or w > gray.shape[1]:
            continue
        scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, position = cv2.minMaxLoc(scores)
        if not np.isfinite(score) or score < 0.95:
            continue
        x, y = position
        other = scores.copy()
        other[max(0, y-h+1):y+h, max(0, x-w+1):x+w] = -1
        if np.any(other >= 0.95):
            continue
        point = [180+x+w//2, 500+y+h//2]
        rows = [i for i, token in enumerate(candidates)
                if token['bbox'][0] <= point[0] <= token['bbox'][2]
                and token['bbox'][1] <= point[1] <= token['bbox'][3]]
        if len(rows) != 1:
            continue
        index = rows[0]
        if candidates[index]['confidence'] >= 0.9 and candidates[index]['text'] != name:
            continue
        if index in verified and verified[index]['name'] != name:
            ambiguous.add(index)
            verified.pop(index)
        elif index not in ambiguous:
            verified[index] = dict(name=name, point=point, score=score)
    return verified

def observe(raw, tokens, target='', username='', native_username_matched=False, collect_records=False, records_layout_verified=False, saved_name_templates=None):
    """Observe this frame; saved_name_templates is an explicit user-local mapping.

    The default performs no template file reads. Existing callers keep exact,
    confident OCR matching; optional templates never guess missing identities.
    """
    result = dict(page='unknown', actions={}, target_selected=False, username_matches=False, frame_sha256=hashlib.sha256(raw).hexdigest())
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return result
    good = [t for t in tokens if t['confidence'] >= 0.9]
    public_labels = {'返回标题页面', '返回标题', '是否返回标题页面？', '确定', '取消', '关闭', '点击屏幕开始', '点击屏幕', '登录记录', '公告', '通知', '登录', '菜单', '解锁角色剧情', '现可解锁角色剧情', '跳过'}
    result['ui_labels'] = [dict(text=t['text'], bbox=t['bbox'], confidence=t['confidence']) for t in tokens if t['text'] in public_labels]
    result['ocr_count'] = len(tokens)
    result['game_header_detected'] = any((t['text'].startswith('Ver.') for t in good))

    def within(t, box):
        a, b, c, d = t['bbox']
        x0, y0, x1, y1 = box
        return x0 <= a and y0 <= b and (c <= x1) and (d <= y1)

    def find(text, box=(0, 0, 720, 1280)):
        matches = [t for t in good if t['text'].replace(' ', '') == text and within(t, box)]
        return matches[0] if len(matches) == 1 else None

    def action(name, token):
        if token:
            x0, y0, x1, y1 = token['bbox']
            result['actions'][name] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])

    def data_download_anchor(text, bbox):
        matches = [t for t in good if t['text'] == text and all((abs(actual - expected) <= 4 for actual, expected in zip(t['bbox'], bbox)))]
        return matches[0] if len(matches) == 1 else None
    from core.session_recovery import conflict_button, disconnect_button
    conflict = conflict_button(image, tokens)
    if conflict:
        result.update(page='connection_conflict', actions={'return_title_after_conflict': conflict})
        return result
    disconnected = disconnect_button(image, tokens)
    if disconnected:
        result.update(page='connection_interrupted', actions={'return_title_after_disconnect': disconnected})
        return result
    if any((t['bbox'][1] < 1100 and any((s in t['text'] for s in ('验证', '实名', '密码错误', '账号或密码', '登录失败', '连接已断开'))) for t in good)):
        result['page'] = 'auth_blocked'
        return result
    footer_loading = [t for t in tokens if t.get('confidence', 0) >= 0.75 and within(t, (470, 1180, 715, 1280)) and re.fullmatch('(?:加载中|正在加载(?:中)?)[.．。…⋯·•]*', re.sub('\\s+', '', t['text']))]
    if len(footer_loading) == 1:
        result.update(page='loading', game_loading=True, loading_phase='home_overlay')
        return result
    from core.passive_tutorial import detect as detect_tutorial, pager_identity
    pager = pager_identity(image)
    if pager:
        tutorial = detect_tutorial(image, tokens)
        result.update(page='loading', tutorial=pager)
        if tutorial:
            result.update(page='notice', tutorial=tutorial, actions={'close_notice': dict(point=tutorial['point'], text=tutorial['button'])})
        return result
    data_download = data_download_anchor('数据下载', [293, 394, 428, 434])
    additional_data = data_download_anchor('下载附加数据', [288, 610, 434, 639])
    cancel_download = data_download_anchor('取消', [161, 816, 242, 854])
    confirm_download = data_download_anchor('确定', [478, 816, 559, 855])
    if result['game_header_detected'] and find('用户协议与隐私政策', (180, 200, 550, 310)) and find('拒绝', (100, 960, 350, 1090)) and find('同意', (360, 960, 620, 1090)) and any(('请点击“同意”进入游戏' in t['text'] and within(t, (80, 740, 650, 880)) for t in good)):
        result['page'] = 'startup_terms'
        action('agree_startup_terms', find('同意', (360, 960, 620, 1090)))
    elif data_download and additional_data and cancel_download and confirm_download:
        result['page'] = 'data_download'
        action('confirm_data_download', confirm_download)
    elif find('登录记录', (200, 300, 520, 500)) or records_layout_verified:
        login = find('登录', (250, 600, 480, 780))
        switch = find('切换其他账号', (200, 700, 550, 900))
        if login and switch:
            result['page'] = 'recent'
            selected = [t for t in good if within(t, (180, 480, 540, 580))]
            result['target_selected'] = bool(target and len(selected) == 1 and (selected[0]['text'] == target))
            action('open_records', selected[0] if len(selected) == 1 else None)
            action('switch_other', switch)
            if result['target_selected']:
                action('login_saved', login)
        else:
            last = [t for t in good if '上次登录' in t['text'] and within(t, (160, 500, 550, 935))]
            row_candidates = [t for t in tokens if within(t, (180, 500, 540, 925)) and (not (t['bbox'][1] < 530 and t['bbox'][3] - t['bbox'][1] < 20)) and any((0 < line['bbox'][1] - t['bbox'][3] < 40 for line in last))]
            rows = [t for t in row_candidates if t['confidence'] >= 0.9]
            if len(last) >= 2 and len(rows) >= 2 or records_layout_verified:
                result['page'] = 'records'
                result['records_layout_verified'] = bool(records_layout_verified)
                configured = saved_name_templates if collect_records else (
                    {target: saved_name_templates[target]} if isinstance(saved_name_templates, Mapping)
                    and target in saved_name_templates else None)
                visual = _saved_record_template_matches(image, row_candidates, configured)
                rows = [dict(token, text=visual[i]['name']) if i in visual else token
                        for i, token in enumerate(row_candidates)
                        if token['confidence'] >= 0.9 or i in visual]
                target_visual = next((value for value in visual.values() if value['name'] == target), None)
                if target_visual is not None:
                    result['target_visual_score'] = round(target_visual['score'], 4)
                    result['target_ocr_confidence'] = max((t['confidence'] for t in tokens if t['text'] == target
                        and within(t, (180, 500, 540, 940))), default=0)
                if collect_records:
                    names = sorted(rows, key=lambda token: token['bbox'][1])
                    if names and len(row_candidates) == len(names) and (len({token['text'] for token in names}) == len(names)):
                        result['record_names'] = [token['text'] for token in names]
                        result['record_rows'] = [dict(name=token['text'], position=[token['bbox'][1], token['bbox'][3]]) for token in names]
                    else:
                        result['record_names'] = None
                matches = [t for t in rows if t['text'] == target]
                if len(matches) == 1:
                    action('select_record', matches[0])
                elif not matches:
                    result['actions']['scroll_records'] = dict(swipe=[400, 840, 400, 580, 500])
                    result['actions']['scroll_records_up'] = dict(swipe=[400, 580, 400, 840, 500])
                if len(matches) == 1 and target_visual is not None:
                    result['actions'] = {'select_record': dict(point=target_visual['point'])}
    elif (layout := _password_layout(tokens)):
        result['page'] = 'password'
        account_box, password_box = layout
        bottom = account_box[1] - 30
        account = find('请输入账号/邮箱号/手机号', account_box)
        password = find('请输入密码', password_box)
        ocr_username_matches = bool(username and any((t['text'] == username and within(t, account_box) for t in good)))
        result['username_matches'] = ocr_username_matches or bool(username and native_username_matched)
        if result['username_matches'] and native_username_matched and (not ocr_username_matches):
            result['username_match_source'] = 'native_exact'
        action('fill_username', account)
        if result['username_matches']:
            action('fill_password', password)
            login = find('登录', (200, bottom + 330, 520, bottom + 480))
            if login:
                _, y0, _, y1 = login['bbox']
                hsv = cv2.cvtColor(image[(y0 + y1) // 2 - 5:(y0 + y1) // 2 + 5, 150:180], cv2.COLOR_BGR2HSV)
                if float((hsv[:, :, 1] > 100).mean()) > 0.9:
                    action('login_password', login)
    elif find('账号密码', (200, 600, 530, 900)) and any(('已阅读并同意' in t['text'] for t in good)):
        result['page'] = 'terms'
        privacy = find('隐私政策', (250, 780, 500, 950))
        agreement = [t for t in good if '已阅读并同意' in t['text'] and '用户使用协议' in t['text']]
        if privacy and len(agreement) == 1:
            y = (agreement[0]['bbox'][1] + agreement[0]['bbox'][3]) // 2
            patch = image[y - 18:y + 18, 95:130]
            hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
            pink = cv2.inRange(hsv, np.array([150, 100, 150]), np.array([179, 255, 255]))
            if (pink > 0).sum() > 150:
                label = find('账号密码', (200, 600, 530, 900))
                if label:
                    top = label['bbox'][1] - 140
                    icon = image[top:label['bbox'][1], 250:470]
                    mask = cv2.inRange(cv2.cvtColor(icon, cv2.COLOR_BGR2HSV), np.array([150, 100, 150]), np.array([179, 255, 255]))
                    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    circles = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) > 3000 and 0.8 < cv2.boundingRect(c)[2] / cv2.boundingRect(c)[3] < 1.2]
                    if len(circles) == 1:
                        x, cy, w, h = circles[0]
                        result['actions']['open_password'] = dict(point=[250 + x + w // 2, top + cy + h // 2])
            else:
                circles = cv2.HoughCircles(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), cv2.HOUGH_GRADIENT, 1, 20, param1=80, param2=12, minRadius=8, maxRadius=15)
                if circles is not None and len(circles[0]) == 1:
                    x, cy, _ = circles[0][0]
                    result['actions']['agree'] = dict(point=[95 + int(x), y - 18 + int(cy)])
    elif find('解锁角色剧情', (180, 20, 540, 120)) and len([t for t in good if t['text'] in {'现可解锁角色剧情', '现可解锁多个剧情', '角色剧情已解锁'} and within(t, (180, 1040, 540, 1140))]) == 1 and find('关闭', (200, 1140, 520, 1250)):
        result['page'] = 'story_unlock'
        action('close_story', find('关闭', (200, 1140, 520, 1250)))
    elif find('解锁剧情', (180, 20, 540, 120)) and find('特别剧情已解锁', (180, 1040, 540, 1140)) and find('关闭', (200, 1140, 520, 1250)):
        result['page'] = 'story_unlock'
        action('close_story', find('关闭', (200, 1140, 520, 1250)))
    elif find('解锁角色剧情', (180, 300, 540, 500)) and find('现可解锁角色剧情', (180, 650, 540, 760)) and find('关闭', (200, 780, 520, 900)):
        result['page'] = 'story_unlock'
        action('close_story', find('关闭', (200, 780, 520, 900)))
    elif find('解锁主线剧情', (180, 300, 540, 500)) and find('主线剧情已解锁', (180, 650, 540, 760)) and find('关闭', (200, 780, 520, 900)):
        result['page'] = 'story_unlock'
        action('close_story', find('关闭', (200, 780, 520, 900)))
    elif find('公告', (200, 20, 520, 120)) and find('关闭', (200, 1100, 520, 1250)):
        result['page'] = 'notice'
        action('close_notice', find('关闭', (200, 1100, 520, 1250)))
    elif len([t for t in good if t['text'].endswith('通知') and within(t, (150, 300, 600, 500))]) == 1 and find('关闭', (100, 780, 280, 900)) and (len([t for t in good if t['text'].startswith('前往') and within(t, (400, 780, 640, 900))]) == 1):
        result['page'] = 'notice'
        action('close_notice', find('关闭', (100, 780, 280, 900)))
    elif find('菜单', (250, 20, 470, 120)) and find('标题页面', (440, 950, 690, 1150)) and find('关闭', (200, 1100, 520, 1250)):
        result['page'] = 'menu'
        action('return_title', find('标题页面', (440, 950, 690, 1150)))
    elif (ui := PageIdentity(image)).home(tokens):
        result['page'] = 'home'
        action('open_menu', find('菜单', (580, 25, 715, 105)) or ui.find('菜单'))
    elif find('手纲小姐的建议', (200, 500, 520, 600)) and len([t for t in good if re.fullmatch('(?:加载中|正在加载(?:中)?)[.．。…⋯·•]*', re.sub('\\s+', '', t['text'])) and within(t, (470, 1180, 690, 1270))]) == 1:
        result.update(page='loading', game_loading=True)
    elif any((t['text'] == '连接中' or '正在启动游戏' in t['text'] for t in good)):
        result['page'] = 'loading'
    elif find('跳过', (570, 1050, 720, 1280)):
        result['page'] = 'promotion'
        action('skip_promotion', find('跳过', (570, 1050, 720, 1280)))
    if result['page'] in {'unknown', 'loading'}:
        from core.title_screen import title_page, title_logo
        title = title_page(image, tokens)
        if title:
            result.update(page='title', title_page_verified=True, actions={'enter_game': {'point': title['point']}})
        elif (title := title_logo(image, tokens)):
            result.update(page='title', title_logo_error=title['error'], actions={'enter_game': {'point': title['point']}})
    task_surface = PageIdentity(image).story_surface()
    if result['page'] == 'unknown' and (not task_surface):
        from core.passive_popup import detect
        passive = detect(image, tokens)
        if passive:
            result.update(page='notice', passive_popup=passive['kind'])
            result['actions']['close_notice'] = dict(point=passive['point'], text=passive['button'])
    if result['page'] == 'unknown' and (not task_surface):
        dismiss = [t for t in good if re.sub('\\s+', '', t['text']) in {'关闭', '取消'} and within(t, (60, 500, 660, 1250))]
        if len(dismiss) == 1:
            token = dismiss[0]
            x0, y0, x1, y1 = token['bbox']
            cx, cy = ((x0 + x1) // 2, (y0 + y1) // 2)
            patch = cv2.cvtColor(image[cy - 20:cy + 20, cx - 80:cx + 80], cv2.COLOR_BGR2HSV)
            if patch.size and float(((patch[:, :, 1] < 65) & (patch[:, :, 2] > 175)).mean()) > 0.65 and PageIdentity(image).dialog_above(cy):
                result['page'] = 'notice'
                action('close_notice', token)
    if result['page'] in {'unknown', 'promotion'} and (not result['game_header_detected']) and (not task_surface):
        point = continue_point(image)
        if point is not None:
            result['page'] = 'promotion'
            result['visual_skip'] = True
            result['actions']['skip_promotion'] = dict(point=point)
    if result['page'] == 'promotion':
        from core.passive_popup import RISK, CONTROL
        words = [re.sub('\\s+', '', t['text']) for t in tokens if t.get('confidence', 0) >= 0.8]
        footer = {re.sub('\\s+', '', t['text']) for t in tokens if t.get('confidence', 0) >= 0.9 and t['bbox'][1] >= 1180}
        navigation_surface = '主页' in footer and len(footer & {'强化编成', '剧情', '招募', '赛事'}) >= 2
        risk_words = [re.sub('^登[录陆](?=奖)', '', re.sub('\\s+', '', t['text'])) if t['bbox'][1] < 180 else re.sub('\\s+', '', t['text']) for t in tokens if t.get('confidence', 0) >= 0.8]
        reviewed = result.get('visual_skip') is True and set(result['actions']) == {'skip_promotion'} and (not navigation_surface) and (not RISK.search(''.join(risk_words))) and (not any((word in CONTROL for word in words))) and (not re.search('验证|实名|密码错误|账号或密码|登录失败', ''.join(words)))
        result['promotion_skip_reviewed'] = reviewed
        if not reviewed:
            result['actions'] = {}
    return result

def observe_promotion_fast(raw, image, ocr, target='', username=''):
    """Local circle/color/glyph first, with a SAME-frame global safety scan.

    Advertisement text is not identity. The smaller global scan still covers
    SDK/auth/resource roles outside the button ROI; any other recognized role
    returns to normal full-resolution perception instead of gaining a Skip.
    """
    if image is None or image.shape[:2] != (1280, 720) or continue_point(image) is None:
        return None
    if PageIdentity(image).story_surface():
        return None
    scale = 0.75
    rows, _ = ocr(cv2.resize(image, (540, 960)))
    tokens = [dict(text=text, confidence=float(score), bbox=[round(min((p[0] for p in box)) / scale), round(min((p[1] for p in box)) / scale), round(max((p[0] for p in box)) / scale), round(max((p[1] for p in box)) / scale)]) for box, text, score in rows or []]
    result = observe(raw, tokens, target, username)
    if result.get('page') == 'loading' and result.get('loading_phase') == 'home_overlay':
        return result
    if result['page'] == 'promotion' and result.get('promotion_skip_reviewed') is True:
        result['promotion_global_risk_scanned'] = True
        return result
    return None

def observe_notice_fast(raw, image, ocr, target='', username=''):
    """Known passive notice titles only; SDK and unknown popups keep full OCR."""
    from core.ocr_regions import read_regions
    if image.shape[:2] != (1280, 720):
        return None
    tokens = read_regions(image, ocr, [(150, 20, 590, 120), (150, 290, 590, 370), (60, 780, 660, 980), (200, 1100, 520, 1250)])
    titles = {t['text'].replace(' ', '') for t in tokens if t['confidence'] >= 0.95}
    if not titles.intersection({'公告', '商品追加'}):
        return None
    result = observe(raw, tokens, target, username)
    return result if result['page'] == 'notice' and 'close_notice' in result['actions'] else None

def redact(raw, page):
    if page not in {'home', 'menu', 'recent', 'records', 'terms', 'password'}:
        return None
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if page in {'recent', 'records', 'terms', 'password'}:
        image[250:1050, 20:700] = 0
    return image
