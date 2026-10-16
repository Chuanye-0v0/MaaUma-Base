"""Identify available event cards by their current labels, never by a fixed slot."""
import re
import unicodedata
NAMES = {'legend': {'传奇赛事', '限时传奇赛'}, 'champions': {'群英联赛'}}

def menu_entries(image, tokens):
    if image.shape[:2] != (1280, 720):
        return None
    good = [dict(t, text=re.sub('\\s+', '', unicodedata.normalize('NFKC', t['text']))) for t in tokens if t.get('confidence', 0) >= 0.95]
    headings = [t for t in good if t['text'] == '赛事活动' and 200 <= t['bbox'][0] < t['bbox'][2] <= 550 and (500 <= t['bbox'][1] < t['bbox'][3] <= 820)]
    if len(headings) != 1:
        return None
    cards = {}
    for kind, names in NAMES.items():
        labels = [t for t in good if t['text'] in names and 40 <= t['bbox'][0] < t['bbox'][2] <= 680 and (850 <= t['bbox'][1] < t['bbox'][3] <= 1060)]
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
        nearby = [t['text'] for t in good if max(0, cx - 160) <= t['bbox'][0] and t['bbox'][2] <= min(720, cx + 160) and (y - 210 <= t['bbox'][1] <= b + 25)]
        closed = any((any((w in text for w in ('即将', '后解锁', '未开放', '已结束'))) for text in nearby))
        ready = kind != 'legend' or ('活动中' in nearby and any((re.fullmatch('[0-3]/3', text) for text in nearby)))
        cards[kind] = dict(available=not closed, ready=ready, point=[cx, (y + b) // 2], bbox=label['bbox'], basis='named_card_closed' if closed else 'named_card')
    result = dict(page='events', actions={})
    for kind in NAMES:
        card = cards.get(kind)
        available = card['available'] if card else None
        basis = card['basis'] if card else None
        if card is None and len(cards) == 1:
            other, visible = next(iter(cards.items()))
            weak_target = any((any((name in re.sub('\\s+', '', t.get('text', '')) for name in NAMES[kind])) and t.get('confidence', 0) >= 0.8 and (700 <= t['bbox'][1] < 1060) for t in tokens))
            if abs(visible['point'][0] - 360) <= 75 and (not weak_target):
                available = False
                basis = 'centered_' + other + '_only'
        result[kind + '_available'] = available
        result[kind + '_availability_basis'] = basis
        if card and available and card['ready']:
            result['actions']['open_' + kind] = dict(point=card['point'])
    result['event_cards'] = cards
    result['availability_basis'] = result['legend_availability_basis']
    return result

def unavailable(observed, kind, endpoint):
    basis = observed.get(kind + '_availability_basis') or (observed.get('availability_basis') if kind == 'legend' else None)
    bases = {'named_card_closed', 'centered_' + ('champions' if kind == 'legend' else 'legend') + '_only'}
    return observed.get('page') == 'events' and observed.get('endpoint') == endpoint and (observed.get(kind + '_available') is False) and (basis in bases) and ('open_' + kind not in observed.get('actions', {}))

def skipped_result_verified(result, task, endpoint):
    kind = 'legend' if task == 'limited_legend' else 'champions'
    reason = 'limited_legend_unavailable_skipped' if kind == 'legend' else 'champions_event_unavailable_skipped'
    if result.get('ok') is not True or result.get('reason') != reason or result.get('skipped') is not True or (result.get('activity_completed') is not False) or (result.get('pending') is not False) or result.get('resumed_from') or result.get('account_conflict') or result.get('conflict_check_error') or (result.get('device_action_count_complete') is False) or (result.get('action_outcome', {}).get('execution_status') == 'uncertain') or (not unavailable(result.get('last_observation') or {}, kind, endpoint)):
        return False
    if kind == 'legend':
        return type(result.get('completed_races')) is int and result.get('completed_races') == 0 and (result.get('races') == []) and (result.get('tickets_before') is None) and (result.get('tickets_after') is None) and (result.get('items_used') == 0) and (result.get('refills') == 0)
    return result.get('completed_rounds') == 0 and result.get('rounds') == [] and (result.get('started') is False) and (result.get('pending_matches') == 0) and all((value == 0 for value in (result.get('resources_used') or {}).values()))
