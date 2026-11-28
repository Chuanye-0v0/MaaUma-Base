"""Shared, read-only identity for the game's paged instruction overlays.

Only the footer pager and its Continue/Close control are actionable; pictures
and feature descriptions never become task actions or event identity.
"""
from functools import lru_cache
from pathlib import Path
import re
import cv2
import numpy as np

@lru_cache(maxsize=2)
def button_template(name):
    return cv2.imread(str(Path(__file__).parent / 'story_event' / 'assets' / ('help-' + name + '.png')))

def pager_identity(image):
    if image is None or image.shape[:2] != (1280, 720):
        return None
    pager = cv2.cvtColor(image[1148:1166, 240:480], cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(pager)
    selected = (h >= 30) & (h <= 55) & (s > 150) & (v > 180)
    unselected = (h >= 5) & (h <= 25) & (s > 150) & (v > 80) & (v < 160)
    _, labels, stats, centers = cv2.connectedComponentsWithStats((selected | unselected).astype(np.uint8))
    dots = sorted(((center[0], center[1], bool(selected[labels == i].any())) for i, (stat, center) in enumerate(zip(stats, centers)) if i and 6 <= stat[2] <= 12 and (6 <= stat[3] <= 12) and (20 <= stat[4] <= 110)))
    if not (3 <= len(dots) <= 12 and sum((dot[2] for dot in dots)) == 1 and (abs((dots[0][0] + dots[-1][0]) / 2 - 120) < 3) and all((abs(dot[1] - 8) < 3 for dot in dots)) and all((14 <= b[0] - a[0] <= 18 for a, b in zip(dots, dots[1:])))):
        return None
    index = next((i + 1 for i, dot in enumerate(dots) if dot[2]))
    return dict(page_index=index, page_count=len(dots))

def detect(image, tokens=()):
    pager = pager_identity(image)
    if pager is None:
        return None
    footer = ''.join((re.sub('\\s+', '', t['text']) for t in tokens if t.get('confidence', 0) >= 0.8 and t['bbox'][1] >= 970))
    if re.search('是否|确定要|确认|购买|消耗|支付|充值|付费|密码|验证码|协议|连接|重试', footer):
        return None
    matches = []
    for name, label in [('continue', '继续'), ('close', '关闭')]:
        template = button_template(name)
        if template is None:
            continue
        _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(image[1160:1250, 200:520], template, cv2.TM_CCOEFF_NORMED))
        if score >= 0.93:
            height, width = template.shape[:2]
            matches.append((label, [200 + point[0] + width // 2, 1160 + point[1] + height // 2]))
    if len(matches) != 1:
        return None
    label, point = matches[0]
    return dict(kind='tutorial_pager', action_id='dismiss_passive_tutorial', button=label, point=point, **pager, content_key=f"tutorial:{pager['page_count']}:{pager['page_index']}:{label}")
