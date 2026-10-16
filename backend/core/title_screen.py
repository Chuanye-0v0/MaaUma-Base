"""Shared native title anchor, independent of the blinking start prompt."""
from pathlib import Path
import re
import cv2
import numpy as np

def title_page(image, tokens):
    """Identify the public title surface independently of its animated caption."""
    if image is None or image.shape[:2] != (1280, 720):
        return None

    def within(token, box):
        a, b, c, d = token['bbox']
        x0, y0, x1, y1 = box
        return x0 <= a < c <= x1 and y0 <= b < d <= y1
    good = [t for t in tokens if t['confidence'] >= 0.9]
    version = [t for t in good if t['text'].startswith('Ver.') and within(t, (0, 0, 300, 70))]
    copyright_header = [t for t in good if 'Cygames' in t['text'] and within(t, (450, 0, 720, 70))]
    if len(version) != 1 or len(copyright_header) != 1:
        return None

    def official_footer_notice(t):
        return t['confidence'] >= 0.9 and within(t, (70, 1240, 645, 1280)) and (re.fullmatch('为维护未成年(?:人)?健康上网环境[，,]?本游戏不支持实名认证18岁以下的用户登录体验[。.]?', re.sub('\\s+', '', t['text'])) is not None)
    if any((t['confidence'] >= 0.75 and (not official_footer_notice(t)) and (any((label in t['text'] for label in ('登录', '密码', '公告', '关闭', '取消', '确定', '同意', '重试', '连接', '下载', '加载中', '返回标题'))) or (t['bbox'][1] < 1100 and any((label in t['text'] for label in ('验证', '实名'))))) for t in tokens)):
        return None
    footer_roles = [(('审批文号', 'ISBN'), (70, 1140, 645, 1195)), (('ICP备案',), (150, 1185, 570, 1230)), (('运营单位', '出版单位'), (70, 1210, 645, 1270))]
    if any((len([t for t in good if within(t, box) and all((label in re.sub('\\s+', '', t['text']) for label in labels))]) != 1 for labels, box in footer_roles)):
        return None
    return dict(point=[360, 1080])

def title_logo(image, tokens):
    if image is None or image.shape[:2] != (1280, 720):
        return None

    def within(t, box):
        a, b, c, d = t['bbox']
        x0, y0, x1, y1 = box
        return x0 <= a < c <= x1 and y0 <= b < d <= y1
    good = [t for t in tokens if t['confidence'] >= 0.9]
    version = any((t['text'].startswith('Ver.') and within(t, (0, 0, 300, 70)) for t in good))
    copyright_header = any(('Cygames' in t['text'] and within(t, (450, 0, 720, 70)) for t in good))
    dialog_visible = any((t['confidence'] >= 0.75 and t['bbox'][1] < 1100 and any((label in t['text'] for label in ('登录', '密码', '验证', '公告', '关闭', '取消', '确定', '同意', '重试', '连接'))) for t in tokens))
    if not version or not copyright_header or dialog_visible:
        return None
    logo = cv2.imread(str(Path(__file__).parent / 'account_inventory/assets/title_logo.png'), cv2.IMREAD_UNCHANGED)
    if logo is None:
        return None
    scores = cv2.matchTemplate(image[650:1020, 100:620], logo[:, :, :3], cv2.TM_SQDIFF_NORMED, mask=logo[:, :, 3])
    error, _, position, _ = cv2.minMaxLoc(scores)
    if not np.isfinite(error) or error > 0.015:
        return None
    height, width = logo.shape[:2]
    return dict(point=[100 + position[0] + width // 2, 650 + position[1] + height // 2], error=round(error, 5))
