"""Fixed UI anchors from our own captures; no identity or resource-value inference."""
import json
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
ASSETS = Path(__file__).with_name('page_assets')

@lru_cache(maxsize=1)
def _manifest():
    return json.loads((ASSETS / 'anchors.json').read_text(encoding='utf8'))

@lru_cache(maxsize=128)
def _template(name, scale=1):
    image = cv2.imread(str(ASSETS / name))
    return cv2.resize(image, None, fx=scale, fy=scale) if image is not None and scale != 1 else image

class PageIdentity:
    """Per-frame, lazy visual fallback. A match never authorizes a click itself."""

    def __init__(self, image):
        self.image = image
        self.cache = {}

    def find(self, text, box=(0, 0, 720, 1280), *, group='hub'):
        key = (text, tuple(box), group)
        if key in self.cache:
            return self.cache[key]
        found = []
        if self.image is not None and self.image.shape[:2] == (1280, 720):
            for anchor in _manifest():
                if anchor['text'] != text or anchor['group'] != group:
                    continue
                a, b, c, d = anchor['search']
                a, b, c, d = (max(a, box[0]), max(b, box[1]), min(c, box[2]), min(d, box[3]))
                scales = (1, 0.99, 1.01) if text == '养成' and group == 'hub' else (1,)
                for scale in scales:
                    source = _template(anchor['file'], scale)
                    if source is None or c - a < source.shape[1] or d - b < source.shape[0]:
                        continue
                    region = self.image[b:d, a:c]
                    _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(region, source, cv2.TM_CCOEFF_NORMED))
                    x, y = point
                    h, w = source.shape[:2]
                    error = float(np.abs(region[y:y + h, x:x + w].astype(np.float32) - source).mean())
                    if score >= 0.94 and error <= 18:
                        found.append(dict(text=text, confidence=float(score), bbox=[a + x, b + y, a + x + w, b + y + h], source='local_ui_template'))
                        break
        unique = []
        for token in found:
            a, b, c, d = token['bbox']
            if not any((abs(a + c - (t['bbox'][0] + t['bbox'][2])) <= 24 and abs(b + d - (t['bbox'][1] + t['bbox'][3])) <= 24 for t in unique)):
                unique.append(token)
        result = unique[0] if len(unique) == 1 else None
        self.cache[key] = result
        return result

    def home(self, tokens=()):
        image = self.image
        if image is None or image.shape[:2] != (1280, 720):
            return False
        hsv = cv2.cvtColor(image[1230:1265, 285:460], cv2.COLOR_BGR2HSV)
        selected = ((hsv[:, :, 0] >= 80) & (hsv[:, :, 0] <= 115) & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 150)).mean() > 0.2

        def control(text, box):
            visual = self.find(text, box)
            if visual:
                return True
            labels = [t for t in tokens if t.get('confidence', 0) >= 0.9 and t['text'].replace(' ', '') == text and (box[0] <= t['bbox'][0] < t['bbox'][2] <= box[2]) and (box[1] <= t['bbox'][1] < t['bbox'][3] <= box[3])]
            if len(labels) != 1:
                return False
            a, b, c, d = labels[0]['bbox']
            patch = cv2.cvtColor(image[b:d, a:c], cv2.COLOR_BGR2HSV)
            green = (patch[:, :, 0] >= 25) & (patch[:, :, 0] <= 85) & (patch[:, :, 1] > 80)
            return bool(green.any() and np.median(patch[:, :, 2][green]) > 180 and (((patch[:, :, 1] < 60) & (patch[:, :, 2] > 220)).mean() > 0.08))
        return bool(selected and control('菜单', (580, 25, 715, 105)) and control('养成', (380, 1000, 720, 1170)))

    def races(self):
        return bool(self.find('赛事', (0, 130, 300, 230)) and (self.find('日常安排', (70, 1010, 360, 1140)) or self.find('团队竞技场', (70, 850, 360, 990))))

    def story_surface(self):
        return any((self.find(text, group='event') for text in ('活动剧情', '剧情播放确认', '播放确认', '跳过确认', '轮盘德比', '轮盘设置')))

    def dialog_above(self, button_y):
        """A modal header, not merely a white patch beneath an OCR label."""
        hsv = cv2.cvtColor(self.image[20:button_y - 40, 30:690], cv2.COLOR_BGR2HSV)
        green = (hsv[:, :, 0] >= 25) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 190)
        rows = (green.mean(axis=1) > 0.8).astype(np.uint8)
        return bool(np.any(np.convolve(rows, np.ones(15), mode='valid') == 15) or self.find('兑换积分到期通知', (150, 300, 600, button_y - 40), group='notice'))
