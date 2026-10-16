"""Foreground-only TP refill recognition from the user's 2026-10-01 videos."""
import re
import cv2
import numpy as np
from core.tp_refill_bridge import ocr_tokens
TITLES = {'回复训练值', '恢复训练值', '回复TP', '恢复TP'}

def energy_drink_30(text):
    """The authorized consumable is the 30-TP drink, not any energy item."""
    return re.search('(?:能量饮料|精力饮料)[（(]?30(?!\\d)', str(text)) is not None

def _foreground(image, reader, titles=TITLES):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (30, 80, 110), (90, 255, 255))
    _, _, stats, _ = cv2.connectedComponentsWithStats(green)
    candidates = []
    for x, y, w, h, area in stats[1:]:
        if w >= 650 and 35 <= h <= 100:
            text = ''.join((t['text'] for t in ocr_tokens(image[y:y + h, x:x + w], reader)))
            if text in titles:
                candidates.append((int(x), int(y), int(w), int(h)))
    if not candidates:
        return None
    x, y, w, h = max(candidates, key=lambda b: b[1])
    dark = (image[:, 30:690].mean(axis=2) < 220).mean(axis=1) > 0.7
    bottom = None
    for row in range(y + h + 200, 1271):
        if dark[row:row + 10].all():
            bottom = row
            break
    if bottom is None:
        return {'tokens': [], 'reason': 'refill_foreground_boundary_unknown'}
    tokens = ocr_tokens(image[y:bottom, x:x + w], reader)
    for token in tokens:
        token['bbox'] = [token['bbox'][0] + x, token['bbox'][1] + y, token['bbox'][2] + x, token['bbox'][3] + y]
    return {'tokens': tokens, 'bbox': [x, y, x + w, bottom], 'header_bottom': y + h}

def _number(text):
    normalized = text.replace(',', '').replace('，', '')
    return int(normalized) if re.fullmatch('\\d+', normalized) else None

def _brown_inventory_before(image, label):
    """Recover an omitted small stock value; never infer it from the blue value."""
    height, width = image.shape[:2]
    cy = round((label['bbox'][1] + label['bbox'][3]) / 2)
    x0 = round(label['bbox'][2] + 10)
    x1 = round(width * 0.67)
    y0 = max(0, cy - 24)
    if x0 >= x1:
        return None
    crop = image[y0:min(height, cy + 24), x0:x1]
    if not crop.size:
        return None
    mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (0, 65, 60), (35, 255, 215))
    x, y, w, h = cv2.boundingRect(mask)
    if not 2 <= w <= 180 or not 8 <= h <= 38:
        return None
    tight = crop[max(0, y - 5):y + h + 5, max(0, x - 5):x + w + 5]
    from core.tp_refill_bridge import LINE_READER as _CALENDAR_REGION_READER
    text, confidence = _CALENDAR_REGION_READER.read_line(tight)
    value = _number(re.sub('[▶►▸›»>→]+$', '', str(text).replace(' ', '')))
    if confidence < 0.95 or value is None:
        return None
    return {'text': str(value), 'bbox': [x0 + x, y0 + y, x0 + x + w, y0 + y + h]}

def _blue_inventory_after(image, label, before):
    """Read the small blue balance when whole-modal detection omits it."""
    height, width = image.shape[:2]
    if before['bbox'][2] > width * 0.67:
        return None
    cy = round((label['bbox'][1] + label['bbox'][3]) / 2)
    x0 = max(round(before['bbox'][2] + 30), round(width * 0.64))
    crop = image[max(0, cy - 24):min(height, cy + 24), x0:round(width * 0.95)]
    if not crop.size:
        return None
    mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (80, 65, 80), (130, 255, 255))
    x, y, w, h = cv2.boundingRect(mask)
    if w < 2 or not 8 <= h <= 38:
        return None
    tight = crop[max(0, y - 5):y + h + 5, max(0, x - 5):x + w + 5]
    from core.tp_refill_bridge import LINE_READER as _CALENDAR_REGION_READER
    text, confidence = _CALENDAR_REGION_READER.read_line(tight)
    return _number(str(text)) if confidence >= 0.95 else None

def _inventory_pair(tokens, image=None):
    labels = [t for t in tokens if t['text'] in {'持有数', '持有', '所持数', '所持数量'}]
    for label in labels:
        cy = (label['bbox'][1] + label['bbox'][3]) / 2
        values = [t for t in tokens if _number(t['text']) is not None and t['bbox'][0] > label['bbox'][2] + 10 and (abs((t['bbox'][1] + t['bbox'][3]) / 2 - cy) <= 18)]
        values.sort(key=lambda t: t['bbox'][0])
        if len(values) == 2:
            return (_number(values[0]['text']), _number(values[1]['text']))
        if len(values) < 2 and image is not None:
            width = image.shape[1]
            before = next((value for value in values if value['bbox'][2] <= width * 0.67), None)
            if before is None:
                before = _brown_inventory_before(image, label)
            if before is None:
                continue
            after_values = [value for value in values if value is not before and value['bbox'][0] >= width * 0.64]
            after = _number(after_values[0]['text']) if len(after_values) == 1 else _blue_inventory_after(image, label, before)
            if after is not None:
                return (_number(before['text']), after)
    return None

def _green_enabled(image, center, boxes):
    containing = [b for b in boxes if b[0] <= center[0] < b[0] + b[2] and b[1] <= center[1] < b[1] + b[3]]
    if not containing:
        return False
    x, y, w, h = min(containing, key=lambda b: b[2] * b[3])
    crop = image[y:y + h, x:x + w]
    green = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (30, 90, 190), (90, 255, 255))
    return float(np.mean(green > 0)) > 0.35

def _plus_button(image, tokens, bbox):
    gauges = [t for t in tokens if re.fullmatch('\\d+/\\d+', t['text'])]
    if len(gauges) != 1:
        return None
    gauge = gauges[0]
    cy = round((gauge['bbox'][1] + gauge['bbox'][3]) / 2)
    x0 = round(gauge['bbox'][2] + 5)
    x1 = bbox[2] - 70
    y0 = max(bbox[1], cy - 55)
    y1 = min(bbox[3], cy + 55)
    if x1 <= x0:
        return None
    crop = image[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, 1, 30, param1=70, param2=20, minRadius=25, maxRadius=43)
    if circles is None:
        return None
    candidates = []
    for cx, yy, r in circles[0]:
        cx, yy, r = (round(float(cx)), round(float(yy)), round(float(r)))
        sx0, sx1 = (max(0, cx - 18), min(crop.shape[1], cx + 19))
        sy0, sy1 = (max(0, yy - 22), min(crop.shape[0], yy + 23))
        glyph = crop[sy0:sy1, sx0:sx1]
        brown = cv2.inRange(cv2.cvtColor(glyph, cv2.COLOR_BGR2HSV), (0, 60, 40), (35, 255, 210))
        _, _, stats, _ = cv2.connectedComponentsWithStats(brown)
        cross = any((14 <= w <= 36 and 20 <= h <= 44 and (0.3 < area / (w * h) < 0.85) for _, _, w, h, area in stats[1:]))
        white = crop[max(0, yy - r + 6):min(crop.shape[0], yy + r - 6), max(0, cx - r + 6):min(crop.shape[1], cx + r - 6)]
        if cross and white.size and (float(np.mean(white.mean(axis=2) > 220)) > 0.45):
            candidates.append([x0 + cx, y0 + yy])
    return candidates[0] if len(candidates) == 1 else None

def analyze_tp_refill(image, *, remaining, selected_resource_kind=None, returning=False, reader=None, medicine_enabled=True):
    """Return None for non-refill pages; never OCR a blurred underlying layer."""
    foreground = _foreground(image, reader)
    if foreground is None:
        return None
    tokens = foreground['tokens']
    result = {'state': 'unknown', 'reason': foreground.get('reason', 'refill_page_unrecognized')}
    if not tokens:
        return result
    from core.tp_refill_bridge import button_boxes, visible_control
    x0, y0, x1, y1 = foreground['bbox']
    boxes = [b for b in button_boxes(image) if x0 <= b[0] and y0 <= b[1] and (b[0] + b[2] <= x1) and (b[1] + b[3] <= y1)]
    text = ''.join((t['text'] for t in tokens))
    result['foreground_bbox'] = foreground['bbox']
    if any((t in text for t in ('充值', '人民币', '支付', '购买商品', '购买宝石', '前往商城'))):
        return {**result, 'reason': 'resource_action_outside_cut_scope'}

    def action(state, labels, next_phase, **details):
        center = visible_control(tokens, boxes, set(labels))
        if center is None:
            return {**result, 'reason': state + '_control_unverified'}
        return {**result, 'state': state, 'reason': state, 'center': center, 'next_phase': next_phase, **details}
    kinds = []
    if '能量饮料' in text or '精力饮料' in text:
        kinds.append('tp_items')
    if '宝石' in text or '萝卜' in text:
        kinds.append('carrots')
    close = visible_control(tokens, boxes, {'关闭'})
    confirm = visible_control(tokens, boxes, {'确定', '确认', 'OK'})
    pair = _inventory_pair(tokens, image if len(kinds) == 1 else None)
    if close is not None and confirm is None and (pair is not None) and (len(kinds) == 1) and ('使用' in text):
        before, after = pair
        gauges = sorted((t for t in tokens if re.fullmatch('\\d+/\\d+', t['text'])), key=lambda t: t['bbox'][0])
        tp_report = {'reported_training_points_before': int(gauges[0]['text'].split('/')[0]), 'reported_training_points_after': int(gauges[1]['text'].split('/')[0])} if len(gauges) == 2 else {}
        return action('close_tp_result', ('关闭',), 'refill_return', reported_resource_kind=kinds[0], reported_resource_before=before, reported_resource_after=after, reported_resource_delta=before - after, **tp_report)
    if pair is not None and len(kinds) == 1 and (close is None):
        kind = kinds[0]
        before, after = pair
        amount = before - after
        if selected_resource_kind not in (None, kind):
            return {**result, 'reason': 'refill_selected_resource_mismatch'}
        if amount < 0:
            return {**result, 'reason': 'refill_inventory_preview_unknown'}
        unit_match = re.search('使用(\\d+)个(?:宝石|萝卜)', text) if kind == 'carrots' else None
        unit = int(unit_match[1]) if unit_match else 1 if kind == 'tp_items' else None
        if unit is None or unit < 1:
            return {**result, 'reason': 'refill_unit_cost_unknown'}
        if amount == 0:
            if remaining[kind] < unit or before < unit:
                return {**result, 'reason': 'tp_refill_limit'}
            plus = _plus_button(image, tokens, foreground['bbox'])
            if plus is None:
                return {**result, 'reason': 'refill_plus_control_unverified'}
            return {**result, 'state': 'select_one_tp_refill', 'reason': 'select_one_tp_refill', 'center': plus, 'next_phase': 'refill', 'selected_resource_kind': kind, 'preview_unit_resource_amount': unit, 'preview_resource_before': before, 'preview_resource_after': after}
        if amount > remaining[kind]:
            return {**result, 'reason': 'tp_refill_limit'}
        if amount != unit:
            return {**result, 'reason': 'refill_quantity_not_one'}
        if confirm is None or not _green_enabled(image, confirm, boxes):
            return {**result, 'reason': 'refill_confirm_disabled_or_unverified'}
        return action('confirm_tp_refill', ('确定', '确认', 'OK'), 'refill', resource_kind=kind, resource_amount=amount, preview_resource_before=before, preview_resource_after=after, selected_units=1)
    uses = [t for t in tokens if t['text'] == '使用' and visible_control([t], boxes, {'使用'}) is not None]
    if close is not None and confirm is None and uses:
        if returning:
            return action('close_tp_menu', ('关闭',), 'refill_return')
        medicine_titles = [t for t in tokens if energy_drink_30(t['text'])]
        known_empty = bool(medicine_titles)

        def row_for(title):
            top = title['bbox'][1] - 10
            following = [t['bbox'][1] for t in tokens if t is not title and ('能量饮料' in t['text'] or '巧克力' in t['text'] or t['text'] in {'宝石', '萝卜'}) and (t['bbox'][1] > top + 20)]
            bottom = min(following) - 5 if following else top + 140
            return [t for t in tokens if top <= (t['bbox'][1] + t['bbox'][3]) / 2 <= bottom]
        for title in medicine_titles if medicine_enabled else ():
            row = row_for(title)
            labels = [t for t in row if t['text'] in {'持有数', '持有', '所持数', '所持数量'}]
            stock = None
            if len(labels) == 1:
                label = labels[0]
                cy = (label['bbox'][1] + label['bbox'][3]) / 2
                values = [_number(t['text']) for t in row if _number(t['text']) is not None and t['bbox'][0] > label['bbox'][2] and (abs((t['bbox'][1] + t['bbox'][3]) / 2 - cy) <= 18)]
                if len(values) == 1:
                    stock = values[0]
            if stock is None:
                known_empty = False
            elif stock > 0:
                known_empty = False
                if remaining['tp_items'] > 0:
                    center = visible_control(row, boxes, {'使用'})
                    if center:
                        return {**result, 'state': 'choose_tp_item', 'reason': 'choose_tp_item', 'center': center, 'next_phase': 'refill', 'observed_item_stock': stock}
                    return {**result, 'reason': 'tp_item_same_row_use_unverified'}
                return {**result, 'reason': 'tp_item_budget_exhausted_stock_remaining', 'observed_item_stock': stock}
        if not medicine_enabled:
            known_empty = True
            result['medicine_inventory'] = {'basis': 'medicine_disabled_by_config'}
        elif not known_empty and (not medicine_titles) and ('tp_items' not in kinds):
            currencies = [t for t in tokens if t['text'] in {'宝石', '萝卜', '胡萝卜'} and foreground['header_bottom'] + 20 <= t['bbox'][1] <= foreground['header_bottom'] + 65]
            chocolates = [t for t in tokens if '巧克力' in t['text']]
            if len(currencies) == 1 and any((110 <= t['bbox'][1] - currencies[0]['bbox'][1] <= 160 for t in chocolates)):
                known_empty = True
                result['medicine_inventory'] = {'basis': 'fixed_first_rows_gems_then_chocolate', 'observed_item_stock': 0}
        if not known_empty:
            return {**result, 'reason': 'tp_item_stock_unknown'}
        if remaining['carrots'] > 0:
            for title in [t for t in tokens if t['text'] in {'宝石', '萝卜'}]:
                center = visible_control(row_for(title), boxes, {'使用'})
                if center:
                    return {**result, 'state': 'choose_tp_carrots', 'reason': 'choose_tp_carrots', 'center': center, 'next_phase': 'refill'}
        return {**result, 'reason': 'tp_refill_limit_or_item_not_visible'}
    return result
