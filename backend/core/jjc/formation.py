"""Offline perception and fail-closed policy for JJC team formation."""
import hashlib
import re
import cv2
import numpy as np
_NATIVE_SIZE = (1280, 720)

def observe_formation(raw: bytes, tokens: list[dict]) -> dict | None:
    """Return one supported formation page and locally anchored candidates.

    The caller supplies local OCR tokens in native 720x1280 coordinates.  This
    module only publishes candidate points; it never sends input to a device.
    """
    if not isinstance(raw, bytes) or not isinstance(tokens, list):
        return None
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != _NATIVE_SIZE:
        return None
    good = _good_tokens(tokens)

    def find(text, box=(0, 0, 720, 1280)):
        matches = [token for token in good if token['text'] == text and _within(token, box)]
        return matches[0] if len(matches) == 1 else None

    def result(page, before=None, after=None, save_enabled=None):
        return dict(page=page, actions={}, team_score_before=before, team_score_after=after, formation_save_enabled=save_enabled, frame_sha256=hashlib.sha256(raw).hexdigest())

    def action(observed, name, token):
        if token is not None:
            x0, y0, x1, y1 = token['bbox']
            observed['actions'][name] = {'point': [(x0 + x1) // 2, (y0 + y1) // 2]}
    if find('最高团队等级更新！', (60, 130, 670, 370)) and find('获得奖励', (250, 570, 480, 710)):
        observed = result('team_rank_reward', after=_score(good, (220, 490, 500, 650)))
        action(observed, 'continue_team_rank_reward', find('点击', (280, 1020, 460, 1150)))
        return observed
    score_update = find('最高团队评分更新！', (60, 130, 670, 280))
    if find('最高团队等级更新！', (60, 130, 670, 280)) or score_update:
        observed = result('team_rank_update', after=_score(good, (220, 430, 520, 540)) if score_update else None)
        action(observed, 'close_team_rank_update', find('关闭', (280, 1100, 460, 1230)))
        return observed
    if find('功能解锁', (250, 280, 480, 400)) and find('功能已解锁', (250, 350, 490, 450)):
        observed = result('team_unlock')
        action(observed, 'close_team_unlock', find('关闭', (280, 850, 470, 980)))
        return observed
    if find('编成确认', (250, 350, 480, 470)) and find('即将完成编成', (200, 650, 520, 760)) and find('是否确认执行？', (200, 700, 520, 780)):
        before = _score(good, (150, 500, 320, 710))
        after = _score(good, (390, 500, 570, 710))
        observed = result('team_formation_save_confirm', before, after)
        confirm = find('确定', (430, 780, 620, 880))
        if confirm is not None and _control_state(image, (440, 780, 600, 880)) is True:
            action(observed, 'confirm_save_team_formation', confirm)
        return observed
    if find('编成确认', (250, 350, 480, 470)) and find('即将替换团队成员', (200, 650, 520, 760)):
        before = _score(good, (150, 500, 320, 710))
        after = _score(good, (390, 500, 570, 710))
        observed = result('team_formation_confirm', before, after)
        confirm = find('确定', (430, 780, 620, 880))
        if confirm is not None and _control_state(image, (440, 780, 600, 880)) is True:
            action(observed, 'confirm_auto_team_formation', confirm)
        return observed
    if find('团队竞技场', (0, 120, 250, 250)) and find('团队编成', (350, 880, 600, 1010)):
        observed = result('team_arena')
        action(observed, 'open_team_formation', find('团队编成', (350, 880, 600, 1010)))
        return observed
    if not find('团队编成', (0, 0, 180, 80)):
        return None
    before = _score(good, (155, 70, 370, 180))
    after = _score(good, (380, 70, 710, 180))
    save = find('编成确定', (260, 1020, 490, 1120))
    auto_enabled = _control_state(image, (490, 940, 630, 1030))
    save_enabled = _control_state(image, (280, 1035, 480, 1120)) if save else None
    if save_enabled is False and auto_enabled is not True:
        save_enabled = None
    observed = result('team_formation', before, after, save_enabled)
    auto = find('自动编成', (470, 930, 660, 1040))
    if auto is not None and auto_enabled is True:
        action(observed, 'auto_team_formation', auto)
    if save_enabled is True:
        action(observed, 'save_team_formation', save)
    if save_enabled is False:
        action(observed, 'formation_home', find('主页', (280, 1210, 430, 1280)))
    return observed

def choose_formation_action(observed: dict, context: dict) -> str | None:
    """Choose a bounded action only when a fresh receipt binds the transition."""
    if not isinstance(observed, dict) or not isinstance(context, dict):
        return None
    actions = observed.get('actions')
    if not isinstance(actions, dict):
        return None

    def allowed(name):
        return name if name in actions else None
    page = observed.get('page')
    if page == 'team_rank_reward':
        if context.get('formation_auto_confirmed') is True and context.get('formation_save_submitted') is True and context.get('formation_save_receipt_id') and (context.get('formation_rank_reward_acknowledged') is not True):
            return allowed('continue_team_rank_reward')
        return None
    if page == 'team_rank_update':
        return allowed('close_team_rank_update') if _formation_progressed(context) else None
    if page == 'team_unlock':
        return allowed('close_team_unlock') if _formation_progressed(context) else None
    if page == 'team_arena':
        return allowed('open_team_formation') if context.get('formation_requested') is True else None
    if page == 'team_formation_confirm':
        if context.get('formation_auto_requested') is True and context.get('formation_auto_confirmed') is not True:
            return allowed('confirm_auto_team_formation')
        return None
    if page == 'team_formation_save_confirm':
        if context.get('formation_auto_confirmed') is True and context.get('formation_save_submitted') is True and context.get('formation_save_receipt_id') and (context.get('formation_save_confirmed') is not True):
            return allowed('confirm_save_team_formation')
        return None
    if page != 'team_formation':
        return None
    if context.get('formation_requested') is True and context.get('formation_auto_requested') is not True and (observed.get('formation_save_enabled') is False):
        return allowed('auto_team_formation')
    if context.get('formation_auto_confirmed') is True and observed.get('formation_save_enabled') is True and (context.get('formation_save_submitted') is not True):
        return allowed('save_team_formation')
    if _verified_home(observed, context):
        return allowed('formation_home')
    return None

def _good_tokens(tokens):
    good = []
    for token in tokens:
        if not isinstance(token, dict) or not isinstance(token.get('text'), str):
            continue
        if not isinstance(token.get('confidence'), (int, float)) or token['confidence'] < 0.9:
            continue
        bbox = token.get('bbox')
        if not isinstance(bbox, list) or len(bbox) != 4 or (not all((isinstance(value, (int, float)) for value in bbox))):
            continue
        good.append(dict(token, text=token['text'].replace(' ', ''), bbox=[int(value) for value in bbox]))
    return good

def _within(token, box):
    x0, y0, x1, y1 = token['bbox']
    left, top, right, bottom = box
    return left <= x0 and top <= y0 and (x1 <= right) and (y1 <= bottom)

def _score(tokens, box):
    values = []
    for token in tokens:
        if not _within(token, box):
            continue
        text = token['text']
        if re.fullmatch('\\d{4,7}', text):
            values.append(int(text))
        elif re.fullmatch('\\d{1,3}(?:[,\\.]\\d{3})+', text):
            values.append(int(re.sub('[,\\.]', '', text)))
    return values[0] if len(values) == 1 else None

def _control_state(image, box):
    """Read a locally anchored control: active, inactive, or unobservable.

    A visible surface/text contrast is required, so blank patches cannot stand
    in for controls. Brightness separates an active surface from its muted
    disabled rendering without constraining its hue or saturation.
    """
    x0, y0, x1, y1 = box
    patch = image[max(0, y0):y1, max(0, x0):x1]
    if not patch.size:
        return None
    value = patch.max(axis=2)
    low, high = np.percentile(value, (2, 98))
    if high - low < 30 or high < 145:
        return None
    return bool(np.median(value) >= 170)

def _same_score(expected, actual):
    return type(expected) is int and type(actual) is int and (expected == actual)

def _formation_progressed(context):
    return context.get('formation_save_submitted') is True or context.get('formation_auto_confirmed') is True

def _verified_home(observed, context):
    return context.get('formation_auto_confirmed') is True and context.get('formation_verified') is True and (observed.get('formation_save_enabled') is False)
