"""Shared perception for verified, neutral presentation controls."""
import hashlib
import re
import cv2
import numpy as np

def continue_point(image):
    if image is None or image.shape[:2] != (1280, 720):
        return None
    gray = cv2.cvtColor(image[1140:1280, 580:720], cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, 1, 50, param1=80, param2=16, minRadius=25, maxRadius=45)
    candidates = [] if circles is None else [circle for circle in circles[0] if 630 <= 580 + circle[0] <= 680 and 1190 <= 1140 + circle[1] <= 1240]
    if len(candidates) != 1:
        return None
    cx, cy, radius = candidates[0]
    hsv = cv2.cvtColor(image[1140:1280, 580:720], cv2.COLOR_BGR2HSV)
    extent = max(1, round(radius) - 5)
    x0, x1 = (max(0, round(cx) - extent), min(140, round(cx) + extent))
    y0, y1 = (max(0, round(cy) - extent), min(140, round(cy) + extent))
    if float(((hsv[y0:y1, x0:x1, 1] < 60) & (hsv[y0:y1, x0:x1, 2] > 210)).mean()) < 0.65:
        return None
    orange = ((hsv[:, :, 0] >= 5) & (hsv[:, :, 0] <= 25) & (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 80)).astype(np.uint8)
    count, _, stats, centers = cv2.connectedComponentsWithStats(orange)
    arrows = [(stats[index], centers[index]) for index in range(1, count) if 24 <= stats[index, 2] <= 40 and 20 <= stats[index, 3] <= 40 and (stats[index, 4] >= 400) and ((centers[index][0] - cx) ** 2 + (centers[index][1] - cy) ** 2 <= 12 ** 2)]
    if len(arrows) != 1:
        return None
    return [round(580 + float(cx)), round(1140 + float(cy))]

def champions_round_start_point(image, tokens):
    """CM's blurred winged-medal hub plus sharp round/start/click foreground.

    The round/finals label is presentation identity only, never progress or an
    admission. Neither a generic Start label nor an unknown page is authority.
    """
    if image is None or image.shape[:2] != (1280, 720):
        return None

    def one(pattern, bounds, width, height):
        x0, y0, x1, y1 = bounds
        matches = [t for t in tokens if t.get('confidence', 0) >= 0.95 and re.fullmatch(pattern, re.sub('\\s+', '', t.get('text', ''))) and isinstance(t.get('bbox'), (list, tuple)) and (len(t['bbox']) == 4) and (x0 <= t['bbox'][0] < t['bbox'][2] <= x1) and (y0 <= t['bbox'][1] < t['bbox'][3] <= y1) and (width <= t['bbox'][2] - t['bbox'][0]) and (height <= t['bbox'][3] - t['bbox'][1])]
        return matches[0]['bbox'] if len(matches) == 1 else None
    ordinal = one('(?:第[0-9一二三四五六七八九十]+轮|决赛轮)', (170, 480, 560, 670), 190, 70)
    start = one('开始[!！]', (170, 630, 560, 800), 190, 70)
    click = one('点击', (280, 1080, 450, 1170), 50, 30)
    if not ordinal or not start or (not click):
        return None
    centers = [(box[0] + box[2]) / 2 for box in (ordinal, start, click)]
    if max(centers) - min(centers) > 25 or ordinal[3] > start[1] + 10:
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    for x0, x1 in ((50, 220), (500, 670)):
        wing = hsv[110:295, x0:x1]
        blue = (wing[:, :, 0] >= 90) & (wing[:, :, 0] < 130) & (wing[:, :, 1] > 35) & (wing[:, :, 2] > 120)
        if float(blue.mean()) < 0.4 or cv2.Laplacian(gray[110:295, x0:x1], cv2.CV_64F).var() > 40:
            return None
    medal = hsv[100:280, 260:465]
    ribbon = hsv[300:385, 200:520]
    if float(((medal[:, :, 0] >= 10) & (medal[:, :, 0] < 40) & (medal[:, :, 1] > 80) & (medal[:, :, 2] > 100)).mean()) < 0.16 or float(((ribbon[:, :, 0] >= 5) & (ribbon[:, :, 0] < 35) & (ribbon[:, :, 1] > 70) & (ribbon[:, :, 2] > 120)).mean()) < 0.1:
        return None
    for index, box in enumerate((ordinal, start, click)):
        x, y, r, b = map(int, box)
        minimum = 80 if index == 2 else 300
        if cv2.Laplacian(gray[y:b, x:r], cv2.CV_64F).var() < minimum:
            return None
    return [round(centers[2]), round((y + b) / 2)]

def detect(image, tokens):
    """Fallback only for unowned presentation pages, never text-only 'skip'.

Keep login's proven circle/color/glyph constraints. Different characters,
reward types and animation backgrounds do not need individual templates.
"""
    from core.passive_popup import RISK, CONTROL
    words = [re.sub('\\s+', '', token['text']) for token in tokens if token.get('confidence', 0) >= 0.8]
    risk_text = ''.join(words).replace('登录奖励', '').replace('登陆奖励', '')
    if RISK.search(risk_text) or any((word in CONTROL for word in words)):
        return None
    point = continue_point(image)
    button = 'round_double_arrow'
    if point is None:
        point = champions_round_start_point(image, tokens)
        button = 'champions_round_start'
    if point is None:
        return None
    return dict(kind='presentation_skip', action_id='skip_passive_presentation', point=point, button=button, content_key=hashlib.sha256('|'.join(words).encode('utf8')).hexdigest())
