"""Read-only same-frame Racing Carnival ('竞速嘉年华') UI observer.

Reuses shared UMAA observers (`core.event_menu`, `core.legend_selection.observer`,
`core.legend_race.observer`, `core.page_identity.PageIdentity`,
`core.limited_legend.observer`, passive UI detectors, and
`Page`/`clean`/`integer`/`template` from `strongest_team.observer`).
"""
from pathlib import Path
import re

import cv2
import numpy as np

from core.event_menu import menu_entries
from core.legend_race.observer import selected_horse
from core.legend_selection.observer import _green, observe as observe_selection, radio_choice
from core.page_identity import PageIdentity
from core.strongest_team.observer import Page, clean, integer, template


def _items_slots_empty(image, page):
    """Measure both boost slots under '选中道具'; True ONLY if both are empty."""
    label = page.find('选中道具', (0, 480, 400, 760))
    please = page.find('请选择道具', (180, 320, 540, 560))
    anchor_y = label['bbox'][3] if label else (please['bbox'][3] + 234 if please else 683)
    y0 = max(0, anchor_y + 10)
    y1 = min(1280, y0 + 108)
    if y1 - y0 < 30:
        return False
    # Two centered item boxes below 选中道具 on 720x1280 portrait.
    slots = ((225, 330), (390, 495))
    for x0, x1 in slots:
        crop = image[y0:y1, x0:x1]
        if crop.size == 0:
            return False
        # Interior away from border radius & thin frame.
        ih, iw = crop.shape[:2]
        inner = crop[max(0, int(ih * 0.2)):max(1, int(ih * 0.8)),
                     max(0, int(iw * 0.2)):max(1, int(iw * 0.8))]
        if inner.size == 0:
            return False
        hsv = cv2.cvtColor(inner, cv2.COLOR_BGR2HSV)
        sat_mean = float(hsv[:, :, 1].mean())
        val_mean = float(hsv[:, :, 2].mean())
        std_val = float(inner.std())
        vivid_frac = float(((hsv[:, :, 1] > 75) & (hsv[:, :, 2] > 70)).mean())
        if sat_mean > 32.0 or val_mean < 175.0 or std_val > 28.0 or vivid_frac > 0.08:
            return False
    return True


def _pill_difficulty(image, page):
    pills = {}
    for diff in ('困难', '普通', '简单'):
        found = [t for t in page.rows((0, 120, 720, 650)) if t['text'] == diff]
        if len(found) == 1:
            pills[diff] = found[0]
    if len(pills) >= 2:
        scores = {}
        for diff, tok in pills.items():
            x, y, r, b = tok['bbox']
            crop = image[max(0, y - 10):min(1280, b + 10), max(0, x - 20):min(720, r + 20)]
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            scores[diff] = float(((hsv[:, :, 1] > 85) & (hsv[:, :, 2] > 150)).mean())
        best = max(scores, key=scores.get)
        second = max((v for k, v in scores.items() if k != best), default=0.0)
        selected = best if scores[best] > 0.15 and scores[best] - second > 0.08 else None
        return selected, pills
    # Single difficulty header / pill inside detail modal.
    for diff in ('困难', '普通', '简单'):
        if any((t['text'] == diff or t['text'].endswith(diff)) and '活动' not in t['text']
               for t in page.rows((120, 120, 620, 560))):
            return diff, pills
    return None, pills


def _raw_bytes(image):
    ok, encoded = cv2.imencode('.png', image)
    return encoded.tobytes() if ok else b''


def _is_dimmed_portrait(image, cx, cy):
    if not (35 <= cx <= 685 and 35 <= cy <= 1245):
        return False
    patch = image[max(0, cy - 25):min(1280, cy + 30), max(0, cx - 30):min(720, cx + 30)]
    if not patch.size:
        return False
    return float(np.percentile(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)[:, :, 2], 90)) < 185.0


def _bottom_center_green_region(img, *, min_y_ratio):
    height, width = img.shape[:2]
    x0, x1 = int(width * 0.18), int(width * 0.82)
    y0, y1 = int(height * min_y_ratio), int(height * 0.98)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        np.array([38, 70, 80], dtype=np.uint8),
        np.array([92, 255, 255], dtype=np.uint8),
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = int(cv2.contourArea(contour))
        if area < width * height * 0.002:
            continue
        cx = x0 + x + w // 2
        cy = y0 + y + h // 2
        if not (width * 0.24 <= cx <= width * 0.76):
            continue
        candidates.append((area, int(cx), int(cy)))
    if not candidates:
        return None
    _, cx, cy = max(candidates, key=lambda item: item[0])
    return cx, cy


def _race_preview_action_points(img):
    height, width = img.shape[:2]
    if width > height:
        return None
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    left = hsv[int(height * 0.875) : int(height * 0.955), int(width * 0.18) : int(width * 0.50)]
    right = hsv[int(height * 0.875) : int(height * 0.955), int(width * 0.50) : int(width * 0.82)]
    gap = hsv[int(height * 0.875) : int(height * 0.955), int(width * 0.47) : int(width * 0.53)]
    if left.size == 0 or right.size == 0:
        return None
    left_green = np.mean(
        (left[:, :, 0] >= 38) & (left[:, :, 0] <= 92) & (left[:, :, 1] > 70) & (left[:, :, 2] > 80)
    )
    left_white = np.mean((left[:, :, 1] < 70) & (left[:, :, 2] > 170))
    left_brown = np.mean(
        (left[:, :, 0] >= 5) & (left[:, :, 0] <= 30) & (left[:, :, 1] > 50) & (left[:, :, 2] > 70)
    )
    right_green = np.mean(
        (right[:, :, 0] >= 38) & (right[:, :, 0] <= 92) & (right[:, :, 1] > 70) & (right[:, :, 2] > 80)
    )
    right_white = np.mean((right[:, :, 1] < 70) & (right[:, :, 2] > 170))
    lock_icon = hsv[int(height * 0.83) : int(height * 0.90), int(width * 0.18) : int(width * 0.30)]
    lock_gold = 0.0
    if lock_icon.size:
        lock_gold = np.mean(
            (lock_icon[:, :, 0] >= 15) & (lock_icon[:, :, 0] <= 35)
            & (lock_icon[:, :, 1] > 60) & (lock_icon[:, :, 2] > 120)
        )
    gap_green = 0.0
    if gap.size:
        gap_green = np.mean(
            (gap[:, :, 0] >= 38) & (gap[:, :, 0] <= 92) & (gap[:, :, 1] > 70) & (gap[:, :, 2] > 80)
        )
    if not (left_white > 0.42 and left_brown > 0.04 and right_green > 0.35 and right_white < 0.20):
        left_gray = np.mean((left[:, :, 1] < 60) & (left[:, :, 2] > 80) & (left[:, :, 2] < 210))
        left_locked = (
            (left_gray > 0.45 and left_white < 0.10 and left_green < 0.10 and right_green > 0.35 and gap_green < 0.35)
            or (lock_gold > 0.04 and left_gray > 0.35 and left_green < 0.20 and right_green > 0.35)
        )
        if not left_locked:
            return None
    else:
        left_gray = np.mean((left[:, :, 1] < 60) & (left[:, :, 2] > 80) & (left[:, :, 2] < 210))
        left_locked = False
    left_locked = bool(
        left_locked
        or (lock_gold > 0.04 and left_gray > 0.35 and left_green < 0.20 and right_green > 0.35 and gap_green < 0.35)
    )
    return {
        'view_result': (int(width * 0.351), int(height * 0.919)),
        'go_to_race': _bottom_center_green_region(img, min_y_ratio=0.86) or (int(width * 0.65), int(height * 0.919)),
        'view_result_locked': left_locked,
    }


def observe(image, tokens, context=None, *, raw=None):
    context = context or {}
    if not isinstance(image, np.ndarray) or image.shape[:2] != (1280, 720):
        return dict(page='unknown', actions={}, reason='portrait_720_required')
    page = Page(image, tokens)
    text = ''.join(t['text'] for t in page.tokens)
    rp_matches = [re.search(r'(?:[:：]\d{2}|^|\D)([0-5])/5(?:\D|$)', t['text']) for t in page.rows((450, 0, 715, 115))]
    rp_values = [int(m[1]) for m in rp_matches if m]
    rp = rp_values[0] if len(rp_values) == 1 else None
    result = dict(
        page='unknown',
        actions={},
        reason='racing_carnival_page_unknown',
        rp=rp,
        tokens=page.tokens,
        requires_context=True,
        native_busy=page.find('连接中', (500, 0, 710, 145)) is not None,
    )

    def finish(page_name, reason_text, actions=None, **fields):
        # Defense-in-depth: never expose 兑换, 养成, or 宝石 anywhere in actions.
        cleaned = {}
        for k, v in (actions or {}).items():
            label = clean(v.get('label', '')) if isinstance(v, dict) else ''
            if any(bad in k or bad in label for bad in ('兑换', '养成', '宝石', 'exchange', 'cultivate', 'jewel')):
                continue
            cleaned[k] = v
        return dict(result, page=page_name, reason=reason_text, actions=cleaned, **fields)

    home = page.tab('home', '主页', (280, 1200, 430, 1280))
    if not home:
        target = PageIdentity(image).find('主页', (290, 1200, 430, 1280))
        if target:
            x, y, r, b = target['bbox']
            home = {
                'home': dict(
                    point=[(x + r) // 2, (y + b) // 2],
                    effect='navigation',
                    resource_kind='free',
                    basis='shared_home_caption_template',
                )
            }
    cancel = page.button('cancel', ('取消', '关闭'), (0, 700, 360, 1250))

    # Account conflict / network error
    if any(s in text for s in ['账号在其他地方登录', '账号已在其他设备', '帐号在其他地方']):
        return finish('connection_conflict', 'racing_carnival_account_conflict')
    if all(s in text for s in ['网络错误', '重试']):
        return finish(
            'carnival_network',
            'native_network_error',
            page.button('retry_network', '重试', (350, 600, 720, 1150)),
        )

    # Foreign event screens (LOH / Special Transfer / Strongest Team)
    if (
        page.has('英杰集结战', (0, 0, 250, 80))
        and page.has('特别赛事', (0, 700, 720, 850))
        and page.find('比赛一览', (0, 850, 720, 1050))
    ) or (
        page.find('特殊转队', (0, 0, 250, 80)) and home
    ) or (
        (page.has('目标!最强团队', (0, 0, 350, 80)) or page.has('目标！最强团队', (0, 0, 350, 80))) and home
    ):
        return finish('foreign_event', 'explicit_foreign_event_page', home, requires_context=False)

    # 1. Playback confirmation modal
    if (
        page.has('比赛播放确认', (200, 340, 550, 480))
        and page.has('画面设置', (0, 420, 320, 540))
        and page.has('横屏', (0, 480, 360, 610))
        and page.has('竖屏', (340, 480, 720, 610))
    ):
        actions = page.button('cancel', '取消', (0, 730, 355, 930), effect='navigation', resource_kind='free')
        actions.update(
            page.button('confirm_playback', '确定', (355, 730, 700, 930), effect='navigation', resource_kind='free')
        )
        return finish('carnival_playback_confirm', 'native_race_playback_confirmation', actions)

    # 2. Refill limit / paid / refill dialogs
    if any(s in text for s in ['竞技值', 'RP']) and any(
        s in text for s in ['今日回复次数已达上限', '今天的回复次数已达上限', '已达到今日回复上限', '今日可回复次数已用完']
    ):
        actions = page.button('close_refill_limit', ('关闭', '确定'), (200, 600, 650, 1200))
        return finish('carnival_refill_limit', 'native_daily_refill_limit', actions, recovery_maxed_verified=True)

    if any(s in text for s in ['消耗宝石', '使用宝石', '购买宝石', '充值']) and not page.has('迷你胡萝卜果冻'):
        return finish('carnival_paid', 'paid_resource_not_allowed', cancel or page.button('cancel', ('取消', '关闭'), (0, 650, 720, 1250)))

    if (
        page.has('回复完毕')
        and (page.has('已回复竞技值') or page.has('回复了竞技值') or page.has('竞技值') or page.has('迷你胡萝卜果冻'))
    ) or (
        page.has('回复竞技值')
        and re.search(r'使用\d+个迷你胡萝卜果冻', text)
        and re.search(r'回复\d+竞技值', text)
    ):
        counts = sorted(
            [
                (t['bbox'][0], int(m[1]))
                for t in page.rows((220, 560, 620, 780))
                for m in [re.search(r'(?:[:：]\d{2}|^|\D)([0-5])/5(?:\D|$)', t['text'])]
                if m
            ],
            key=lambda pair: pair[0],
        )
        return finish(
            'carnival_refill_done',
            'native_rp_refill_completed',
            page.button('close_refill_done', '关闭', (200, 740, 520, 1220)),
            rp=counts[-1][1] if counts else None,
        )

    if page.has('回复竞技值') and page.has('迷你胡萝卜果冻') and (
        page.has('获得1点竞技值')
        or page.has('使用后竞技值回复')
        or page.has('回复数量')
        or page.has('消耗数量')
        or page.has('回复后')
        or (page.has('持有数') and page.has('最小') and page.has('最大'))
    ):
        standalone_qty = [re.fullmatch(r'([1-5])', t['text']) for t in page.rows((250, 570, 470, 700))]
        rp_after_rows = [
            re.search(r'(?:[:：]\d{2}|^|\D)([1-5])/5(?:\D|$)', t['text'])
            for t in page.rows((230, 570, 495, 700))
        ]
        rp_after = next((int(m[1]) for m in rp_after_rows if m), None)
        before = page.number((300, 710, 465, 810))
        after = page.number((505, 710, 650, 810))
        quantity = next((int(m[1]) for m in standalone_qty if m), None)
        if quantity is None and rp_after is not None and before is not None and after is not None:
            delta = before - after
            if 1 <= delta <= 5 and delta <= rp_after:
                quantity = delta
        actions = dict(cancel)
        actions.update(page.button('minimum_refill', '最小', (20, 580, 165, 700)))
        actions.update(page.button('maximum_refill', '最大', (555, 580, 705, 700)))
        plus = page.find('+', (440, 580, 580, 700))
        if plus and quantity is not None and quantity < 5:
            x, y, r, b = plus['bbox']
            actions['increase_refill'] = dict(point=[(x + r) // 2, (y + b) // 2], effect='navigation', resource_kind='free')
        minus = page.find('-', (140, 580, 290, 700))
        if minus and quantity is not None and quantity > 1:
            x, y, r, b = minus['bbox']
            actions['decrease_refill'] = dict(point=[(x + r) // 2, (y + b) // 2], effect='navigation', resource_kind='free')
        circles = cv2.HoughCircles(
            cv2.cvtColor(image[585:710, 140:580], cv2.COLOR_BGR2GRAY),
            cv2.HOUGH_GRADIENT, 1, 100, param1=80, param2=22, minRadius=25, maxRadius=48,
        )
        if circles is not None and quantity is not None:
            measured = [(float(x) + 140, float(y) + 585) for x, y, _ in circles[0] if 610 <= float(y) + 585 <= 680]
            for key, left, right, allowed in [
                ('decrease_refill', 150, 280, quantity > 1),
                ('increase_refill', 440, 570, quantity < 5),
            ]:
                found = [(x, y) for x, y in measured if left <= x <= right]
                if len(found) == 1 and allowed and key not in actions:
                    actions[key] = dict(
                        point=[int(found[0][0]), int(found[0][1])],
                        effect='navigation',
                        resource_kind='free',
                        basis='named_refill_measured_circle',
                    )
        if quantity is not None and before is not None and after is not None and before - after == quantity:
            confirm = page.green(
                'confirm_refill',
                (350, 820, 710, 1000),
                effect='resource',
                resource_kind='mini_jelly',
                quantity=quantity,
                stock_before=before,
                stock_after=after,
            )
            if not confirm:
                confirm = page.button(
                    'confirm_refill',
                    ('确定', '使用', '回复'),
                    (350, 820, 710, 1000),
                    effect='resource',
                    resource_kind='mini_jelly',
                    quantity=quantity,
                    stock_before=before,
                    stock_after=after,
                )
            actions.update(confirm)
        return finish(
            'carnival_refill_qty',
            'native_mini_jelly_refill',
            actions,
            quantity=quantity,
            stock_before=before,
            stock_after=after,
            item='mini_jelly',
        )

    if page.has('回复竞技值') and page.has('迷你胡萝卜果冻'):
        # Item selection modal: NEVER expose 宝石 row; ONLY expose 迷你胡萝卜果冻's 使用 button.
        actions = page.button('close_refill_items', ('关闭', '取消'), (150, 840, 570, 1250))
        jelly_tok = page.find('迷你胡萝卜果冻', (40, 180, 500, 860))
        use_tokens = [t for t in page.rows((480, 120, 710, 860)) if t['text'] == '使用']
        if jelly_tok and use_tokens:
            jy = (jelly_tok['bbox'][1] + jelly_tok['bbox'][3]) // 2
            matching = [
                t for t in use_tokens
                if t['bbox'][1] >= jelly_tok['bbox'][1] - 20
                and abs((t['bbox'][1] + t['bbox'][3]) // 2 - jy) <= 75
            ]
            if len(matching) == 1 and page.enabled(matching[0]):
                ux, uy, ur, ub = matching[0]['bbox']
                actions['select_jelly'] = dict(
                    point=[(ux + ur) // 2, (uy + ub) // 2],
                    bbox=matching[0]['bbox'],
                    label='使用',
                    effect='navigation',
                    resource_kind='free',
                    basis='named_mini_jelly_row',
                )
        # Parse stock from jelly row next to 持有数 (exclude lower '竞技值回复 1' row).
        stock = None
        if jelly_tok:
            jy = (jelly_tok['bbox'][1] + jelly_tok['bbox'][3]) // 2
            hold = page.find('持有数', (100, max(0, jy - 5), 320, min(1280, jy + 65)))
            if hold:
                stock = page.number((hold['bbox'][2], hold['bbox'][1] - 8, 460, hold['bbox'][3] + 8))
            if stock is None:
                stock = page.number((120, max(0, jy + 15), 460, min(1280, jy + 58)))
        return finish(
            'carnival_refill_select',
            'native_existing_mini_jelly_selector',
            actions,
            item='mini_jelly',
            stock_before=stock,
        )

    if page.has('回复竞技值') and (page.has('竞技值不足') or page.has('是否回复竞技值')):
        actions = dict(cancel)
        actions.update(page.button('open_refill', ('确定', '回复'), (350, 730, 700, 950)))
        return finish(
            'carnival_refill_prompt',
            'native_insufficient_rp_recovery_prompt',
            actions,
            recovery_needed_verified=True,
            rp=0 if rp is None else rp,
        )

    # 3. Missions & Claimed dialogs
    if (
        (context.get('context_verified') or (context.get('pending_operation') or {}).get('resource') == 'claim')
        and page.has('领取成功')
        and page.has('已获得以上奖励')
    ):
        close = page.button('close_claimed', '关闭', (180, 750, 540, 1250))
        return finish('carnival_claimed', 'native_rewards_claimed', close)

    if (page.has('活动任务', (180, 0, 540, 150)) or page.has('限定任务', (180, 0, 540, 150))) and (
        page.has('日常', (0, 80, 360, 210)) or page.has('期间限定', (360, 80, 720, 210))
    ):
        daily_tok = page.find('日常', (0, 80, 360, 210))
        period_tok = page.find('期间限定', (360, 80, 720, 210))
        tab = None
        if daily_tok and period_tok:
            # Active tab has higher saturation / bright green vs gray, OR fallback to visible mission copy
            tab_scores = {}
            for k, tok in (('daily', daily_tok), ('period', period_tok)):
                x, y, r, b = tok['bbox']
                crop = image[max(0, y - 10):min(1280, b + 10), max(0, x - 35):min(720, r + 35)]
                hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
                tab_scores[k] = float(((hsv[:, :, 0] >= 25) & (hsv[:, :, 0] <= 90) & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 140)).mean())
            if abs(tab_scores['daily'] - tab_scores['period']) > 0.08:
                tab = 'daily' if tab_scores['daily'] > tab_scores['period'] else 'period'
        if tab is None:
            if page.has('【日常】') or page.has('日常任务'):
                tab = 'daily'
            elif page.has('【期间限定') or page.has('期间限定任务') or page.has('嘉年华积分'):
                tab = 'period'
        actions = page.button('close_missions', '关闭', (0, 1080, 360, 1260))
        actions.update(page.button('tab_daily', '日常', (0, 80, 360, 210)))
        actions.update(page.button('tab_period', '期间限定', (360, 80, 720, 210)))
        claimable = len([t for t in page.rows((480, 180, 710, 1080)) if t['text'] == '领取'])
        if claimable or any(t['text'] == '一键领取' and page.enabled(t) for t in page.rows((340, 1080, 705, 1260))):
            claim_btn = page.button(
                'claim_all', '一键领取', (340, 1080, 705, 1260),
                effect='claim', resource_kind='earned_rewards',
            )
            if claim_btn:
                actions.update(claim_btn)
        return finish('carnival_missions', 'native_event_missions', actions, mission_tab=tab, claimable=claimable)

    # 3.9 Challenge Race 10-minute expiration notice ("挑战赛事结束 / 距赛事出现已超过10分钟")
    # Must run BEFORE carnival_race_end because "挑战赛事结束" contains substring "赛事结束".
    if page.has('挑战赛事结束', (140, 300, 580, 580)) and (
        page.has('超过10分钟') or page.has('再次挑战') or not page.has('养成', (360, 700, 720, 960))
    ):
        close_exp = page.button(
            'continue_notice', '关闭', (150, 720, 570, 960),
            effect='navigation', resource_kind='free',
        )
        return finish(
            'carnival_notice', 'native_carnival_challenge_expired', close_exp,
            requires_context=False, event_verified=True,
        )

    # 4. Settlement chain (Race End -> Factor -> Rewards -> Points -> Result)
    if page.has('赛事结束', (200, 350, 520, 560)) and not page.has('挑战赛事结束'):
        # STRICT RULE: expose ONLY left 关闭, NEVER right green 养成!
        close = page.button('close_race_end', '关闭', (0, 700, 370, 960), effect='settlement', resource_kind='free')
        return finish('carnival_race_end', 'native_carnival_race_end', close)

    if page.has('获得嘉年华加成因子', (100, 200, 640, 420)):
        btn = page.button('continue_factor', ('继续', '关闭'), (200, 1050, 540, 1260), effect='settlement', resource_kind='free')
        return finish('carnival_factor', 'native_carnival_factor_gained', btn)

    if page.has('获得奖励', (180, 160, 540, 330)):
        btn = page.button('continue_rewards', ('继续', '关闭'), (200, 1050, 540, 1260), effect='settlement', resource_kind='free')
        return finish('carnival_rewards', 'native_carnival_rewards', btn)

    if page.has('获得嘉年华积分', (80, 180, 640, 460)):
        btn = page.button('close_points', ('关闭', '继续'), (200, 680, 540, 1180), effect='settlement', resource_kind='free')
        pts = page.number((220, 560, 520, 740))
        return finish('carnival_points', 'native_carnival_points_gained', btn, points_gained=pts)

    # Race result banner (胜利 / 失败 + 继续)
    if (
        (page.has('胜利', (0, 650, 380, 1050)) or page.has('失败', (0, 650, 380, 1050)))
        and (
            page.has('继续', (300, 1100, 680, 1260))
            or page.has('排名表', (40, 1100, 360, 1260))
            or page.has('名单表', (40, 1100, 360, 1260))
        )
    ):
        outcome = 'win' if page.has('胜利', (0, 650, 380, 1050)) else 'loss'
        btn = page.button('continue_result', ('继续', '下一步'), (300, 1100, 680, 1260), effect='settlement', resource_kind='free')
        if not btn:
            btn = page.green('continue_result', (320, 1140, 680, 1250), min_height=18, effect='settlement', resource_kind='free')
        return finish('carnival_result', 'native_carnival_race_result', btn, outcome=outcome)

    # Quick-result placement reveal after 查看结果 ("第 2 名" + translucent "点击")
    if (
        context.get('context_verified')
        and context.get('pending_entry')
        and not page.has('前往赛事', (320, 1100, 690, 1260))
        and not page.has('养成', (360, 1000, 720, 1280))
    ):
        click_tok = page.find('点击', (200, 960, 520, 1180))
        has_place = (
            page.has('名', (220, 700, 560, 920))
            or any(t['text'].isdigit() for t in page.rows((220, 700, 520, 920)))
        )
        if click_tok and has_place:
            cx, cy, cr, cb = click_tok['bbox']
            return finish(
                'carnival_result',
                'native_carnival_quick_result_tap',
                {
                    'continue_result': dict(
                        point=[(cx + cr) // 2, (cy + cb) // 2],
                        bbox=click_tok['bbox'],
                        effect='settlement',
                        resource_kind='free',
                        basis='named_quick_result_tap',
                    )
                },
            )

    # 5. Lineup / Boost item selection modal (选择道具 / 选中道具)
    if page.has('选择道具', (180, 120, 540, 380)) and (page.has('选中道具') or page.has('请选择道具')):
        modal_settled = (
            page.find('选中道具', (0, 480, 400, 760)) is not None
            or page.find(('参赛!', '参赛'), (350, 790, 680, 960)) is not None
        )
        items_empty = _items_slots_empty(image, page) if modal_settled else None
        actions = page.button('cancel', '取消', (40, 790, 360, 960), effect='navigation', resource_kind='free')
        # Parse RP from inside dialog row (e.g., 5/5 -> 4/5 or 0/5; OCR may glue timer '剩余0:00:005/5')
        dialog_rp_tokens = sorted(
            page.rows((200, 740, 640, 870)), key=lambda t: t['bbox'][0]
        )
        dialog_rp_vals = [
            int(m[1])
            for t in dialog_rp_tokens
            for m in [re.search(r'([0-5])/5', t['text'])]
            if m
        ]
        rp_before = dialog_rp_vals[0] if dialog_rp_vals else rp
        # Check cost tokens
        cost_matches = [re.search(r'消耗[:：]?([0-9]+)', t['text']) for t in page.rows((340, 820, 680, 960))]
        known_costs = [int(m[1]) for m in cost_matches if m]
        cost_ok = not any(c != 1 for c in known_costs)
        if rp_before == 0:
            plus = page.find('+', (480, 740, 680, 885))
            if plus:
                px, py, pr, pb = plus['bbox']
                actions['open_refill'] = dict(point=[(px + pr) // 2, (py + pb) // 2], effect='navigation', resource_kind='free')
            else:
                zero_matches = [t for t in dialog_rp_tokens if '0/5' in t['text']]
                if zero_matches:
                    zx, zy, zr, zb = zero_matches[-1]['bbox']
                    actions['open_refill'] = dict(
                        point=[min(700, zr + 24), min(880, zb + 6)],
                        effect='navigation',
                        resource_kind='free',
                    )
        elif items_empty and cost_ok:
            enter = page.green(
                'enter_race',
                (350, 790, 680, 960),
                effect='admission',
                resource_kind='carnival_rp',
                cost=1,
            )
            if not enter:
                enter = page.button(
                    'enter_race', ('参赛!', '参赛'), (350, 790, 680, 960),
                    effect='admission', resource_kind='carnival_rp', cost=1,
                )
            actions.update(enter)
        return finish(
            'carnival_lineup',
            'native_carnival_items_confirm',
            actions,
            items_empty=items_empty,
            rp=rp_before,
        )

    # 6. Pre-race Preview & Race Grid (前往赛事 / 查看结果, OR pre-items 继续/参赛!)
    go_tok = page.find('前往赛事', (320, 1100, 690, 1260))
    view_tok = page.find('查看结果', (140, 1100, 405, 1260))
    preview_pts = _race_preview_action_points(image)
    if (go_tok and page.enabled(go_tok)) or (
        view_tok and view_tok.get('enabled') is not False and preview_pts and not preview_pts.get('view_result_locked')
    ):
        actions = page.button('cancel_preview', '返回', (0, 1100, 210, 1260), effect='navigation', resource_kind='free')
        if (
            view_tok
            and view_tok.get('enabled') is not False
            and preview_pts
            and not preview_pts.get('view_result_locked')
        ):
            vx, vy, vr, vb = view_tok['bbox']
            actions['view_result'] = dict(
                point=[(vx + vr) // 2, (vy + vb) // 2],
                bbox=view_tok['bbox'],
                effect='progress',
                resource_kind='free',
                basis='named_carnival_view_result',
            )
        if go_tok and page.enabled(go_tok):
            x, y, r, b = go_tok['bbox']
            actions['continue_race'] = dict(
                point=[(x + r) // 2, (y + b) // 2],
                bbox=go_tok['bbox'],
                effect='progress',
                resource_kind='free',
                basis='named_carnival_go_to_race',
            )
        elif preview_pts:
            actions['continue_race'] = dict(
                point=list(preview_pts['go_to_race']),
                effect='progress',
                resource_kind='free',
                basis='shared_race_preview_geometry',
            )
        return finish(
            'carnival_race_ready',
            'owned_carnival_race_preview',
            actions,
            view_result_locked=bool(preview_pts.get('view_result_locked')) if preview_pts else True,
        )

    if (
        (page.has('嘉年华赛事', (0, 0, 520, 120)) or page.has('挑战赛事', (0, 0, 520, 120)))
        and len([t for t in page.rows((480, 120, 710, 1120)) if '人气第' in t['text']]) >= 2
        and (page.find('继续', (200, 1110, 540, 1260)) or page.find(('参赛!', '参赛'), (200, 1110, 540, 1260)))
    ):
        actions = page.button('open_items', ('继续', '参赛!', '参赛'), (200, 1110, 540, 1260), effect='navigation', resource_kind='free')
        actions.update(page.button('start_race', ('参赛!', '参赛'), (200, 1110, 540, 1260), effect='progress', resource_kind='free'))
        if not context.get('pending_entry'):
            actions.update(page.button('cancel_grid', '返回', (0, 1110, 210, 1260), effect='navigation', resource_kind='free'))
        return finish('carnival_race_grid', 'native_carnival_participant_grid', actions)

    # 7. Track / Challenge Detail modal (初获第1名奖励 / 挑战赛事)
    if (
        page.has('初获第1名奖励', (0, 620, 720, 1080))
        or (
            (page.has('挑战赛事', (100, 100, 620, 460)) or page.has('对手优俊少女信息', (0, 300, 400, 680)))
            and (page.find(('参赛!', '参赛', '选择参赛马娘'), (250, 1020, 700, 1260)) is not None)
        )
    ):
        is_challenge = page.has('挑战赛事') or page.has('高难') or page.has('超高难')
        first_win_claimed = (
            page.has('已完成', (0, 680, 720, 1100)) if page.has('初获第1名奖励', (0, 620, 720, 1080)) else None
        )
        selected_diff, diff_pills = _pill_difficulty(image, page)
        actions = page.button('close_detail', ('取消', '关闭', '返回'), (0, 1020, 370, 1260))
        actions.update(
            page.button(
                'open_selection',
                ('选择参赛马娘', '参赛!', '参赛'),
                (250, 1020, 700, 1260),
                effect='navigation',
                resource_kind='free',
            )
        )
        for diff, tok in diff_pills.items():
            x, y, r, b = tok['bbox']
            actions['select_difficulty_' + diff] = dict(
                point=[(x + r) // 2, (y + b) // 2],
                bbox=tok['bbox'],
                effect='navigation',
                resource_kind='free',
            )
        return finish(
            'carnival_detail',
            'native_carnival_track_detail',
            actions,
            event_verified=True,
            requires_context=False,
            is_challenge=is_challenge,
            first_win_claimed=first_win_claimed,
            selected_difficulty=selected_diff,
        )

    # 8. Selection & Sort screens (reuse core.legend_selection.observer + selected_horse)
    raw_buf = raw if raw is not None else _raw_bytes(image)
    if raw_buf:
        sel = observe_selection(raw_buf, page.tokens, daily=True)
        if sel.get('page') == 'sort' or (
            page.has('显示设置', (200, 20, 540, 100))
            and page.has('显示顺序', (30, 160, 260, 240))
        ):
            actions = {
                k: dict(v, effect='navigation', resource_kind='free')
                for k, v in sel.get('actions', {}).items()
            }
            eval_tok = page.find('评价', (60, 225, 680, 740))
            evaluation_selected = sel.get('evaluation_selected')
            if eval_tok:
                ex0, ey0, ex1, ey1 = eval_tok['bbox']
                actions['sort_evaluation'] = dict(
                    point=[(ex0 + ex1) // 2, (ey0 + ey1) // 2],
                    bbox=eval_tok['bbox'],
                    effect='navigation',
                    resource_kind='free',
                )
                rx = 73 if ex0 < 300 else 363
                ry = (ey0 + ey1) // 2
                centers = [
                    (73, 274), (363, 274),
                    (73, 374), (363, 374),
                    (73, 474), (363, 474),
                    (73, 574), (363, 574),
                ]
                if page.rows((60, 635, 680, 720)):
                    centers.extend([(73, 674), (363, 674)])
                snapped = False
                for idx, (cx, cy) in enumerate(centers):
                    if abs(cx - rx) <= 28 and abs(cy - ry) <= 28:
                        centers[idx] = (rx, ry)
                        snapped = True
                        break
                if not snapped:
                    centers.append((rx, ry))
                choice = radio_choice(image, centers)
                eval_patch = image[max(0, ry - 9):min(1280, ry + 9), max(0, rx - 9):min(720, rx + 9)]
                eval_green = float(_green(eval_patch).mean()) if eval_patch.size else 0.0
                other_green = max(
                    (
                        float(_green(image[max(0, cy - 9):min(1280, cy + 9), max(0, cx - 9):min(720, cx + 9)]).mean())
                        for (cx, cy) in centers
                        if (cx, cy) != (rx, ry)
                    ),
                    default=0.0,
                )
                if (choice is not None and centers[choice] == (rx, ry)) or (eval_green >= 0.35 and other_green < 0.10):
                    evaluation_selected = True
                elif (choice is not None and centers[choice] != (rx, ry)) or (eval_green < 0.08 and other_green >= 0.35):
                    evaluation_selected = False
            if evaluation_selected is True:
                apply_tok = page.find('确定', (380, 1130, 670, 1230))
                if apply_tok:
                    ax0, ay0, ax1, ay1 = apply_tok['bbox']
                    actions['apply_sort'] = dict(
                        point=[(ax0 + ax1) // 2, (ay0 + ay1) // 2],
                        bbox=apply_tok['bbox'],
                        effect='navigation',
                        resource_kind='free',
                    )
            else:
                actions.pop('apply_sort', None)
            has_bonus_skill = page.has('加成技能', (60, 225, 680, 740))
            sort_verified = bool(context.get('context_verified') or has_bonus_skill)
            extra_sort = {'event_verified': True} if has_bonus_skill else {}
            return finish(
                'sort',
                'shared_legend_selection_sort',
                actions,
                evaluation_selected=evaluation_selected,
                requires_context=not sort_verified,
                **extra_sort,
            )
        if sel.get('page') == 'selection':
            actions = {
                k: dict(v, effect='navigation', resource_kind='free')
                for k, v in sel.get('actions', {}).items()
            }
            for idx, cand in enumerate(sel.get('candidates') or []):
                pt = cand.get('point') or []
                if len(pt) == 2 and _is_dimmed_portrait(image, int(pt[0]), int(pt[1])):
                    actions.pop(f'horse_{idx}', None)
            copied = {
                k: sel[k]
                for k in (
                    'sort_field', 'direction', 'filter_off', 'filter_state',
                    'surface', 'distance', 'name', 'candidates', 'selected',
                    'grid_complete', 'grid_geometry_verified', 'at_top', 'at_bottom',
                    'selection_rejection', 'frame_sha256',
                )
                if k in sel
            }
            if 'open_sort' not in actions:
                sort_cands = [
                    t for t in page.rows((385, 960, 575, 1028))
                    if t['text'] and not t['text'].startswith(('筛选', '升序', '降序'))
                ]
                if sort_cands:
                    sort_tok = sort_cands[0]
                    sx, sy, sr, sb = sort_tok['bbox']
                    actions['open_sort'] = dict(
                        point=[(sx + sr) // 2, (sy + sb) // 2],
                        bbox=sort_tok['bbox'],
                        effect='navigation',
                        resource_kind='free',
                        basis='carnival_selection_sort_button',
                    )
                    if copied.get('sort_field') is None and sort_tok['text'] != '评价':
                        copied['sort_field'] = sort_tok['text']
                elif sel.get('filter_state') is not None and sel.get('direction') in ('升序', '降序'):
                    actions['open_sort'] = dict(
                        point=[452, 993],
                        effect='navigation',
                        resource_kind='free',
                        basis='carnival_selection_sort_pill_anchor',
                    )
            if not copied.get('selection_rejection') and any(
                ('无法选择' in t['text']) or ('重复' in t['text'] and '优俊少女' in t['text'])
                for t in page.rows((60, 590, 660, 700))
            ):
                copied['selection_rejection'] = 'opponent_duplicate'
            if image is not None and image.shape[:2] == (1280, 720):
                s_tpl = cv2.imread(str(Path(__file__).resolve().parent.parent / 'legend_selection/assets/aptitude-s.png'))
                if s_tpl is not None:
                    for g_key, g_labels, l_box, c_box in [
                        ('surface', ('草地', '泥地'), (470, 190, 548, 239), (548, 196, 580, 232)),
                        ('distance', ('短距离', '英里', '中距离', '长距离'), (463, 239, 548, 283), (548, 242, 580, 277)),
                    ]:
                        if sel.get(g_key) is None and any(page.has(lbl, l_box) for lbl in g_labels):
                            grade_tokens = [
                                t for t in tokens
                                if t.get('source') == g_key + '_grade' and len(t['text']) == 1
                                and t['text'] in 'SABCDEFG'
                            ]
                            weak = {t['text'] for t in grade_tokens if t.get('confidence', 0) >= 0.50}
                            other_strong = any(t['text'] != 'S' and t.get('confidence', 0) >= 0.80 for t in grade_tokens)
                            gx0, gy0, gx1, gy1 = c_box
                            g_score = float(cv2.minMaxLoc(cv2.matchTemplate(image[gy0:gy1, gx0:gx1], s_tpl, cv2.TM_CCOEFF_NORMED))[1])
                            if (weak == {'S'} and g_score >= 0.82) or (not other_strong and g_score >= 0.86):
                                sel[g_key] = 'S'
                                copied[g_key] = 'S'
            horse = selected_horse(sel)
            if horse is not None:
                actions.update(
                    page.button('confirm_selected', ('确定', '决定', '下一步'), (200, 1040, 560, 1260),
                                effect='navigation', resource_kind='free')
                )
            actions.update(page.button('cancel_selection', '返回', (0, 1040, 195, 1260)))
            legend_vs = any(t['text'].replace(' ', '').startswith('VS') for t in page.rows((110, 45, 480, 105)))
            extra_sel = (
                dict(event_verified=True, requires_context=False)
                if (not legend_vs or page.has('加成技能', (380, 955, 580, 1030)))
                else {}
            )
            return finish('selection', 'shared_legend_selection', actions, horse=horse, **extra_sel, **copied)

    # 8.5 First-time Racing Carnival tutorial carousel (5-page guide with bottom 继续/关闭).
    # Must run BEFORE carnival_hub because page 1 embeds a miniature carnival_hub thumbnail!
    tutorial_btn = page.button(
        'continue_notice', ('继续', '关闭'), (180, 1130, 540, 1265),
        effect='navigation', resource_kind='free',
    )
    if tutorial_btn and not home and not page.has('活动任务', (480, 950, 715, 1180)) and (
        context.get('context_verified')
        or page.has('上一页', (0, 1130, 180, 1265))
        or any(s in text for s in ['嘉年华', '挑战槽', '迷你胡萝卜果冻'])
    ):
        return finish('carnival_notice', 'native_carnival_tutorial_carousel', tutorial_btn, requires_context=False)

    # 9. Racing Carnival Hub (竞速嘉年华主页)
    if page.has('竞速嘉年华', (0, 0, 400, 260)) or (
        page.has('嘉年华赛事', (0, 220, 720, 820)) and page.has('嘉年华积分', (0, 100, 720, 460))
    ):
        if any(s in text for s in ['活动已结束', '尚未开放', '举办期间已结束']):
            return finish('carnival_closed', 'native_carnival_closed', home, event_verified=True, requires_context=False)
        hub_body_ready = any(
            page.has(tok)
            for tok in ('嘉年华赛事', '挑战赛事', '正在参加的比赛', '嘉年华积分', '兑换', '活动任务')
        )
        hub_footer_ready = bool(
            page.has('正在参加的比赛', (0, 440, 400, 620))
            or page.has('活动任务', (480, 950, 715, 1180))
            or page.has('返回', (0, 950, 220, 1180))
        )
        if not (hub_body_ready and hub_footer_ready):
            return finish('loading', 'native_carnival_hub_rendering', {}, requires_context=False)
        actions = dict(home)
        # RP refill trigger (+ button or X/5 counter when rp < 5)
        if rp is not None and rp < 5:
            plus_tok = page.find('+', (580, 15, 718, 115))
            if plus_tok:
                px, py, pr, pb = plus_tok['bbox']
                actions['open_refill'] = dict(
                    point=[(px + pr) // 2, (py + pb) // 2],
                    bbox=plus_tok['bbox'],
                    effect='navigation',
                    resource_kind='free',
                    basis='named_hub_rp_plus',
                )
            else:
                counter_matches = [
                    t for t in page.rows((450, 0, 715, 115))
                    if re.search(r'(?:[:：]\d{2}|^|\D)([0-4])/5(?:\D|$)', t['text'])
                ]
                if counter_matches:
                    cx0, cy0, cx1, cy1 = counter_matches[0]['bbox']
                    actions['open_refill'] = dict(
                        point=[min(705, cx1 + 18), (cy0 + cy1) // 2 + 4],
                        bbox=counter_matches[0]['bbox'],
                        effect='navigation',
                        resource_kind='free',
                        basis='named_hub_rp_counter_plus',
                    )
        ongoing = page.has('正在参加的比赛', (0, 440, 400, 620))
        is_ongoing_challenge = bool(
            ongoing
            and (
                page.has('挑战赛事', (0, 460, 500, 620))
                or any(t['text'] in {'高难', '超高难'} for t in page.rows((0, 460, 500, 620)))
            )
        )
        # Challenge detection (max gauge OR active timed 高难/超高难 banner)
        challenge_full = bool(
            not ongoing
            and (
                page.has('最大', (120, 200, 620, 660))
                or page.has('挑战槽已满')
                or page.has('冲击挑战赛事')
            )
        )
        challenge_banner_active = bool(
            not ongoing
            and (
                page.has('重新挑战加成', (0, 220, 720, 660))
                or page.has('距离赛事结束剩余', (0, 220, 720, 660))
                or page.has('进入挑战', (0, 220, 720, 660))
                or any(t['text'] in {'高难', '超高难'} for t in page.rows((0, 220, 720, 660)))
            )
        )
        if ongoing:
            resume_btn = page.button(
                'resume_race', ('继续比赛', '进入比赛'), (180, 760, 540, 945),
                effect='navigation', resource_kind='free',
            )
            actions.update(resume_btn)
            if is_ongoing_challenge and 'resume_race' in actions:
                actions['enter_challenge'] = dict(actions['resume_race'], basis='named_carnival_ongoing_challenge')
        elif challenge_full or challenge_banner_active:
            enter_btn = page.find(('进入挑战', '挑战'), (250, 260, 700, 660))
            if enter_btn:
                ex, ey, er, eb = enter_btn['bbox']
                pt = [(ex + er) // 2, (ey + eb) // 2]
            elif challenge_banner_active:
                pt = [420, 560]
            else:
                max_tok = page.find('最大', (120, 200, 620, 660)) or page.find('挑战槽', (120, 200, 620, 660))
                pt = (
                    [(max_tok['bbox'][0] + max_tok['bbox'][2]) // 2, (max_tok['bbox'][1] + max_tok['bbox'][3]) // 2]
                    if max_tok else [360, 580]
                )
            actions['enter_challenge'] = dict(
                point=pt,
                effect='navigation',
                resource_kind='free',
                basis='named_carnival_challenge_entry',
            )

        # Track grid 1..4 inside the carnival_hub scroll container (x=15..705, y=472..940).
        heading = page.find('嘉年华赛事', (0, 220, 720, 820))
        track_keywords = ('草地', '泥地', '困难', '普通', '简单', '短距离', '英里', '中距离', '长距离')
        tracks_rendered = bool(
            heading
            or any(
                any(k in t['text'] for k in track_keywords)
                for t in page.rows((25, 472, 695, 940))
            )
        )
        challenge_section_toks = [
            tok
            for key in ('挑战赛事', '挑战槽', '距离出现', '参与嘉年华赛事')
            for tok in [page.find(key, (0, 420, 720, 760))]
            if tok
        ]
        if heading:
            grid_top = heading['bbox'][3] + 10
        elif challenge_section_toks:
            challenge_floor = max(t['bbox'][3] + 40 for t in challenge_section_toks)
            track_tok_tops = [
                t['bbox'][1] - 25
                for t in page.rows((25, 472, 695, 940))
                if t['bbox'][1] > challenge_floor - 40
                and any(k in t['text'] for k in track_keywords)
            ]
            grid_top = max(
                challenge_floor,
                min(track_tok_tops) if track_tok_tops else 743,
                565 if challenge_banner_active else 425,
            )
        else:
            grid_top = 565 if challenge_banner_active else 425
        row_h, row_gap = 134, 10
        grid_rows = (
            (grid_top, grid_top + row_h),
            (grid_top + row_h + row_gap, grid_top + row_h * 2 + row_gap),
        )
        grid_cols = ((25, 355), (365, 695))
        # Scroll container is bounded by y=472..940 (anything below y=940 is covered by 返回/养成/活动任务).
        view_top = 472 if heading and heading['bbox'][3] >= 460 else 0
        view_bottom = 940
        exchange_tok = page.find('兑换', (420, 200, 710, 430))
        cultivate_tok = page.find('养成', (180, 920, 540, 1200))
        for idx in (range(4) if (tracks_rendered and not ongoing) else []):
            r_i, c_i = divmod(idx, 2)
            x0, x1 = grid_cols[c_i]
            y0, y1 = grid_rows[r_i]
            vis_y0 = max(view_top, y0)
            vis_y1 = min(view_bottom, y1)
            if vis_y1 - vis_y0 < 90:
                continue
            cy = (vis_y0 + vis_y1) // 2
            cx = (x0 + x1) // 2
            if exchange_tok:
                ex0, ey0, ex1, ey1 = exchange_tok['bbox']
                if ex0 - 15 <= cx <= ex1 + 15 and ey0 - 15 <= cy <= ey1 + 15:
                    continue
            if cultivate_tok:
                ux0, uy0, ux1, uy1 = cultivate_tok['bbox']
                if ux0 - 15 <= cx <= ux1 + 15 and uy0 - 15 <= cy <= uy1 + 15:
                    continue
            actions[f'open_track_{idx + 1}'] = dict(
                point=[cx, cy],
                bbox=[x0, vis_y0, x1, vis_y1],
                effect='navigation',
                resource_kind='free',
                basis='anchored_carnival_track_card',
            )
        if not ongoing and page.has('活动任务', (480, 950, 715, 1180)) and (heading or page.has('挑战赛事', (0, 220, 720, 720))):
            actions['hub_scroll_down'] = dict(
                swipe=[360, 860, 360, 500, 400],
                effect='navigation',
                resource_kind='free',
                basis='anchored_carnival_hub_scroll',
            )
            actions['hub_scroll_up'] = dict(
                swipe=[360, 540, 360, 880, 400],
                effect='navigation',
                resource_kind='free',
                basis='anchored_carnival_hub_scroll',
            )

        missions = [t for t in page.rows((480, 950, 715, 1180)) if t['text'].startswith('活动任')]
        if len(missions) == 1:
            mx, my, mr, mb = missions[0]['bbox']
            actions['open_missions'] = dict(
                point=[(mx + mr) // 2, max(920, my - 22)],
                bbox=missions[0]['bbox'],
                effect='navigation',
                resource_kind='free',
                basis='native_event_mission_icon_caption',
            )
        return finish(
            'carnival_hub',
            'named_racing_carnival_hub',
            actions,
            event_verified=True,
            requires_context=False,
            challenge_full=challenge_full,
            challenge_banner_active=challenge_banner_active,
            ongoing_is_challenge=is_ongoing_challenge,
        )

    # 10. Event menu (uses shared core.event_menu.menu_entries!)
    entries = menu_entries(image, page.tokens)
    if entries is not None:
        actions = dict(home)
        for nav_key in ('event_scroll_down', 'event_scroll_up'):
            nav = entries.get('navigation_actions', {}).get(nav_key)
            if nav and 'start' in nav and 'end' in nav:
                actions[nav_key] = dict(
                    swipe=[
                        nav['start'][0], nav['start'][1],
                        nav['end'][0], nav['end'][1],
                        nav.get('duration_ms', 450),
                    ],
                    effect='navigation',
                    resource_kind='free',
                    basis='shared_event_menu_scroll',
                )
        if 'open_carnival' in entries['actions']:
            actions['open_carnival'] = dict(
                entries['actions']['open_carnival'],
                effect='navigation',
                resource_kind='free',
                basis='named_carnival_event_card',
            )
        return finish(
            'events',
            'shared_event_menu',
            actions,
            requires_context=False,
            carnival_available=entries.get('carnival_available'),
            carnival_availability_basis=entries.get('carnival_availability_basis'),
            navigation_actions=entries.get('navigation_actions', {}),
            event_cards=entries.get('event_cards', {}),
        )

    # 11. Races & Home (shared identity & navigation)
    if PageIdentity(image).home():
        actions = page.tab('open_races', '赛事', (430, 1200, 580, 1280))
        if not actions:
            target = PageIdentity(image).find('赛事', (430, 1200, 580, 1280))
            if target:
                x, y, r, b = target['bbox']
                actions['open_races'] = dict(
                    point=[(x + r) // 2, (y + b) // 2],
                    effect='navigation',
                    resource_kind='free',
                    basis='shared_races_caption_template',
                )
        return finish('home', 'shared_home_identity', actions, requires_context=False)

    if page.has('赛事', (0, 0, 300, 150)):
        actions = page.button('open_events', '赛事活动', (200, 650, 700, 1000))
        if actions:
            return finish('races', 'native_races_menu', actions, requires_context=False)

    from core.limited_legend.observer import observe as navigation_observe
    nav = navigation_observe(raw_buf or _raw_bytes(image), image, page.tokens)
    if nav.get('page') == 'races' and 'open_events' in nav.get('actions', {}):
        candidate = dict(nav['actions']['open_events'], effect='navigation', resource_kind='free')
        return finish('races', 'shared_native_races_navigation', {'open_events': candidate}, requires_context=False)

    # 12. In-race skip & verified tutorial/passive dismissal
    if context.get('context_verified'):
        if context.get('pending_entry'):
            if page.has('拍摄', (350, 1150, 550, 1280)):
                icon = template('native_race_fast_forward')
                if icon is not None:
                    scores = cv2.matchTemplate(image[1150:1280], icon, cv2.TM_CCOEFF_NORMED)
                    _, score, _, loc = cv2.minMaxLoc(scores)
                    clusters, _ = cv2.connectedComponents((scores >= .97).astype(np.uint8))
                    if score >= .97 and clusters == 2:
                        h, w = icon.shape[:2]
                        return finish(
                            'carnival_race',
                            'owned_native_fast_forward_icon',
                            {
                                'skip_race': dict(
                                    point=[loc[0] + w // 2, 1150 + loc[1] + h // 2],
                                    effect='progress',
                                    resource_kind='free',
                                    basis='measured_native_fast_forward_icon',
                                )
                            },
                        )
            from core.passive_skip import detect as detect_skip
            skip = detect_skip(image, page.tokens)
            if skip:
                return finish(
                    'carnival_race',
                    'owned_native_skip',
                    {'skip_race': dict(point=skip['point'], effect='progress', resource_kind='free')},
                )
        from core.passive_tutorial import detect as detect_tutorial
        tutorial = detect_tutorial(image, page.tokens)
        if tutorial:
            return finish(
                'carnival_notice',
                'shared_native_tutorial',
                {'continue_notice': dict(point=tutorial['point'], effect='navigation', resource_kind='free')},
            )
        if not any(s in text for s in ['消耗', '回复', '宝石', '养成', '兑换']):
            neutral = page.button('continue_notice', ('继续', '下一步', '点击', '跳过'), (180, 950, 560, 1260))
            if neutral:
                return finish('carnival_notice', 'owned_neutral_presentation', neutral)

    if not page.tokens or any(s in text for s in ['加载中', '连接中', '正在下载', '手纲小姐的建议']):
        return finish('loading', 'native_loading')

    return result
