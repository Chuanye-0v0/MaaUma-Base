"""Preparation-only scenario and race-plan policy. Never starts training."""
import re
from pathlib import Path
SCENARIOS = {'ura': 'URA', 'aoharu': '青春杯', 'climax': '巅峰杯', 'live': '偶像杯', 'masters': '女神杯', 'arc': '凯旋门'}
SCENARIO_WORDS = {'ura': ('URA',), 'aoharu': ('青春杯',), 'climax': ('巅峰', 'Make'), 'live': ('闪耀舞台',), 'masters': ('女神杯',), 'arc': ('凯旋门',)}
DEFAULTS = dict(scenario='arc', plan_source='default', plan_slot=1, local_plan='mile_middle_test', save_slot=0, overwrite_plan=False)
TEST_RACES = [(0, 12, 0, ('朝日杯',)), (0, 12, 1, ('希望锦标赛', '希望')), (1, 4, 0, ('皋月奖',)), (1, 5, 0, ('广播协会', '英里杯')), (1, 6, 0, ('东京英里赛',)), (1, 10, 1, ('天王奖秋', '天皇奖')), (1, 11, 0, ('伊丽莎白女王杯', '伊丽莎白')), (1, 11, 1, ('英里冠军赛', '冠军赛')), (2, 3, 1, ('大阪杯',)), (2, 5, 0, ('维多利亚',)), (2, 6, 0, ('东京英里赛',)), (2, 6, 1, ('宝冢纪念', '宝家纪念'))]
PAGES = {'plan_calendar', 'plan_races', 'plan_slots', 'plan_overwrite', 'plan_consecutive', 'plan_loaded'}

def validate(options):
    result = {**DEFAULTS, **{k: v for k, v in options.items() if k in DEFAULTS}}
    if result['scenario'] not in SCENARIOS or result['plan_source'] not in ('default', 'account', 'local') or result['local_plan'] != 'mile_middle_test' or (type(result['plan_slot']) is not int) or (not 1 <= result['plan_slot'] <= 8) or (type(result['save_slot']) is not int) or (not 0 <= result['save_slot'] <= 8) or (type(result['overwrite_plan']) is not bool):
        raise ValueError('mfa_plant_plan_invalid')
    return result

def effective_scenario(context):
    options = context['plant_options']
    if options['plan_source'] == 'local' and options['save_slot'] and (not context.get('plan_prepared')):
        return 'climax'
    return options['scenario']

def next_half(date):
    year, month, half = date
    return (year + (month == 12 and half == 1), 1 if month == 12 and half == 1 else month + half, 1 - half)

def _save_control_enabled(image, token):
    """Keep the disabled-state check without requiring a green button palette."""
    if not token:
        return False
    import numpy as np
    a, b, c, d = token['bbox']
    fill = image[max(0, b - 15):min(image.shape[0], d + 2), max(0, a - 81):max(0, a - 16)]
    return bool(fill.size and float(np.median(np.max(fill, axis=2))) >= 155)

def _year_label_selected(image, token):
    """Selection is light text on a darker tab, not the tab's exact hue.

    All three year labels are simultaneously visible. Their presence alone is
    not evidence of selection; ambiguous/flat text polarity remains unknown.
    """
    if not token:
        return False
    import cv2
    import numpy as np
    a, b, c, d = token['bbox']
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    strips = [gray[b:d, max(0, a - 12):max(0, a - 4)].flatten(), gray[b:d, min(gray.shape[1], c + 4):min(gray.shape[1], c + 12)].flatten()]
    surrounding = np.concatenate(strips)
    text = gray[b:d, a:c].astype(float)
    if not surrounding.size or not text.size:
        return False
    background = float(np.median(surrounding))
    return bool(float(np.mean(text > background + 25)) >= 0.15 and float(np.mean(text < background - 25)) < 0.1)

def reobserve_once(observed, context, capture, action=None):
    """Read missing plan evidence once; never retry a submitted save input.

    Call before choosing an action or after an already recorded receipt and
    before after_action. Explicit dates/count conflicts do not request a read.
    """
    if 'plant_options' not in context:
        return observed
    page = observed.get('page')
    missing = False
    if action and (action.startswith('plan_store_') or action == 'plan_accept_overwrite'):
        if page == 'plan_slots':
            slot = context['plant_options']['save_slot']
            row = next((r for r in observed.get('plan_slots', []) if r['number'] == slot), None)
            missing = row is None or row.get('count') is None
    elif action is None and (not context.get('plan_restart')):
        if page == 'plan_races':
            missing = observed.get('plan_date') is None
        elif page == 'plan_slots' and context.get('plan_phase') != 'slot_done':
            options = context['plant_options']
            storing = context.get('plan_phase') == 'storing_slot'
            slot = options['save_slot'] if storing or context.get('plan_cached') else options['plan_slot']
            row = next((r for r in observed.get('plan_slots', []) if r['number'] == slot), None)
            if row is not None and (not row.get('bottom_clipped')):
                control = ('plan_store_' if storing else 'plan_enable_') + str(slot)
                if control in observed.get('actions', {}):
                    missing = observed.get('plan_current_count') is None if storing else row.get('count') is None
    return capture() if missing else observed

def enrich(observed, image, tokens, context):
    """Derive controls only from a recognized preparation page and current OCR."""
    if 'plant_options' not in context:
        return
    import cv2
    import numpy as np
    good = [dict(t, text=t['text'].replace(' ', '')) for t in tokens if t.get('confidence', 0) >= 0.9]

    def find(word, box=(0, 0, 720, 1280), exact=False):
        x0, y0, x1, y1 = box
        hits = [t for t in good if (t['text'] == word if exact else word in t['text']) and x0 <= t['bbox'][0] and (y0 <= t['bbox'][1]) and (t['bbox'][2] <= x1) and (t['bbox'][3] <= y1)]
        return hits[0] if len(hits) == 1 else None

    def add(name, token=None, point=None, **fields):
        if token:
            a, b, c, d = token['bbox']
            point = [(a + c) // 2, (b + d) // 2]
        if point:
            observed['actions'][name] = dict(point=point, **fields)
    if observed['page'] == 'scenario':
        ids = [key for key, words in SCENARIO_WORDS.items() if any((w in t['text'] for w in words for t in good))]
        observed['scenario_id'] = ids[0] if len(ids) == 1 else None
        add('continue_scenario', find('继续', (150, 960, 590, 1160)))
        add('scenario_next', point=[691, 584])
    if find('比赛预约成功', (150, 300, 600, 500), exact=True) and find('已载入我的参赛计划', (100, 500, 650, 750), exact=True):
        observed.update(page='plan_loaded', actions={})
        add('plan_dismiss_loaded', find('关闭', (150, 750, 600, 930), exact=True))
    elif find('覆盖并保存', (0, 0, 720, 400)):
        observed.update(page='plan_overwrite', actions={})
        slot = context['plant_options']['save_slot']
        observed['plan_overwrite_slot'] = slot if find('我的参赛计划' + str(slot), (0, 580, 720, 750), exact=True) else None
        add('plan_accept_overwrite', find('保存', (360, 800, 700, 1100), exact=True))
        add('plan_cancel_overwrite', find('取消', (0, 800, 360, 1100)))
    elif find('连续参赛', (0, 300, 720, 500), exact=True):
        observed.update(page='plan_consecutive', actions={})
        close = next((t for t in tokens if t['text'].replace(' ', '') == '关闭' and t.get('confidence', 0) >= 0.85 and (700 < t['bbox'][1] < 950)), None)
        add('plan_dismiss_consecutive', close)
    elif find('我的参赛计划', (0, 0, 720, 120), exact=True):
        observed.update(page='plan_slots', actions={}, plan_slots=[])
        current = [int(t['text']) for t in good if t['text'].isdigit() and 145 < t['bbox'][0] < 205 and (310 < t['bbox'][1] < 370)]
        observed['plan_current_count'] = current[0] if len(current) == 1 else None
        headers = [(int(m.group(1)), t) for t in good for m in [re.fullmatch('我的参赛计划([1-8])', t['text'])] if m and 400 < t['bbox'][1] < 1090]
        for number, header in headers:
            y = header['bbox'][1]
            bottom = min([t['bbox'][1] for n, t in headers if t['bbox'][1] > y] + [1110])
            enable = find('启用', (470, y, 705, bottom))
            save = find('保存至这里', (470, y, 705, bottom))
            count = None
            for t in good:
                if 30 <= t['bbox'][0] < 225 and y + 100 < t['bbox'][1] < bottom:
                    m = re.fullmatch('(?:预约数)?(\\d+)', t['text'])
                    if m:
                        count = int(m.group(1))
            if count is None:
                blank = image[y + 45:min(y + 105, image.shape[0]), 75:170]
                label = find('预约数', (25, y + 100, 140, bottom), exact=True)
                if blank.size and float(np.std(blank)) < 3 and label:
                    zero = cv2.imread(str(Path(__file__).parent / 'assets/plan_zero.png'))
                    cy = label['bbox'][1]
                    crop = image[cy - 5:cy + 36, 150:202]
                    if zero is not None and cv2.minMaxLoc(cv2.matchTemplate(crop, zero, cv2.TM_CCOEFF_NORMED))[1] >= 0.96:
                        count = 0
            observed['plan_slots'].append(dict(number=number, count=count, bottom_clipped=y + 180 > 1110))
            add('plan_enable_' + str(number), enable, slot=number, count=count)
            add('plan_store_' + str(number), save, slot=number)
        add('plan_slots_down', point=[360, 900], gesture='swipe', end=[360, 700])
        add('plan_slots_up', point=[360, 700], gesture='swipe', end=[360, 900])
        add('plan_close_slots', find('关闭', (100, 1120, 620, 1260)))
    elif find('参赛设定', (0, 0, 720, 120), exact=True):
        observed.update(page='plan_races', actions={}, plan_race_selected=False)
        observed['plan_list_signature'] = cv2.resize(cv2.cvtColor(image[235:1055, 25:690], cv2.COLOR_BGR2GRAY), (24, 24)).flatten().tolist()
        titles = [t['text'] for t in good if 100 < t['bbox'][1] < 220]
        date = next((re.search('(初级|经典级|高级)(\\d+)月(上|下)半月', t) for t in titles if re.search('(初级|经典级|高级)(\\d+)月(上|下)半月', t)), None)
        observed['plan_date'] = (['初级', '经典级', '高级'].index(date[1]), int(date[2]), 0 if date[3] == '上' else 1) if date else None
        observed['plan_date_mandatory'] = bool(find('目标', (20, 225, 280, 400), exact=True)) and any(('无法参加目标赛事以外' in t['text'] for t in good))
        current = find('设定中', (20, 230, 280, 1070), exact=True)
        observed['plan_has_selection'] = bool(current)
        if current and (not observed['plan_date_mandatory']):
            add('plan_deselect_race', current)
        index = context.get('plan_race_index', 0)
        if index < len(TEST_RACES):
            race = TEST_RACES[index]
            if observed['plan_date'] == race[:3]:
                from core.autonomous_training.plant_races import match_g1
                target = match_g1(race[:3], race[3], tokens, image)
                if target:
                    y = target['bbox'][1]
                    selected = current and 0 < current['bbox'][1] - target['bbox'][3] < 90
                    locked = find('目标', (20, max(220, y - 100), 280, min(1070, y + 80)), exact=True)
                    observed['plan_race_selected'] = bool(selected)
                    observed['plan_race_locked'] = bool(locked)
                    add('plan_select_race', target, race_index=index)
        save = find('保存', (360, 1110, 710, 1260), exact=True)
        observed['plan_save_enabled'] = _save_control_enabled(image, save)
        if observed['plan_save_enabled']:
            add('plan_save_races', save)
        if observed['plan_date']:
            add('plan_next_half', point=[605, 164], next_date=next_half(observed['plan_date']))
        add('plan_cancel_races', find('取消', (0, 1110, 360, 1260)))
        add('plan_races_down', point=[550, 950], gesture='swipe', end=[550, 370])
    elif find('参赛计划', (0, 0, 720, 120), exact=True):
        observed.update(page='plan_calendar', actions={})
        years = []
        for year, word in enumerate(('初级', '经典级', '高级')):
            label = find(word, (0, 90, 720, 170), exact=True)
            if _year_label_selected(image, label):
                years.append(year)
            add('plan_year_' + str(year), label)
        observed['plan_year'] = years[0] if len(years) == 1 else None
        observed['plan_year_source'] = 'label_polarity' if len(years) == 1 else None
        index = context.get('plan_race_index', 0)
        if index < len(TEST_RACES):
            year, month, half, _ = TEST_RACES[index]
            if observed['plan_year'] == year:
                cell = (month - 1) * 2 + half
                add('plan_open_date', point=[110 + cell % 4 * 168, 245 + cell // 4 * 144], race_index=index)
        add('plan_open_slots', find('我的参赛计划', (350, 1050, 710, 1140)))
        add('plan_close_calendar', find('关闭', (100, 1120, 620, 1260)))
    if observed['page'] == 'final_autonomous':
        ids = [key for key, words in SCENARIO_WORDS.items() if any((w in t['text'] for w in words for t in good if 90 < t['bbox'][1] < 190))]
        observed['scenario_id'] = ids[0] if len(ids) == 1 else None
        add('plan_edit', find('编辑', (450, 250, 710, 970)))
        add('plan_cancel_final', find('取消', (0, 1100, 360, 1260)))
    if observed['page'] in ('support_deck', 'inheritance', 'trainee', 'exam_mode'):
        add('plan_back_' + observed['page'], find('返回', (0, 950, 180, 1170)))

def choose(observed, context):
    if 'plant_options' not in context or context.get('start_submitted'):
        return None
    options = context['plant_options']
    page = observed['page']
    if page in PAGES and (not context.get('plan_phase')) and (not context.get('verified_scenario')):
        context['plan_restart'] = True
    if context.get('plan_restart'):
        if page == 'final_autonomous':
            context.update(plan_restart=False, plan_switching=True)
            return 'plan_cancel_final'
        return {'plan_races': 'plan_cancel_races', 'plan_calendar': 'plan_close_calendar', 'plan_slots': 'plan_close_slots', 'plan_overwrite': 'plan_cancel_overwrite', 'plan_consecutive': 'plan_dismiss_consecutive', 'plan_loaded': 'plan_dismiss_loaded'}.get(page)
    if context.get('plan_switching'):
        if page == 'scenario':
            prepared = context.get('plan_prepared', False) or context.pop('plan_handoff', False)
            context.update(plan_switching=False, plan_prepared=prepared, plan_done=False, plan_race_index=0, plan_dirty=False, plan_phase='')
        else:
            return 'plan_cancel_final' if page == 'final_autonomous' else 'plan_back_' + page
    if page == 'scenario':
        return 'continue_scenario' if observed.get('scenario_id') == effective_scenario(context) else 'scenario_next'
    if page == 'final_autonomous' and observed.get('scenario_id') != effective_scenario(context):
        if observed.get('scenario_id') is None:
            raise RuntimeError('final_scenario_not_verified')
        context['plan_switching'] = True
        return 'plan_cancel_final'
    source = options['plan_source']
    if source == 'default' or context.get('plan_done'):
        return None
    phase = context.get('plan_phase', '')
    if page == 'final_autonomous':
        if phase == 'closing':
            if source == 'local' and options['save_slot'] and (not context.get('plan_prepared')):
                context['plan_switching'] = True
                context['plan_handoff'] = True
                return 'plan_cancel_final'
            context['plan_done'] = True
            return None
        context['plan_phase'] = 'editing'
        return 'plan_edit'
    if page == 'plan_consecutive':
        return 'plan_dismiss_consecutive'
    if page == 'plan_loaded':
        return 'plan_dismiss_loaded'
    if page == 'plan_overwrite':
        if observed.get('plan_overwrite_slot') != options['save_slot']:
            raise RuntimeError('plan_overwrite_slot_not_verified')
        return 'plan_accept_overwrite' if options['overwrite_plan'] else 'plan_cancel_overwrite'
    if page == 'plan_calendar':
        if phase in ('closing', 'slot_done'):
            context['plan_phase'] = 'closing'
            return 'plan_close_calendar'
        if source == 'account' or context.get('plan_cached'):
            context['plan_phase'] = 'loading_slot'
            return 'plan_open_slots'
        index = context.get('plan_race_index', 0)
        if index >= len(TEST_RACES):
            if options['save_slot'] and (not context.get('plan_prepared')):
                context['plan_phase'] = 'storing_slot'
                return 'plan_open_slots'
            context['plan_phase'] = 'closing'
            return 'plan_close_calendar'
        year = TEST_RACES[index][0]
        return 'plan_year_' + str(year) if observed.get('plan_year') != year else 'plan_open_date'
    if page == 'plan_races':
        index = context.get('plan_race_index', 0)
        date = observed.get('plan_date')
        if date is None:
            raise RuntimeError('plan_date_not_verified')
        if index < len(TEST_RACES) and date == TEST_RACES[index][:3]:
            if observed.get('plan_date_mandatory') or observed.get('plan_race_locked'):
                context.setdefault('plan_mandatory_dates', []).append(list(date))
            elif not observed.get('plan_race_selected'):
                if 'plan_select_race' in observed['actions']:
                    context['plan_scroll_stalls'] = 0
                    return 'plan_deselect_race' if observed.get('plan_has_selection') else 'plan_select_race'
                context['plan_before_scroll'] = observed.get('plan_list_signature')
                return 'plan_races_down'
            index += 1
            context['plan_race_index'] = index
        if index < len(TEST_RACES):
            if not (index > 0 and TEST_RACES[index - 1][:3] <= date < TEST_RACES[index][:3]):
                raise RuntimeError('plan_date_not_verified')
            context['plan_expected_date'] = next_half(date)
            return 'plan_next_half'
        if observed.get('plan_save_enabled'):
            return 'plan_save_races'
        if context.get('plan_dirty'):
            raise RuntimeError('plan_changes_not_saveable')
        return 'plan_cancel_races'
    if page == 'plan_slots':
        if phase == 'slot_done':
            return 'plan_close_slots'
        slot = options['save_slot'] if phase == 'storing_slot' or context.get('plan_cached') else options['plan_slot']
        row = next((r for r in observed['plan_slots'] if r['number'] == slot), None)
        if row is None:
            numbers = [r['number'] for r in observed['plan_slots']]
            return 'plan_slots_up' if numbers and slot < min(numbers) else 'plan_slots_down'
        if row.get('bottom_clipped'):
            return 'plan_slots_down'
        control = 'plan_store_' if phase == 'storing_slot' else 'plan_enable_'
        if control + str(slot) not in observed['actions']:
            return 'plan_slots_down'
        if phase == 'storing_slot':
            if type(observed.get('plan_current_count')) is not int:
                raise RuntimeError('plan_save_count_unreadable')
            if observed['plan_current_count'] <= 0:
                raise RuntimeError('plan_save_count_empty')
            context['plan_expected_count'] = observed['plan_current_count']
            return 'plan_store_' + str(slot)
        if row['count'] == 0:
            context.update(plan_phase='slot_done', plan_empty=True)
            return 'plan_close_slots'
        if row['count'] is None:
            raise RuntimeError('plan_slot_count_unreadable')
        return 'plan_enable_' + str(slot)
    return None

def after_action(action, observed, context):
    if context.get('plan_restart'):
        return
    if action == 'plan_next_half':
        if observed.get('plan_date') != context.get('plan_expected_date'):
            raise RuntimeError('plan_date_transition_not_verified')
        context['plan_scroll_stalls'] = 0
    if action in ('plan_select_race', 'plan_deselect_race'):
        selected = observed.get('plan_race_selected') if action == 'plan_select_race' else not observed.get('plan_has_selection', True)
        if observed['page'] != 'plan_races' or not selected:
            raise RuntimeError('plan_selection_not_verified')
        context['plan_dirty'] = True
    if action == 'plan_races_down':
        before, after = (context.get('plan_before_scroll'), observed.get('plan_list_signature'))
        unchanged = before and after and (len(before) == len(after)) and (sum((abs(a - b) for a, b in zip(before, after))) / len(before) < 2)
        context['plan_scroll_stalls'] = context.get('plan_scroll_stalls', 0) + 1 if unchanged else 0
        if context['plan_scroll_stalls'] >= 2:
            raise RuntimeError('plan_race_not_found_at_list_end')
    if action in ('plan_save_races', 'plan_cancel_races') and observed['page'] in ('plan_calendar', 'plan_consecutive'):
        context['plan_dirty'] = False
    if action.startswith('plan_enable_') and observed['page'] in ('plan_calendar', 'plan_slots', 'plan_consecutive', 'plan_loaded'):
        context['plan_phase'] = 'slot_done'
    if (action.startswith('plan_store_') or action == 'plan_accept_overwrite') and observed['page'] == 'plan_slots':
        slot = context['plant_options']['save_slot']
        row = next((r for r in observed['plan_slots'] if r['number'] == slot), None)
        if row is None:
            raise RuntimeError('plan_saved_slot_unreadable')
        if row.get('count') is None:
            raise RuntimeError('plan_saved_slot_count_unreadable')
        if row['count'] != context.get('plan_expected_count'):
            raise RuntimeError('plan_saved_slot_count_mismatch')
        context.update(plan_phase='slot_done', plan_cached=True)
    if action == 'plan_cancel_overwrite' and observed['page'] == 'plan_slots':
        context.update(plan_phase='slot_done', plan_cached=False)
ACTION_PAGES = {'plan_edit': 'final_autonomous', 'plan_cancel_final': 'final_autonomous', 'plan_open_slots': 'plan_calendar', 'plan_close_calendar': 'plan_calendar', 'plan_open_date': 'plan_calendar', 'plan_select_race': 'plan_races', 'plan_deselect_race': 'plan_races', 'plan_next_half': 'plan_races', 'plan_save_races': 'plan_races', 'plan_cancel_races': 'plan_races', 'plan_races_down': 'plan_races', 'plan_close_slots': 'plan_slots', 'plan_slots_up': 'plan_slots', 'plan_slots_down': 'plan_slots', 'plan_accept_overwrite': 'plan_overwrite', 'plan_cancel_overwrite': 'plan_overwrite', 'plan_dismiss_consecutive': 'plan_consecutive', 'plan_dismiss_loaded': 'plan_loaded'}
ACTION_PAGES.update({'plan_year_' + str(i): 'plan_calendar' for i in range(3)})
ACTION_PAGES.update({'plan_' + verb + '_' + str(i): 'plan_slots' for verb in ('enable', 'store') for i in range(1, 9)})
ACTION_PAGES.update({'plan_back_' + p: p for p in ('support_deck', 'inheritance', 'trainee', 'exam_mode')})
NEXT_PAGES = {a: {p} for a, p in ACTION_PAGES.items()}
NEXT_PAGES.update(plan_edit={'plan_calendar'}, plan_open_slots={'plan_slots'}, plan_open_date={'plan_races'}, plan_close_calendar={'final_autonomous'}, plan_save_races={'plan_calendar', 'plan_consecutive'}, plan_cancel_races={'plan_calendar'}, plan_close_slots={'plan_calendar'}, plan_dismiss_consecutive={'plan_calendar', 'plan_slots'}, plan_dismiss_loaded={'plan_slots', 'plan_calendar', 'plan_consecutive'}, plan_cancel_overwrite={'plan_slots'}, plan_accept_overwrite={'plan_slots'}, plan_cancel_final={'support_deck'}, plan_back_support_deck={'inheritance'}, plan_back_inheritance={'trainee'}, plan_back_trainee={'scenario', 'exam_mode'}, plan_back_exam_mode={'scenario'})
for i in range(1, 9):
    NEXT_PAGES['plan_store_' + str(i)] = {'plan_slots', 'plan_overwrite'}
    NEXT_PAGES['plan_enable_' + str(i)] = {'plan_slots', 'plan_calendar', 'plan_consecutive', 'plan_loaded'}
