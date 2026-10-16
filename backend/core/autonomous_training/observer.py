"""Pure, region-anchored autonomous-training page observation."""
import hashlib
import re
import cv2
import numpy as np
from core.page_identity import PageIdentity
from core.running_menu import detect as detect_running_menu
from core.skill_scroll import SKILL_OVERLAP_SWIPE
from core.autonomous_training.harvest_observer import observe_harvest, carrot_quantity_box, _control_enabled
from core.jjc.formation import observe_formation

def skill_available_badge(image):
    """Only inspect a clear native-size skill button; uncertain is not absent."""
    if image is None or image.shape[:2] != (1280, 720):
        return None
    button = cv2.cvtColor(image[1050:1105, 100:320], cv2.COLOR_BGR2HSV)
    if float(((button[:, :, 0] >= 80) & (button[:, :, 0] <= 105) & (button[:, :, 1] > 90) & (button[:, :, 2] > 200)).mean()) < 0.25:
        return None
    badge = cv2.cvtColor(image[988:1038, 307:357], cv2.COLOR_BGR2HSV)
    pixels = int(((badge[:, :, 0] >= 155) & (badge[:, :, 0] <= 179) & (badge[:, :, 1] > 140) & (badge[:, :, 2] > 190)).sum())
    return True if pixels >= 180 else False if pixels <= 3 else None

def read_tokens(image, ocr):
    """Read OCR tokens in native 720x1280 coordinates."""
    rows, _ = ocr(image, use_cls=False)

    def tokens_from(rows, offset=(0, 0), scale=1):
        return [dict(text=text, confidence=float(confidence), bbox=[int(min((point[0] for point in box)) / scale + offset[0]), int(min((point[1] for point in box)) / scale + offset[1]), int(max((point[0] for point in box)) / scale + offset[0]), int(max((point[1] for point in box)) / scale + offset[1])]) for box, text, confidence in rows or []]
    tokens = tokens_from(rows)
    if any((t['text'].replace(' ', '') == '我的参赛计划' and t['bbox'][1] < 120 for t in tokens)):
        for label in list(tokens):
            if label['text'].replace(' ', '') == '预约数' and 400 < label['bbox'][1] < 1080:
                y = label['bbox'][1]
                crop = cv2.resize(image[y - 4:y + 34, 150:202], None, fx=4, fy=4)
                direct, _ = ocr(crop, use_det=False, use_cls=False)
                if direct and len(direct) == 1 and str(direct[0][0]).isdigit() and (direct[0][1] >= 0.9):
                    tokens.append(dict(text=str(direct[0][0]), confidence=float(direct[0][1]), bbox=[150, y, 202, y + 30], source='plan_slot_count'))
    finished = any((token['confidence'] >= 0.9 and token['text'].replace(' ', '') == '养成结束确认' for token in tokens))
    if not finished and image.shape[:2] == (1280, 720):
        labels = {token['text'].replace(' ', '') for token in tokens if token['confidence'] >= 0.9}
        if '选择养成优俊少女' in labels and (not any((t['confidence'] >= 0.9 and 0 <= t['bbox'][0] < 400 and (375 <= t['bbox'][1] < 416) and (t['bbox'][3] <= 420) for t in tokens))):
            crop = cv2.resize(image[387:417, 35:325], None, fx=3, fy=3)
            direct, _ = ocr(crop, use_det=False, use_cls=False)
            if direct and len(direct) == 1 and (float(direct[0][1]) >= 0.9) and str(direct[0][0]).strip():
                tokens.insert(0, dict(text=direct[0][0], confidence=float(direct[0][1]), bbox=[35, 387, 325, 417], source='selected_outfit_title'))
        if {'战绩', '合计获得道具'} <= labels:
            box = carrot_quantity_box(image)
            if box is not None:
                x0, y0, x1, y1 = box
                hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
                quantity = 255 - cv2.inRange(hsv, np.array([0, 80, 40]), np.array([30, 255, 180]))
                direct, _ = ocr(cv2.resize(quantity, None, fx=4, fy=4), use_det=False, use_cls=False)
                if direct and len(direct) == 1 and (float(direct[0][1]) >= 0.9) and re.fullmatch('[xX×]\\d+', direct[0][0]):
                    tokens.append(dict(text=direct[0][0], confidence=float(direct[0][1]), bbox=box, source='history_carrot_quantity'))
        if '自主训练养成' in labels and any((t['text'] == '剩余时间' and t['confidence'] >= 0.9 and (1080 <= t['bbox'][1]) and (t['bbox'][3] <= 1155) for t in tokens)) and (not any((t['confidence'] >= 0.9 and 1080 <= t['bbox'][1] < 1150 and re.search('\\d+:[0-5]\\d(?:[：:]|分)[0-5]\\d$', t['text'].replace(' ', '')) for t in tokens))):
            crop = cv2.resize(image[1097:1128, 390:476], None, fx=4, fy=4)
            direct, _ = ocr(crop, use_det=False, use_cls=False)
            if direct and len(direct) == 1 and (float(direct[0][1]) >= 0.95) and re.fullmatch('\\d{1,2}:[0-5]\\d:[0-5]\\d', direct[0][0]):
                tokens.append(dict(text=direct[0][0], confidence=float(direct[0][1]), bbox=[390, 1097, 476, 1128], source='running_countdown'))
        if '团队编成' in labels and '主页' not in labels:
            crop = cv2.resize(image[1240:1270, 320:393], None, fx=4, fy=4)
            direct, _ = ocr(crop, use_det=False, use_cls=False)
            if direct and len(direct) == 1 and (direct[0][0].replace(' ', '') == '主页') and (float(direct[0][1]) >= 0.95):
                tokens.append(dict(text='主页', confidence=float(direct[0][1]), bbox=[320, 1240, 393, 1270]))
        if {'最终确认', '是否消耗金币、训练值开始养成？'} <= labels:
            crop = cv2.resize(image[1080:1135, 325:505], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            local_rows, _ = ocr(crop, use_cls=False)
            local = tokens_from(local_rows, (325, 1080), 2)
            if len(local) == 2 and all((t['confidence'] >= 0.95 and t['text'].isdigit() for t in local)):
                tokens.extend(({**t, 'source': 'final_tp_pair'} for t in local))
        number_seen = any((re.fullmatch('\\d+', token['text']) and 510 <= token['bbox'][0] and (390 <= token['bbox'][1]) and (token['bbox'][2] <= 690) and (token['bbox'][3] <= 455) for token in tokens))
        if {'技能获取', '当前技能点数'} <= labels and (not number_seen):
            crop = cv2.resize(image[390:450, 520:690], None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            direct, _ = ocr(crop, use_det=False, use_cls=False)
            if direct and len(direct) == 1 and (float(direct[0][1]) >= 0.95) and re.fullmatch('\\d{1,5}', direct[0][0]):
                tokens.append(dict(text=direct[0][0], confidence=float(direct[0][1]), bbox=[520, 390, 690, 450]))
            else:
                local_rows, _ = ocr(crop)
                local = tokens_from(local_rows, (520, 390), 3)
                if len(local) == 1 and local[0]['confidence'] >= 0.95 and re.fullmatch('\\d{1,5}', local[0]['text']):
                    tokens.extend(local)
    if not finished or image.shape[:2] != (1280, 720):
        return tokens
    raw = cv2.imencode('.png', image)[1].tobytes()
    parsed = observe(raw, tokens)
    if parsed['page'] == 'finish_confirm':
        if parsed.get('current_skill_points') is None:
            from pathlib import Path
            zero = cv2.imread(str(Path(__file__).parent / 'assets/finish_zero.png'))
            region = image[735:795, 400:505]
            if zero is not None and zero.shape == region.shape and (float(cv2.matchTemplate(region, zero, cv2.TM_CCOEFF_NORMED)[0, 0]) >= 0.99) and (float(cv2.absdiff(region, zero).mean()) < 2):
                tokens.append(dict(text='0', confidence=1.0, bbox=[400, 735, 505, 795], source='verified_zero_template'))
                return tokens
            crop = cv2.resize(image[735:795, 400:505], None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
            local_rows, _ = ocr(crop)
            local = tokens_from(local_rows, (400, 735), 4)
            if len(local) == 1 and local[0]['confidence'] >= 0.95 and re.fullmatch('\\d{1,5}', local[0]['text']):
                tokens.extend(local)
        return tokens
    for field, (x0, y0, x1, y1) in (('name', (30, 761, 195, 800)), ('finish_training', (400, 1050, 650, 1110)), ('领跑', (460, 710, 580, 757)), ('跟前', (585, 715, 700, 755)), ('居中', (475, 755, 580, 795)), ('后追', (585, 755, 700, 795)), ('长距离', (580, 670, 700, 707)), ('points', (235, 1075, 291, 1110))):
        summary = parsed.get('finish_summary') or {}
        styles = summary.get('run_styles') or {}
        complete = bool(summary.get('name')) if field == 'name' else field in parsed['actions'] if field == 'finish_training' else parsed.get('current_skill_points') is not None if field == 'points' else bool(summary.get('long_distance')) if field == '长距离' else bool(styles.get(field))
        if complete:
            continue
        enlarged = cv2.resize(image[y0:y1, x0:x1], None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        direct, _ = ocr(enlarged, use_det=False, use_cls=False)
        pattern = '[\\u4e00-\\u9fff·]{2,12}' if field == 'name' else '养成结束！?' if field == 'finish_training' else '\\d{1,5}' if field == 'points' else re.escape(field) + '[SABCDEFG]'
        if direct and len(direct) == 1 and (float(direct[0][1]) >= 0.95) and re.fullmatch(pattern, direct[0][0].replace(' ', '')):
            local_tokens = [dict(text=direct[0][0], confidence=float(direct[0][1]), bbox=[x0, y0, x1, y1])]
        else:
            local_rows, _ = ocr(enlarged)
            local_tokens = tokens_from(local_rows, (x0, y0), 4)
            if field == 'points':
                local_tokens = [local for local in local_tokens if re.fullmatch('\\d{1,5}', local['text'])]
        for local in local_tokens:
            left, top, right, bottom = local['bbox']
            duplicate = next((index for index, token in enumerate(tokens) if token['text'].replace(' ', '') == local['text'].replace(' ', '') and min(right, token['bbox'][2]) > max(left, token['bbox'][0]) and (min(bottom, token['bbox'][3]) > max(top, token['bbox'][1]))), None)
            if duplicate is None:
                tokens.append(local)
            elif local['confidence'] > tokens[duplicate]['confidence']:
                tokens[duplicate] = local
        parsed = observe(raw, tokens)
    return tokens

def read_start_notice_tokens(image, ocr):
    """Read the start modal without detecting text across its dimmed background."""
    if image.shape[:2] != (1280, 720):
        return read_tokens(image, ocr)
    x0, y0, x1, y1 = (100, 360, 700, 915)
    rows, _ = ocr(image[y0:y1, x0:x1], use_cls=False)
    return [dict(text=text, confidence=float(confidence), bbox=[int(min((point[0] for point in box)) + x0), int(min((point[1] for point in box)) + y0), int(max((point[0] for point in box)) + x0), int(max((point[1] for point in box)) + y0)]) for box, text, confidence in rows or []]

def read_skill_scroll_tokens(image, ocr):
    """Read only page anchors for read-only skill scrolling, never purchase rows."""
    from core.skill_acquisition_parser import _rapidocr_ocr_data
    from core.skill_ocr_performance import active_harvest_ocr
    if active_harvest_ocr() is None or image.shape[:2] != (1280, 720):
        return read_tokens(image, ocr)
    track = cv2.cvtColor(image[480:1026, 697:704], cv2.COLOR_BGR2GRAY)
    if float((track < 180).mean()) > 0.8:
        return read_tokens(image, ocr)
    tokens = []
    for index, (x0, y0, x1, y1) in enumerate(((0, 0, 200, 60), (270, 390, 700, 455))):
        for item in _rapidocr_ocr_data(image[y0:y1, x0:x1], upscale=2):
            box = item.get('box')
            if not box:
                continue
            tokens.append(dict(text=item['text'], confidence=item['confidence'], bbox=[int(min((point[0] for point in box)) / 2 + x0), int(min((point[1] for point in box)) / 2 + y0), int(max((point[0] for point in box)) / 2 + x0), int(max((point[1] for point in box)) / 2 + y0)]))
        if index == 0 and (not any((token['text'].replace(' ', '') == '技能获取' and token['confidence'] >= 0.98 for token in tokens))):
            return read_tokens(image, ocr)
    if not any((re.fullmatch('\\d+', token['text']) for token in tokens)):
        for item in _rapidocr_ocr_data(image[390:450, 520:690], upscale=3, force_detection=True):
            box = item.get('box')
            if box and re.fullmatch('\\d{1,5}', item['text']) and (item['confidence'] >= 0.95):
                tokens.append(dict(text=item['text'], confidence=item['confidence'], bbox=[int(min((point[0] for point in box)) / 3 + 520), int(min((point[1] for point in box)) / 3 + 390), int(max((point[0] for point in box)) / 3 + 520), int(max((point[1] for point in box)) / 3 + 390)]))
    return tokens

def observe(raw, tokens, required_variant=None):
    result = dict(page='unknown', actions={}, frame_sha256=hashlib.sha256(raw).hexdigest(), remaining_seconds=None, course_rows=[], course_name=None, selected_course_status=None, exam_selection_applied=None, inheritance_full=None, support_full=None, borrow_checked=None, autonomous_selected=None, tp_before=None, tp_after=None, tp_cost=None, skill_list_at_bottom=None, skill_list_at_top=None, autonomous_active=None, finish_summary=None, current_skill_points=None, skill_list_navigation_verified=False)
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return result
    labels = {token.get('text', '').replace(' ', '') for token in tokens if token.get('confidence', 0) >= 0.9}
    if '关闭应用' in labels and '等待' in labels and any(('没有响应' in label for label in labels)):
        result['page'] = 'application_not_responding'
        return result
    for recognizer in (observe_harvest, observe_formation):
        settlement = recognizer(raw, tokens)
        if settlement is not None:
            result.update(settlement)
            return result
    good = [dict(token, text=token['text'].replace(' ', '')) for token in tokens if token.get('confidence', 0) >= 0.9]

    def within(token, box):
        x0, y0, x1, y1 = box
        left, top, right, bottom = token['bbox']
        return x0 <= left and y0 <= top and (right <= x1) and (bottom <= y1)

    def find(text, box=(0, 0, 720, 1280)):
        matches = [token for token in good if token['text'] == text and within(token, box)]
        return matches[0] if len(matches) == 1 else None

    def has(text, box=(0, 0, 720, 1280)):
        return any((text in token['text'] and within(token, box) for token in good))

    def action(name, token):
        if token:
            x0, y0, x1, y1 = token['bbox']
            result['actions'][name] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])

    def masked_course_row(y):
        gray = cv2.cvtColor(image[max(0, y - 70):min(1280, y + 70), 20:690], cv2.COLOR_BGR2GRAY)
        return float((gray < 25).mean()) > 0.3

    def green_fraction(box):
        x0, y0, x1, y1 = box
        hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        return float(((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 110)).mean())

    def cards_full():
        slots = ((50, 255, 220, 525), (270, 255, 450, 525), (490, 255, 670, 525), (50, 545, 220, 795), (270, 545, 450, 795), (490, 545, 670, 795))
        return all((green_fraction(slot) > 0.03 or cv2.cvtColor(image[slot[1]:slot[3], slot[0]:slot[2]], cv2.COLOR_BGR2HSV)[:, :, 1].mean() > 45 for slot in slots))

    def inheritance_full():
        slots = ((45, 610, 350, 920), (370, 610, 675, 920))
        if any((find('请选择', slot) for slot in slots)):
            return False
        return all((cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)[:, :, 1].mean() > 20 for x0, y0, x1, y1 in slots))

    def checked(box):
        x0, y0, x1, y1 = box
        patch = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        return float((patch < 190).mean()) > 0.12

    def menu_icon_point():
        found = detect_running_menu(image)
        return found['point'] if found else None

    def seconds():
        matches = []
        for token in good:
            match = re.fullmatch('(?:还有)?(\\d+):(\\d\\d):(\\d\\d)', token['text'])
            chinese = re.fullmatch('还有(\\d+):(\\d\\d)分(\\d\\d)', token['text'])
            marked = re.fullmatch('[①D]还有(\\d+):(\\d\\d)[：:](\\d\\d)', token['text']) if token['text'].startswith('①') or within(token, (300, 1080, 500, 1150)) else None
            if match or chinese or marked:
                hours, minutes, value = map(int, (match or chinese or marked).groups())
                matches.append(hours * 3600 + minutes * 60 + value)
        return matches[0] if len(matches) == 1 else None

    def aptitude(label):
        label_token = find(label)
        grades = []
        if label_token is not None:
            left, top, right, bottom = label_token['bbox']
            grades = [token['text'] for token in good if len(token['text']) == 1 and token['text'] in 'SABCDEFG' and (right <= token['bbox'][0] <= right + 100) and (abs(token['bbox'][1] - top) <= 25)]
        merged = []
        if label in ('领跑', '跟前', '居中', '后追', '长距离'):
            for token in good:
                if within(token, (580, 670, 700, 707) if label == '长距离' else (460, 710, 700, 810)):
                    match = re.fullmatch(f'{re.escape(label)}\\s*([SABCDEFG])', token['text'])
                    if match:
                        merged.append(match.group(1))
        if len(merged) > 1 or len(grades) > 1:
            return None
        if merged and grades and (merged[0] != grades[0]):
            return None
        if merged:
            return merged[0]
        return grades[0] if len(grades) == 1 else None
    from core.session_recovery import disconnect_button
    disconnected = disconnect_button(image, tokens)
    if disconnected:
        result.update(page='connection_interrupted', actions={'return_title_after_disconnect': disconnected})
        return result
    running_menu = menu_icon_point()
    tutorial_button = find('继续', (220, 1140, 500, 1260)) or find('关闭', (220, 1140, 500, 1260))
    if tutorial_button and (has('自主训练养成', (20, 970, 700, 1120)) or has('自主训练期间', (20, 970, 700, 1120))):
        result['page'] = 'autonomous_tutorial'
        action('continue_autonomous_tutorial', tutorial_button)
    elif find('连接已断开', (200, 370, 520, 460)) and find('由于长时间内未进行操作，', (150, 560, 570, 630)) and find('或与服务器的连接发生中断，', (150, 610, 570, 670)) and find('即将返回游戏标题页面', (150, 640, 570, 710)):
        result['page'] = 'connection_interrupted'
    elif has('自主训练养成') and seconds() is not None and (running_menu is not None or any((t.get('confidence', 0) >= 0.75 and t['text'].replace(' ', '').startswith('自主训练中') and within(t, (180, 1180, 570, 1280)) for t in tokens))):
        result.update(page='autonomous_running', autonomous_active=True, remaining_seconds=seconds())
        if running_menu is not None:
            result['actions']['open_training_menu'] = dict(point=running_menu)
    elif find('自主训练养成开始确认', (120, 370, 620, 460)):
        result['page'] = 'autonomous_start_notice'
        confirm = find('确定', (350, 780, 700, 900))
        if has('要开始自主训练养成吗', (120, 510, 620, 575)) and has('无法切换为手动养成', (100, 570, 650, 640)):
            if confirm and _control_enabled(image, confirm):
                action('confirm_autonomous_start', confirm)
            cancel = find('取消', (70, 780, 350, 900))
            if cancel and _control_enabled(image, cancel):
                action('cancel_autonomous_start', cancel)
    elif has('自动编成确认'):
        result['page'] = 'auto_support_confirm'
        action('confirm_auto_support', find('确定', (380, 760, 690, 980)))
    elif has('自动选择确认'):
        result['page'] = 'inherit_confirm'
        borrow = find('包含借用', (100, 620, 600, 780))
        if borrow:
            left, top, _, bottom = borrow['bbox']
            center_y = (top + bottom) // 2
            result['borrow_checked'] = checked((left - 66, center_y - 31, left + 9, center_y + 39))
            if not result['borrow_checked']:
                result['actions']['toggle_borrow'] = dict(point=[left - 38, center_y])
            action('confirm_inheritance', find('确定', (380, 760, 690, 980)))
    elif has('新剧本'):
        result['page'] = 'new_scenario'
        action('close_notice', find('关闭', (200, 820, 520, 1120)))
    elif has('自主训练养成') and (has('剩余时间') or has('前往养成')):
        result.update(page='training_status', remaining_seconds=seconds())
        action('forward_harvest', find('前往养成', (350, 750, 700, 980)))
        action('cancel_training_status', find('取消', (20, 750, 350, 980)))
    elif has('养成结束确认'):
        result['page'] = 'finish_ready'
        points = [int(match.group(1)) for token in good if within(token, (90, 1040, 350, 1130)) for match in [re.fullmatch('技能点数(\\d+)', token['text'])] if match]
        label = find('技能点数', (90, 1040, 350, 1130))
        if label:
            points.extend((int(token['text']) for token in good if re.fullmatch('\\d+', token['text']) and within(token, (90, 1040, 350, 1130)) and (-12 <= token['bbox'][0] - label['bbox'][2] <= (45 if len(token['text']) <= 2 and within(token, (240, 1075, 285, 1110)) else 25)) and (abs(sum(token['bbox'][1::2]) - sum(label['bbox'][1::2])) <= 12)))
        if len(points) == 1:
            result['current_skill_points'] = points[0]
        result['finish_summary'] = dict(name=next((token['text'] for token in good if token['bbox'][0] < 200 and 761 <= token['bbox'][1] <= 810 and re.fullmatch('[\\u4e00-\\u9fff·]{2,12}', token['text'])), None), long_distance=aptitude('长距离'), run_styles={label: aptitude(label) for label in ('领跑', '跟前', '居中', '后追')})
        action('open_skills', find('技能', (40, 900, 350, 1220)))
        result['skill_available_badge'] = skill_available_badge(image) if 'open_skills' in result['actions'] else None
        finish_controls = [token for token in good if token['text'] in {'养成结束！', '养成结束!', '养成结束'} and within(token, (0, 900, 720, 1280))]
        if len(finish_controls) == 1 and _control_enabled(image, finish_controls[0]):
            finish = finish_controls[0]
            action('finish_training', finish)
            result['actions']['finish_training'].update(text=finish['text'], bbox=list(finish['bbox']), enabled=True)
    elif has('技能获取确认'):
        result['page'] = 'skill_confirmation'
        cancel = [token for token in good if token['text'] == '取消' and within(token, (60, 1135, 350, 1225))]
        if find('技能获取确认', (120, 25, 610, 100)) and len(cancel) == 1:
            action('cancel_skill_confirmation', cancel[0])
            result['actions']['scroll_skill_confirmation'] = dict(gesture='swipe', point=[360, 980], end=[360, 490])
            result['actions']['rewind_skill_confirmation'] = dict(gesture='swipe', point=[360, 490], end=[360, 980])
            track = cv2.cvtColor(image[115:1038, 694:698], cv2.COLOR_BGR2GRAY).mean(axis=1)
            thumb = np.flatnonzero(track < 180)
            result['skill_confirmation_at_top'] = bool(len(thumb) and thumb[0] <= 4)
    elif find('技能获取', (0, 0, 300, 100)):
        result['page'] = 'skills'
        result['skill_list_navigation_verified'] = True
        if find('技能获取', (0, 0, 200, 60)) and find('当前技能点数', (270, 390, 480, 455)):
            points = [token for token in good if re.fullmatch('\\d+', token['text']) and within(token, (510, 390, 690, 455))]
            if len(points) == 1:
                result['current_skill_points'] = int(points[0]['text'])
                action('reset_skill_selection', find('重置', (535, 1040, 710, 1130)))
                if find('确定', (210, 1040, 510, 1125)):
                    from core.skill_purchase_live_adapter import _enabled_skill_green_confirm_control
                    control = _enabled_skill_green_confirm_control(image)
                    if control:
                        result['actions']['open_skill_confirmation'] = dict(point=[control['x'], control['y']])
        back = find('返回', (10, 1120, 160, 1230))
        if back:
            action('return_harvest_skills', back)
        scrollbar = cv2.cvtColor(image[480:1026, 697:704], cv2.COLOR_BGR2GRAY).mean(axis=1)
        thumb_rows = np.flatnonzero(scrollbar < 180)
        groups = np.split(thumb_rows, np.where(np.diff(thumb_rows) > 1)[0] + 1) if len(thumb_rows) else []
        groups = [group for group in groups if len(group) >= 20]
        if len(groups) == 1:
            thumb = groups[0]
            result['skill_list_at_top'] = bool(thumb[0] <= 10)
            result['skill_list_at_bottom'] = bool(thumb[-1] >= len(scrollbar) - 18)
        if result['skill_list_at_bottom'] is False or result['skill_list_at_bottom'] is None:
            result['actions']['scroll_skill_overlap'] = dict(gesture='swipe', point=list(SKILL_OVERLAP_SWIPE['start']), end=list(SKILL_OVERLAP_SWIPE['end']))
            result['actions']['scroll_skill_list'] = dict(gesture='swipe', point=[360, 900], end=[360, 610])
            result['actions']['scroll_skill_reveal_bottom'] = dict(gesture='swipe', point=[360, 900], end=[360, 760])
        if result['skill_list_at_top'] is False or result['skill_list_at_top'] is None:
            result['actions']['scroll_skill_up'] = dict(gesture='swipe', point=[360, 610], end=[360, 900])
    elif has('选择考试模式') or has('选择养成模式'):
        result['page'] = 'exam_mode'
        result['course_name'] = next((token['text'] for token in good if '课程' in token['text'] and 600 <= token['bbox'][1] <= 800), None)
        action('change_exam', find('更换考试', (100, 500, 700, 900)))
        action('confirm_mode', find('确定', (350, 900, 700, 1220)))
    elif has('考试一览') or ((has('第') and has('考试')) and any((token['text'] == '未挑战' for token in good))):
        result.update(page='exam_list', course_rows=[])
        for status in [token for token in good if token['text'] == '未挑战']:
            y = (status['bbox'][1] + status['bbox'][3]) // 2
            nearby = [token for token in good if abs((token['bbox'][1] + token['bbox'][3]) // 2 - y) <= 120]
            number = next((re.search('第(\\d+)', token['text']).group(1) for token in nearby if re.search('第(\\d+)', token['text'])), None)
            if number is None:
                number = next((token['text'] for token in nearby if token['text'].isdigit() and token['bbox'][0] < 150), None)
            if number is None:
                continue
            name = next((token['text'] for token in nearby if '课程' in token['text']), '')
            locked = any(('锁定' in token['text'] or '解锁' in token['text'] for token in nearby)) or masked_course_row(y)
            row = dict(course_id=number, course_name=name, status='未挑战', locked=locked, challenged=False)
            if not locked:
                row['action_id'] = f'course_{number}'
                action(row['action_id'], status)
            result['course_rows'].append(row)
        action('close_exams', find('关闭', (250, 1100, 470, 1220)))
    elif has('考试详情'):
        result['page'] = 'exam_detail'
        course = next((token['text'] for token in good if '课程' in token['text'] and token['bbox'][1] < 200), '')
        result['course_name'] = course
        status = find('未挑战', (100, 200, 620, 340))
        selection_applied = has('已选择该项考试', (150, 1050, 600, 1140))
        selection_pending = has('是否要参加该项考试', (150, 1050, 600, 1140))
        if selection_applied:
            result.update(exam_selection_applied=True, selected_course_status=status['text'] if status else None)
            action('close_exam_detail', find('取消', (50, 1100, 350, 1220)))
        elif selection_pending:
            result['exam_selection_applied'] = False
            action('confirm_exam', find('确定', (350, 1100, 700, 1220)))
    elif has('选择继承优俊少女'):
        result.update(page='inheritance', inheritance_full=inheritance_full())
        if not result['inheritance_full']:
            action('auto_inheritance', find('自动选择', (300, 850, 700, 1050)))
        else:
            action('continue_inheritance', find('继续', (180, 950, 550, 1160)))
    elif has('协助卡编成'):
        result.update(page='support_deck', support_full=cards_full())
        action('auto_support', find('自动编成', (320, 750, 700, 1000)))
        if result['support_full']:
            next_step = find('继续', (180, 900, 700, 1160))
            if next_step is None:
                next_step = next((token for token in good if '开始养成' in token['text'] and within(token, (180, 900, 700, 1160))), None)
            action('continue_support', next_step)
    elif has('选择养成优俊少女'):
        result['page'] = 'trainee'
        if not required_variant:
            from core.autonomous_training.trainee import REQUIRED_TRAINEE, bakushin_candidate, scarlet_candidate, initial_candidate
            from core.autonomous_training.plant_catalog import TRAINEES
            names = [name for name in TRAINEES.values() if find(name, (20, 375, 400, 450))]
            result['selected_trainee_name'] = names[0] if len(names) == 1 else None
            urara = initial_candidate(image, 'urara')
            if urara and any((t['text'].replace(' ', '') == '春乌拉拉' and t['confidence'] >= 0.75 and within(t, (20, 410, 400, 450)) for t in tokens)):
                x, y = urara['point']
                hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
                mask = cv2.inRange(hsv, (35, 100, 140), (85, 255, 255))
                corners = [mask[max(640, y + a):min(1000, y + b), max(10, x + c):min(710, x + d)] for a, b in [(-65, -25), (65, 100)] for c, d in [(-70, -35), (45, 90)]]
                if all((cv2.countNonZero(corner) >= 35 for corner in corners)):
                    result['selected_trainee_name'] = TRAINEES['urara']
            selected = result['selected_trainee_name'] == REQUIRED_TRAINEE
            candidate = bakushin_candidate(image)
            if candidate and (not selected):
                result['actions']['select_bakushin'] = candidate
            elif not selected:
                result['actions']['scroll_trainees'] = dict(gesture='swipe', point=[360, 920], end=[360, 690])
            scarlet = scarlet_candidate(image)
            if scarlet and result['selected_trainee_name'] != '大和赤骥':
                result['actions']['select_scarlet'] = scarlet
            for key in ('gold_ship', 'vodka', 'urara'):
                candidate = urara if key == 'urara' else initial_candidate(image, key)
                if candidate and result['selected_trainee_name'] != TRAINEES[key]:
                    result['actions']['select_' + key] = candidate
        result['actions']['scroll_trainees'] = dict(gesture='swipe', point=[360, 920], end=[360, 690])
        result['actions']['scroll_trainees_up'] = dict(gesture='swipe', point=[360, 690], end=[360, 920])
        action('continue_trainee', find('继续', (180, 950, 550, 1160)))
        if required_variant:
            from core.autonomous_training.trainee import variant_observation
            variant = variant_observation(image, tokens, required_variant)
            result.update(selected_variant=variant['selected_variant'], selected_card_id=variant.get('selected_card_id'), selected_character_id=variant.get('selected_character_id'), selected_costume_id=variant.get('selected_costume_id'), trainee_identity_basis=variant.get('identity_basis'), selected_trainee_header=variant['selected_header'], selected_trainee_name=variant['selected_name'], trainee_list_signature=variant['signature'])
            for index, candidate in enumerate(variant['candidates']):
                result['actions']['inspect_trainee_' + str(index)] = candidate
        else:
            result['trainee_list_signature'] = cv2.resize(cv2.cvtColor(image[640:970], cv2.COLOR_BGR2GRAY), (24, 12)).flatten().tolist()
    elif has('最终确认'):
        starts = [token for token in good if within(token, (350, 1100, 700, 1240)) and re.fullmatch('(?:自主训练养成开始|开始养成)[！!]?', token['text'])]
        start = starts[0] if len(starts) == 1 else None
        autonomous = start['text'].startswith('自主训练养成开始') if start else None
        result['autonomous_selected'] = autonomous
        costs = [match for token in good if 950 <= token['bbox'][1] <= 1120 for match in [re.search('消耗(\\d+)训练值', token['text'])] if match]
        cost = costs[0] if len(costs) == 1 else None
        result['tp_cost'] = int(cost.group(1)) if cost else None
        tp_labels = [token for token in good if token['text'] == '训练值']
        tp_values = [token['text'] for label in tp_labels for token in good if token['text'].isdigit() and token['bbox'][0] >= label['bbox'][2] and (token['bbox'][0] <= label['bbox'][2] + 220) and (abs(token['bbox'][1] - label['bbox'][1]) <= 40)]
        pairs = [(int(value[:split]), int(value[split:])) for value in tp_values if re.fullmatch('\\d{4,5}', value) for split in range(1, len(value)) if result['tp_cost'] is not None and 0 <= int(value[split:]) < int(value[:split]) <= 100 and (int(value[:split]) - int(value[split:]) == result['tp_cost'])]
        if len(pairs) == 1:
            result['tp_before'], result['tp_after'] = pairs[0]
        if result['tp_cost'] is None and has('是否消耗金币、训练值开始养成？'):
            values = sorted((t for t in good if t.get('source') == 'final_tp_pair'), key=lambda t: t['bbox'][0])
            if len(values) == 2 and all((t['text'].isdigit() for t in values)):
                before, after = (int(t['text']) for t in values)
                if 0 <= after < before <= 100:
                    result.update(tp_before=before, tp_after=after, tp_cost=before - after)
        if autonomous is not True:
            result['page'] = 'final_confirm'
            if autonomous is False:
                result['actions']['select_autonomous'] = dict(point=[535, 215])
        else:
            result['page'] = 'final_autonomous'
            if start and result['tp_before'] is not None and (result['tp_after'] is not None) and (result['tp_cost'] is not None) and (result['tp_after'] == result['tp_before'] - result['tp_cost']) and _control_enabled(image, start):
                action('start_autonomous', start)
    elif has('选择剧本'):
        result['page'] = 'loading' if has('连接中') else 'scenario'
        if result['page'] == 'scenario':
            action('leave_empty_scenario', find('返回', (0, 1000, 200, 1160)))
        if result['page'] == 'scenario' and has('凯旋门'):
            action('continue_scenario', find('继续', (180, 850, 550, 1160)))
    elif has('菜单') and has('放弃'):
        result['page'] = 'training_menu'
        action('return_home', find('前往主页', (40, 480, 360, 650)))
    elif (ui := PageIdentity(image)).home(tokens):
        result['page'] = 'home'
        action('open_training', find('养成', (380, 1000, 700, 1160)) or ui.find('养成'))
    elif has('养成履历'):
        result['page'] = 'history'
        action('confirm_history', find('确定', (250, 900, 500, 1220)))
    return result
