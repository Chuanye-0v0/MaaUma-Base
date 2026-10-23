"""Read-only LOH page observations using local OCR and current control surfaces.

Qianye is a flow reference, not a source of tap coordinates or retry policy.
This module never clicks, starts OCR engines, writes frames, or spends resources.
The live adapter supplies its existing OCR results for the same frame.
"""
from __future__ import annotations

import re
import unicodedata

import cv2
import numpy as np


LOH_NAMES = ("LEAGUEOFHEROES", "英雄联赛", "英雄赛事", "限时英雄赛", "限时群英联赛", "群英联赛", "英杰集结战")
REGIONS = {
    "header": (0, 0, 1, .23), "body": (.03, .17, .97, .84),
    "modal": (.06, .19, .94, .94), "footer": (.03, .64, .97, .99),
    "cards": (.02, .28, .98, .94),
}


def _text(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).strip("!！♪·").casefold()


def _image(frame):
    if isinstance(frame, (bytes, bytearray, memoryview)):
        return cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR) if len(frame) else None
    return frame if isinstance(frame, np.ndarray) else None


def _tokens(items, width, height):
    result = []
    for item in items or ():
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        box = item.get("bbox")
        confidence = item.get("confidence", item.get("score", 0))
        if (not isinstance(box, (tuple, list)) or len(box) != 4
                or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not np.isfinite(confidence) or confidence < .8):
            continue
        try:
            values = [float(v) for v in box]
        except (ValueError, TypeError):
            continue
        if not all(np.isfinite(v) for v in values):
            continue
        x, y, right, bottom = [int(v) for v in values]
        if not (0 <= x < right <= width and 0 <= y < bottom <= height):
            continue
        result.append({"text": _text(item["text"]), "confidence": float(confidence),
            "bbox": [x, y, right, bottom],
            **{key: item[key] for key in ("enabled", "selected") if type(item.get(key)) is bool}})
    return result


def _dialog_box(image):
    """Find a foreground light card on a dim backdrop, without reading its title."""
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    outer = np.concatenate([hsv[:max(1, height // 12), :, 2].ravel(),
                            hsv[:, :max(1, width // 24), 2].ravel(),
                            hsv[:, -max(1, width // 24):, 2].ravel()])
    if np.median(outer) >= 175:
        return None
    light = ((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 195)).astype("uint8")
    count, _, stats, _ = cv2.connectedComponentsWithStats(light)
    boxes = []
    for x, y, w, h, area in stats[1:]:
        if (area > width * height * .12 and w > width * .55 and h > height * .16
                and .28 * width < x + w / 2 < .72 * width
                and y > height * .06 and y + h < height * .98):
            boxes.append((int(x), int(y), int(x + w), int(y + h)))
    return max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if boxes else None


class _Page:
    def __init__(self, image, tokens):
        self.image, self.tokens = image, tokens
        self.height, self.width = image.shape[:2]
        self.dialog = _dialog_box(image)

    def rows(self, region):
        left, top, right, bottom = REGIONS.get(region, region) if isinstance(region, str) else region
        x, y, r, b = self.dialog or (0, 0, self.width, self.height)
        width, height = r - x, b - y
        return [t for t in self.tokens if x + left * width <= t["bbox"][0]
                and t["bbox"][2] <= x + right * width and y + top * height <= t["bbox"][1]
                and t["bbox"][3] <= y + bottom * height]

    def has(self, words, region="body"):
        words = (words,) if isinstance(words, str) else words
        return any(any(_text(word) in t["text"] for word in words) for t in self.rows(region))

    def find(self, words, region="footer"):
        words = {_text(w) for w in ((words,) if isinstance(words, str) else words)}
        found = [t for t in self.rows(region) if t["text"] in words]
        return found[0] if len(found) == 1 else None

    def enabled(self, token):
        if type(token.get("enabled")) is bool:
            return token["enabled"]
        x, y, r, b = token["bbox"]
        px, py = max(8, (r - x) // 3), max(5, (b - y) // 3)
        crop = self.image[max(0, y - py):min(self.height, b + py),
                          max(0, x - px):min(self.width, r + px)]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        color = (hsv[:, :, 1] > 65) & (hsv[:, :, 2] > 115)
        white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 215)
        return bool(color.mean() > .25 or (white.mean() > .65 and crop.std() > 12))

    def action(self, name, words, region="footer", **extra):
        token = self.find(words, region)
        if token is None or not self.enabled(token):
            return {}
        x, y, r, b = token["bbox"]
        return {name: {"point": [(x + r) // 2, (y + b) // 2], "bbox": token["bbox"],
            "label": token["text"], "basis": "current_ocr_control", **extra}}


def _result(page, reason, actions=None, **fields):
    return {"page": page, "reason": reason, "actions": actions or {},
            "ticket_count": None, "score": None, **fields}


def _numbers(page, observation):
    # Metadata only. Unreadable/zero counts never stand in for page identity.
    tickets, scores = [], []
    for row in page.rows("header") + page.rows("body"):
        text = row["text"]
        match = re.fullmatch(r"(?:持有|剩余)(?:门票|英雄赛门票|出走券|报名券)[:：]?(\d+)(?:张|枚)?", text)
        if match:
            tickets.append(int(match[1]))
        match = re.fullmatch(r"(?:累计|总|联赛)?积分[:：](\d+|\d{1,3}(?:,\d{3})+)", text)
        if match:
            scores.append(int(match[1].replace(",", "")))
    if len(set(tickets)) == 1:
        observation["ticket_count"] = tickets[0]
    if len(set(scores)) == 1:
        observation["score"] = scores[0]
    return observation


def _paid_text(text):
    return bool(re.search(r"(?:消耗|使用|支付|花费).{0,12}(?:宝石|萝卜|付费|有偿)", text)
                or re.search(r"(?:购买|补充).{0,8}(?:门票|出走券|报名券|道具|英雄技能)", text))


def _unknown_consumption(text, *, hero=False):
    """Every debit clause must be known; one ticket word cannot bless another cost."""
    text = text.replace('|', '')  # OCR may split the verb, count and resource label.
    clauses = re.finditer(r"(使用|消耗|支付|花费)(.*?)(?=使用|消耗|支付|花费|[,，。?!？!|]|$)", text)
    for match in clauses:
        for part in re.split(r"和|及|与|并|、", match[2]):
            part = re.split(r'进行|参加|开始|用于|后|以便|是否|取消|确定|确认', part, maxsplit=1)[0]
            if not part:
                continue
            if (hero and match[1] == '使用'
                    and re.fullmatch(r'(?:当前|该)?英雄技能(?:获得.*|发动.*|提升.*)?', part)):
                continue
            rest = re.sub(r"英雄赛门票|门票|出走券|报名券", "", part)
            if rest == part:
                return True
            rest = re.sub(r"[\d,]+|[张枚个次]|本次|一次|出走|参赛|比赛|报名|挑战|将会|立即|需要", "", rest)
            if rest.strip(' :：;；()（）'):
                return True
    return False


def _resource_confirmation(page):
    title = page.has(("出走确认", "参赛确认", "报名确认", "门票确认", "购买确认", "购买门票", "购买道具"), "header")
    controls = page.action("cancel_purchase", ("取消", "返回"))
    confirmation = page.action("confirm_entry", ("确定", "确认", "出走", "参赛"))
    if not title or not (controls or confirmation):
        return None
    body = "|".join(t["text"] for t in page.rows((0, 0, 1, 1)))
    paid = _paid_text(body) or page.has(("购买确认", "购买门票", "购买道具"), "header")
    free = any(word in body.replace('|', '') for word in ("免费参赛", "免费出走", "不消耗门票", "不需要消耗门票"))
    ticket = bool(re.search(r"(?:使用|消耗).{0,10}(?:门票|出走券|报名券)", body))
    unknown = _unknown_consumption(body)
    kind = "paid" if paid else "unknown" if unknown else "free" if free else "ticket" if ticket else "unknown"
    if kind in {"free", "ticket"}:
        for action in confirmation.values():
            action.update(effect="admission", resource_kind=kind)
        controls.update(confirmation)
    return _result("loh_entry_confirm" if kind in {"free", "ticket"} else "loh_purchase", "loh_resource_" + kind, controls,
        entry_resource={"kind": kind, "basis": "current_modal_text"},
        paid_purchase=paid, consumption_allowed=kind in {"free", "ticket"},
        requires_loh_context=True)


def _choice(page):
    hero = page.has(("选择英雄技能", "英雄技能选择", "英雄技能确认", "发动英雄技能"), "header")
    items = page.has(("选择道具", "道具选择", "使用道具确认"), "header")
    if not (hero or items):
        return None
    # Do not use a reward amount or a decorative shop button as a purchase gate.
    body = "|".join(t["text"] for t in page.rows("body"))
    payment = _paid_text(body)
    unknown = _unknown_consumption(body, hero=hero)
    if items:
        # The CN item dialog defaults to two empty grey selection slots. This
        # is a regular entry, not a purchase, even though OCR reads 消耗0.
        slots = [page.image[int(page.height*.36):int(page.height*.45),
                            int(page.width*l):int(page.width*r)] for l, r in ((.34,.47),(.53,.66))]
        blank_slots = all(cv2.cvtColor(c, cv2.COLOR_BGR2HSV)[:, :, 1].mean() < 12
                          and c.mean() > 205 for c in slots if c.size)
        free_entry = any('为止免费' in t['text'] for t in page.tokens)
        selected_label = page.find('选中道具', (0,0,1,1))
        auto_off = page.find('关', (0,0,1,1))
        auto_on = page.find('开', (0,0,1,1))
        auto_label = any('自动选择英杰技能对应道具' in t['text'] for t in page.tokens)
        if (selected_label and auto_label and (auto_off or auto_on) and not payment
                and (free_entry or page.find('持有', (0,0,1,1)))):
            actions = {}
            if auto_off:
                x,y,r,b = auto_off['bbox']
                actions['enable_hero_items'] = dict(point=[(x+r)//2,(y+b)//2],
                    effect='navigation',resource_kind='free',basis='native_auto_hero_items_switch')
            if auto_on or blank_slots:
                entry = page.action('confirm_entry', ('参赛', '参赛!', '出走'),
                    effect='admission',resource_kind='free' if free_entry else 'ticket')
                for candidate in entry.values():
                    candidate.update(uses_items=bool(auto_on and not blank_slots))
                    if auto_on:
                        candidate['item_policy'] = 'existing_hero_corresponding'
                actions.update(entry)
            return _result('loh_entry_confirm','native_auto_hero_items_entry',actions,
                entry_resource={'kind':'free' if free_entry else 'ticket'},
                selection_empty=bool(blank_slots),auto_hero_items=bool(auto_on),requires_loh_context=True)
        actions = page.action("skip_items", ("不使用道具", "不使用", "跳过", "不选择", "全部取消", "清空选择"),
                              resource_kind="none", uses_items=False)
        selected = [t for t in page.rows("body") if t.get("selected") is True]
        positive_count = any(re.search(r"(?:已选择|已选|选中)[1-9]\d*(?:个|件|种)(?:道具)?", t['text'])
                             for t in page.rows('body'))
        empty = (not selected and not positive_count
                 and page.has(("未选择道具", "已选择0个", "已选择0件", "不使用道具出走"), "body"))
        if empty and not payment and not unknown:
            actions.update(page.action("confirm_items", ("确定", "确认", "继续", "出走"),
                                       uses_items=False, resource_kind="none"))
        if (payment or unknown) and not actions:
            return _result("loh_purchase", "choice_requires_paid_purchase",
                page.action("cancel_purchase", ("取消", "返回")),
                entry_resource={"kind": "paid" if payment else "unknown"})
        actions.update(page.action("back_loh", ("取消", "返回")))
        return _result("loh_items", "existing_item_selection", actions,
            selection_kind="items", item_purchase_allowed=False,
            selected_count=len(selected) if selected or empty else None,
            selection_empty=False if selected or positive_count else True if empty else None,
            requires_loh_context=True)
    if payment or unknown:
        return _result("loh_purchase", "choice_requires_paid_purchase",
            page.action("cancel_purchase", ("取消", "返回")),
            entry_resource={"kind": "paid" if payment else "unknown"})
    actions = page.action("confirm_hero_skill", ("发动", "发动英雄技能", "使用英雄技能"),
                          resource_kind="free")
    selected = any(t.get("selected") is True for t in page.rows("cards"))
    confirmation_page = page.has(("英雄技能确认", "确认发动英雄技能"), "header")
    if selected or confirmation_page or page.has(("已选择英雄技能", "确认发动英雄技能"), "body"):
        actions.update(page.action("confirm_hero_skill", ("确定", "确认", "继续"), resource_kind="free"))
    choices = [t for t in page.rows("cards") if t["text"] in {"选择", "选用"} and page.enabled(t)]
    candidates = []
    for index, token in enumerate(sorted(choices, key=lambda t: (t["bbox"][1], t["bbox"][0]))):
        x, y, r, b = token["bbox"]
        candidates.append({"id": str(index), "point": [(x + r) // 2, (y + b) // 2],
                           "bbox": token["bbox"], "label": token["text"],
                           "selected": token.get("selected")})
    # Keep an existing selected choice, or the sole enabled control. Multiple
    # unselected skills are data, not a new arbitrary first-option strategy.
    chosen = [c for c in candidates if c['selected'] is True]
    existing = chosen[0] if len(chosen) == 1 else candidates[0] if len(candidates) == 1 else None
    if existing is not None:
        actions["select_hero_skill"] = {**existing, "basis": "existing_or_sole_choice",
                                        "resource_kind": "free"}
    actions.update(page.action("back_loh", ("取消", "返回")))
    return _result("loh_hero_skill", "existing_hero_skill_selection", actions,
        selection_kind="hero_skill", choices=candidates, skill_purchase_allowed=False,
        selection_observed=selected, requires_loh_context=True)


def observe_owned_tutorial(frame, ocr_items=None):
    """Keep a verified LOH guide step under the already established owner."""
    from core.passive_tutorial import detect as detect_tutorial
    image = _image(frame)
    if image is None:
        return None
    found = detect_tutorial(image,ocr_items or [],footer_top=1166)
    if not found:
        return None
    return _result('notice','loh_owned_tutorial',
        {'close_notice':dict(point=found['point'],effect='navigation',resource_kind='free',
            basis='shared_verified_tutorial_footer',label=found['button'],
            tutorial_page_index=found['page_index'],tutorial_page_count=found['page_count'])},
        tutorial=found,requires_loh_context=True)


def observe_owned_playback(frame, ocr_items=None):
    """Called only by the live adapter with an already owned LOH entry."""
    from core.passive_skip import detect as detect_skip
    image = _image(frame)
    if image is None:
        return None
    found = detect_skip(image, ocr_items or [])
    if not found or found.get('button') != 'round_double_arrow':
        return None
    return _result('loh_race','loh_owned_playback_skip',
        {'skip_race':dict(point=found['point'],effect='progress',resource_kind='free',
                          basis='shared_verified_playback_glyph')},requires_loh_context=True)


def observe_owned_fast_skip(frame):
    """The native LOH short-results circle sits 40px above the shared layout.

    Reuse the exact shared circle/color/double-arrow checks in a translated
    image, then map its measured center back to the original native frame.
    Caller must already own the short-result presentation window.
    """
    from core.passive_skip import continue_point
    image = _image(frame)
    if image is None or image.shape[:2] != (1280, 720):
        return None
    shifted = np.zeros_like(image)
    shifted[40:] = image[:-40]
    point = continue_point(shifted)
    if point is None:
        return None
    return _result('loh_race', 'loh_owned_fast_skip',
        {'skip_race':dict(point=[point[0], point[1]-40], effect='progress', resource_kind='free',
                          basis='shared_verified_playback_glyph_loh_height')},requires_loh_context=True)


def _playback(page):
    status = any(t['text'] in {'比赛进行中', '赛事进行中'} for t in page.rows((0, 0, 1, 1)))
    if not status and not page.has(("直接查看过程", "查看赛事结果", "跳过比赛"), (0, 0, 1, 1)):
        return None
    actions = page.action("skip_race", ("跳过", "跳过比赛", "直接查看过程", "直接查看结果"), (.45, .55, 1, 1))
    actions.update(page.action("view_result", ("查看赛事结果", "查看结果")))
    return _result("loh_race", "current_race_playback_controls", actions,
                   requires_loh_context=not page.has(LOH_NAMES, 'header'))


def _terminal(page, event_header):
    if not event_header:
        return None
    if page.has(("没有门票", "门票不足", "门票已用完", "今日可挑战次数已用完"), (0, 0, 1, 1)):
        return _result("loh_no_tickets", "explicit_no_tickets")
    if page.has(("尚未开放", "活动未开放", "活动已结束", "出走登记期间", "出走登录期间"), (0, 0, 1, 1)):
        return _result("loh_closed", "explicit_loh_unavailable")
    return None


def _zero_ticket_exit(page):
    zero = any(t['text'] == '0/10' and t['bbox'][1] < page.height*.12 for t in page.tokens)
    entries = [t for t in page.rows('footer') if t['text'] in {'确定对战对手', '决定对战对手'}]
    if not zero or not entries:
        return None
    # Regional OCR may repeat the same footer label from the full frame.
    entry = max(entries, key=lambda t: t['confidence'])
    x,y,r,b = entry['bbox']
    label = cv2.cvtColor(page.image[y:b,x:r], cv2.COLOR_BGR2HSV)
    white = ((label[:,:,1] < 65) & (label[:,:,2] > 215)).mean()
    grey = ((label[:,:,1] < 65) & (label[:,:,2] > 100) & (label[:,:,2] < 205)).mean()
    if grey <= .25 or white >= .02:
        return None
    actions = page.action('continue_result', ('下一步',), 'footer',
                          effect='progress', resource_kind='free')
    home = page.find('主页', 'footer')
    if home:
        hx,hy,hr,hb = home['bbox']
        actions['back_loh'] = dict(point=[(hx+hr)//2,(hy+hb)//2], effect='navigation',
                                  resource_kind='free',basis='current_home_footer_caption')
    return _result('loh_no_tickets', 'native_zero_tickets_disabled_entry', actions,
                   ticket_count=0, entry_disabled=True, exhaustion_verified=True)


def _event_menu(image, tokens, page):
    """Reuse D1's multi-card/scroll reader without its other task actions."""
    from core.event_menu import menu_entries

    shared = menu_entries(image, tokens)
    if shared is not None:
        if (shared.get('loh_available') is False
                and shared.get('loh_availability_basis') == 'named_card_closed'):
            return _result('loh_closed', 'named_loh_card_closed')
        actions = {key: value for key, value in (shared.get('actions') or {}).items()
                   if key == 'open_loh'}
        if not actions and 'scroll_events' in (shared.get('navigation_actions') or {}):
            actions['scroll_events'] = shared['navigation_actions']['scroll_events']
        return _result('events', 'shared_current_event_menu', actions)
    if not page.has(("赛事活动", "竞赛活动"), "header"):
        return None
    action = page.action('open_loh', LOH_NAMES, 'cards')
    return _result('events', 'named_event_card' if action else 'loh_card_not_read', action)


def observe(frame, ocr_items=None):
    """Return serializable observations; OCR boxes use this frame's pixel units.

    ndarray frames are BGR, like the existing project observers. Encoded image
    bytes are accepted for a thin live adapter. No OCR or action is inferred
    from a missing frame or empty token list.
    """
    image = _image(frame)
    if (image is None or image.ndim != 3 or image.shape[2] != 3
            or image.dtype != np.uint8 or min(image.shape[:2]) < 64):
        return _result("unknown", "frame_unavailable")
    height, width = image.shape[:2]
    tokens = _tokens(ocr_items, width, height)
    if not tokens:
        return _result("unknown", "ocr_unavailable")
    page = _Page(image, tokens)
    if page.has(("账号在别处登录", "其他设备登录", "另一台设备登录"), (0, 0, 1, 1)):
        return _result("connection_conflict", "account_connection_conflict")
    if page.has(("连接中断", "通信错误", "连接失败", "网络连接错误"), (0, 0, 1, 1)):
        return _result("connection_interrupted", "connection_interrupted")
    if page.has(("加载中", "连接中", "正在匹配", "正在连接"), (0, 0, 1, 1)):
        return _result("loading", "transient_loading")
    promotions = [t for t in tokens if t['text'].startswith(('升至', '已升至'))]
    promotion_close = [t for t in tokens if t['text'] == '关闭']
    if promotions and len(promotion_close) == 1 and page.enabled(promotion_close[0]):
        x,y,r,b = promotion_close[0]['bbox']
        return _result('notice','rank_promotion_information',
            {'close_notice':dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free')},
            requires_loh_context=True)
    warning_text = ''.join(t['text'] for t in tokens)
    warning_confirm = [t for t in tokens if t['text'] == '确定']
    if ('英杰技能无法发动' in warning_text and '是否确定开始比赛' in warning_text
            and len(warning_confirm) == 1 and page.enabled(warning_confirm[0])
            and not _paid_text(warning_text)):
        # A bonus skill need not activate: continue the already chosen race
        # without adding an item or reserving a second ticket.
        x,y,r,b = warning_confirm[0]['bbox']
        return _result('loh_preview','optional_hero_skill_not_active',
            {'start_race':dict(point=[(x+r)//2,(y+b)//2],effect='progress',resource_kind='free')},
            requires_loh_context=True)
    # A resource modal owns the foreground even with a LOH home behind it.
    resource = _resource_confirmation(page)
    if resource is not None:
        return resource
    choice = _choice(page)
    if choice is not None:
        return choice
    prompt_text = ''.join(t['text'] for t in tokens)
    start_prompt = '团队成员' in prompt_text and '开始比赛' in prompt_text
    start_confirm = [t for t in tokens if t['text'] == '确定']
    if start_prompt and len(start_confirm) == 1 and page.enabled(start_confirm[0]):
        x, y, r, b = start_confirm[0]['bbox']
        return _result('loh_preview', 'existing_admission_team_preview',
            {'start_race': dict(point=[(x+r)//2, (y+b)//2], effect='progress', resource_kind='free')},
            requires_loh_context=True)
    auto_title = [t for t in tokens if t['text'] == '自动编成确认']
    auto_confirm = [t for t in tokens if t['text'] == '确定'
                    and auto_title and t['bbox'][1] > auto_title[0]['bbox'][3]]
    if len(auto_title) == 1 and len(auto_confirm) == 1 and page.enabled(auto_confirm[0]):
        x, y, r, b = auto_confirm[0]['bbox']
        return _result('loh_auto_team', 'named_game_auto_team_confirmation',
            {'auto_team_confirm': dict(point=[(x+r)//2, (y+b)//2], effect='navigation',
                                       resource_kind='free')}, requires_loh_context=True)
    team_title = [t for t in tokens if t['text'] == '编成确认']
    team_confirm = [t for t in tokens if t['text'] == '确定'
                    and team_title and t['bbox'][1] > team_title[0]['bbox'][3]]
    if len(team_title) == 1 and len(team_confirm) == 1 and page.enabled(team_confirm[0]):
        x, y, r, b = team_confirm[0]['bbox']
        return _result('loh_team_confirm', 'named_existing_team_confirmation',
            {'confirm_team': dict(point=[(x+r)//2, (y+b)//2], effect='navigation', resource_kind='free')},
            team_complete=True, team_confirmation_modal=True, requires_loh_context=True)
    # A modal may hide the event header, but the same-frame background heading
    # can identify which event owns an explicit exhaustion/closure message.
    loh = page.has(LOH_NAMES, "header")
    event_header = loh or any(t["bbox"][1] < height * .23 and
        any(_text(name) in t["text"] for name in LOH_NAMES) for t in tokens)
    terminal = _terminal(page, event_header)
    if page.dialog is not None and terminal is not None:
        return terminal
    if page.has(("公告", "活动说明", "活动通知"), "header"):
        close = page.action("close_notice", ("关闭", "确定", "继续"))
        if close:
            return _result("notice", "current_notice_close", close)
    intros = [t for t in tokens if t['text'] == '点击'
              and height*.80 <= t['bbox'][1] < t['bbox'][3] <= height*.95]
    foreground_controls = {'确定', '确认', '取消', '购买', '充值', '参赛', '出走'}
    intro = intros[0] if len(intros) == 1 else None
    if (intro and intro.get('enabled') is not False
            and not any(t['text'] in foreground_controls for t in tokens)):
        # The real CN event title animation has only this OCR caption. The
        # acknowledged open_loh receipt, not this generic word, owns the page.
        x, y, r, b = intro['bbox']
        action = dict(point=[(x+r)//2, (y+b)//2],
                      effect='navigation', resource_kind='free', basis='visible_intro_caption')
        return _result("notice", "loh_title_presentation", {'close_notice': action},
                       requires_loh_context=True)
    awards = [t for t in tokens if t['text'] == '获得'
              and height*.75 <= t['bbox'][1] < t['bbox'][3] <= height*.92]
    if (len(awards) == 1 and page.enabled(awards[0])
            and not any(t['text'] in foreground_controls for t in tokens)):
        x,y,r,b = awards[0]['bbox']
        return _result('notice', 'free_hero_skill_award_presentation',
            {'close_notice':dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free')},
            requires_loh_context=True)
    hero_awarded = any(t['text'] == '获得英杰技能' for t in tokens)
    hero_dialogue = [t for t in tokens if t['bbox'][1] > height*.72
                     and '获得' in t['text'] and '英杰技能' in t['text']]
    if (hero_awarded and hero_dialogue
            and not any(t['text'] in foreground_controls for t in tokens)):
        # The obtained-skill explanation advances by its visible dialogue text.
        # It neither selects an item nor submits another race entry.
        target = max(hero_dialogue,key=lambda t:t['bbox'][1])
        x,y,r,b = target['bbox']
        return _result('notice','free_hero_skill_explanation',
            {'close_notice':dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free')},
            requires_loh_context=True)
    # A grey matching button at zero tickets is exit-only, including the last
    # settled summary. Recognize it before offering the direct-match shortcut.
    exhausted = _zero_ticket_exit(page) if loh else None
    if exhausted is not None:
        return exhausted
    labels = {t['text'] for t in tokens}
    if event_header and {'集结战积分','获得的集结战积分','英杰槽'} <= labels:
        next_tokens = [t for t in tokens if t['text'] == '下一步' and t['bbox'][1] > height*.85]
        actions = {}
        if len(next_tokens) == 1 and page.enabled(next_tokens[0]):
            x,y,r,b = next_tokens[0]['bbox']
            actions['continue_result'] = dict(point=[(x+r)//2,(y+b)//2],effect='progress',resource_kind='free')
        actions.update(page.action('choose_opponent', ('确定对战对手', '决定对战对手'),
                                   'footer', effect='admission', resource_kind='ticket'))
        return _result('loh_result','settled_points_summary',actions,
                       result_visible=True,requires_loh_context=True)
    ended = any(t['text'] == '赛事结束' for t in tokens)
    continues = [t for t in tokens if t['text'] == '继续' and t['bbox'][1] > height*.85]
    if ended and len(continues) == 1 and page.enabled(continues[0]):
        x,y,r,b = continues[0]['bbox']
        return _result('loh_result', 'five_race_aggregate_result',
            {'continue_result':dict(point=[(x+r)//2,(y+b)//2],effect='progress',resource_kind='free')},
            result_visible=True, requires_loh_context=True)
    if (page.has(("赛事结束", "比赛结果", "赛事结果", "联赛结果", "重新选择对手"), "header")
            or (loh and page.find("重新选择对手", "footer"))):
        actions = page.action("continue_result", ("继续", "下一步", "关闭"))
        reselect = page.action("reselect_opponent", ("重新选择对手",), "footer",
                               effect="admission", resource_kind="ticket")
        actions.update(reselect)
        return _numbers(page, _result("loh_result", "current_result_controls", actions,
                                      result_visible=True, requires_loh_context=not event_header))
    if (page.has(("获得奖励", "参赛奖励", "领取成功", "已获得以上奖励", "联赛报酬", "联赛奖励"), "header")
            or (loh and page.has(("报酬", "奖励"), "body")
                and page.find(("获得", "领取", "领取报酬"), "footer"))):
        actions = page.action("claim_reward", ("获得", "领取", "一键领取", "领取报酬"))
        actions.update(page.action("close_reward", ("关闭", "继续", "确定")))
        return _result("loh_rewards", "current_reward_controls", actions,
                       reward_visible=True, requires_loh_context=not event_header)
    if event_header:
        short = page.action('start_race', ('比赛缩短版','缩短版'), (0,.88,1,1),
                            effect='progress',resource_kind='free')
        skip = page.action('skip_race', ('跳过','跳过比赛','全部跳过','跳过全部比赛'), (0,.88,1,1),
                           effect='progress',resource_kind='free')
        results = page.action('view_result', ('查看结果','查看各赛事结果'), (0,.88,1,1),
                              effect='progress',resource_kind='free')
        off = page.find('缩短版：关',(0,.75,1,.90))
        on = page.find('缩短版：开',(0,.75,1,.90))
        if short or skip or results or off or on:
            actions = {**short,**skip,**results}
            if off:
                x,y,r,b = off['bbox']
                actions['enable_short'] = dict(point=[(x+r)//2,(y+b)//2],effect='navigation',
                    resource_kind='free',basis='native_short_mode_switch')
            return _result('loh_preview','existing_entry_short_race',actions,
                short_mode=True if on else False if off else None,requires_loh_context=True)
    playback = _playback(page)
    if playback is not None:
        return playback
    if terminal is not None:
        return terminal
    menu = _event_menu(image, page.rows((0, 0, 1, 1)), page)
    if menu is not None:
        return menu
    if page.has(("赛事", "竞赛", "赛场"), "header"):
        action = page.action("open_events", ("赛事活动", "竞赛活动"), "cards")
        if action:
            return _result("races", "current_events_navigation", action)
    current_home = page.find("主页", "footer")
    if page.has(("主界面", "主页"), "header") or (current_home and current_home.get("selected") is True):
        action = page.action("open_races", ("赛事", "竞赛", "赛场"))
        if action:
            return _result("home", "current_races_navigation", action)
    if page.has(('自动编成',), 'header') and page.dialog is not None:
        return _result('loh_auto_team', 'game_auto_team_options',
            page.action('auto_team_confirm', ('编成', '确定', '自动编成')),
            requires_loh_context=True)
    if page.has(("英雄联赛编组", "编组信息", "团队编成", "参赛队伍确认", "出走队伍确认"), "header"):
        empty = (page.has(("尚未编队", "尚未编成", "未编成", "请选择参赛", "请选择3名"), "body")
                 or any(t["text"] == "+" for t in page.rows("body"))
                 or page.has(('请编入',), 'body'))
        if empty:
            all_empty = sum(t['text'] == '请编入' for t in page.rows('body')) == 3
            return _result("loh_unconfigured", "team_not_configured",
                page.action('auto_team', ('自动编成',)),
                team_complete=False, team_empty=all_empty, requires_loh_context=not event_header)
        action = page.action("confirm_team", ("确定", "确认", "编成确定", "出走", "继续"))
        action.update(page.action('back_loh', ('返回',)))
        return _result("loh_team_confirm", "existing_team_confirmation", action,
                       team_complete=True if 'confirm_team' in action else None,
                       team_confirmation_modal=False, requires_loh_context=not event_header)
    if page.has(("选择对战对手", "对战对手选择", "决定对战对手", "选择对手"), "header"):
        action = page.action("choose_opponent", ("决定对战对手", "决定对手", "确定", "确认"),
                             effect="progress")
        return _result("loh_opponents", "current_opponent_selection", action,
                       requires_loh_context=not event_header)
    if page.dialog is not None:
        return _result("unknown", "unrecognized_foreground_modal", modal_visible=True)
    if loh and sum(bool(re.fullmatch(r'第[1-5]场比赛', t['text'])) for t in tokens) >= 2:
        # Matching first animates the five opponent rows, then offers Next.
        # Both belong to the already submitted entry, not another admission.
        action = page.action('choose_opponent', ('下一页',), effect='progress', resource_kind='free')
        return _result('loh_opponents' if action else 'loh_matching',
                       'matched_five_races' if action else 'opponent_rows_appearing', action,
                       requires_loh_context=True)
    if loh:
        if page.has(("尚未编队", "尚未编成", "队伍未编成"), "body"):
            return _result("loh_unconfigured", "team_not_configured", team_complete=False)
        actions = page.action("choose_opponent", ("决定对战对手", "确定对战对手", "决定对手", "选择对手"),
                              effect="admission", resource_kind="ticket")
        actions.update(page.action("enter_loh", ("编成队伍", "队伍编成", "团队编成", "编组信息"),
                                   effect="navigation"))
        if page.has(("开始比赛", "出走确认", "比赛准备"), "header"):
            actions.update(page.action("start_race", ("开始比赛", "出走", "前往比赛")))
            return _numbers(page, _result("loh_preview", "loh_race_ready", actions))
        if not actions:
            # The event heading appears before controls during matching/race
            # transitions. An unfinished render is not a return to entrance.
            return _result('loading', 'loh_controls_rendering')
        return _numbers(page, _result("loh_hub", "loh_home", actions))
    return _result("unknown", "page_not_recognized")
