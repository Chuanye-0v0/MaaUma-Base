"""Shared exact disconnect-modal perception and same-account session recovery."""
from core.autonomous_training.harvest_observer import _control_enabled

def _conflict_modal_button(image, tokens, expected_body):
    """The shared title is ambiguous; the complete body determines recovery."""
    if image is None or image.shape[:2] != (1280, 720):
        return None

    def inside(t, box):
        x0, y0, x1, y1 = box
        a, b, c, d = t['bbox']
        return t['confidence'] >= 0.9 and x0 <= a < c <= x1 and (y0 <= b < d <= y1)
    headers = [t for t in tokens if t['text'] == '连接冲突错误' and inside(t, (180, 370, 550, 460))]
    buttons = [t for t in tokens if t['text'] == '返回标题页面' and inside(t, (200, 780, 520, 890))]
    body_tokens = [t for t in tokens if 100 <= t['bbox'][0] < t['bbox'][2] <= 650 and 570 <= t['bbox'][1] < t['bbox'][3] <= 710]
    if any((t['confidence'] < 0.9 for t in body_tokens)):
        return None
    body = ''.join((t['text'] for t in sorted(body_tokens, key=lambda t: (t['bbox'][1] // 30, t['bbox'][0]))))
    body = body.replace('，', '').replace(',', '').replace(' ', '')
    if len(headers) != 1 or len(buttons) != 1 or body != expected_body:
        return None
    if not _control_enabled(image, buttons[0]):
        return None
    x0, y0, x1, y1 = buttons[0]['bbox']
    return {'point': [(x0 + x1) // 2, (y0 + y1) // 2]}

def conflict_button(image, tokens):
    """Other-device login is a hold, never a reconnect/retry authorization."""
    return _conflict_modal_button(image, tokens, '已在其他设备登录即将返回标题界面')

def disconnect_button(image, tokens):
    if image is None or image.shape[:2] != (1280, 720):
        return None
    retry = _conflict_modal_button(image, tokens, '即将返回标题界面请在网络环境稳定后重试')
    if retry:
        return retry
    labels = [('连接已断开', (200, 370, 520, 460)), ('由于长时间内未进行操作，', (150, 560, 570, 630)), ('或与服务器的连接发生中断，', (150, 610, 570, 670)), ('即将返回游戏标题页面', (150, 640, 570, 710)), ('返回标题页面', (200, 780, 520, 890))]
    matches = []
    for text, (x0, y0, x1, y1) in labels:
        found = [t for t in tokens if t['text'] == text and t['confidence'] >= 0.9 and (x0 <= t['bbox'][0] < t['bbox'][2] <= x1) and (y0 <= t['bbox'][1] < t['bbox'][3] <= y1)]
        if len(found) != 1:
            return None
        matches.append(found[0])
    body_tokens = [t for t in tokens if 100 <= t['bbox'][0] < t['bbox'][2] <= 650 and 560 <= t['bbox'][1] < t['bbox'][3] <= 710]
    expected_body = [text for text, _ in labels[1:-1]]
    if len(body_tokens) != len(expected_body) or any((t['confidence'] < 0.9 for t in body_tokens)) or [t['text'] for t in sorted(body_tokens, key=lambda t: t['bbox'][1])] != expected_body or (not _control_enabled(image, matches[-1])):
        return None
    x0, y0, x1, y1 = matches[-1]['bbox']
    return {'point': [(x0 + x1) // 2, (y0 + y1) // 2]}

class SessionRecovery:
    """One recovery per account attempt; task runners retain their own progress."""

    def __init__(self, runtime_factory, relogin):
        self.runtime_factory, self.relogin = (runtime_factory, relogin)
        self.attempted = False

    def __call__(self, observed=None):
        if observed is not None and observed.get('page') not in {'connection_interrupted', 'disconnected', 'blocked', 'auth_blocked', 'title', 'recent'}:
            return None
        runtime = self.runtime_factory()
        fresh = runtime.device.capture()
        if fresh.get('page') not in {'connection_interrupted', 'title', 'recent'}:
            return None
        if self.attempted:
            raise RuntimeError('session_recovery_limit')
        self.attempted = True
        return runtime.recover_connection(self.relogin, fresh)
