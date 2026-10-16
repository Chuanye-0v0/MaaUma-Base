"""Local, portrait-only recognition of the user-demonstrated story event."""
import hashlib
import re
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from core.story_event.banner import observe_banner
from core.page_identity import PageIdentity
from core.passive_tutorial import detect as detect_tutorial, pager_identity
from core.legend_selection.observer import radio_choice
from core.jjc.formation import _control_state
from core.passive_popup import RISK, CONTROL

@lru_cache(maxsize=12)
def template(name):
    if name == 'roulette-gear-blurred':
        source = template('roulette-gear')
        return cv2.GaussianBlur(source, (0, 0), 4) if source is not None else None
    if name == 'reward-obtained':
        source = template('bingo-rewards')
        return source[:34] if source is not None else None
    return cv2.imread(str(Path(__file__).with_name('assets') / (name + '.png')))

def observe(raw, tokens):
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    result = dict(page='unknown', actions={}, frame_sha256=hashlib.sha256(raw).hexdigest())
    if image is None or image.shape[:2] != (1280, 720):
        return result
    ui = PageIdentity(image)
    good = [dict(t, text=re.sub('\\s+', '', t['text'])) for t in tokens if t.get('confidence', 0) >= 0.88]

    def find(text, box=(0, 0, 720, 1280), partial=False):
        a, b, c, d = box
        matches = [t for t in good if (text in t['text'] if partial else text == t['text']) and a <= t['bbox'][0] < t['bbox'][2] <= c and (b <= t['bbox'][1] < t['bbox'][3] <= d)]
        if matches:
            return matches[0] if len(matches) == 1 else None
        return ui.find(text, box, group='event')

    def add(name, token):
        if token:
            a, b, c, d = token['bbox']
            result['actions'][name] = dict(point=[(a + c) // 2, (b + d) // 2])

    def locate(name, box):
        source = template(name)
        if source is None:
            return None
        a, b, c, d = box
        region = image[b:d, a:c]
        if region.shape[0] < source.shape[0] or region.shape[1] < source.shape[1]:
            return None
        _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(region, source, cv2.TM_CCOEFF_NORMED))
        return [a + point[0], b + point[1]] if score >= 0.93 else None

    def match(name, box):
        return locate(name, box) is not None
    tutorial = detect_tutorial(image, tokens)
    close = find('关闭', (120, 680, 620, 1260))
    if find('连接中', partial=True) or find('加载中', partial=True):
        result['page'] = 'loading'
    elif pager_identity(image):
        result['page'] = 'loading'
        if tutorial:
            result.update(page='tutorial_help', tutorial=tutorial, actions={'dismiss_tutorial': dict(point=tutorial['point'])})
    elif find('研究成果奖励', (40, 60, 675, 205)):
        result.update(page='tachyon_rewards', activity_kind='tachyon')
        continued = find('继续', (160, 1030, 580, 1200))
        foreground = [t for t in good if 40 <= t['bbox'][1] < t['bbox'][3] <= 1200]
        controls = [t for t in foreground if t['text'] in CONTROL]
        if continued and len(controls) == 1 and (controls[0]['text'] == '继续') and (not RISK.search(''.join((t['text'] for t in foreground)))) and (_control_state(image, (220, 1050, 500, 1180)) is True):
            add('tachyon_continue', continued)
    elif find('因子研究', (0, 0, 260, 110)) and find('研究槽', (120, 580, 620, 760)) and find('积蓄进度槽', (160, 1000, 590, 1180)):
        result.update(page='tachyon_event', activity_kind='tachyon')
        add('tachyon_back', find('返回', (0, 1000, 160, 1160)))
    elif find('因子研究', (0, 0, 260, 110)) and find('领取期间', (260, 350, 720, 440), partial=True) and find('活动期间已结束', (180, 650, 550, 715)) and find('期间仍可进行因子强化', (180, 700, 550, 780)):
        result.update(page='tachyon_event', activity_kind='tachyon')
        add('tachyon_back', find('返回', (0, 1000, 160, 1160)))
    elif find('领取成功', (180, 0, 560, 480)):
        result['page'] = 'claim'
        add('close', close)
    elif find('活动剧情解锁', (140, 0, 590, 900)):
        result['page'] = 'story_unlock'
        add('close', close)
    elif find('设置完成', (170, 320, 550, 470)):
        result['page'] = 'settings_saved'
        add('close', close)
    elif find('参与次数达成', (140, 320, 590, 470)):
        result['page'] = 'roulette_limit'
        add('close', close)
    elif find('宾果表更换确认', (100, 280, 620, 500)):
        result['page'] = 'roulette_replace'
        add('close', close)
        if not close and find('将更换为新的宾果表', (150, 500, 580, 700)):
            edges = cv2.Canny(image[760:920, 160:560], 60, 140)
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            buttons = [cv2.boundingRect(c) for c in contours]
            buttons = [(x, y, w, h) for x, y, w, h in buttons if 240 <= w <= 325 and 55 <= h <= 100]
            if len(buttons) == 1:
                x, y, w, h = buttons[0]
                result['actions']['close'] = dict(point=[160 + x + w // 2, 760 + y + h // 2])
    elif find('停止转动', (150, 280, 570, 500)) and find('轮盘币不足', partial=True):
        result['page'] = 'roulette_empty'
        add('close', close)
    elif find('跳过确认', (170, 320, 550, 480)):
        result['page'] = 'story_skip_confirm'
        add('skip_confirm', find('跳过', (400, 750, 680, 920)))
    elif find('剧情播放确认', (150, 320, 570, 480)):
        result['page'] = 'story_settings'
        portrait = radio_choice(image, ((69, 543), (407, 543)), radius=12, extent=28)
        silent = radio_choice(image, ((69, 708), (407, 708)), radius=12, extent=28)
        result['portrait'] = None if portrait is None else portrait == 1
        result['silent'] = None if silent is None else silent == 1
        add('portrait', find('竖屏', (410, 500, 620, 590)))
        add('silent', find('无语音', (410, 650, 660, 760)))
        add('confirm_story', find('确定', (400, 750, 680, 920)))
        add('cancel_story', find('取消', (80, 750, 320, 920)))
    elif find('播放确认', (180, 320, 550, 480)):
        result['page'] = 'story_next'
        if find('可连续播放接下来的剧情', (100, 600, 620, 750), partial=True) or find('新', (25, 470, 150, 570)) or match('story-new', (25, 470, 150, 570)) or match('next-new', (25, 470, 150, 570)):
            add('play_next', find('播放', (400, 750, 680, 920), partial=True))
        if ui.find('播放确认', group='event') and ui.find('播放', group='event') and match('story-reward', (575, 470, 635, 620)):
            add('play_next', find('播放', (400, 750, 680, 920), partial=True))
        add('cancel', find('取消', (80, 750, 320, 920)))
    elif find('连续转动轮盘', (20, 130, 400, 330)) and find('轮盘设置', (0, 70, 300, 180)):
        result['page'] = 'roulette_settings'
        on = find('开', (550, 175, 670, 235))
        off = find('关', (550, 175, 670, 235))
        result['continuous'] = True if on and (not off) else False if off and (not on) else None
        selected = radio_choice(image, ((85, 438), (85, 555), (85, 673)), radius=10, extent=27)
        result['eight_lines'] = None if selected is None else selected == 1
        result['actions']['continuous'] = dict(point=[610, 204])
        add('eight_lines', find('直到达成8行宾果', (70, 450, 590, 570)))
        add('save', find('保存', (400, 1120, 650, 1240)))
        add('cancel_settings', find('取消', (80, 1120, 320, 1240)))
    elif find('活动任务', (0, 0, 250, 80)) and sum((bool(find(label, (0, 430, 720, 560))) for label in ('日常', '称号', '期间限定'))) >= 2:
        result['page'] = 'missions'
        surfaces = []
        for index, label in enumerate(('日常', '称号', '期间限定')):
            add('tab_' + str(index), find(label, (0, 430, 720, 560)))
            patch = image[455:520, 25 + index * 230:210 + index * 230].max(axis=2)
            surfaces.append(float(np.median(patch)))
        selected = [i for i, value in enumerate(surfaces) if all((other - value >= 18 for j, other in enumerate(surfaces) if j != i)) and _control_state(image, (25 + i * 230, 455, 210 + i * 230, 520)) is not None]
        result['tab'] = selected[0] if len(selected) == 1 else None
        claim = find('一键领取', (200, 1000, 550, 1160))
        if claim and _control_state(image, (230, 1040, 490, 1130)) is True:
            add('claim', claim)
        add('back', find('返回', (0, 1000, 180, 1160)))
    elif find('活动剧情', (180, 0, 540, 120)):
        result['page'] = 'story_list'
        episodes = sorted((t for t in good if re.fullmatch('第\\d+话', t['text']) and 230 <= t['bbox'][0] <= 350 and (110 <= t['bbox'][1] <= 1030)), key=lambda t: t['bbox'][1])
        for episode in episodes:
            y = episode['bbox'][1]
            patch = image[y + 35:y + 65, 450:550]
            if float((patch.min(axis=2) >= 225).mean()) < 0.85:
                continue
            reward = match('story-reward', (575, max(100, y - 20), 635, y + 50))
            new = find('新', (20, max(100, y - 35), 100, y + 25)) or match('story-new', (25, max(100, y - 35), 100, y + 25))
            if reward or new:
                result['actions']['play_story'] = dict(point=[350, y + 55], episode=episode['text'])
                break
        if 'play_story' not in result['actions']:
            source = template('story-reward')
            scores = cv2.matchTemplate(image[100:1080, 575:635], source, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(scores >= 0.93)
            rows = []
            for y, x in zip(ys, xs):
                if not rows or y - rows[-1][0] > 40:
                    rows.append((int(y), int(x)))
            for index, (y, x) in enumerate(rows):
                top = 100 + y
                patch = image[top + 35:top + 65, 450:550]
                if float((patch.min(axis=2) >= 225).mean()) >= 0.85:
                    result['actions']['play_story'] = dict(point=[350, top + 55], episode=f'visible_reward_row_{index}')
                    break
        add('close', close)
    elif find('跳过', (590, 850, 720, 1010)) and find('旋转屏幕', (570, 960, 720, 1090)):
        result['page'] = 'story_menu'
        add('skip', find('跳过', (590, 850, 720, 1010)))
    elif match('story-menu', (600, 1160, 720, 1280)) and find('自动', (510, 1160, 615, 1280)):
        result['page'] = 'story_playing'
        result['actions']['menu'] = dict(point=[653, 1220])
    elif match('bingo-title', (90, 285, 640, 465)) or match('bingo-lines', (120, 470, 610, 810)) or ((find('获得了', (300, 675, 425, 730)) or match('reward-obtained', (300, 675, 425, 730))) and match('roulette-gear-blurred', (610, 85, 715, 190))):
        result['page'] = 'bingo'
        if close:
            add('close_bingo', close)
        else:
            result['actions']['advance_bingo'] = dict(point=[360, 1030])
    elif find('轮盘德比', (0, 0, 260, 90)):
        result['page'] = 'roulette'
        result['eight_mode'] = bool(find('8行', (280, 475, 445, 540)))
        add('options', find('选项', (610, 100, 710, 210)))
        if match('roulette-gear', (610, 85, 715, 190)):
            result['actions']['options'] = dict(point=[664, 130])
        add('back', find('返回', (0, 1000, 180, 1160)))
        if find('点击', (280, 390, 445, 505), partial=True):
            enabled = _control_state(image, (280, 400, 445, 510))
            if enabled is True:
                result['actions']['spin'] = dict(point=[360, 460])
            elif enabled is False and 'back' in result['actions'] and ('options' in result['actions']):
                result['spin_disabled'] = True
        elif find('跳过', (280, 400, 445, 510)) or match('roulette-obtained', (90, 270, 630, 720)):
            result['page'] = 'roulette_animation'
            result['actions'] = {'skip_spin': dict(point=[360, 460])}
    elif find('剧情活动', (0, 0, 260, 90)) and find('轮盘德比', (130, 820, 355, 960)):
        result['page'] = 'event'
        add('missions', find('活动任务', (490, 810, 715, 960)))
        add('stories', find('活动剧情', (70, 730, 335, 820)))
        add('roulette', find('轮盘德比', (130, 820, 355, 960)))
        add('home', find('主页', (280, 1180, 440, 1280)))
    elif PageIdentity(image).home(tokens):
        result['page'] = 'home'
        banner = observe_banner(raw)
        if banner['page'] == 'home':
            result.update(banner)
    elif find('主页', (280, 1180, 440, 1280)) and find('返回', (0, 1000, 180, 1160)) and find('强化编成', (0, 1180, 160, 1280)):
        result['page'] = 'other_event'
        add('leave_other', find('主页', (280, 1180, 440, 1280)))
    return result
