"""Selected-card inventory fields; uncertain markers remain unknown."""
import re
import cv2
import numpy as np

def short_name(full_name, support_type=''):
    if not support_type:
        return full_name
    character = re.sub('^[\\[【].*[\\]】]\\s*', '', full_name)
    if support_type == '智' and character in ('目白高峰', '目白拉莫娜'):
        return '智高峰'
    if support_type == '速' and character == '丸善斯基' and any((t in full_name for t in ('感伤警报', '感傷警報'))):
        return '速水司机'
    return support_type + character

def read_card(image, tokens):
    good = [t for t in tokens if t['confidence'] >= 0.9]
    badges = [t['text'] for t in good if 40 < t['bbox'][0] < 100 and 215 < t['bbox'][1] < 270]
    rarity = next(({'超稀有': 'SSR', '稀有': 'SR', '普通': 'R'}[s] for s in badges if s in ('超稀有', '稀有', '普通')), None)
    names = [t['text'].strip() for t in tokens if t['confidence'] >= 0.85 and 20 < t['bbox'][0] < 80 and (550 < t['bbox'][1] < 620) and (t['bbox'][2] < 350)]
    full_name = ' '.join(names)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    markers = []
    for x in (54, 85, 116, 147):
        region = hsv[528:538, x - 4:x + 4]
        cyan = ((region[:, :, 0] >= 80) & (region[:, :, 0] <= 105) & (region[:, :, 1] > 100) & (region[:, :, 2] > 130)).mean()
        gray = ((region[:, :, 1] < 50) & (region[:, :, 2] > 80) & (region[:, :, 2] < 235)).mean()
        markers.append(1 if cyan >= 0.7 else 0 if gray >= 0.7 else None)
    stage = sum(markers) if None not in markers and markers == sorted(markers, reverse=True) else None
    icon = hsv[224:258, 273:309]
    colored = icon[(icon[:, :, 1] > 130) & (icon[:, :, 2] > 150)]
    hue = float(np.median(colored[:, 0])) if len(colored) > 400 else -1
    support_type = '速' if 96 <= hue <= 108 else '智' if 74 <= hue <= 85 else ''
    return dict(full_name=full_name, short_name=short_name(full_name, support_type), rarity=rarity, support_type=support_type, breakthrough_stage=stage, variant_known=bool(re.match('^[\\[【].+[\\]】]\\s+\\S', full_name)))
