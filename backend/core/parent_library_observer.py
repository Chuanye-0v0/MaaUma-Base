"""Read-only observations of the hall-of-fame library and inheritance detail.

All coordinates returned are 720 x 1280, with xyxy bounding boxes. OCR input is
either RapidOCR [quadrilateral, text, confidence] or {bbox, text, confidence},
in the input image's coordinates. Supplying [] deliberately disables OCR.
Group IDs are viewport-local: they are boundary evidence, not pedigree IDs.
"""
from __future__ import annotations
import difflib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any
import cv2
import numpy as np
VIEWPORT = [20, 591, 704, 1110]
_COLUMNS = ((139, 409), (415, 685))

def _normal(value: str) -> str:
    return re.sub('\\s+', '', unicodedata.normalize('NFKC', value))

def _tokens(values, sx=1.0, sy=1.0):
    result = []
    for value in values:
        try:
            if isinstance(value, dict):
                box, text = (value['bbox'], value['text'])
                confidence = float(value.get('confidence', 1.0))
            else:
                box, text, confidence = value
                confidence = float(confidence)
            if len(box) == 4 and isinstance(box[0], (int, float)):
                x, y, r, b = box
            else:
                x, y = (min((p[0] for p in box)), min((p[1] for p in box)))
                r, b = (max((p[0] for p in box)), max((p[1] for p in box)))
            box = [int(round(x * sx)), int(round(y * sy)), int(round(r * sx)), int(round(b * sy))]
            if not (0 <= box[0] < box[2] <= 720 and 0 <= box[1] < box[3] <= 1280):
                continue
            if not isinstance(text, str) or not text.strip() or (not 0 <= confidence <= 1):
                continue
            result.append({'text': text.strip(), 'confidence': confidence, 'bbox': box})
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return sorted(result, key=lambda t: (t['bbox'][1], t['bbox'][0]))

def _inside(token, box):
    x, y, r, b = token['bbox']
    return box[0] <= (x + r) / 2 <= box[2] and box[1] <= (y + b) / 2 <= box[3]

def _find(tokens, text, box, minimum=0.8):
    return next((t for t in tokens if _normal(t['text']) == text and t['confidence'] >= minimum and _inside(t, box)), None)

def _control(name, box, evidence):
    return {'name': name, 'bbox': list(box), 'center': [int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2)], 'evidence': evidence}

def _arrow(image, name, box):
    x, y, r, b = box
    hsv = cv2.cvtColor(image[y:b, x:r], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([35, 65, 105]), np.array([95, 255, 255]))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        xx, yy, w, h = cv2.boundingRect(contour)
        if 12 <= w <= 48 and 35 <= h <= 85 and (cv2.contourArea(contour) >= 60):
            candidates.append([x + xx, y + yy, x + xx + w, y + yy + h])
    return _control(name, max(candidates, key=lambda p: (p[2] - p[0]) * (p[3] - p[1])), 'visible_arrow_outline') if candidates else None

def _tab_selected(image, box):
    x, y, r, b = box
    hsv = cv2.cvtColor(image[y:b, x:r], cv2.COLOR_BGR2HSV)
    return float(((hsv[:, :, 0] >= 30) & (hsv[:, :, 0] <= 70) & (hsv[:, :, 1] >= 120) & (hsv[:, :, 2] >= 130)).mean()) > 0.45

def _kind(image, column, y):
    x, r = _COLUMNS[column]
    patch = cv2.cvtColor(image[max(591, y - 7):min(1110, y + 7), r - 35:r - 9], cv2.COLOR_BGR2HSV)
    if not patch.size:
        return None
    h, s, v = (float(np.median(patch[:, :, i])) for i in range(3))
    if v < 120:
        return None
    if 85 <= h <= 115 and s >= 70:
        return 'blue'
    if 145 <= h <= 179 and s >= 65:
        return 'red'
    if 30 <= h <= 75 and s >= 70:
        return 'green'
    if s <= 45 and v >= 170:
        return 'white'
    return None

def _stars(image, column, text_box):
    y = int((text_box[1] + text_box[3]) / 2) + 20
    if y - 12 < VIEWPORT[1] or y + 13 > VIEWPORT[3]:
        return None
    slots = (250, 273, 296) if column == 0 else (526, 549, 572)
    states = []
    for x in slots:
        patch = cv2.cvtColor(image[y - 12:y + 13, x - 10:x + 11], cv2.COLOR_BGR2HSV)
        h, s, v = (patch[:, :, 0], patch[:, :, 1], patch[:, :, 2])
        gold = int(((h >= 15) & (h <= 40) & (s >= 110) & (v >= 150)).sum())
        grey = int(((s < 65) & (v >= 80) & (v <= 210)).sum())
        states.append(1 if gold >= 75 else 0 if grey >= 12 else None)
    if None in states or states != sorted(states, reverse=True) or sum(states) == 0:
        return None
    return sum(states)

def _catalog_entries(catalog):
    values = catalog.get('factors', []) if isinstance(catalog, dict) else catalog or []
    return [x for x in values if isinstance(x, dict) and isinstance(x.get('name'), str) and isinstance(x.get('key'), str)]

def _match_catalog(raw, kind, catalog):
    catalog_kind = 'pink' if kind == 'red' else kind
    choices = [x for x in catalog if x['key'].split(':', 1)[0] == catalog_kind]
    exact = [x for x in choices if _normal(x['name']) == _normal(raw)]
    if len(exact) == 1:
        return (exact[0]['name'], kind + ':' + exact[0]['name'], [])
    ranked = sorted(((difflib.SequenceMatcher(None, _normal(raw), _normal(x['name'])).ratio(), x) for x in choices), key=lambda p: p[0], reverse=True)
    return (None, None, [{'name': x['name'], 'factor_key': kind + ':' + x['name']} for score, x in ranked[:3] if score >= 0.65])

def _visual_signature(image, box):
    x, y, r, b = box
    crop = cv2.cvtColor(image[y:b, x:r], cv2.COLOR_BGR2GRAY)
    small = cv2.resize(crop, (17, 8), interpolation=cv2.INTER_AREA)
    bits = np.packbits((small[:, 1:] > small[:, :-1]).reshape(-1)).tobytes().hex()
    return 'dhash128:' + bits

def _group_boundaries(image, rows):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    def boxes(column, lower, upper):
        x, r = _COLUMNS[column]
        mask = cv2.inRange(hsv[591:1110, x:r], np.array(lower), np.array(upper))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        result = []
        for contour in contours:
            xx, yy, w, h = cv2.boundingRect(contour)
            if w >= 220 and h >= 12 and (cv2.contourArea(contour) >= 1800):
                result.append([x + xx, 591 + yy, x + xx + w, 591 + yy + h])
        return sorted(result, key=lambda b: b[1])
    blue = boxes(0, [85, 70, 120], [115, 255, 255])
    red = boxes(1, [145, 65, 120], [179, 255, 255])
    result = []
    for left in blue:
        right = next((r for r in red if abs(r[1] - left[1]) <= 8 and abs(r[3] - left[3]) <= 8), None)
        if right:
            box = [left[0], min(left[1], right[1]), right[2], max(left[3], right[3])]
            complete_cards = all((any((row['column'] == column and row['kind'] == kind and (not row['clipped']) and (abs(row['full_bbox'][1] - color_box[1]) <= 12) and (row['full_bbox'][3] >= color_box[3]) for row in rows)) for column, kind, color_box in ((0, 'blue', left), (1, 'red', right))))
            result.append({'kind': 'blue_red_start', 'bbox': box, 'blue_bbox': left, 'red_bbox': right, 'full': complete_cards and box[1] > VIEWPORT[1] + 3 and (box[3] < VIEWPORT[3] - 3) and (min(left[3] - left[1], right[3] - right[1]) >= 36)})
    return result

def _scrollbar(image):
    strip = image[604:1098, 694:701]
    value = np.median(strip, axis=(1, 2))
    dark = np.where(value < 170)[0]
    if len(dark) < 15:
        return {'bbox': None, 'at_bottom': None}
    top, bottom = (int(dark[0]) + 604, int(dark[-1]) + 605)
    return {'bbox': [694, top, 701, bottom], 'at_bottom': bottom >= 1093}

def _visible_star_rows(image):
    """Find the first filled star in each fixed factor-column star band.

    A card has at least one filled star, always at the first horizontal slot.
    Restrict hue, brightness, shape/area and that slot before requiring the
    other two slots to be readable through the existing star reader. Brown
    labels, portrait decorations, yellow border lines and solid rectangles do
    not by themselves provide a factor-row observation.
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    result = []
    for column, slot in enumerate((250, 526)):
        left = slot - 12
        mask = cv2.inRange(hsv[591:1110, left:left + 25], np.array([15, 110, 190]), np.array([40, 255, 255]))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if not (12 <= w <= 24 and 12 <= h <= 36 and (75 <= area <= 350) and (0.25 <= area / (w * h) <= 0.72) and (abs(left + x + w / 2 - slot) <= 5)):
                continue
            star_y = int(round(591 + y + h / 2))
            name_y = star_y - 20
            stars = _stars(image, column, [0, name_y - 10, 1, name_y + 10])
            if stars is not None:
                result.append((column, name_y, stars))
    return sorted(result, key=lambda r: (r[1], r[0]))

def _non_scrollable_factor_panel(image, at_top, rows, headers, scrollbar):
    """Positive evidence for a complete short panel, not missing-thumb fallback."""
    if not at_top or scrollbar['bbox'] is not None or len(headers) != 3 or (not all((h['full'] for h in headers))) or (len(rows) < 6) or any((r['clipped'] for r in rows)):
        return False
    last_bottom = max((row['full_bbox'][3] for row in rows))
    if last_bottom + 8 > 1062:
        return False
    gutter = image[645:1090, 693:701].astype(np.int16)
    background = np.median(gutter, axis=(0, 1))
    if np.any(background < 230) or np.any(background > 248) or float(np.max(background) - np.min(background)) > 8 or (float((np.max(np.abs(gutter - background), axis=2) <= 5).mean()) < 0.99):
        return False
    blank = image[last_bottom + 8:1095, 139:685].astype(np.int16)
    return bool(blank.size and float((np.max(np.abs(blank - background), axis=2) <= 8).mean()) >= 0.99)

def _factor_end_heading(image, tokens):
    heading = _find(tokens, '继承记录', [20, 591, 210, 1108], minimum=0.85)
    if heading is None:
        return None
    cy = int((heading['bbox'][1] + heading['bbox'][3]) / 2)
    top, bottom = (max(591, cy - 20), min(1110, cy + 21))
    strip = cv2.cvtColor(image[top:bottom, 130:655], cv2.COLOR_BGR2HSV)
    green = ((strip[:, :, 0] >= 30) & (strip[:, :, 0] <= 85) & (strip[:, :, 1] >= 100) & (strip[:, :, 2] >= 130)).mean(axis=1)
    ys = np.where(green >= 0.85)[0]
    if len(ys) < 12 or not top + int(ys[0]) <= cy <= top + int(ys[-1]):
        return None
    return {'bbox': [30, top + int(ys[0]), 690, top + int(ys[-1]) + 1], 'reason': 'inheritance_history_heading'}

def _factor_view(image, tokens, catalog):
    top = _find(tokens, '因子', [20, 591, 130, 658])
    boundary = _find(tokens, '继承对象', [30, 591, 160, 1108])
    factor_end = _factor_end_heading(image, tokens)
    rows = []
    for t in tokens:
        x, y, r, b = t['bbox']
        if not (VIEWPORT[1] <= y and b <= VIEWPORT[3]) or t['confidence'] < 0.45:
            continue
        column = 0 if 153 <= x <= 207 and r <= 403 else 1 if 427 <= x <= 486 and r <= 682 else None
        raw = t['text']
        if column is None or re.fullmatch('[★☆✩✭*\\s]+', raw):
            continue
        cy = int((y + b) / 2)
        kind = _kind(image, column, cy)
        left, right = _COLUMNS[column]
        name, key, candidates = _match_catalog(raw, kind, catalog)
        full_box = [left, cy - 22, right, cy + 34]
        box = [left, max(VIEWPORT[1], full_box[1]), right, min(VIEWPORT[3], full_box[3])]
        rows.append({'raw_name': raw, 'name': name, 'factor_key': key, 'catalog_candidates': candidates, 'kind': kind, 'stars': _stars(image, column, t['bbox']), 'bbox': box, 'full_bbox': full_box, 'clipped': box != full_box, 'visual_signature': _visual_signature(image, box), 'text_bbox': list(t['bbox']), 'confidence': t['confidence'], 'column': column})
    for column, cy, stars in _visible_star_rows(image):
        if any((row['column'] == column and abs(row['full_bbox'][1] + 22 - cy) <= 12 for row in rows)):
            continue
        left, right = _COLUMNS[column]
        full_box = [left, cy - 22, right, cy + 34]
        box = [left, max(VIEWPORT[1], full_box[1]), right, min(VIEWPORT[3], full_box[3])]
        rows.append({'raw_name': '', 'name': None, 'factor_key': None, 'catalog_candidates': [], 'kind': _kind(image, column, max(599, min(1102, cy))), 'stars': stars, 'bbox': box, 'full_bbox': full_box, 'clipped': box != full_box, 'visual_signature': _visual_signature(image, box), 'text_bbox': None, 'confidence': None, 'column': column, 'observation': 'visible_stars_name_unreadable'})
    if factor_end:
        rows = [row for row in rows if row['bbox'][3] <= factor_end['bbox'][1]]
    rows.sort(key=lambda t: t['full_bbox'][1] + 22)
    bands = []
    for row in rows:
        cy = row['full_bbox'][1] + 22
        if not bands or cy - bands[-1][0] > 12:
            bands.append((cy, []))
        bands[-1][1].append(row)
    rows = [row for _, band in bands for row in sorted(band, key=lambda r: r['column'])]
    groups = []
    for row_index, row in enumerate(rows):
        y = row['full_bbox'][1] + 12
        is_blue_start = row['kind'] == 'blue' and row['column'] == 0
        if not groups or is_blue_start:
            if not groups and top:
                reason, role = ('self_top', 'self')
            elif is_blue_start and boundary and (y > boundary['bbox'][3]) and (not any((g['boundary'] == 'inheritance_section' for g in groups))):
                reason, role = ('inheritance_section', None)
            elif is_blue_start:
                reason, role = ('blue_factor_start', None)
            else:
                reason, role = ('viewport_continuation', None)
            groups.append({'id': f'g{len(groups)}', 'boundary': reason, 'role': role, 'y': row['bbox'][1], 'row_indices': []})
        row['group'] = groups[-1]['id']
        groups[-1]['row_indices'].append(row_index)
    scrollbar = _scrollbar(image)
    headers = _group_boundaries(image, rows)
    if factor_end:
        headers = [header for header in headers if header['bbox'][3] <= factor_end['bbox'][1]]
    non_scrollable = _non_scrollable_factor_panel(image, bool(top), rows, headers, scrollbar)
    return {'bbox': list(VIEWPORT), 'at_top': bool(top), 'at_bottom': True if non_scrollable else scrollbar['at_bottom'], 'non_scrollable': non_scrollable, 'factor_end': factor_end, 'scrollbar': scrollbar['bbox'], 'inheritance_boundary': boundary['bbox'] if boundary else None, 'group_boundaries': headers, 'groups': groups, 'rows': rows, 'signature': [[r['raw_name'], r['kind'], r['stars'], r['column'], r['group']] for r in rows]}

def _identity(tokens):

    def in_box(box):
        return [t for t in tokens if _inside(t, box)]
    names = in_box([340, 141, 650, 181])
    outfits = in_box([340, 105, 650, 141])
    score_tokens = [t for t in in_box([65, 258, 210, 300]) if re.fullmatch('[\\d,]+', t['text'])]
    score = int(score_tokens[0]['text'].replace(',', '')) if len(score_tokens) == 1 else None
    return {'name': names[0]['text'] if len(names) == 1 and names[0]['confidence'] >= 0.85 else None, 'outfit': outfits[0]['text'] if len(outfits) == 1 and outfits[0]['confidence'] >= 0.85 else None, 'score': score, 'raw': names + outfits + score_tokens}

def _library_cells(image, tokens):
    """Scores sit in white horizontal pills at the foot of a five-column card.

    Low-score finished characters use plain integers. Rank decorations can also
    OCR as digits, so number syntax alone is not enough to locate a character.
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    columns = (92, 224, 357, 490, 623)
    cells = []
    for token in tokens:
        if not re.fullmatch('(?:\\d{1,5}|\\d{1,2},\\d{3})', token['text']):
            continue
        x, y, r, b = token['bbox']
        cx, cy = ((x + r) / 2, (y + b) / 2)
        column = min(range(5), key=lambda i: abs(columns[i] - cx))
        center = columns[column]
        if not (235 <= cy <= 950 and abs(center - cx) <= 16 and (9 <= b - y <= 34)):
            continue
        patches = [hsv[int(cy) - 4:int(cy) + 5, center - 53:center - 45], hsv[int(cy) - 4:int(cy) + 5, center + 45:center + 53]]
        if any((float(((p[:, :, 1] < 60) & (p[:, :, 2] > 215)).mean()) < 0.65 for p in patches)):
            continue
        band = hsv[int(cy) - 7:int(cy) + 8, center - 53:center + 54]
        if float(((band[:, :, 1] < 60) & (band[:, :, 2] > 215)).mean()) < 0.65:
            continue
        cells.append({'score': int(token['text'].replace(',', '')), 'bbox': [center - 65, int(cy) - 145, center + 65, b], 'center': [center, int(cy) - 76], 'column': column})
    cells.sort(key=lambda c: c['bbox'][3])
    bands = []
    for cell in cells:
        if not bands or cell['bbox'][3] - bands[-1][0] > 15:
            bands.append((cell['bbox'][3], []))
        bands[-1][1].append(cell)
    result = []
    for row_index, (_, band) in enumerate(bands):
        for cell in sorted(band, key=lambda c: c['column']):
            cell['row'] = row_index
            result.append(cell)
    return result

def _library_scrollbar(image):
    values = np.median(image[109:962, 694:701], axis=(1, 2))
    dark = np.where(values < 170)[0]
    if len(dark) < 15:
        return (None, None)
    top, bottom = (int(dark[0]) + 109, int(dark[-1]) + 110)
    return ([694, top, 701, bottom], top <= 116)

def observe_parent_library(image: np.ndarray, tokens=None, *, catalog=None) -> dict[str, Any]:
    """Observe one frame. No input, scrolling, filesystem writes or networking."""
    if image is None or image.ndim != 3 or image.shape[2] != 3 or (image.dtype != np.uint8):
        raise ValueError('parent_library_bgr_frame_required')
    h, w = image.shape[:2]
    if abs(w / h - 720 / 1280) > 0.015:
        raise ValueError('parent_library_portrait_frame_required')
    sx, sy = (720 / w, 1280 / h)
    frame = image if (w, h) == (720, 1280) else cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    if tokens is None:
        from core.local_ocr_runtime import default_rapidocr_engine
        items = _tokens(default_rapidocr_engine()(frame)[0] or [])
    else:
        items = _tokens(tokens, sx, sy)
    if catalog is None:
        catalog_path = Path(__file__).resolve().parents[1] / 'data' / 'parent_library' / 'factor_catalog.json'
        try:
            catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            catalog = []
    result = {'schema_version': 'uma.parent_library_observation.v1', 'page': 'other', 'size': [720, 1280], 'controls': [], 'library': None, 'identity': None, 'viewport': None, 'training_date': None}
    title = _find(items, '名人堂优俊少女一览', [0, 0, 390, 75])
    if title:
        result['page'] = 'library'
        count = next((re.fullmatch('(\\d+)\\s*/\\s*(\\d+)', t['text']) for t in items if _inside(t, [100, 966, 250, 1020]) and re.fullmatch('(\\d+)\\s*/\\s*(\\d+)', t['text'])), None)
        filter_token = next((t for t in items if _inside(t, [240, 964, 370, 1025]) and '筛选' in t['text']), None)
        sort = next((t['text'] for t in items if _inside(t, [380, 964, 560, 1025])), None)
        order = next((t['text'] for t in items if _inside(t, [590, 964, 700, 1025]) and any((s in t['text'] for s in ('升序', '降序')))), None)
        cells = _library_cells(frame, items)
        scrollbar, at_top = _library_scrollbar(frame)
        result['library'] = {'count': int(count[1]) if count else None, 'capacity': int(count[2]) if count else None, 'filter': False if filter_token and re.search('[:：]\\s*关', filter_token['text']) else True if filter_token and re.search('[:：]\\s*开', filter_token['text']) else None, 'filter_raw': filter_token['text'] if filter_token else None, 'sort': sort, 'order': order, 'at_top': at_top, 'scrollbar': scrollbar, 'cells': cells}
        back = _find(items, '返回', [10, 1020, 155, 1130])
        if back:
            result['controls'].append(_control('back', back['bbox'], 'ocr_label'))
        return result
    detail = _find(items, '优俊少女详情', [190, 15, 540, 100])
    close = _find(items, '关闭', [190, 1130, 530, 1240])
    if not (detail and close):
        return result
    result['page'] = 'detail'
    result['controls'].append(_control('close', close['bbox'], 'ocr_label'))
    for name, label, box in (('inheritance', '继承', [246, 524, 474, 578]), ('info', '养成信息', [475, 524, 700, 578])):
        t = _find(items, label, box)
        if t:
            result['controls'].append(_control(name, t['bbox'], 'ocr_label'))
    for name, box in (('prev', [2, 410, 56, 501]), ('next', [663, 410, 718, 501])):
        control = _arrow(frame, name, box)
        if control:
            result['controls'].append(control)
    result['identity'] = _identity(items)
    if _tab_selected(frame, [255, 538, 462, 567]):
        result['page'] = 'detail_inheritance'
        result['viewport'] = _factor_view(frame, items, _catalog_entries(catalog))
    elif _tab_selected(frame, [483, 538, 687, 567]):
        result['page'] = 'detail_info'
        dates = [t for t in items if _inside(t, [20, 590, 700, 1110]) and re.search('20\\d{2}\\s*(?:年|[/.-])\\s*\\d{1,2}\\s*(?:月|[/.-])\\s*\\d{1,2}', t['text'])]
        if len(dates) == 1:
            result['training_date'] = {'raw': dates[0]['text'], 'bbox': dates[0]['bbox'], 'confidence': dates[0]['confidence']}
    return result
