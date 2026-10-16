"""Explicit event artwork whitelist. Unknown carousel entries never authorize a tap."""
import hashlib
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from core.page_identity import PageIdentity
WHITELIST = {'farewell': ('banner-farewell', 'banner-farewell-live'), 'tachyon': ('banner-tachyon-live', 'banner-tachyon-video'), 'autumn_thoughts': ('banner-autumn-thoughts', 'banner-autumn-thoughts-native')}
STORY_EVENT_IDS = frozenset({'farewell', 'autumn_thoughts'})

@lru_cache(maxsize=16)
def _template(name):
    return cv2.imread(str(Path(__file__).with_name('assets') / (name + '.png')))

def carousel_identity(image):
    """Read the top carousel's round, equally spaced indicators; never a tap."""
    hsv = cv2.cvtColor(image[327:345, 495:610], cv2.COLOR_BGR2HSV)
    selected = cv2.inRange(hsv, np.array([35, 180, 180]), np.array([45, 255, 255]))
    inactive = cv2.inRange(hsv, np.array([10, 175, 90]), np.array([17, 240, 145]))
    circles = []
    for chosen, mask in ((True, selected), (False, inactive)):
        count, _, stats, centers = cv2.connectedComponentsWithStats(mask)
        for index in range(1, count):
            _, _, w, h, area = stats[index]
            if 6 <= w <= 14 and 6 <= h <= 14 and (abs(w - h) <= 2) and (0.58 <= area / (w * h) <= 0.9):
                x, y = centers[index]
                circles.append((float(x) + 495, float(y) + 327, chosen))
    circles.sort()
    if not 2 <= len(circles) <= 6 or sum((c[2] for c in circles)) != 1 or abs((circles[0][0] + circles[-1][0]) / 2 - 552) > 3 or (max((c[1] for c in circles)) - min((c[1] for c in circles)) > 2) or any((not 20 <= b[0] - a[0] <= 24 for a, b in zip(circles, circles[1:]))):
        return None
    return dict(slot_count=len(circles), selected_slot=next((i for i, c in enumerate(circles) if c[2])))

def observe_banner(raw):
    result = dict(page='unknown', actions={}, frame_sha256=hashlib.sha256(raw).hexdigest())
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return result

    def score(name, box):
        a, b, c, d = box
        source = _template(name)
        if source is None:
            return 0.0
        return float(cv2.minMaxLoc(cv2.matchTemplate(image[b:d, a:c], source, cv2.TM_CCOEFF_NORMED))[1])
    if not PageIdentity(image).home():
        return result
    result['page'] = 'home'
    dots = carousel_identity(image)
    if dots:
        result['carousel'] = dots
    region = (450, 155, 660, 285)
    if max((score(name, region) for name in ('banner-transfer', 'banner-transfer-autumn-native'))) >= 0.9:
        result['banner_excluded'] = 'special_transfer'
        result['actions']['next_banner'] = dict(swipe=[635, 245, 455, 245, 350])
        return result
    scores = {key: max((score(name, region) for name in names)) for key, names in WHITELIST.items()}
    result['banner_scores'] = scores
    matches = [key for key, value in scores.items() if value >= 0.9 and all((value - other >= 0.12 for name, other in scores.items() if name != key))]
    if len(matches) == 1:
        result['banner_id'] = matches[0]
        result['actions']['event_banner'] = dict(point=[550, 245])
    return result
