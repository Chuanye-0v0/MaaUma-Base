"""Explicit saved-deck and sixth-slot support selection; no own-card edits."""
from functools import lru_cache
import json
from pathlib import Path
import re
CATALOG = Path(__file__).resolve().parents[2] / 'runs/support-card-library/catalog.json'
OPTION_KEYS = {'support_deck', 'borrow_card_id', 'borrow_min_level', 'borrow_min_break'}
TYPE_LABELS = dict(speed='速度', stamina='耐力', power='力量', guts='毅力', intelligence='智力', friend='友人', group='小组')
FILTER_LABELS = ['普通', '稀有', '超稀有', *TYPE_LABELS.values()]
MAX_SCAN_SCROLLS = 12
MAX_REFRESHES = 8
SEARCH_ACTION_LIMITS = dict(support_scroll=MAX_SCAN_SCROLLS * (MAX_REFRESHES + 1), support_refresh=MAX_REFRESHES)
ACTION_PAGES = dict(support_prev='support_deck', support_next='support_deck', support_open_friend='support_deck', support_select_friend='support_friend', support_scroll='support_friend', support_refresh='support_friend', support_settings='support_friend', support_filter_tab='support_settings', support_filter_reset='support_filter', support_filter_apply='support_filter')
ACTION_PAGES.update({f'support_filter_{i}': 'support_filter' for i in range(10)})
NEXT_PAGES = {a: {p} for a, p in ACTION_PAGES.items()}
NEXT_PAGES.update(support_open_friend={'support_friend'}, support_select_friend={'support_deck'}, support_settings={'support_settings', 'support_filter'}, support_filter_tab={'support_filter'}, support_filter_apply={'support_friend'})

@lru_cache(maxsize=1)
def catalog():
    data = json.loads(CATALOG.read_text(encoding='utf8'))
    if data.get('schema') != 'uma.support_card_art.v1':
        raise ValueError('support_catalog_invalid')
    return {int(c['card_id']): c for c in data['cards'] if c.get('image_status') == 'ready'}

@lru_cache(maxsize=1)
def matcher():
    from core.support_library.art_matcher import CardArtMatcher
    return CardArtMatcher.from_catalog(CATALOG)

def validate(options):
    deck, card = (options.get('support_deck', 0), options.get('borrow_card_id', 0))
    level, stage = (options.get('borrow_min_level', 1), options.get('borrow_min_break', 0))
    if any((type(v) is not int for v in (deck, card, level, stage))) or not 0 <= deck <= 10 or (not 1 <= level <= 50) or (not 0 <= stage <= 4) or (card < 0):
        raise ValueError('support_options_invalid')
    if card and (card not in catalog() or catalog()[card]['type'] not in TYPE_LABELS):
        raise ValueError('requested_support_card_unavailable')
    result = {'support_deck': deck} if deck else {}
    if card:
        result.update(borrow_card_id=card, borrow_min_level=level, borrow_min_break=stage)
    elif any((k in options for k in ('borrow_min_level', 'borrow_min_break'))):
        raise ValueError('borrow_card_required_for_constraints')
    return result

def deck_index(image):
    """Ten aligned page dots, one lime and nine orange; titles are editable."""
    import cv2
    import numpy as np
    if image.shape[:2] != (1280, 720):
        return None
    hsv = cv2.cvtColor(image[970:998, 275:448], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([5, 100, 80]), np.array([90, 255, 255]))
    count, labels, stats, centres = cv2.connectedComponentsWithStats(mask)
    dots = [i for i in range(1, count) if 4 <= stats[i, 2] <= 13 and 4 <= stats[i, 3] <= 14 and (18 <= stats[i, 4] <= 145)]
    dots.sort(key=lambda i: centres[i, 0])
    if len(dots) != 10 or max((centres[i, 1] for i in dots)) - min((centres[i, 1] for i in dots)) > 3 or any((not 12 <= centres[b, 0] - centres[a, 0] <= 20 for a, b in zip(dots, dots[1:]))):
        return None
    hues = [float(np.median(hsv[:, :, 0][labels == i])) for i in dots]
    active = [n + 1 for n, hue in enumerate(hues) if 30 <= hue <= 80]
    return active[0] if len(active) == 1 and all((5 <= h <= 25 or 30 <= h <= 80 for h in hues)) else None

def friend_rectangles(image):
    """Locate full list panels afresh; never infer a row from a previous scroll."""
    import cv2
    import numpy as np
    if image.shape[:2] != (1280, 720):
        return []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    white = ((gray[95:1065, 20:683] > 245) & (hsv[95:1065, 20:683, 1] < 40)).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(white)
    interiors = [(int(x + 20), int(y + 95), int(w), int(h)) for x, y, w, h, area in stats[1:] if 15 <= x + 20 <= 32 and 620 <= w <= 670 and (145 <= h <= 176) and (y + 95 > 95) and (y + 95 + h <= 1065) and (area > w * h * 0.5)]
    edges = cv2.Canny(gray, 50, 110)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rectangles = [cv2.boundingRect(c) for c in contours]
    panels = [(x, y, w, h) for x, y, w, h in rectangles if 15 <= x <= 32 and 620 <= w <= 670 and (145 <= h <= 176) and (y > 95) and (y + h <= 1065)]
    portraits = [(x, y, w, h) for x, y, w, h in rectangles if 30 <= x <= 48 and 94 <= w <= 102 and (125 <= h <= 134) and (y > 95) and (y + h <= 1065)]
    columns = [x for x, y, w, h in portraits if h >= 128 and any((abs(y - top - 14) <= 4 for _, top, _, _ in interiors))]
    column = int(np.median(columns)) if columns else None
    result = []
    for x, y, w, h in sorted(interiors, key=lambda b: b[1]):
        aligned = [p for p in panels if abs(p[1] - y) <= 3 and abs(p[0] - x) <= 4]
        if aligned:
            px, py, pw, ph = min(aligned, key=lambda p: (abs(p[1] - y), abs(p[0] - x)))
            result.append((px, py, px + pw, py + ph))
        else:
            anchors = [p for p in portraits if p[3] >= 128 and abs(p[1] - y - 14) <= 3]
            top = min(anchors, key=lambda p: abs(p[1] - y - 14))[1] - 14 if anchors else y
            left = column - 14 if column is not None else x
            result.append((left, top, x + w, min(1065, y + h + 3)))
    return result

def friend_list_end(image, panels):
    """Recognize the visible end, including short lists without a scrollbar."""
    import cv2
    if image.shape[:2] != (1280, 720):
        return (False, 'layout_unverified')
    rail = image[105:1105, 688:700]
    dark = (rail.max(axis=2) - rail.min(axis=2) < 55) & (rail.mean(axis=2) > 70) & (rail.mean(axis=2) < 180)
    bottom = dark[-25:].sum(axis=1) >= 3
    if bottom.sum() >= 15 and bottom[-7:].sum() >= 3:
        return (True, 'scrollbar_bottom')
    thumbs = cv2.connectedComponentsWithStats(dark.astype('uint8'))[2][1:]
    if any((3 <= w <= 12 and h >= 30 for _, _, w, h, _ in thumbs)):
        return (False, 'scrollbar_above_bottom')
    if not 1 <= len(panels) <= 4:
        return (False, 'short_list_not_established')
    tail = image[max((box[3] for box in panels)) + 8:1045, 36:590]
    if tail.shape[0] < 90:
        return (False, 'insufficient_visible_tail')
    blank = (tail.max(axis=2) - tail.min(axis=2) < 18) & (tail.mean(axis=2) >= 225) & (tail.mean(axis=2) <= 249)
    empty = float(blank.mean()) >= 0.97 and float(blank.mean(axis=1).min()) >= 0.8
    return (True, 'short_list_empty_tail') if empty else (False, 'content_below_last_full_row')

def card_details(image, box, tokens, ocr=None, *, friend_slot=False):
    import cv2
    x, y, right, bottom = box
    crop = image[y:bottom, x:right]
    identity = matcher().match(crop)
    result = dict(card_id=identity.card_id, match=identity.to_dict(), level=None, limit_break=None)
    if identity.card_id is None:
        return result
    h, w = crop.shape[:2]
    levels = [int(m.group(1)) for t in tokens if t['confidence'] >= 0.94 and x <= t['bbox'][0] and (y + 0.75 * h <= t['bbox'][1]) and (t['bbox'][2] <= right + 4) and (t['bbox'][3] <= bottom + 4) if (m := re.fullmatch('等级(\\d{1,2})', t['text'].replace(' ', '')))]
    if len(levels) == 1:
        result['level'] = levels[0]
    elif ocr is not None:
        region = crop[round(h * 0.77):round(h * 0.89), round(w * 0.78):round(w * 0.98)] if friend_slot else crop[round(h * 0.81):round(h * 0.95), round(w * 0.79):round(w * 0.98)]
        patch = cv2.resize(region, None, fx=5, fy=5)
        rows, _ = ocr(patch, use_det=False, use_cls=False)
        if rows and len(rows) == 1 and (rows[0][1] >= 0.95):
            match = re.fullmatch('(?:等级|级)?(\\d{1,2})', rows[0][0].replace(' ', ''))
            if match and 1 <= int(match[1]) <= 50:
                result['level'] = int(match[1])
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    markers = []
    centres = (0.056, 0.152, 0.248, 0.344) if friend_slot else (0.113, 0.23, 0.345, 0.46)
    for ratio in centres:
        cx, cy = (round(w * ratio), round(h * (0.823 if friend_slot else 0.89)))
        region = hsv[cy - max(1, round(h * 0.012)):cy + max(2, round(h * 0.012)), cx - max(1, round(w * 0.015)):cx + max(2, round(w * 0.015))]
        cyan = ((region[:, :, 0] >= 78) & (region[:, :, 0] <= 108) & (region[:, :, 1] > 90) & (region[:, :, 2] > 130)).mean()
        gray = ((region[:, :, 1] < 60) & (region[:, :, 2] > 60) & (region[:, :, 2] < 235)).mean()
        markers.append(1 if cyan >= 0.55 else 0 if gray >= 0.6 else None)
    if None not in markers and markers == sorted(markers, reverse=True):
        result['limit_break'] = sum(markers)
    return result

def matches(card, options):
    return card.get('card_id') == options.get('borrow_card_id') and type(card.get('level')) is int and (card['level'] >= options.get('borrow_min_level', 1)) and (options.get('borrow_min_break', 0) == 0 or (type(card.get('limit_break')) is int and card['limit_break'] >= options['borrow_min_break']))

def enrich(observed, image, tokens, context, ocr=None):
    options = context.get('support_options')
    if not options or image.shape[:2] != (1280, 720):
        return
    import cv2
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.9]

    def find(text, region):
        x, y, r, b = region
        hits = [t for t in good if t['text'] == text and x <= t['bbox'][0] and (y <= t['bbox'][1]) and (t['bbox'][2] <= r) and (t['bbox'][3] <= b)]
        return hits[0] if len(hits) == 1 else None

    def button(name, token):
        if token:
            x, y, r, b = token['bbox']
            observed['actions'][name] = dict(point=[(x + r) // 2, (y + b) // 2])
    if find('选择好友协助卡', (100, 20, 620, 100)):
        observed.update(page='support_friend', actions={}, support_rows=[])
        panels = friend_rectangles(image)
        observed['support_list_bottom'], observed['support_list_end_basis'] = friend_list_end(image, panels)
        for x, y, r, b in panels:
            box = (x + 14, y + 14, x + 112, y + 145)
            card = card_details(image, box, tokens, ocr)
            warning = any((('协助卡重复' in t['text'] or '育成优俊少女' in t['text']) and y - 35 <= t['bbox'][1] <= y + 25 for t in good))
            tag = cv2.cvtColor(image[max(0, y - 26):y + 18, 20:160], cv2.COLOR_BGR2HSV)
            warning |= float((((tag[:, :, 0] < 10) | (tag[:, :, 0] > 170)) & (tag[:, :, 1] > 130) & (tag[:, :, 2] > 160)).mean()) > 0.15
            background = image[y + 40:b - 20, x + 165:r - 30]
            bright = float((cv2.cvtColor(background, cv2.COLOR_BGR2GRAY) > 225).mean()) > 0.75
            card.update(box=list(box), selectable=not warning and bright)
            observed['support_rows'].append(card)
            if card['selectable'] and matches(card, options) and ('support_select_friend' not in observed['actions']):
                observed['actions']['support_select_friend'] = dict(point=[(box[0] + box[2]) // 2, (box[1] + box[3]) // 2], card=card)
        if panels:
            observed['actions']['support_scroll'] = dict(point=[620, 920], end=[620, 360], gesture='swipe')
            hsv = cv2.cvtColor(image[970:1046, 615:688], cv2.COLOR_BGR2HSV)
            if float(((hsv[:, :, 0] >= 5) & (hsv[:, :, 0] <= 30) & (hsv[:, :, 1] > 70)).mean()) > 0.06:
                observed['actions']['support_refresh'] = dict(point=[652, 1007])
        settings = next((t for t in good if t['text'] in ('最后登录时间', '等级', '突破次数', '技能Pt加成', '友情加成') and 360 <= t['bbox'][0] < 570 and (1050 <= t['bbox'][1]) and (t['bbox'][3] <= 1120)), None)
        button('support_settings', settings)
        return
    if find('显示设置', (150, 20, 580, 100)) and context.get('support_filter_open'):
        observed.update(page='support_settings', actions={})
        button('support_filter_tab', find('筛选', (350, 100, 690, 170)))
        if find('稀有度', (20, 160, 230, 230)) and find('擅长训练', (20, 300, 250, 380)):
            observed['page'] = 'support_filter'
            states = {}
            positions = [(70, 273), (294, 273), (518, 273), (70, 415), (294, 415), (518, 415), (70, 493), (294, 493), (518, 493), (70, 570)]
            for i, (label, (cx, cy)) in enumerate(zip(FILTER_LABELS, positions)):
                token = find(label, (cx + 20, cy - 32, min(710, cx + 160), cy + 32))
                if token:
                    patch = cv2.cvtColor(image[cy - 18:cy + 18, cx - 18:cx + 18], cv2.COLOR_BGR2HSV)
                    green = float(((patch[:, :, 0] >= 30) & (patch[:, :, 0] <= 85) & (patch[:, :, 1] > 100) & (patch[:, :, 2] > 140)).mean())
                    white = float(((patch[:, :, 1] < 50) & (patch[:, :, 2] > 200)).mean())
                    states[label] = True if green > 0.1 else False if green < 0.01 and white > 0.5 else None
                    if states[label] is not None:
                        observed['actions'][f'support_filter_{i}'] = dict(point=[cx, cy], label=label)
            observed['support_filters'] = states
            button('support_filter_reset', find('重置筛选项', (370, 990, 710, 1110)))
            button('support_filter_apply', find('确定', (350, 1110, 680, 1260)))
        return
    if observed['page'] != 'support_deck':
        return
    index = deck_index(image)
    observed['support_deck_index'] = index
    observed['actions'].pop('auto_support', None)
    boxes = [(x, y, x + 196, y + 260) for y in (260, 535) for x in (50, 265, 478)]
    observed['owned_support_full'] = all((float(cv2.cvtColor(image[y + 55:b - 50, x + 30:r - 30], cv2.COLOR_BGR2HSV)[:, :, 1].mean()) > 45 for x, y, r, b in boxes[:5]))
    if index is not None:
        for name, x in [('support_prev', 32), ('support_next', 690)]:
            patch = cv2.cvtColor(image[530:608, max(0, x - 24):min(720, x + 24)], cv2.COLOR_BGR2HSV)
            if float(((patch[:, :, 0] >= 30) & (patch[:, :, 0] <= 85) & (patch[:, :, 1] > 90)).mean()) > 0.025:
                observed['actions'][name] = dict(point=[x, 570], from_deck=index)
        if options.get('borrow_card_id'):
            observed['borrowed_support'] = card_details(image, boxes[5], tokens, ocr, friend_slot=True)
            observed['actions']['support_open_friend'] = dict(point=[575, 650])

def target_filters(options):
    card = catalog()[options['borrow_card_id']]
    return ({'SSR': '超稀有', 'SR': '稀有'}[card['rarity']], TYPE_LABELS[card['type']])

def choose(observed, context):
    options = context.get('support_options')
    if not options or context.get('start_submitted'):
        return None
    page, actions = (observed['page'], observed.get('actions', {}))
    if page == 'support_deck':
        current = observed.get('support_deck_index')
        if type(current) is not int:
            raise RuntimeError('support_deck_index_unverified')
        desired = options.get('support_deck', context.get('support_origin_deck', current))
        if current != desired:
            return 'support_next' if current < desired else 'support_prev'
        if observed.get('owned_support_full') is not True:
            raise RuntimeError('owned_support_incomplete')
        if options.get('borrow_card_id') and (not matches(observed.get('borrowed_support') or {}, options)):
            if context.get('support_selected'):
                raise RuntimeError('borrowed_support_selection_unverified')
            return 'support_open_friend'
        if observed.get('support_full') is not True:
            raise RuntimeError('support_not_full')
        return 'continue_support'
    if page == 'support_settings':
        return 'support_filter_tab'
    if page == 'support_filter':
        if not context.get('support_filter_reset'):
            return 'support_filter_reset'
        wanted = set(target_filters(options))
        states = observed.get('support_filters', {})
        if set(states) != set(FILTER_LABELS) or any((v is None for v in states.values())):
            raise RuntimeError('support_filter_state_unverified')
        for i, label in enumerate(FILTER_LABELS):
            if states[label] != (label in wanted):
                return f'support_filter_{i}'
        return 'support_filter_apply'
    if page == 'support_friend':
        if not context.get('support_filters_applied'):
            return 'support_settings'
        if 'support_select_friend' in actions:
            return 'support_select_friend'
        if not observed.get('support_rows'):
            raise RuntimeError('support_friend_list_not_ready')
        if not observed.get('support_list_bottom') and context.get('support_scrolls', 0) < MAX_SCAN_SCROLLS:
            return 'support_scroll'
        if context.get('support_refreshes', 0) < MAX_REFRESHES and 'support_refresh' in actions:
            return 'support_refresh'
        raise RuntimeError('requested_borrow_card_not_found')
    return None

def authorize(action, observed, context):
    options = context.get('support_options')
    if not options:
        if action in ACTION_PAGES:
            raise RuntimeError('support_selection_not_requested')
        return
    if action in ('auto_support', 'confirm_auto_support'):
        raise RuntimeError('explicit_support_forbids_auto_compilation')
    if action in ACTION_PAGES:
        if context.get('mode') != 'start' or context.get('start_submitted'):
            raise RuntimeError('support_preparation_not_authorized')
        if choose(observed, context) != action:
            raise RuntimeError('support_action_not_current')
        if action == 'support_select_friend':
            candidate = observed['actions'][action]['card']
            if candidate.get('selectable') is not True or not matches(candidate, options):
                raise RuntimeError('borrow_candidate_unverified')
    if action == 'continue_support':
        if observed.get('support_deck_index') != options.get('support_deck', context.get('support_origin_deck', observed.get('support_deck_index'))) or type(observed.get('support_deck_index')) is not int or observed.get('owned_support_full') is not True:
            raise RuntimeError('support_deck_unverified')
        if options.get('borrow_card_id') and (not matches(observed.get('borrowed_support') or {}, options)):
            raise RuntimeError('borrowed_support_unverified')
    if action == 'start_autonomous' and context.get('verified_support', {}).get('options') != options:
        raise RuntimeError('support_preparation_incomplete')

def after_action(action, before, after, context):
    if not context.get('support_options'):
        return
    if action in ACTION_PAGES:
        context.pop('verified_support', None)
        if after['page'] not in NEXT_PAGES[action]:
            raise RuntimeError('support_transition_unverified:' + action)
    if action in ('support_prev', 'support_next'):
        expected = (before['support_deck_index'] - 1 + (1 if action == 'support_next' else -1)) % 10 + 1
        if after.get('support_deck_index') != expected:
            raise RuntimeError('support_deck_switch_unverified')
    if action == 'support_open_friend':
        context['support_origin_deck'] = before['support_deck_index']
    elif action == 'support_settings':
        context['support_filter_open'] = True
    elif action == 'support_filter_reset':
        states = after.get('support_filters', {})
        if set(states) != set(FILTER_LABELS) or any((v is not False for v in states.values())):
            raise RuntimeError('support_filter_reset_unverified')
        context['support_filter_reset'] = True
    elif action == 'support_filter_apply':
        context['support_filters_applied'] = True
        context.pop('support_filter_open', None)
    elif action.startswith('support_filter_') and action.removeprefix('support_filter_').isdigit():
        label = before['actions'][action]['label']
        if after.get('support_filters', {}).get(label) is not (not before['support_filters'][label]):
            raise RuntimeError('support_filter_toggle_unverified')
    elif action == 'support_select_friend':
        context['support_selected'] = True
        authorize('continue_support', after, context)
    elif action == 'support_scroll':
        context['support_scrolls'] = context.get('support_scrolls', 0) + 1
    elif action == 'support_refresh':
        context['support_refreshes'] = context.get('support_refreshes', 0) + 1
        context['support_scrolls'] = 0
    elif action == 'continue_support':
        if after['page'] not in ('final_confirm', 'final_autonomous', 'autonomous_tutorial'):
            raise RuntimeError('support_continue_unverified')
        context['verified_support'] = dict(options=dict(context['support_options']), deck=before['support_deck_index'], borrowed_support=before.get('borrowed_support'), source_frame_sha256=before.get('frame_sha256'))
