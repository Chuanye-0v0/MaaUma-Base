"""Local recognition of bounded passive notifications and activity invitations.

This is not a generic confirmation recognizer. Purchases, authentication and
domain-owned settlement screens must keep their own executors.
"""
import hashlib
import re
import cv2
from core.jjc.formation import _control_state
BUTTONS = {'关闭', '继续', '确定', '知道了'}
RISK = re.compile('购买|消耗|支付|充值|付费|补充|兑换|花费|是否|确认要|确定要|放弃|覆盖|替换|删除|重置|登录|密码|验证码|认证|协议|同意|连接|重试')
CONTROL = BUTTONS | {'取消', '确认', '领取', '购买', '前往', '返回', '使用', '兑换', '同意'}

def _notice_control_enabled(image, cx, cy):
    return _control_state(image, (cx - 140, cy - 45, cx + 140, cy + 45)) is True

def _date_panel_visible(image, button):
    """The date message is inside one large rounded foreground card."""
    x0, y0, x1, y1 = button['bbox']
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    contours, _ = cv2.findContours(cv2.Canny(gray, 35, 100), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if x <= 80 and x + w >= 640 and (300 <= y <= 500) and (300 <= h <= 650) and (x <= x0 < x1 <= x + w) and (y <= 550) and (y1 + 20 <= y + h <= 1000):
            return True
    return False

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
    x0, y0, x1, y1 = button['bbox']
    cx, cy = ((x0 + x1) // 2, (y0 + y1) // 2)
    if not _date_panel_visible(image, button) or not _notice_control_enabled(image, cx, cy):
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
    if not _notice_control_enabled(image, cx, cy):
        return None
    return dict(kind='factor_research_progress', action_id='continue_factor_research_progress', point=[cx, cy], button='继续', content_key='factor_research_progress')

def detect_factor_research_invitation(image, tokens):
    """Cancel only the report-held invitation to the factor-research activity."""
    if image is None or image.shape[:2] != (1280, 720):
        return None
    words = [dict(t, text=re.sub('\\s+', '', t['text'])) for t in tokens if t.get('confidence', 0) >= 0.8]

    def inside(t, box):
        x0, y0, x1, y1 = t['bbox']
        a, b, c, d = box
        return a <= x0 < x1 <= c and b <= y0 < y1 <= d
    foreground = [t for t in words if inside(t, (20, 350, 700, 930))]
    text = ''.join((t['text'] for t in foreground))
    if RISK.search(text) or re.search('付款|收费|账号|帐号|账户|顶号|别处|其他设备|异地', text):
        return None
    title = [t for t in foreground if t['confidence'] >= 0.95 and t['text'] == '活动确认' and inside(t, (240, 375, 480, 450))]
    report = [t for t in foreground if t['confidence'] >= 0.95 and t['text'] == '您持有因子研究报告' and inside(t, (230, 690, 490, 745))]
    question = [t for t in foreground if t['confidence'] >= 0.95 and re.fullmatch('要前往活动页面吗[?？]', t['text']) and inside(t, (230, 740, 500, 785))]
    if len(title) != 1 or len(report) != 1 or len(question) != 1:
        return None
    controls = [t for t in foreground if t['text'] in CONTROL | {'前往活动首页'}]
    if len(controls) != 2 or {t['text'] for t in controls} != {'取消', '前往活动首页'}:
        return None
    cancel = next((t for t in controls if t['text'] == '取消'))
    forward = next((t for t in controls if t['text'] == '前往活动首页'))
    if cancel['confidence'] < 0.95 or forward['confidence'] < 0.95 or (not inside(cancel, (100, 790, 310, 880))) or (not inside(forward, (390, 790, 660, 880))):
        return None
    x0, y0, x1, y1 = cancel['bbox']
    cx, cy = ((x0 + x1) // 2, (y0 + y1) // 2)
    if not all((_date_panel_visible(image, t) for t in controls)) or not _notice_control_enabled(image, cx, cy):
        return None
    return dict(kind='factor_research_invitation', action_id='cancel_factor_research_invitation', point=[cx, cy], button='取消', content_key='factor_research_invitation')

def detect(image, tokens):
    if image is None or image.shape[:2] != (1280, 720):
        return None
    date_change = detect_date_change(image, tokens)
    if date_change:
        return date_change
    research = detect_factor_research(image, tokens)
    if research:
        return research
    invitation = detect_factor_research_invitation(image, tokens)
    if invitation:
        return invitation
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
    panel = (hsv[:, :, 1] < 40) & (hsv[:, :, 2] > 210)
    _, _, components, _ = cv2.connectedComponentsWithStats(panel.astype('uint8'))
    panels = [s for s in components[1:] if 0 < s[0] <= 80 and 640 <= s[0] + s[2] < 720 and (20 < s[1] <= cy - 100) and (y1 + 20 <= s[1] + s[3] < 1260) and (s[4] > 0.5 * s[2] * s[3])]
    if len(panels) != 1:
        return None
    header_bottom = int(panels[0][1])
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
