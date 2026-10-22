"""Identify available event cards by their current labels, never by a fixed slot."""
import re
import unicodedata
NAMES = {'legend': {'传奇赛事', '限时传奇赛'}, 'champions': {'群英联赛'}, 'loh': {'限时英雄赛', '英雄联赛', '限时群英联赛', 'LEAGUEOFHEROES'}}

def menu_entries(image, tokens):
    if image.shape[:2] != (1280, 720):
        return None
    good = [dict(t, text=re.sub('\\s+', '', unicodedata.normalize('NFKC', t['text']))) for t in tokens if t.get('confidence', 0) >= 0.95]
    headings = [t for t in good if t['text'] == '赛事活动' and 200 <= t['bbox'][0] < t['bbox'][2] <= 550 and (100 <= t['bbox'][1] < t['bbox'][3] <= 820)]
    if len(headings) != 1:
        return None
    top = headings[0]['bbox'][3]
    cards = {}
    for kind, names in NAMES.items():
        labels = [t for t in good if t['text'] in names and 40 <= t['bbox'][0] < t['bbox'][2] <= 680 and (top < t['bbox'][1] < t['bbox'][3] <= 1170)]
        captions = []
        for label in labels:
            x, y, r, b = label['bbox']
            band = image[max(0, y - 3):min(1280, b + 8), max(0, x - 20):min(720, r + 20)]
            if band.size and float((band.min(axis=2) > 205).mean()) > 0.45:
                captions.append(label)
        if len(captions) != 1:
            continue
        label = captions[0]
        x, y, r, b = label['bbox']
        cx = (x + r) // 2
        names_on_menu = set().union(*NAMES.values())
        peers = [t for t in good if t['text'] in names_on_menu and top < t['bbox'][1] < t['bbox'][3] <= 1170 and (abs((t['bbox'][0] + t['bbox'][2]) // 2 - cx) <= 140)]
        lower = max([top, y - 210] + [(t['bbox'][3] + y) // 2 for t in peers if t['bbox'][3] < y])
        upper = min([b + 25] + [(b + t['bbox'][1]) // 2 for t in peers if t['bbox'][1] > b])
        nearby = [t['text'] for t in good if max(0, cx - 160) <= t['bbox'][0] and t['bbox'][2] <= min(720, cx + 160) and (lower <= t['bbox'][1] <= upper)]
        closed = any((any((w in text for w in ('即将', '后解锁', '未开放', '已结束'))) for text in nearby))
        ready = kind != 'legend' or ('活动中' in nearby and any((re.fullmatch('[0-3]/3', text) for text in nearby)))
        cards[kind] = dict(available=not closed, ready=ready, point=[cx, (y + b) // 2], bbox=label['bbox'], basis='named_card_closed' if closed else 'named_card')
    result = dict(page='events', actions={}, menu_scan_complete=False, navigation_actions={})
    for kind in NAMES:
        card = cards.get(kind)
        available = card['available'] if card else None
        basis = card['basis'] if card else None
        result[kind + '_available'] = available
        result[kind + '_availability_basis'] = basis
        if card and available and card['ready']:
            result['actions']['open_' + kind] = dict(point=card['point'])
    result['event_cards'] = cards
    result['availability_basis'] = result['legend_availability_basis']
    if top + 100 < 1120:
        result['navigation_actions'] = {'event_scroll_down': dict(start=[360, 1090], end=[360, max(top + 35, 760)], duration_ms=450), 'event_scroll_up': dict(start=[360, max(top + 35, 760)], end=[360, 1090], duration_ms=450)}
        result['navigation_actions']['scroll_events'] = dict(swipe=[360, 1090, 360, max(top + 35, 760)], duration_ms=450, effect='navigation')
    return result

def unavailable(observed, kind, endpoint):
    basis = observed.get(kind + '_availability_basis') or (observed.get('availability_basis') if kind == 'legend' else None)
    bases = {'named_card_closed'}
    return observed.get('page') == 'events' and observed.get('endpoint') == endpoint and (observed.get(kind + '_available') is False) and (basis in bases) and ('open_' + kind not in observed.get('actions', {}))

def skipped_result_verified(result, task, endpoint):
    if task not in {'limited_legend', 'champions_race'}:
        return False
    kind = 'legend' if task == 'limited_legend' else 'champions'
    reason = 'limited_legend_unavailable_skipped' if kind == 'legend' else 'champions_event_unavailable_skipped'
    if result.get('ok') is not True or result.get('reason') != reason or result.get('skipped') is not True or (result.get('activity_completed') is not False) or (result.get('pending') is not False) or result.get('resumed_from') or result.get('account_conflict') or result.get('conflict_check_error') or (result.get('device_action_count_complete') is False) or (result.get('action_outcome', {}).get('execution_status') == 'uncertain') or (not unavailable(result.get('last_observation') or {}, kind, endpoint)):
        return False
    if kind == 'legend':
        return type(result.get('completed_races')) is int and result.get('completed_races') == 0 and (result.get('races') == []) and (result.get('tickets_before') is None) and (result.get('tickets_after') is None) and (result.get('items_used') == 0) and (result.get('refills') == 0)
    return result.get('completed_rounds') == 0 and result.get('rounds') == [] and (result.get('started') is False) and (result.get('pending_matches') == 0) and all((value == 0 for value in (result.get('resources_used') or {}).values()))

def find_event(capture, scroll, kind, stop, *, max_scrolls=4):
    """Bounded multi-card search; scroll(action, candidate) navigates only.

    Missing/blank OCR never establishes absence. The event owner reobserves
    before opening the returned visible card. No device is created here.
    """
    if kind not in NAMES or type(max_scrolls) is not int or (not 0 <= max_scrolls <= 8):
        raise ValueError('event_menu_scan_options_invalid')
    observed = capture()
    seen = set()
    direction = 'event_scroll_down'
    for _ in range(max_scrolls):
        if stop.is_set() or observed.get('page') != 'events':
            break
        if 'open_' + kind in observed.get('actions', {}) or observed.get(kind + '_available') is False:
            break
        signature = tuple(sorted(((name, tuple(card['bbox'])) for name, card in observed.get('event_cards', {}).items())))
        if signature in seen:
            if direction == 'event_scroll_up':
                break
            direction = 'event_scroll_up'
            seen.clear()
        seen.add(signature)
        candidate = observed.get('navigation_actions', {}).get(direction)
        if not candidate:
            break
        scroll(direction, candidate)
        if stop.wait(0.3):
            break
        observed = capture()
    return observed
