"""Local, region-anchored navigation and selected-horse observations."""
import hashlib
import re
import cv2
import numpy as np
from core.page_identity import PageIdentity
from core.jjc.observer import read_tokens as read_page_tokens

def read_tokens(image, ocr):
    tokens = read_page_tokens(image, ocr)
    if image.shape[:2] == (1280, 720) and any(('选择参赛' in t['text'] and t['bbox'][1] < 55 for t in tokens)):
        for source, box in [('surface_grade', (548, 196, 580, 232)), ('distance_grade', (548, 242, 580, 277))]:
            x0, y0, x1, y1 = box
            rows, _ = ocr(cv2.resize(image[y0:y1, x0:x1], None, fx=4, fy=4), use_det=False)
            for text, confidence in rows or []:
                tokens.append(dict(text=text, confidence=float(confidence), bbox=list(box), source=source))
            if not any((text in 'SABCDEFG' and len(text) == 1 and (confidence >= 0.9) for text, confidence in rows or [])):
                label_box, pattern = ((475, 193, 584, 234), '(?:草地|泥地)([SABCDEFG])') if source == 'surface_grade' else ((463, 238, 584, 281), '(?:短距离|英里|中距离|长距离)([SABCDEFG])')
                x, y, right, bottom = label_box
                refined, _ = ocr(cv2.resize(image[y:bottom, x:right], None, fx=4, fy=4), use_det=False, use_cls=False)
                for text, confidence in refined or []:
                    match = re.fullmatch(pattern, text.replace(' ', ''))
                    if match and confidence >= 0.95:
                        tokens.append(dict(text=match[1], confidence=float(confidence), bbox=list(box), source=source))
        tokens = refine_scores(image, tokens, ocr)
    return tokens

def refine_scores(image, tokens, ocr):
    """Two agreeing reads of missing cell text only; never relax global OCR."""
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.9]
    if not any((t['text'] == '选择参赛优俊少女' and t['bbox'][1] < 55 for t in good)):
        return tokens
    candidates, _, _, geometry = _grid(image, good)
    if not geometry:
        return tokens
    result = list(tokens)
    for c in candidates:
        if c['score'] is not None:
            continue
        x, y = (c['point'][0], c['row_y'])
        box = [x - 45, y - 12, x + 45, y + 13]
        part = image[box[1]:box[3], box[0]:box[2]]
        first, _ = ocr(cv2.resize(part, None, fx=3, fy=3), use_det=False, use_cls=False)

        def number(reading):
            if not reading or len(reading) != 1 or float(reading[0][1]) < 0.98:
                return None
            text = re.sub('\\s+', '', reading[0][0])
            return int(text.replace(',', '')) if re.fullmatch('(?:\\d{2,6}|[1-9]\\d{0,2},\\d{3})', text) else None
        value = number(first)
        if value is None:
            continue
        grey = cv2.cvtColor(part, cv2.COLOR_BGR2GRAY)
        second, _ = ocr(cv2.resize(grey, None, fx=3, fy=3), use_det=False, use_cls=False)
        if number(second) != value:
            continue
        prior = [t for t in tokens if 0.75 <= t['confidence'] < 0.9 and abs((t['bbox'][0] + t['bbox'][2]) / 2 - x) < 45 and (abs((t['bbox'][1] + t['bbox'][3]) / 2 - y) < 12) and re.fullmatch('(?:\\d{2,6}|[1-9]\\d{0,2},\\d{3})', t['text'].replace(' ', ''))]
        if any((int(t['text'].replace(',', '').replace(' ', '')) != value for t in prior)):
            continue
        result.append(dict(text=str(value), confidence=min(float(first[0][1]), float(second[0][1])), bbox=box, source='candidate_score_double_read'))
    return result

def _green(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return (hsv[:, :, 0] >= 30) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 110) & (hsv[:, :, 2] > 100)

def radio_choice(image, centers, *, radius=9, extent=26):
    """Read the unique filled radio, relative to its own background, in any hue.

    Centers must belong to an already identified radio group. Missing or
    conflicting marks stay unknown; a requested click never supplies the state.
    """
    contrasts = []
    for index, (x, y) in enumerate(centers):
        patch = image[y - extent:y + extent + 1, x - extent:x + extent + 1]
        if patch.shape[:2] != (2 * extent + 1, 2 * extent + 1):
            return None
        corners = np.concatenate([patch[:4, :4], patch[:4, -4:], patch[-4:, :4], patch[-4:, -4:]], axis=0)
        background = np.median(corners.reshape(-1, 3), axis=0)
        center = np.median(image[y - radius:y + radius, x - radius:x + radius].reshape(-1, 3), axis=0)
        contrasts.append(float(np.max(np.abs(center - background))))
    if not contrasts or min(contrasts) > 60:
        return None
    selected = [index for index, value in enumerate(contrasts) if value > min(contrasts) + 35]
    return selected[0] if len(selected) == 1 else None

def _selection_brackets(image, x, y):
    histograms = []
    for side in (image[y - 143:y - 20, x - 70:x - 50], image[y - 143:y - 20, x + 50:x + 70]):
        background = np.percentile(side.reshape(-1, 3), 90, axis=0)
        ink = np.max(np.abs(side.astype(float) - background), axis=2) > 18
        for band, foreground in ((side[:43], ink[:43]), (side[-43:], ink[-43:])):
            colors = band.astype(np.uint16) // 32
            code = colors[:, :, 0] * 64 + colors[:, :, 1] * 8 + colors[:, :, 2]
            histograms.append(np.bincount(code[foreground], minlength=512))
    return np.minimum.reduce(histograms).sum() >= 40

def _grid(image, tokens):
    scores = [t for t in tokens if re.fullmatch('(?:\\d{2,6}|[1-9]\\d{0,2},\\d{3})', t['text']) and 640 < t['bbox'][1] < 969 and (t['bbox'][3] < 974) and (25 < (t['bbox'][0] + t['bbox'][2]) / 2 < 680)]
    ys = sorted(((t['bbox'][1] + t['bbox'][3]) / 2 for t in scores))
    rows = []
    for y in ys:
        if not rows or y - rows[-1] > 12:
            rows.append(y)
    if not rows:
        return ([], False, None, False)
    anchor = int(np.median([y for y in ys if y - rows[0] <= 12]) + 0.5)
    for y in range(anchor - 314, 974, 157):
        if y >= 787 and (not any((abs(y - other) < 12 for other in rows))):
            rows.append(y)
    candidates, selected, complete, geometry = ([], [], True, True)
    aligned_rows = {anchor + round((y - anchor) / 157) * 157 for y in rows}
    for y in sorted((y for y in aligned_rows if 787 <= y < 968)):
        row_scores = [score_y for score_y in ys if abs(score_y - y) <= 12]
        if row_scores:
            y = int(np.median(row_scores) + 0.5)
        strip = np.concatenate([image[y - 12:y + 13, x - 45:x + 45] for x in (92, 228, 364, 500, 636)], axis=1).astype(np.int16)
        blue, green, red = cv2.split(strip)
        ink = (red > 100) & (red > green * 1.3) & (green > 35) & (green < 145) & (blue < 100)
        counts = ink.sum(axis=1)
        ink_rows = np.flatnonzero(counts >= max(5, counts.max() * 0.15))
        if len(ink_rows) >= 7 and ink_rows[-1] - ink_rows[0] <= 18:
            y += int((ink_rows[0] + ink_rows[-1]) / 2 + 0.5) - 12
        for col, x in enumerate((92, 228, 364, 500, 636)):
            interior = image[y - 115:y - 40, x - 36:x + 36]
            hsv = cv2.cvtColor(interior, cv2.COLOR_BGR2HSV)
            occupied = (hsv[:, :, 1] > 55).mean() > 0.08
            matches = [t for t in scores if abs((t['bbox'][0] + t['bbox'][2]) / 2 - x) < 45 and abs((t['bbox'][1] + t['bbox'][3]) / 2 - y) < 12]
            if not occupied and (not matches):
                continue
            if not occupied or len(matches) > 1:
                geometry = False
                complete = False
                continue
            if not matches:
                complete = False
            gray = cv2.resize(cv2.cvtColor(interior, cv2.COLOR_BGR2GRAY), (9, 8))
            fingerprint = ''.join(('1' if b else '0' for b in (gray[:, 1:] > gray[:, :-1]).flat))
            candidate = dict(score=int(matches[0]['text'].replace(',', '')) if matches else None, point=[x, y - 75], column=col, row_y=y, portrait=fingerprint)
            index = len(candidates)
            candidates.append(candidate)
            if _selection_brackets(image, x, y):
                selected.append(index)
    return (candidates, complete and bool(candidates), selected[0] if len(selected) == 1 else None, geometry and bool(candidates))

def grid_ready(observed):
    return observed.get('grid_geometry_verified', observed.get('grid_complete')) is True

def same_candidate(a, b):
    left, right = (a.get('score'), b.get('score'))
    return (left is None or right is None or left == right) and a['column'] == b['column'] and (len(a['portrait']) == len(b['portrait']) == 64) and (sum((p != q for p, q in zip(a['portrait'], b['portrait']))) <= 3)

def same_list(before, after):
    a, b = (before.get('candidates', []), after.get('candidates', []))
    return grid_ready(before) and grid_ready(after) and bool(a) and (len(a) == len(b)) and any((type(x.get('score')) is int and x.get('score') == y.get('score') for x, y in zip(a, b))) and all((same_candidate(x, y) and abs(x['row_y'] - y['row_y']) <= 4 for x, y in zip(a, b)))

def observe(raw, tokens, *, daily=False):
    result = dict(page='unknown', actions={}, tokens=tokens, frame_sha256=hashlib.sha256(raw).hexdigest())
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return result
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.9]

    def inside(t, box):
        a, b, c, d = t['bbox']
        x0, y0, x1, y1 = box
        return x0 <= a and y0 <= b and (c <= x1) and (d <= y1)

    def find(text, box=(0, 0, 720, 1280)):
        found = [t for t in good if t['text'] == text and inside(t, box)]
        return found[0] if len(found) == 1 else None

    def action(name, token):
        if token:
            x0, y0, x1, y1 = token['bbox']
            result['actions'][name] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    from core.session_recovery import disconnect_button
    if disconnect_button(image, tokens):
        result.update(page='connection_interrupted', actions={})
        return result
    if any(('连接已断开' in t['text'] or ('验证' in t['text'] and t['text'] not in ('验证治愈效果', '爱丽速子②验证治愈效果')) for t in good)):
        result['page'] = 'blocked'
        return result
    if any((t['confidence'] >= 0.75 and (t['text'].replace(' ', '') == '连接中' and t['bbox'][1] < 130 or ('加载中' in t['text'] and t['bbox'][1] > 1150)) for t in tokens)):
        result['page'] = 'loading'
        return result
    if find('显示设置', (200, 20, 540, 100)) and find('显示顺序', (30, 160, 260, 240)):
        result['page'] = 'sort'
        evaluation = find('评价', (80, 240, 240, 310))
        choice = radio_choice(image, [(73, 274), (363, 274), (73, 374), (73, 474), (363, 474), (73, 574), (363, 574)])
        result['evaluation_selected'] = choice == 0 if evaluation and choice is not None else None
        action('sort_evaluation', evaluation)
        if result['evaluation_selected']:
            action('apply_sort', find('确定', (380, 1130, 670, 1230)))
    elif any(('选择参赛优俊少女' == t['text'] and inside(t, (0, 0, 280, 55)) for t in good)):
        if any((t['confidence'] >= 0.75 and inside(t, (0, 500, 720, 1030)) and (t['text'].replace(' ', '') in {'取消', '关闭', '确定'} or any((label in t['text'] for label in ('购买', '消耗宝石', '付费')))) for t in tokens)):
            return result
        result['page'] = 'selection'
        field = next((value for value in ('评价', '登记日') if find(value, (400, 960, 560, 1020))), None)
        direction = next((value for value in ('升序', '降序') if any((t['text'].startswith(value) and inside(t, (590, 960, 700, 1020)) for t in good))), None)
        filter_labels = [t for t in good if inside(t, (250, 960, 400, 1020)) and re.fullmatch('筛选[：:]关(?:[，,]?\\d+)?', t['text'])]
        filter_on = any((inside(t, (250, 960, 400, 1020)) and re.fullmatch('筛选[：:]开(?:[，,]?\\d+)?', t['text']) for t in good))
        result.update(sort_field=field, direction=direction, surface=None, distance=None, filter_off=len(filter_labels) == 1, filter_state='off' if len(filter_labels) == 1 else 'on' if filter_on else None)
        for key, label, box in [('surface', ('草地', '泥地'), (470, 190, 548, 239)), ('distance', ('短距离', '英里', '中距离', '长距离'), (463, 239, 548, 283))]:
            values = [t['text'] for t in good if t.get('source') == key + '_grade' and t['text'] in 'SABCDEFG' and (len(t['text']) == 1)]
            if any((find(value, box) for value in label)) and len(set(values)) == 1:
                result[key] = values[0]
        names = [t['text'] for t in good if inside(t, (25, 495, 245, 535))]
        result['name'] = names[0] if len(names) == 1 else None
        candidates, complete, selected, geometry = _grid(image, good)
        result.update(candidates=candidates, grid_complete=complete, selected=selected, grid_geometry_verified=geometry)
        rejected = [t for t in good if t['confidence'] >= 0.95 and inside(t, (80, 610, 640, 690)) and re.fullmatch('与传奇优俊少女重复[，,]无法选择', t['text'])]
        if len(rejected) == 1:
            result['selection_rejection'] = 'opponent_duplicate'
        bar = image[647:1007, 697:707].astype(int)
        thumb = ((bar[:, :, 0] - bar[:, :, 1] > 14) & (bar[:, :, 0] < 185) & (bar[:, :, 1] < 165) & (bar[:, :, 1] > 60)).sum(axis=1) >= 3
        result.update(at_top=bool(thumb[:12].sum() >= 6), at_bottom=bool(thumb[-12:].sum() >= 6))
        action('open_sort', find(field, (400, 960, 560, 1020)) if field else None)
        if direction == '升序':
            action('descending', next((t for t in good if t['text'].startswith('升序') and inside(t, (590, 960, 700, 1020)))))
        if geometry and field == '评价' and (direction == '降序'):
            for index, candidate in enumerate(candidates):
                result['actions']['horse_' + str(index)] = dict(point=candidate['point'])
            result['actions']['scroll_top'] = dict(swipe=[350, 760, 350, 880, 350])
            rows = sorted({candidate['row_y'] for candidate in candidates})
            row_height = min((b - a for a, b in zip(rows, rows[1:])), default=157)
            distance = max(60, min(90, row_height // 2))
            result['actions']['scroll_next'] = dict(swipe=[350, 880, 350, 880 - distance, 600])
    elif find('赛事详情', (200, 20, 550, 100)) and find('传奇赛事', (240, 120, 470, 200)):
        result['page'] = 'details'
        action('open_selection', find('参赛！', (380, 1130, 670, 1230)))
    elif find('日常传奇赛事' if daily else '传奇赛事', (0, 0, 260, 70)):
        result['page'] = 'legend'
        result['zero_tickets'] = any((re.fullmatch('0/[1-9]\\d*', t['text']) and inside(t, (570, 0, 720, 60)) for t in good))
        courses = [t for t in good if '米' in t['text'] and ('草地' in t['text'] or '泥地' in t['text']) and inside(t, (245, 580, 705, 1050))]
        if len(courses) == 1 and (not result['zero_tickets']):
            action('open_details', courses[0])
    elif daily and find('日常安排', (280, 730, 550, 815)):
        result['page'] = 'events'
        tile = find('传奇赛事', (380, 970, 630, 1050))
        if tile:
            action('open_legend', tile)
    elif find('赛事活动', (200, 500, 540, 700)):
        result['page'] = 'events'
        tile = find('传奇赛事', (50, 690, 670, 1130))
        if tile:
            x0, y0, x1, y1 = tile['bbox']
            nearby = [t['text'] for t in good if inside(t, (max(0, x0 - 80), y0 - 200, min(720, x1 + 80), y1 + 15))]
            if not any(('即将' in text or '后解锁' in text or re.fullmatch('0/\\d+', text) for text in nearby)):
                action('open_legend', tile)
    elif (ui := PageIdentity(image)).races() or (find('赛事', (20, 140, 200, 240)) and find('团队竞技场', (60, 850, 360, 980))):
        result['page'] = 'races'
        action('open_events', find('日常安排', (60, 1020, 360, 1120)) or ui.find('日常安排') if daily else find('赛事活动', (360, 830, 680, 1000)) or ui.find('赛事活动'))
    elif ui.home(tokens):
        result['page'] = 'home'
        action('open_races', find('赛事', (460, 1180, 580, 1280)) or ui.find('赛事', (460, 1180, 580, 1280)))
    return result
