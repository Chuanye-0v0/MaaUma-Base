"""Pure offline perception for the autonomous-training settlement route."""
import hashlib
import re
from pathlib import Path
import cv2
import numpy as np
from core.page_identity import PageIdentity
_FRAME_SHAPE = (1280, 720)

def observe_harvest(raw: bytes, tokens: list[dict], *, allow_navigation=False, allow_animation_taps=True) -> dict | None:
    """Return a single recognized settlement page and its safe local actions."""
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != _FRAME_SHAPE:
        return None
    result = {'page': None, 'actions': {}, 'frame_sha256': hashlib.sha256(raw).hexdigest(), 'history_rewards_visible': None, 'carrot_reward': None, 'current_skill_points': None}
    good = [token for token in tokens if token.get('confidence', 0) >= 0.9 and _valid_box(token)]

    def normalized(token):
        return re.sub('[\\s！!。,.，、]', '', str(token['text']))

    def within(token, box):
        x0, y0, x1, y1 = box
        left, top, right, bottom = token['bbox']
        return x0 <= left and y0 <= top and (right <= x1) and (bottom <= y1)

    def exact(text, box=(0, 0, 720, 1280)):
        matches = [token for token in good if normalized(token) == text and within(token, box)]
        return matches[0] if len(matches) == 1 else None

    def contains(text, box=(0, 0, 720, 1280)):
        return any((text in normalized(token) and within(token, box) for token in good))

    def add_action(name, text, box, *, enabled=True):
        token = exact(text, box)
        if token is None or (enabled and (not _control_enabled(image, token))):
            return
        left, top, right, bottom = token['bbox']
        result['actions'][name] = {'point': [(left + right) // 2, (top + bottom) // 2]}
    if exact('领取成功', (220, 350, 500, 470)) and exact('已获得以上奖励', (180, 650, 540, 770)):
        result['page'] = 'activity_points_claim'
        add_action('close_activity_points_claim', '关闭', (220, 780, 500, 900))
    elif (exact('活动剧情解锁', (180, 350, 540, 960)) or exact('活动剧情解锁', (180, 0, 540, 120))) and (contains('现可解锁', (100, 600, 620, 1140)) or exact('活动剧情已解锁', (100, 600, 620, 1140))):
        result['page'] = 'activity_story_unlock'
        add_action('close_activity_story_unlock', '关闭', (220, 760, 500, 1240))
    elif exact('活动奖励', (160, 0, 590, 220)) or PageIdentity(image).find('活动奖励', (160, 0, 590, 220), group='event'):
        result['page'] = 'activity_reward_animation'
        add_action('advance_activity_reward', '继续', (220, 1120, 500, 1240))
        if not result['actions'] and allow_animation_taps:
            result['actions']['advance_activity_reward'] = {'point': [360, 1180]}
    elif contains('获得的活动积分', (0, 180, 600, 800)) and contains('活动加成总计', (0, 200, 600, 820)):
        result['page'] = 'activity_points_result'
        add_action('next_activity_points', '下一页', (220, 1100, 500, 1270))
        if not result['actions'] and allow_animation_taps:
            result['actions']['next_activity_points'] = {'point': [360, 1180]}
    elif contains('已获得以上奖励', (180, 1000, 540, 1200)) and exact('关闭', (220, 1100, 500, 1270)):
        result['page'] = 'activity_points_claim'
        add_action('close_activity_points_claim', '关闭', (220, 1100, 500, 1270))
    elif exact('解锁角色剧情', (180, 350, 540, 960)) and exact('现可解锁角色剧情', (180, 660, 540, 1140)):
        result['page'] = 'character_story_unlock'
        add_action('close_character_story_unlock', '关闭', (220, 760, 500, 1240))
        if not result['actions']:
            add_action('close_character_story_unlock', '前往主页', (220, 760, 500, 1240))
    elif exact('确定因子确认', (180, 0, 540, 120)) and contains('是否确定获得该因子', (150, 1000, 570, 1140)):
        result['page'] = 'factor_confirm'
        add_action('confirm_factor_choice', '确定', (400, 1120, 650, 1240))
    elif exact('养成结束确认', (180, 250, 540, 390)) and contains('即将完成养成', (200, 580, 520, 720)) and exact('剩余技能点数', (150, 700, 450, 820)):
        result['page'] = 'finish_confirm'
        point_token = _unique_number(good, (390, 700, 520, 820))
        if point_token is not None:
            result['current_skill_points'] = int(normalized(point_token))
            add_action('complete_training', '完成', (400, 840, 650, 1000))
    elif exact('关注训练员', (180, 350, 540, 500)) and exact('对方的被关注数已达上限', (100, 620, 620, 750)) and exact('无法关注', (200, 650, 520, 760)):
        result['page'] = 'follow_trainer'
        add_action('cancel_follow_trainer', '关闭', (220, 760, 500, 920))
    elif exact('关注训练员', (180, 350, 540, 500)) and contains('是否关注提供了协助卡的训练员', (100, 620, 620, 750)):
        result['page'] = 'follow_trainer'
        add_action('cancel_follow_trainer', '取消', (80, 760, 340, 920))
    elif exact('完成养成', (200, 340, 520, 480)) and contains('将完成养成的优俊少女编入队伍吧', (80, 550, 650, 720)):
        result['page'] = 'training_complete'
        add_action('open_completed_formation', '团队编成', (400, 760, 650, 920))
        add_action('close_completed_training', '关闭', (80, 760, 340, 920))
        if 'close_completed_training' not in result['actions']:
            add_action('close_completed_training', '前往主页', (80, 760, 340, 920))
    elif contains('今后不再弹出该提示', (180, 980, 560, 1100)) and (contains('自主训练优俊少女', (0, 90, 350, 260)) or exact('获得誉名', (220, 20, 500, 110))):
        result['page'] = 'title_notice'
        add_action('confirm_title_notice', '确定', (220, 1120, 500, 1240))
    elif _history_page_evidence(exact) and exact('主题', (250, 100, 480, 210)):
        result['page'] = 'history_summary'
        arrow_point = _unique_history_next_arrow(image)
        if arrow_point is not None:
            result['actions']['history_next'] = {'point': arrow_point}
    elif _history_page_evidence(exact) and exact('战绩', (250, 100, 480, 210)):
        result['page'] = 'history_races'
        result['carrot_reward'] = {'status': 'unknown', 'amount': None}
        result['history_rewards_visible'] = contains('合计获得道具', (0, 240, 300, 930))
        if result['history_rewards_visible']:
            box = carrot_quantity_box(image)
            quantities = [t for t in good if box is not None and within(t, box) and (t.get('source') == 'history_carrot_quantity') and re.fullmatch('[xX×]\\d+', normalized(t))]
            if len(quantities) == 1:
                result['carrot_reward'] = {'status': 'recognized', 'amount': int(normalized(quantities[0])[1:])}
            add_action('confirm_history', '确定', (220, 1120, 500, 1240))
        else:
            result['actions']['history_scroll'] = {'gesture': 'swipe', 'point': [360, 940], 'end': [360, 560]}
    elif exact('技能', (250, 760, 500, 900)) and contains('消耗技能点数即可获得', (0, 930, 400, 1060)) or (exact('启发等级', (300, 780, 620, 910)) and contains('启发等级会影响所需技能点数折扣', (20, 1000, 650, 1140)) and exact('上一页', (20, 1140, 160, 1260))) or (exact('固有技能和稀有技能', (100, 760, 630, 910)) and contains('技能大致分为以下3种', (20, 970, 650, 1040)) and exact('上一页', (20, 1140, 160, 1260))):
        result['page'] = 'skill_tutorial'
        add_action('continue_skill_tutorial', '继续', (220, 1140, 500, 1260))
    elif exact('技能进化', (20, 600, 350, 760)) and contains('将可进化为强力的进化技能', (20, 1040, 650, 1140)) and exact('上一页', (20, 1140, 160, 1260)):
        result['page'] = 'skill_tutorial'
        add_action('continue_skill_tutorial', '关闭', (220, 1140, 500, 1260))
    elif exact('养成等级', (120, 0, 600, 140)):
        result['page'] = 'training_rating'
        score = _unique_score_number(good, (280, 540, 560, 720))
        result['training_rating_score'] = score
        result['training_rating_score_status'] = 'recognized' if score is not None else 'unreadable_or_ambiguous'
        rating_controls = [token for text in ('确定', '确认', '下一页') if (token := exact(text, (220, 1120, 500, 1240))) is not None]
        if len(rating_controls) == 1 and _control_enabled(image, rating_controls[0]):
            left, top, right, bottom = rating_controls[0]['bbox']
            result['actions']['confirm_training_rating'] = {'point': [(left + right) // 2, (top + bottom) // 2]}
    elif exact('因子获取', (100, 0, 620, 130)) and exact('确定因子', (400, 1100, 650, 1240)):
        result['page'] = 'factor_result'
        add_action('confirm_factors', '确定因子', (400, 1100, 650, 1240))
    elif (contains('自主训练优俊少女', (250, 160, 620, 260)) or (exact('优俊少女详情', (220, 20, 500, 110)) and exact('场地适应性', (20, 380, 160, 450)) and exact('跑法适应性', (20, 470, 160, 530)))) and exact('关闭', (220, 1120, 500, 1240)) and exact('养成信息', (480, 500, 690, 620)):
        result['page'] = 'trained_detail'
        add_action('close_trained_detail', '关闭', (220, 1120, 500, 1240))
    elif contains('历代评分', (100, 180, 620, 450)) and contains('第1位更新', (50, 250, 650, 450)):
        result['page'] = 'score_record'
        add_action('continue_score_record', '继续', (220, 1040, 500, 1180))
    elif any((contains(text, (40, 180, 680, 450)) for text in ('剧本评分纪录已刷新', '剧本评分记录已刷新'))):
        result['page'] = 'scenario_record'
        add_action('continue_scenario_record', '继续', (220, 1040, 500, 1180))
    elif contains('总成绩', (250, 540, 500, 660)) and exact('下一页', (220, 1120, 500, 1240)):
        result['page'] = 'training_summary'
        add_action('next_training_summary', '下一页', (220, 1120, 500, 1240))
    elif exact('亲密度等级', (0, 100, 250, 220)) and exact('下一页', (220, 1120, 500, 1240)):
        result['page'] = 'affinity_result'
        add_action('next_affinity_result', '下一页', (220, 1120, 500, 1240))
    elif exact('获得奖励', (100, 0, 620, 140)) and exact('下一页', (220, 1120, 500, 1240)):
        result['page'] = 'training_rewards'
        add_action('next_training_rewards', '下一页', (220, 1120, 500, 1240))
    if result['page'] is None and allow_navigation:
        labels = [text for text in ('继续', '关闭', '下一页') if exact(text, (220, 1040, 500, 1240))]
        if len(labels) == 1:
            add_action('advance_settlement', labels[0], (220, 1040, 500, 1240))
            if result['actions']:
                result['page'] = 'settlement_navigation'
    return result if result['page'] is not None else None

def _valid_box(token):
    box = token.get('bbox')
    return isinstance(box, list) and len(box) == 4 and all((isinstance(value, (int, float)) for value in box))

def _unique_number(tokens, box):
    values = [token for token in tokens if re.fullmatch('\\d+', re.sub('\\s', '', str(token['text']))) and box[0] <= token['bbox'][0] and (box[1] <= token['bbox'][1]) and (token['bbox'][2] <= box[2]) and (token['bbox'][3] <= box[3])]
    return values[0] if len(values) == 1 else None

def _unique_score_number(tokens, box):
    values = [token for token in tokens if re.fullmatch('\\d[\\d,，]*', str(token['text'])) and box[0] <= token['bbox'][0] and (box[1] <= token['bbox'][1]) and (token['bbox'][2] <= box[2]) and (token['bbox'][3] <= box[3])]
    return values[0] if len(values) == 1 else None

def _history_page_evidence(exact):
    if exact('自主训练养成履历', (50, 0, 670, 120)) is not None:
        return True
    if exact('自主训练养成结果', (0, 180, 260, 250)) is None:
        return False
    if exact('战绩', (250, 100, 480, 210)) is not None and exact('确定', (220, 1120, 500, 1240)) is not None and (exact('参赛记录', (0, 240, 200, 290)) is not None or exact('合计获得道具', (0, 240, 300, 930)) is not None):
        return True
    return all((exact('养成优俊少女', (0, 240, 240, 320)) is not None, exact('基础能力', (0, 430, 180, 520)) is not None, exact('适应性', (0, 560, 180, 640)) is not None, exact('跑法适应性', (0, 660, 230, 760)) is not None))

def carrot_quantity_box(image):
    """Locate a carrot reward icon, excluding its variable quantity from matching."""
    template = cv2.imread(str(Path(__file__).with_name('assets') / 'history_carrot_icon.png'))
    if template is None:
        return None
    scores = cv2.matchTemplate(image[240:950, 20:690], template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(scores)
    if score < 0.94:
        return None
    x, y = (x + 20, y + 240)
    return [x + 15, y + 57, x + 83, y + 80] if y + 80 <= 950 else None

def _unique_history_next_arrow(image):
    """Locate the one green outlined > control, rather than clicking a fixed point."""
    x0, y0, x1, y1 = (600, 100, 710, 200)
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] >= 80) & (hsv[:, :, 2] >= 80)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    arrows = []
    for index in range(1, count):
        left, top, width, height, area = stats[index]
        if not (20 <= width <= 50 and 40 <= height <= 80 and (120 <= area <= 500)):
            continue
        component = labels[top:top + height, left:left + width] == index
        third = height // 3
        if third == 0:
            continue
        sections = [np.where(component[start:end])[1] for start, end in ((0, third), (third, third * 2), (third * 2, height))]
        if any((len(section) == 0 for section in sections)):
            continue
        top_x, middle_x, bottom_x = [float(section.mean()) for section in sections]
        if middle_x <= top_x + width * 0.15 or middle_x <= bottom_x + width * 0.15:
            continue
        arrows.append([x0 + int(left + width // 2), y0 + int(top + height // 2)])
    return arrows[0] if len(arrows) == 1 else None

def _control_enabled(image, token):
    left, top, right, bottom = map(int, token['bbox'])
    center_x, center_y = ((left + right) // 2, (top + bottom) // 2)
    patch = image[max(0, center_y - 45):min(1280, center_y + 45), max(0, center_x - 150):min(720, center_x + 150)]
    if patch.size == 0:
        return False
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    colorful = ((hsv[:, :, 0] >= 10) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] >= 70) & (hsv[:, :, 2] >= 100)).mean()
    white = ((hsv[:, :, 1] < 40) & (hsv[:, :, 2] > 230)).mean()
    label_patch = image[max(0, top):min(1280, bottom), max(0, left):min(720, right)]
    pink = 0.0
    if label_patch.size:
        label_hsv = cv2.cvtColor(label_patch, cv2.COLOR_BGR2HSV)
        pink = ((label_hsv[:, :, 0] >= 140) & (label_hsv[:, :, 0] <= 179) & (label_hsv[:, :, 1] >= 70) & (label_hsv[:, :, 2] >= 100)).mean()
    return bool(colorful >= 0.5 or white >= 0.7 or pink >= 0.5)
