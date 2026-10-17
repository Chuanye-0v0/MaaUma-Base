"""Region-anchored JJC decisions from local OCR and button evidence."""
import hashlib
import re
from pathlib import Path
from functools import lru_cache
import cv2
import numpy as np
from core.page_identity import PageIdentity
from core.ocr_regions import read_regions
from core.jjc.formation import _control_state

@lru_cache(maxsize=4)
def template(name):
    try:
        data = np.frombuffer((Path(__file__).parent / 'assets' / (name + '.png')).read_bytes(), np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE) if data.size else None
    except (OSError, cv2.error):
        return None

def observe_skip(raw, image):
    """Playback-only fast path: the exact skip glyph, not an arbitrary white circle."""
    if image.shape[:2] != (1280, 720):
        return None
    crop = cv2.cvtColor(image[1110:1230, 610:715], cv2.COLOR_BGR2GRAY)
    asset = template('skip')
    if (asset is None or asset.ndim != 2 or asset.dtype != crop.dtype
            or min(asset.shape) == 0 or asset.shape[0] > crop.shape[0] or asset.shape[1] > crop.shape[1]):
        return None
    _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(crop, asset, cv2.TM_CCOEFF_NORMED))
    if score < 0.96:
        return None
    h, w = asset.shape
    return dict(page='animation', actions={'skip': dict(point=[610 + point[0] + w // 2, 1110 + point[1] + h // 2])}, navigation_actions={}, rp=None, tokens=[], skip_confidence=float(score), frame_sha256=hashlib.sha256(raw).hexdigest())

def observe_results_fast(raw, image, ocr):
    """Playback-only result navigation; uncertain/overlaid frames use full recognition."""
    if image.shape[:2] != (1280, 720):
        return None
    button = image[1050:1180, 200:520]
    green = (button[:, :, 1] > 190) & (button[:, :, 0] < 120) & (button[:, :, 2] < 190)
    if float(green.mean()) < 0.25:
        return None
    tokens = []
    for text, box, scale in [('赛事结束', (150, 0, 590, 130), 0.5), ('继续', (200, 1050, 520, 1180), 2)]:
        x0, y0, x1, y1 = box
        rows, _ = ocr(cv2.resize(image[y0:y1, x0:x1], None, fx=scale, fy=scale))
        matches = [(points, word, confidence) for points, word, confidence in rows or [] if word.replace(' ', '') == text and confidence >= 0.9]
        if len(matches) != 1:
            return None
        points, word, confidence = matches[0]
        tokens.append(dict(text=word, confidence=float(confidence), bbox=[int(min((p[0] for p in points)) / scale) + x0, int(min((p[1] for p in points)) / scale) + y0, int(max((p[0] for p in points)) / scale) + x0, int(max((p[1] for p in points)) / scale) + y0]))
    result = observe(raw, tokens)
    return result if result['page'] == 'results' and 'continue_results' in result['actions'] else None

def observe_ready_fast(raw, image, ocr):
    """Read the ready-page controls only, never authorize race entry or purchases."""
    if image.shape[:2] != (1280, 720):
        return None
    button = image[1180:1230, 400:520]
    green = (button[:, :, 1] > 190) & (button[:, :, 0] < 120) & (button[:, :, 2] < 190)
    if float(green.mean()) < 0.25:
        return None
    tokens = read_regions(image, ocr, [(0, 0, 260, 80), (250, 1000, 470, 1120), (150, 1140, 580, 1250)])
    labels = {t['text'].replace(' ', '') for t in tokens if t['confidence'] >= 0.95}
    if '团队竞技场' not in labels:
        return None
    if not ({'缩短版：开', '查看各赛事结果'} <= labels or {'缩短版：关', '前往比赛', '查看结果'} <= labels):
        return None
    result = observe(raw, tokens)
    if result['page'] == 'race_ready' and set(result['actions']) in ({'enable_short'}, {'view_results'}):
        return result
    return None

def observe_summary_fast(raw, image, ocr):
    """Settlement-only anchors; resource-consuming pages always fall back."""
    if image.shape[:2] != (1280, 720):
        return None
    tokens = read_regions(image, ocr, [(0, 0, 590, 200), (390, 535, 720, 620), (0, 600, 330, 920), (30, 1100, 700, 1260)])
    result = observe(raw, tokens, settlement=True)
    if result['page'] == 'summary' and 'accelerate_summary' in result['actions']:
        headings = [t for t in tokens if t['text'].replace(' ', '') in {'获得奖励', '亲密度·粉丝数', '本月社团收获粉丝数'} and 600 <= t['bbox'][1] < 920]
        if any((visible_label(image, t['bbox']) for t in headings)):
            return result
    if result['page'] == 'rewards' and 'continue_rewards' in result['actions'] or (result['page'] == 'summary' and 'next' in result['actions']):
        button = result['actions'].get('continue_rewards') or result['actions']['next']
        x, y = button['point']
        if _control_state(image, (x - 80, y - 20, x + 80, y + 20)) is True:
            return result
    return None

def visible_label(image, box):
    x0, y0, x1, y1 = box
    patch = image[y0:y1, x0:x1]
    if not patch.size:
        return False
    low, high = np.percentile(patch.max(axis=2), (2, 98))
    return bool(high >= 170 and high - low >= 30)

def observe_weekly_settlement(image, tokens, *, allow_animation=False):
    """Shared passive weekly banner identity; rank digits never authorize input."""
    if image is None or image.shape[:2] != (1280, 720):
        return None
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.9]

    def within(t, box):
        a, b, c, d = t['bbox']
        x0, y0, x1, y1 = box
        return x0 <= a < c <= x1 and y0 <= b < d <= y1
    headings = [t for t in good if t['text'].rstrip('!！') in {'保持段位', '段位保持', '段位升级', '段位下降', '段位降级'} and within(t, (180, 300, 550, 580))]
    if len(headings) != 1:
        return None
    result = dict(page='weekly_settlement', actions={})
    buttons = [t for t in good if t['text'] == '继续' and within(t, (200, 1020, 520, 1140))]
    token, action = (None, None)
    if len(buttons) == 1:
        token, action = (buttons[0], 'continue_weekly')
    elif allow_animation and (not buttons) and (headings[0]['text'] == '保持段位') and within(headings[0], (180, 470, 550, 580)) and any((t['text'].startswith('等级') and within(t, (240, 420, 480, 650)) for t in good)):
        token, action = (headings[0], 'accelerate_weekly')
    if token:
        x0, y0, x1, y1 = token['bbox']
        result['actions'][action] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    return result

def observe_loading_fast(raw, image, ocr):
    """Two loading labels only; this shortcut never emits an action."""
    if image.shape[:2] != (1280, 720):
        return None
    tokens = read_regions(image, ocr, [(525, 45, 685, 115), (480, 1190, 715, 1280)])
    if any((t['confidence'] >= 0.75 and (t['text'].replace(' ', '') == '连接中' or (t['bbox'][1] > 1190 and '加载中' in t['text'])) for t in tokens)):
        return dict(page='loading', actions={}, navigation_actions={}, rp=None, tokens=tokens, frame_sha256=hashlib.sha256(raw).hexdigest())
    return None

def opponent_cards(image):
    """Locate the three enabled opponent cards, never their power/grade text.

    Reviewed name-strip palettes locate the rows; body edges and visible click
    areas confirm cards without requiring a white border or identical widths.
    A partial list, modal dimming or ambiguous click area has no candidates.
    Page identity and positive RP are checked separately by observe().
    """
    if image is None or image.shape[:2] != (1280, 720):
        return []
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    bands = []
    partial_strips = []
    narrow_fragments = []
    for low, high, saturation, value in ((5, 35, 125, 175), (40, 75, 125, 175), (80, 110, 125, 175), (120, 140, 15, 210)):
        colored = (hue >= low) & (hue <= high) & (hsv[:, :, 1] >= saturation) & (hsv[:, :, 2] >= value)
        rows = np.count_nonzero(colored, axis=1) >= 0.42 * width
        padded = np.pad(rows.astype(np.int8), (1, 1))
        starts = np.flatnonzero(np.diff(padded) == 1)
        ends = np.flatnonzero(np.diff(padded) == -1)
        strips = []
        for y0, y1 in zip(starts, ends):
            _, _, components, _ = cv2.connectedComponentsWithStats(colored[y0:y1].astype(np.uint8), connectivity=8)
            for x0, offset, span, thickness, _ in components[1:]:
                if span < 0.75 * width:
                    narrow_fragments.append(([int(x0), int(y0 + offset), int(x0 + span), int(y0 + offset + thickness)], colored))
                    continue
                x0, x1 = (int(x0), int(x0 + span))
                a, b = (int(y0 + offset), int(y0 + offset + thickness))
                if strips and 0 < a - strips[-1][3] <= 0.006 * height:
                    previous = strips[-1]
                    overlap = min(x1, previous[2]) - max(x0, previous[0])
                    if overlap >= 0.85 * min(x1 - x0, previous[2] - previous[0]) and b - previous[1] <= 0.06 * height:
                        previous[:] = [min(x0, previous[0]), previous[1], max(x1, previous[2]), b]
                    else:
                        strips.append([x0, a, x1, b])
                else:
                    strips.append([x0, a, x1, b])
        bands.extend((strip for strip in strips if 0.009 * height <= strip[3] - strip[1] <= 0.06 * height))
        partial_strips.extend(((strip, colored) for strip in strips if 0 < strip[3] - strip[1] < 0.009 * height and 15 <= strip[0] < strip[2] <= width - 15 and (200 <= strip[1] <= 1100)))
    bands = [band for band in bands if 15 <= band[0] < band[2] <= width - 15 and 200 <= band[1] <= 1100]
    if len(bands) not in (2, 3):
        return []
    edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 40, 100)
    if partial_strips or (len(bands) == 2 and narrow_fragments):
        anchors = sorted(bands, key=lambda band: band[1])
        above, below = (anchors[0], anchors[-1])
        restored = []
        restored_keys = set()
        if abs(above[0] - below[0]) <= 0.02 * width and abs(above[2] - below[2]) <= 0.02 * width and (abs(above[3] - above[1] - (below[3] - below[1])) <= 0.006 * height):
            left, right = (max(above[0], below[0]), min(above[2], below[2]))
            strip_height = min(above[3] - above[1], below[3] - below[1])
            local_fragments = [entry for entry in narrow_fragments if len(bands) == 2 and above[3] <= entry[0][1] and (entry[0][3] <= below[1])]
            for fragment, colored in partial_strips + local_fragments:
                top, bottom = (fragment[1], fragment[1] + strip_height)
                if not (left <= fragment[0] < fragment[2] <= right and all((bottom <= band[1] or top >= band[3] for band in bands))):
                    continue
                local = colored[top:bottom, left:right]
                if local.mean() < 0.42:
                    continue
                local_hsv = hsv[top:bottom, left:right]
                palette_support = sum((((local_hsv[:, :, 0] >= low) & (local_hsv[:, :, 0] <= high) & (local_hsv[:, :, 1] >= 125) & (local_hsv[:, :, 2] >= 175)).mean() >= 0.42 for low, high in ((5, 35), (40, 75), (80, 110))))
                if palette_support != 1:
                    continue
                present = np.any(local, axis=1)
                missing = np.pad((~present).astype(np.int8), (1, 1))
                gaps = np.flatnonzero(np.diff(missing) == -1) - np.flatnonzero(np.diff(missing) == 1)
                if not present[0] or not present[-1] or np.any(gaps > 0.006 * height):
                    continue
                sides = [edges[top:bottom, x - 8:x + 8] for x in (left, right)]
                if not all((np.any(side, axis=1).mean() >= 0.55 for side in sides)):
                    continue
                candidate = [left, top, right, bottom]
                candidate_key = (*candidate, id(colored))
                if candidate_key not in restored_keys:
                    restored.append(candidate)
                    restored_keys.add(candidate_key)
        if restored:
            if len(bands) != 2 or len(restored) != 1:
                return []
            bands.extend(restored)
    if len(bands) != 3:
        return []
    cards = []
    for x0, y0, x1, y1 in bands:
        span = x1 - x0
        top, bottom = (int(y0 - 0.25 * span), int(y0 - 0.1 * span))
        if top < 0 or bottom <= top or x0 + 20 >= width:
            continue
        side_top = max(top, y0 - 36)
        sides = [edges[side_top:y0, max(0, x - 8):min(width, x + 8)] for x in (x0, x1)]
        if not any((np.any(side, axis=1).mean() >= 0.55 for side in sides if side.size)):
            continue
        x, y = ((x0 + x1) // 2, bottom)
        click = image[y - 20:y + 20, x - 30:x + 30].max(axis=2)
        low, high = np.percentile(click, (10, 90))
        if high - low < 20:
            continue
        cards.append(dict(point=[(x0 + x1) // 2, bottom], strip_bbox=[x0, y0, x1, y1], evidence='visible_opponent_card'))
    if len(cards) != 3:
        return []
    cards.sort(key=lambda card: card['strip_bbox'][1])
    spacing = [cards[index + 1]['strip_bbox'][1] - cards[index]['strip_bbox'][1] for index in (0, 1)]
    if min(spacing) < 0.15 * height or max(spacing) > 0.35 * height:
        return []
    return cards

def read_tokens(image, ocr):

    def read(part, x=0, y=0, scale=1):
        rows, _ = ocr(part)
        return [dict(text=text, confidence=float(confidence), bbox=[int(min((p[0] for p in box)) / scale) + x, int(min((p[1] for p in box)) / scale) + y, int(max((p[0] for p in box)) / scale) + x, int(max((p[1] for p in box)) / scale) + y]) for box, text, confidence in rows or []]
    tokens = read(image)
    if any(('加载中' in t['text'] and t['bbox'][1] > 1150 or t['text'] == '连接中' for t in tokens)):
        return tokens
    needs_rp = any((t['text'].replace(' ', '') in ('团队赛事', '选择对战对手') for t in tokens)) or (any((t['text'] == '团队竞技场' and t['bbox'][1] < 80 for t in tokens)) and any((t['text'].replace(' ', '') == '继续' and 1020 <= t['bbox'][1] < 1140 for t in tokens)))
    if image.shape[:2] == (1280, 720) and needs_rp and (not any((t['confidence'] >= 0.9 and re.search('[0-5]/5$', t['text']) and (t['bbox'][1] < 110) for t in tokens))):
        tokens += [t for t in read(cv2.resize(image[5:45, 610:665], None, fx=3, fy=3), 610, 5, 3) if re.fullmatch('[0-5]/5', t['text'])]
    if image.shape[:2] == (1280, 720) and any((t['text'] in ('强化编成', '主页', '招募') and t['bbox'][1] > 1180 for t in tokens)) and (not any((t['text'] == '赛事' and t['bbox'][1] > 1200 for t in tokens))):
        tokens += read(cv2.resize(image[1210:1280, 490:567], None, fx=3, fy=3), 490, 1210, 3)
    if image.shape[:2] == (1280, 720) and any((t['text'].replace(' ', '') == '赛事结束' and t['bbox'][1] < 130 for t in tokens)):
        for text, box in [('赛事结束', (150, 0, 590, 130)), ('继续', (200, 1050, 520, 1180))]:
            x0, y0, x1, y1 = box
            if not any((t['confidence'] >= 0.9 and t['text'].replace(' ', '') == text and (x0 <= t['bbox'][0]) and (t['bbox'][2] <= x1) and (y0 <= t['bbox'][1]) and (t['bbox'][3] <= y1) for t in tokens)):
                scale = 0.5 if text == '赛事结束' else 2
                tokens += read(cv2.resize(image[y0:y1, x0:x1], None, fx=scale, fy=scale), x0, y0, scale)
    return tokens

def observe(raw, tokens, *, settlement=False):
    result = dict(page='unknown', actions={}, navigation_actions={}, rp=None, tokens=tokens, frame_sha256=hashlib.sha256(raw).hexdigest())
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return result
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.9]

    def within(t, box):
        a, b, c, d = t['bbox']
        x0, y0, x1, y1 = box
        return x0 <= a and y0 <= b and (c <= x1) and (d <= y1)

    def find(text, box=(0, 0, 720, 1280)):
        matches = [t for t in good if t['text'] == text and within(t, box)]
        return matches[0] if len(matches) == 1 else None

    def rp(box):
        matches = [re.search('([0-5])/5$', t['text']) for t in good if within(t, box)]
        values = [int(m[1]) for m in matches if m]
        return values[0] if len(values) == 1 else None

    def action(name, token):
        if token:
            x0, y0, x1, y1 = token['bbox']
            result['actions'][name] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    reward = find('获得奖励', (0, 600, 250, 680))
    fans = find('本月社团收获粉丝数', (0, 710, 330, 810))
    summary_panel = settlement and reward and fans and find('好友积分', (0, 645, 230, 710)) and visible_label(image, reward['bbox']) and visible_label(image, fans['bbox'])

    def visual(name, region):
        x0, y0, x1, y1 = region
        asset = template(name)
        if asset is None:
            return
        gray = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        if (asset.ndim != 2 or asset.dtype != gray.dtype or min(asset.shape) == 0
                or asset.shape[0] > gray.shape[0] or asset.shape[1] > gray.shape[1]):
            return None
        score = cv2.matchTemplate(gray, asset, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, position = cv2.minMaxLoc(score)
        result[name + '_confidence'] = float(confidence)
        if confidence >= 0.9:
            h, w = asset.shape
            x, y = (x0 + position[0] + w // 2, y0 + position[1] + h // 2)
            if name == 'next' and _control_state(image, (x - 80, y - 20, x + 80, y + 20)) is not True:
                return None
            return dict(point=[x, y])
    summary_next = None
    if find('团队竞技场', (0, 0, 260, 80)) and any((t['text'] in ('胜利', '失败', '平局') and within(t, (150, 280, 690, 450)) for t in good)) and (sum((bool(re.fullmatch('\\+[0-9]+人', t['text'])) and within(t, (30, 640, 710, 1100)) for t in good)) >= 5):
        summary_next = visual('next', (200, 1110, 690, 1250))
    if any(('连接已断开' in t['text'] for t in good)):
        result['page'] = 'disconnected'
        return result
    if any(('加载中' in t['text'] and t['bbox'][1] > 1150 or t['text'] == '连接中' for t in tokens)):
        result['page'] = 'loading'
        return result
    weekly = observe_weekly_settlement(image, tokens, allow_animation=True)
    if weekly is not None:
        result.update(weekly)
    elif find('最高分更新！', (200, 350, 570, 480)) and any((re.fullmatch('第[0-9,.，]+名', t['text']) and within(t, (150, 480, 570, 600)) for t in good)) and any((re.fullmatch('[0-9,.]+pt', t['text']) and within(t, (200, 600, 530, 710)) for t in good)):
        result['page'] = 'high_score'
        action('dismiss_high_score', find('最高分更新！', (200, 350, 570, 480)))
    elif find('限时特卖开售', (200, 350, 540, 470)) and find('前往限时特卖', (380, 780, 650, 900)) and any((re.fullmatch('限时特卖已开售[1-3]次！', t['text']) and within(t, (180, 620, 550, 735)) for t in good)):
        result['page'] = 'limited_sale'
        action('cancel_limited_sale', find('取消', (100, 780, 320, 900)))
    elif find('获胜奖励', (150, 50, 570, 200)):
        result['page'] = 'rewards'
        action('continue_rewards', find('继续', (200, 1100, 520, 1250)))
    elif find('现已开放新角色剧情！', (300, 90, 650, 170)) and find('标记设置', (530, 1020, 710, 1130)) and find('返回', (0, 1020, 160, 1130)):
        result['page'] = 'story_detail'
        action('return_story_races', find('赛事', (460, 1180, 580, 1280)))
    elif find('解锁新的故事！', (300, 800, 650, 900)) and find('推进剧情获得奖励！', (150, 890, 580, 960)) and find('帮助', (550, 1160, 690, 1250)):
        result['page'] = 'story'
        action('close_story', find('关闭', (220, 1140, 510, 1250)))
    elif find('解锁角色剧情', (200, 20, 550, 110)) and find('现可解锁角色剧情', (200, 1050, 550, 1140)):
        result['page'] = 'story'
        action('close_story', find('关闭', (220, 1140, 510, 1230)))
    elif find('解锁角色剧情', (200, 300, 550, 500)):
        result['page'] = 'story'
        action('close_story', find('关闭', (220, 750, 510, 920)))
    elif find('选择道具', (200, 250, 550, 400)) and find('请选择道具', (200, 390, 550, 500)):
        result.update(page='items', rp=rp((200, 800, 405, 875)), rp_after=rp((500, 800, 610, 875)))
        slots = [image[720:800, x:x + 70] for x in (260, 395)]
        result['items_empty'] = all(((cv2.cvtColor(s, cv2.COLOR_BGR2HSV)[:, :, 1] < 35).mean() > 0.98 and float(s.mean()) > 180 for s in slots))
        if result['items_empty'] and isinstance(result['rp'], int) and (result['rp'] > 0) and (result['rp_after'] == result['rp'] - 1) and (find('竞技值消耗', (380, 900, 600, 970)) or find('竞技值消耗1', (380, 900, 600, 970))):
            action('enter_race', find('参赛！', (390, 860, 620, 940)))
        cancel = find('取消', (100, 860, 320, 970))
        if cancel and result['items_empty']:
            x0, y0, x1, y1 = cancel['bbox']
            result['navigation_actions']['cancel_items'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    elif find('赛事结束', (150, 0, 590, 130)):
        result['page'] = 'results'
        action('continue_results', find('继续', (200, 1050, 520, 1180)))
    elif summary_next or summary_panel or (find('团队竞技场', (0, 0, 260, 80)) and (find('亲密度·粉丝数', (0, 600, 250, 900)) or (find('各赛事结果', (400, 540, 580, 615)) and find('得分信息', (580, 540, 715, 615)) and find('再次参赛', (50, 1120, 350, 1250))) or (any((t['text'] in ('胜利', '失败', '平局') and within(t, (150, 280, 690, 450)) for t in good)) and (any((re.fullmatch('[0-9,.]+pt', t['text']) and within(t, (150, 420, 690, 540)) for t in good)) or (any((t['confidence'] >= 0.85 and re.fullmatch('(胜利|失败|平局)\\+[0-9,.]+pt', t['text'].replace(' ', '')) and within(t, (150, 500, 690, 550)) for t in tokens)) and sum((bool(re.fullmatch('\\+[0-9]+人', t['text'])) and within(t, (30, 640, 710, 1100)) for t in good)) >= 10) or (find('最高分', (150, 400, 400, 460)) and sum((bool(re.fullmatch('\\+[0-9]+人', t['text'])) and within(t, (30, 640, 710, 1100)) for t in good)) >= 10)) and (sum((bool(re.fullmatch('\\+[0-9]+人', t['text'])) and within(t, (30, 640, 710, 1100)) for t in good)) >= 5)))):
        result['page'] = 'summary'
        next_button = summary_next or visual('next', (200, 1110, 690, 1250))
        if next_button:
            result['actions']['next'] = next_button
        rematch = find('再次参赛', (50, 1120, 350, 1250))
        if rematch and 'next' in result['actions']:
            x0, y0, x1, y1 = rematch['bbox']
            result['navigation_actions']['rematch'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
        if not result['actions']:
            result['actions']['accelerate_summary'] = dict(point=[360, 400])
    elif find('查看各赛事结果', (180, 1100, 540, 1250)) or (find('团队竞技场', (0, 0, 260, 80)) and find('缩短版：关', (250, 1000, 470, 1120)) and find('前往比赛', (150, 1140, 360, 1250)) and find('查看结果', (370, 1140, 580, 1250))):
        result['page'] = 'race_ready'
        if find('缩短版：开', (250, 1000, 470, 1120)):
            action('view_results', find('查看各赛事结果', (180, 1100, 540, 1250)))
        else:
            action('enable_short', find('缩短版：关', (250, 1000, 470, 1120)))
    elif any((t['text'] in ('胜利', '失败', '平局') and within(t, (100, 600, 620, 930)) for t in good)) and any(('pt' in t['text'].lower() and within(t, (80, 750, 650, 1020)) for t in good)):
        result['page'] = 'animation'
        skip_button = visual('skip', (610, 1110, 715, 1230))
        if skip_button:
            result['actions']['skip'] = skip_button
    elif find('选择对战对手', (200, 150, 550, 250)):
        result.update(page='opponents', rp=rp((400, 0, 700, 110)))
        cards = opponent_cards(image)
        if result['rp'] is not None and result['rp'] > 0 and cards:
            result['opponent_cards'] = cards
            result['actions']['choose_top'] = cards[0]
            for row, y in enumerate((430, 700, 970)):
                gift = find('胜利时必得', (520, y, 705, y + 60))
                if gift:
                    result['navigation_actions']['choose_gift'] = dict(**cards[row], opponent_row=row, text=gift['text'])
                    break
    elif find('团队竞技场', (0, 0, 260, 80)) and find('继续', (200, 1020, 520, 1140)):
        result.update(page='matchup', rp=rp((400, 0, 700, 110)))
        if result['rp'] is not None and result['rp'] > 0:
            action('continue_matchup', find('继续', (200, 1020, 520, 1140)))
    elif find('团队赛事', (200, 760, 540, 920)) and find('团队编成', (200, 900, 600, 1030)):
        result.update(page='arena', rp=rp((400, 0, 700, 110)))
        if find('统计中', (180, 400, 550, 570)) and find('统计期间', (200, 550, 530, 650)):
            result['arena_tallying'] = True
            periods = [t['text'] for t in good if within(t, (120, 600, 620, 680)) and re.fullmatch('\\d{2}/\\d{2}\\d{2}:\\d{2}[~～]\\d{2}/\\d{2}\\d{2}:\\d{2}', t['text'])]
            if len(periods) == 1:
                result['tally_period'] = periods[0]
        elif find('正在参赛', (80, 700, 550, 910)):
            result['race_in_progress'] = True
            action('resume_race', find('团队赛事', (200, 760, 540, 920)))
            if result['rp'] is not None and result['rp'] > 0:
                result['navigation_actions']['resume_matchup'] = result['actions']['resume_race']
            if type(result['rp']) is int and 0 <= result['rp'] <= 5:
                result['navigation_actions']['open_pending_race'] = result['actions']['resume_race']
        elif result['rp'] is not None and result['rp'] > 0:
            action('open_opponents', find('团队赛事', (200, 760, 540, 920)))
    elif (ui := PageIdentity(image)).races() or (find('日常安排', (70, 1010, 360, 1140)) and (find('团队竞技场', (70, 850, 360, 990)) or find('赛事', (0, 130, 300, 230)))):
        result['page'] = 'races'
        action('open_arena', find('团队竞技场', (70, 850, 360, 990)) or find('团队技场', (70, 850, 360, 990)) or ui.find('团队竞技场'))
    elif ui.home(tokens):
        result['page'] = 'home'
        action('open_races', find('赛事', (460, 1180, 580, 1280)) or ui.find('赛事', (460, 1180, 580, 1280)))
    return result
