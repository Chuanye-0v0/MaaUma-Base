"""Numbered three-column navigation, corrected against visible static card art."""
from functools import lru_cache
from pathlib import Path
import re
import cv2
from core.daily_legend.catalog import opponent

def help_close(tokens):
    """The first-entry explanation overlay, not an opponent or race dialog."""

    def found(text, box):
        x0, y0, x1, y1 = box
        return [t for t in tokens if t['confidence'] >= 0.95 and re.sub('\\s+', '', t['text']) == text and (x0 <= t['bbox'][0] < t['bbox'][2] <= x1) and (y0 <= t['bbox'][1] < t['bbox'][3] <= y1)]
    close = found('关闭', (220, 1160, 505, 1250))
    daily = found('日常', (100, 30, 280, 110)) and found('传奇赛事', (70, 90, 330, 165))
    footer = ''.join((re.sub('\\s+', '', t['text']) for t in tokens if t['confidence'] >= 0.95 and 20 <= t['bbox'][0] < t['bbox'][2] <= 690 and (970 <= t['bbox'][1] < t['bbox'][3] <= 1160)))
    limited = '名人堂' in footer and '传奇赛事' in footer
    if len(close) == 1 and found('帮助', (550, 1160, 700, 1250)) and (daily or limited):
        x0, y0, x1, y1 = close[0]['bbox']
        return dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    return None

@lru_cache(maxsize=46)
def template(number):
    image = cv2.imread(str(Path(__file__).with_name('assets') / f'{number:02d}.png'), 0)
    if image is None:
        raise RuntimeError('daily_legend_template_missing')
    return image

def match_card(gray, number):
    art = template(number)
    scores = cv2.matchTemplate(gray[347:759, 20:512], art, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(scores)
    if score < 0.88:
        return None
    scores[max(0, y - 18):y + 19, max(0, x - 18):x + 19] = -1
    if score - float(scores.max()) < 0.04:
        return None
    x, y = (x + 20, y + 347)
    column = min(range(3), key=lambda c: abs(x - (37 + c * 165)))
    if abs(x - (37 + column * 165)) > 8:
        return None
    return dict(number=number, column=column, art_y=y, score=round(score, 4), point=[round((x + 66) * 720 / 531), round((y + 22) * 1280 / 945)])

def locate(image, number):
    target = opponent(number)
    gray = cv2.cvtColor(cv2.resize(image, (531, 945)), cv2.COLOR_BGR2GRAY)
    found = match_card(gray, number)
    if found:
        return dict(target=target, found=found, action=dict(point=found['point']))
    anchors = [m for n in range(1, 47, 3) if (m := match_card(gray, n))]
    if not anchors:
        return None
    anchor = max(anchors, key=lambda m: m['score'])
    delta = target['row'] - opponent(anchor['number'])['row']
    projected_y = anchor['art_y'] + 124 * delta
    shift = round((projected_y - 535) * 1280 / 945)
    shift = max(-360, min(360, shift))
    if abs(shift) < 50:
        return dict(target=target, anchor=anchor, reason='target_art_not_verified')
    return dict(target=target, anchor=anchor, action=dict(swipe=[360, 740 + shift // 2, 360, 740 - shift // 2, 450]))

def authorize_list(action, current, expected, number):
    if current.get('page') != 'daily_legend_list' or expected.get('page') != current['page']:
        raise RuntimeError('daily_legend_list_not_bound')
    a, b = (current.get('listing', {}), expected.get('listing', {}))
    if a.get('target') != opponent(number) or a.get('target') != b.get('target'):
        raise RuntimeError('daily_legend_target_not_bound')
    key = 'found' if action == 'choose_opponent' else 'anchor'
    if action not in ('choose_opponent', 'scroll_opponents') or not a.get(key) or (not b.get(key)):
        raise RuntimeError('daily_legend_action_not_allowed')
    if a[key]['number'] != b[key]['number'] or a[key]['column'] != b[key]['column'] or abs(a[key]['art_y'] - b[key]['art_y']) > 3:
        from core.action_recovery import ReobserveRequired
        if action in current.get('actions', {}):
            raise ReobserveRequired(action, current)
        raise RuntimeError('daily_legend_list_moved')
    candidate = current['actions'].get(action)
    if not candidate:
        raise RuntimeError('daily_legend_action_not_allowed')
    return candidate
