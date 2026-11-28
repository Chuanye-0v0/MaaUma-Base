"""Local recognition of single-button, already-completed reward notifications.

This is not a generic confirmation recognizer. Purchases, authentication and
domain-owned settlement screens must keep their own executors.
"""
import hashlib
import re
import cv2
import numpy as np
BUTTONS = {'关闭', '继续', '确定', '知道了'}
RISK = re.compile('购买|消耗|支付|充值|付费|补充|兑换|花费|是否|确认要|确定要|放弃|覆盖|替换|删除|重置|登录|密码|验证码|认证|协议|同意|连接|重试')
CONTROL = BUTTONS | {'取消', '确认', '领取', '购买', '前往', '返回', '使用', '兑换', '同意'}

def detect_date_change(image, tokens):
    """A server date notification, not permission to reset tasks or replay entry."""
    if image is None or image.shape[:2] != (1280, 720):
        return None
    words = [dict(t, text=re.sub('\\s+', '', t['text'])) for t in tokens if t.get('confidence', 0) >= 0.8]

    def inside(t, box):
        x0, y0, x1, y1 = t['bbox']
        a, b, c, d = box
        return a <= x0 < x1 <= c and b <= y0 < y1 <= d
    title = [t for t in words if t['confidence'] >= 0.95 and t['text'] == '日期变化' and inside(t, (200, 350, 520, 460))]
    message = [t for t in words if t['confidence'] >= 0.95 and t['text'] == '日期发生了变化' and inside(t, (180, 550, 540, 740))]
    foreground = [t for t in words if inside(t, (20, 340, 700, 920))]
    controls = [t for t in foreground if t['text'] in CONTROL]
    if len(title) != 1 or len(message) != 1 or len(controls) != 1 or RISK.search(''.join((t['text'] for t in foreground))):
        return None
    button = controls[0]
    if button['text'] != '确定' or button['confidence'] < 0.95 or (not inside(button, (240, 770, 480, 910))):
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] >= 25) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 190)
    white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 175)
    x0, y0, x1, y1 = button['bbox']
    cx, cy = ((x0 + x1) // 2, (y0 + y1) // 2)
    if float(green[385:435, 50:670].mean()) < 0.45 or float(white[465:770, 25:695].mean()) < 0.8 or float(green[cy - 20:cy + 20, cx - 80:cx + 80].mean()) < 0.35:
        return None
    return dict(kind='date_change_notice', action_id='acknowledge_date_change', point=[cx, cy], button='确定', content_key='date_change_notice')

def detect_factor_research(image, tokens):
    """A passive research-progress overlay, not a spending/selection dialog."""
    if image is None or image.shape[:2] != (1280, 720):
        return None
    words = [dict(t, text=re.sub('\\s+', '', t['text'])) for t in tokens if t.get('confidence', 0) >= 0.8]

    def inside(t, box):
        x0, y0, x1, y1 = t['bbox']
        a, b, c, d = box
        return a <= x0 < x1 <= c and b <= y0 < y1 <= d
    title = [t for t in words if t['confidence'] >= 0.9 and t['text'] == '因子研究' and inside(t, (140, 90, 590, 320))]
    notice = [t for t in words if t['confidence'] >= 0.9 and re.fullmatch('研究槽(?:提升|上升|增加|已满)[!！]?', t['text']) and inside(t, (120, 280, 610, 450))]
    foreground = [t for t in words if inside(t, (100, 80, 620, 1170))]
    controls = [t for t in foreground if t['text'] in CONTROL]
    if len(title) != 1 or len(notice) != 1 or len(controls) != 1 or RISK.search(''.join((t['text'] for t in foreground))):
        return None
    button = controls[0]
    if button['text'] != '继续' or button['confidence'] < 0.95 or (not inside(button, (200, 980, 520, 1160))):
        return None
    x0, y0, x1, y1 = button['bbox']
    cx, cy = ((x0 + x1) // 2, (y0 + y1) // 2)
    hsv = cv2.cvtColor(image[cy - 20:cy + 20, cx - 80:cx + 80], cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] >= 25) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 190)
    if float(green.mean()) < 0.35:
        return None
    return dict(kind='factor_research_progress', action_id='continue_factor_research_progress', point=[cx, cy], button='继续', content_key='factor_research_progress')

def detect(image, tokens):
    if image is None or image.shape[:2] != (1280, 720):
        return None
    date_change = detect_date_change(image, tokens)
    if date_change:
        return date_change
    research = detect_factor_research(image, tokens)
    if research:
        return research
    words = [dict(t, text=re.sub('\\s+', '', t['text'])) for t in tokens if t.get('confidence', 0) >= 0.8]
    buttons = [t for t in words if t['text'] in BUTTONS and 100 <= t['bbox'][0] < t['bbox'][2] <= 620 and (450 <= t['bbox'][1] < t['bbox'][3] <= 1230)]
    if len(buttons) != 1 or buttons[0]['confidence'] < 0.95:
        return None
    button = buttons[0]
    x0, y0, x1, y1 = button['bbox']
    cx, cy = ((x0 + x1) // 2, (y0 + y1) // 2)
    if not 240 <= cx <= 480:
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 175)
    if white[cy - 20:cy + 20, cx - 80:cx + 80].mean() < 0.65:
        return None
    green = (hsv[:, :, 0] >= 25) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 190)
    rows = green[20:cy - 100, 25:695].mean(axis=1) > 0.45
    runs = np.flatnonzero(np.convolve(rows.astype(int), np.ones(10, int), 'valid') == 10)
    if not len(runs):
        return None
    header_bottom = int(runs[-1]) + 30
    if cy - header_bottom < 100:
        return None
    foreground = [t for t in words if t['bbox'][1] >= header_bottom - 100 and t['bbox'][3] <= y1 + 20]
    text = ''.join((t['text'] for t in foreground))
    if RISK.search(text) or len([t for t in foreground if t['text'] in CONTROL]) != 1:
        return None
    reliable = ''.join((t['text'] for t in foreground if t['confidence'] >= 0.95 and t is not button))
    reward = bool(re.search('已(?:经)?(?:获得|领取|领取完)[^。！？?]{0,35}奖励|获得了(?:以上|下列|如下)奖励|领取成功|奖励已(?:全部)?领取', reliable))
    notice = '通知' in reliable and bool(re.search('已(?:完成|结束|解锁|开放)|现已开放', reliable))
    if not reward and (not (notice and button['text'] == '关闭')):
        return None
    return dict(kind='reward_received' if reward else 'passive_notice', action_id='dismiss_passive_popup', point=[cx, cy], button=button['text'], content_key=hashlib.sha256(reliable.encode('utf8')).hexdigest())
