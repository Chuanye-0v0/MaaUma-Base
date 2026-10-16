"""Separate race allowlist; the selection-only feature remains non-consuming."""
import re
import cv2
import numpy as np
from core.legend_selection.observer import observe as observe_selection, read_tokens, grid_ready
from core.legend_selection.runner import qualifies

def selected_horse(observed):
    index = observed.get('selected')
    candidates = observed.get('candidates', [])
    if observed.get('page') != 'selection' or type(index) is not int or (not 0 <= index < len(candidates)) or (not observed.get('name')) or (not grid_ready(observed)) or (observed.get('filter_off') is not True) or (observed.get('sort_field') != '评价') or (observed.get('direction') != '降序') or (qualifies(observed) is not True):
        return None
    if type(candidates[index].get('score')) is not int:
        return None
    return dict(name=observed['name'], score=candidates[index]['score'], surface=observed['surface'], distance=observed['distance'])

def observe(raw, tokens, *, navigation=False, event=None, daily=False):
    result = observe_selection(raw, tokens, daily=daily)
    if result['page'] in {'loading', 'blocked', 'connection_interrupted', 'connection_conflict'}:
        return result
    result['actions'] = dict(result['actions']) if navigation else {}
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return result
    result['horse'] = selected_horse(result)
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t['confidence'] >= 0.95]

    def find(text, box):
        x0, y0, x1, y1 = box
        matches = [t for t in good if t['text'].rstrip('!！') == text.rstrip('!！') and x0 <= t['bbox'][0] < t['bbox'][2] <= x1 and (y0 <= t['bbox'][1] < t['bbox'][3] <= y1)]
        return matches[0] if len(matches) == 1 else None

    def bind_event(**evidence):
        visible = {key: value for key, value in evidence.items() if isinstance(value, str) and value}
        if visible:
            result['event'] = visible

    def course_from(text):
        match = re.match('(.+?)(?:札幌|函馆|福岛|新潟|东京|中山|中京|京都|阪神|小仓)(?:草地|泥地)\\d+米', text)
        if not match:
            match = re.match('(.+?(?:锦标赛|大奖赛|纪念|杯|奖|赏))(?:困难|\\d+米|$)', text)
        return match.group(1) if match else None

    def bound_course(tokens):
        course = event.get('course') if isinstance(event, dict) else None
        matches = [token for token in tokens if isinstance(course, str) and course and (course in token['text'])]
        return course if len(matches) == 1 else None

    def header_course():
        titles = [t for t in good if course_from(t['text']) and t['bbox'][1] < 110]
        bound = bound_course([t for t in good if t['bbox'][1] < 110])
        if isinstance(event, dict) and event.get('course'):
            return bound
        if len(titles) != 1:
            return None
        return course_from(titles[0]['text'])
    if result['page'] == 'selection':
        header = [t for t in good if t['bbox'][1] < 150]
        course = bound_course(header)
        courses = [course] if isinstance(event, dict) and event.get('course') and course else [course_from(t['text']) for t in header if course_from(t['text'])]
        opponents = [t['text'][2:] for t in good if t['bbox'][1] < 150 and t['text'].startswith('VS') and (len(t['text']) > 2)]
        if len(courses) == len(opponents) == 1 and courses[0]:
            bind_event(course=courses[0], opponent=opponents[0])
    elif result['page'] == 'details':
        titles = [t for t in good if 190 <= t['bbox'][1] < 360 and t['text'] != '困难']
        course = bound_course(titles)
        courses = [course] if isinstance(event, dict) and event.get('course') and course else [t['text'] for t in titles]
        info = find('传奇优俊少女信息', (0, 470, 260, 550))
        opponents = [t['text'] for t in good if info and info['bbox'][3] + 25 <= t['bbox'][1] < 640 and (t['bbox'][0] < 360)]
        if info and len(courses) == len(opponents) == 1 and courses[0]:
            bind_event(course=courses[0], opponent=opponents[0])
    if result['page'] == 'legend':
        counts = [t for t in good if re.fullmatch('[0-3]/[1-3]' if daily else '[0-3]/3', t['text']) and 610 <= t['bbox'][0] < 690 and (t['bbox'][1] < 60)]
        if daily:
            counts = [t for t in counts if int(t['text'][0]) <= int(t['text'][-1])]
        result['tickets'] = int(counts[0]['text'][0]) if len(counts) == 1 else None
        home = find('主页', (290, 1200, 430, 1280))
        if home:
            x0, y0, x1, y1 = home['bbox']
            result['actions']['return_home'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
        return result
    trophy_close = find('关闭', (270, 870, 450, 970))
    rewards_continue = find('继续', (270, 1130, 450, 1230))
    if find('获得奖励', (180, 180, 540, 310)) and rewards_continue:
        x0, y0, x1, y1 = rewards_continue['bbox']
        result.update(page='rewards')
        result['actions']['continue_rewards'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
        return result
    trophy_opponents = [t['text'][2:] for t in good if t['text'].startswith('VS') and 370 <= t['bbox'][0] < t['bbox'][2] <= 540 and (590 <= t['bbox'][1] < t['bbox'][3] <= 680) and (len(t['text']) > 2)]
    merged_trophy = [t['text'][6:] for t in good if t['text'].startswith('传奇赛事VS') and 250 <= t['bbox'][0] < t['bbox'][2] <= 550 and (590 <= t['bbox'][1] < t['bbox'][3] <= 680) and (len(t['text']) > 6)]
    trophy_opponents += merged_trophy
    if find('获得奖杯！', (180, 250, 580, 380)) and trophy_close:
        x0, y0, x1, y1 = trophy_close['bbox']
        result.update(page='trophy', victory=True)
        if len(trophy_opponents) == 1:
            bind_event(opponent=trophy_opponents[0])
        result['actions']['close_trophy'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
        return result
    victory_continue = find('继续', (420, 1130, 590, 1230))
    winners = [t for t in good if 10 <= t['bbox'][0] < t['bbox'][2] <= 220 and 1070 <= t['bbox'][1] < t['bbox'][3] <= 1120]
    opponents = [t['text'] for t in good if 350 <= t['bbox'][0] < t['bbox'][2] <= 530 and 165 <= t['bbox'][1] < t['bbox'][3] <= 240]
    draw = find('平局', (0, 880, 260, 1050))
    if (find('失败', (0, 880, 260, 1050)) or draw) and find('排名表', (100, 1130, 280, 1230)) and victory_continue:
        x0, y0, x1, y1 = victory_continue['bbox']
        result.update(page='defeat', victory=False, result_outcome='draw' if draw else 'defeat')
        if len(winners) == 1:
            result['race_horse'] = winners[0]['text']
        if len(opponents) == 1:
            bind_event(opponent=opponents[0])
        result['actions']['continue_defeat'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
        return result
    victory_label = find('胜利', (0, 880, 260, 1050))
    owned_result_layout = find('排名表', (100, 1130, 280, 1230)) and len(winners) == 1 and (len(opponents) == 1)
    if find('恭喜获胜！', (100, 0, 280, 90)) and victory_continue and (victory_label or owned_result_layout):
        x0, y0, x1, y1 = victory_continue['bbox']
        result.update(page='victory', victory=True)
        if len(winners) == 1:
            result['winner'] = winners[0]['text']
        if len(opponents) == 1:
            bind_event(opponent=opponents[0])
        result['actions']['continue_victory'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
        return result
    race_names = [t for t in good if 300 <= t['bbox'][0] < t['bbox'][2] <= 480 and 820 <= t['bbox'][1] < t['bbox'][3] <= 875]
    race_buttons = [t for t in good if t['text'] == '前往赛事' and 380 <= t['bbox'][0] < 540 and (1140 <= t['bbox'][1] < 1210)]
    course = header_course()
    if course and len(race_names) == len(race_buttons) == 1:
        from core.public_race_geometry import RaceRoutePlanner
        preview = RaceRoutePlanner._race_preview_action_points(image)
        point = preview.get('go_to_race') if isinstance(preview, dict) else None
        valid_point = isinstance(point, (list, tuple)) and len(point) == 2 and all((type(v) is int for v in point)) and (0 <= point[0] < 720) and (0 <= point[1] < 1280)
        if isinstance(preview, dict) and preview.get('view_result_locked') is True and valid_point:
            result.update(page='race_ready', race_horse=race_names[0]['text'], preview=preview)
            bind_event(course=course)
            result['actions']['start_race'] = dict(point=list(preview['go_to_race']))
            return result
        view_point = preview.get('view_result') if isinstance(preview, dict) else None
        valid_view = isinstance(view_point, (list, tuple)) and len(view_point) == 2 and all((type(v) is int for v in view_point)) and (0 <= view_point[0] < 720) and (0 <= view_point[1] < 1280)
        if isinstance(preview, dict) and preview.get('view_result_locked') is False and valid_view:
            result.update(page='race_ready', race_horse=race_names[0]['text'], preview=preview)
            bind_event(course=course)
            result['actions']['view_result'] = dict(point=list(view_point))
            return result
    item_titles = [t for t in good if t['text'] == '选择道具' and 280 <= t['bbox'][1] < 370]
    item_prompts = [t for t in good if t['text'] == '请选择道具' and 400 <= t['bbox'][1] < 480]
    if len(item_titles) == len(item_prompts) == 1:
        slots = [image[720:800, x:x + 70] for x in (260, 395)]
        empty = all(((cv2.cvtColor(s, cv2.COLOR_BGR2HSV)[:, :, 1] < 35).mean() > 0.98 and float(s.mean()) > 180 for s in slots))
        counts = []
        denominators = []
        for t in sorted(good, key=lambda t: t['bbox'][0]):
            if 330 <= t['bbox'][0] < t['bbox'][2] <= 510 and 820 <= t['bbox'][1] < t['bbox'][3] <= 880:
                match = re.fullmatch('([0-3])/[1-3](?:([0-3])/[1-3])?' if daily else '([0-3])/3(?:([0-3])/3)?', t['text'])
                if match:
                    counts.extend((int(v) for v in match.groups() if v is not None))
                    if daily:
                        denominators.extend((int(v) for v in re.findall('/([1-3])', t['text'])))
        if daily and (len(counts) != len(denominators) or len(set(denominators)) != 1 or any((n > d for n, d in zip(counts, denominators)))):
            counts = []
        result.update(page='items', items_empty=empty, tickets=counts[0] if len(counts) == 2 else None, tickets_after=counts[1] if len(counts) == 2 else None)
        entry = [t for t in good if t['text'] == '参赛！' and 390 <= t['bbox'][0] < t['bbox'][2] <= 620 and (860 <= t['bbox'][1] < t['bbox'][3] <= 920)]
        if empty and len(counts) == 2 and (counts[0] > 0) and (counts[1] == counts[0] - 1) and any((t['text'] == '消耗1' and 900 <= t['bbox'][1] < 960 for t in good)) and (len(entry) == 1):
            x0, y0, x1, y1 = entry[0]['bbox']
            result['actions']['enter_race'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
        return result
    titles = [t for t in good if course_from(t['text']) and 60 <= t['bbox'][1] < 110]
    bound_header = bound_course([t for t in good if 60 <= t['bbox'][1] < 110])
    if isinstance(event, dict) and event.get('course'):
        titles = [t for t in good if 60 <= t['bbox'][1] < 110 and bound_header and (bound_header in t['text'])]
    grid_buttons = [t for t in good if t['text'] == '参赛！' and 270 <= t['bbox'][0] < t['bbox'][2] <= 450 and (1120 <= t['bbox'][1] < t['bbox'][3] <= 1220)]
    if len(titles) == len(grid_buttons) == 1:
        own = []
        for t in good:
            x0, y0, x1, y1 = t['bbox']
            if 315 <= x0 < x1 <= 430 and 240 <= y0 < y1 <= 1080:
                hsv = cv2.cvtColor(image[y0:y1, x1 + 5:x1 + 35], cv2.COLOR_BGR2HSV)
                if ((hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 35) & (hsv[:, :, 1] > 35)).mean() > 0.8:
                    own.append(t['text'])
        if len(own) == 1:
            x0, y0, x1, y1 = grid_buttons[0]['bbox']
            result.update(page='race_grid', race_horse=own[0])
            bind_event(course=bound_header or course_from(titles[0]['text']))
            result['actions']['run_race'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
            return result
    continues = [t for t in good if t['text'] == '继续' and 1100 <= t['bbox'][1] < 1210 and (270 <= t['bbox'][0] < t['bbox'][2] <= 450)]
    if len(titles) == len(continues) == 1:
        own = []
        for t in good:
            x0, y0, x1, y1 = t['bbox']
            if 200 <= x0 < x1 <= 370 and 240 <= y0 < y1 <= 1080:
                hsv = cv2.cvtColor(image[y0:y1, x1 + 5:x1 + 35], cv2.COLOR_BGR2HSV)
                if ((hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 35) & (hsv[:, :, 1] > 35)).mean() > 0.8:
                    own.append(t['text'])
        result.update(page='lineup', lineup_names=own, course=titles[0]['text'])
        bind_event(course=bound_header or course_from(titles[0]['text']))
        x0, y0, x1, y1 = continues[0]['bbox']
        result['actions']['continue_lineup'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    if result['horse']:
        confirm = [t for t in tokens if t['confidence'] >= 0.95 and t['text'].replace(' ', '') == '确定' and (270 <= t['bbox'][0] < t['bbox'][2] <= 440) and (1030 <= t['bbox'][1] < t['bbox'][3] <= 1120)]
        if len(confirm) == 1:
            x0, y0, x1, y1 = confirm[0]['bbox']
            result['actions']['confirm_selected'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    return result
