"""Bounded event cleanup; no premium currency, refills, or training actions."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import time
from copy import deepcopy
import cv2
import numpy as np
from core.support_library.live import SupportDevice
from core.observation_timing import observation_is_stale
from core.action_recovery import action_boundary, await_candidate, failure_details, ReobserveRequired, ReobserveBudget, receipt_replan
from core.story_event.observer import observe
from core.story_event.banner import observe_banner, WHITELIST, STORY_EVENT_IDS, EXCLUDED_BANNERS
PAGES = {'next_banner': 'home', 'dismiss_tutorial': 'tutorial_help', 'event_banner': 'home', 'missions': 'event', 'stories': 'event', 'roulette': 'event', 'home': 'event', 'claim': 'missions', 'tab_0': 'missions', 'tab_1': 'missions', 'tab_2': 'missions', 'play_story': 'story_list', 'portrait': 'story_settings', 'silent': 'story_settings', 'confirm_story': 'story_settings', 'cancel_story': 'story_settings', 'play_next': 'story_next', 'cancel': 'story_next', 'menu': 'story_playing', 'skip': 'story_menu', 'skip_confirm': 'story_skip_confirm', 'options': 'roulette', 'continuous': 'roulette_settings', 'eight_lines': 'roulette_settings', 'save': 'roulette_settings', 'spin': 'roulette', 'skip_spin': 'roulette_animation', 'close_bingo': 'bingo', 'advance_bingo': 'bingo', 'cancel_settings': 'roulette_settings', 'leave_other': 'other_event', 'tachyon_continue': 'tachyon_rewards', 'tachyon_back': 'tachyon_event'}
CLOSE_PAGES = {'claim', 'story_unlock', 'settings_saved', 'roulette_limit', 'roulette_replace', 'roulette_empty', 'story_list'}
LABELS = {'event_banner': '检查活动入口', 'missions': '打开活动任务', 'stories': '打开活动剧情', 'next_banner': '滑动切换活动入口', 'dismiss_tutorial': '继续或关闭玩法说明', 'roulette': '打开轮盘', 'home': '返回主页', 'claim': '一键领取任务奖励', 'tab_0': '检查日常奖励', 'tab_1': '检查称号奖励', 'tab_2': '检查期间限定奖励', 'play_story': '播放未读剧情', 'portrait': '选择竖屏', 'silent': '关闭语音下载', 'confirm_story': '确认播放', 'play_next': '继续下一段未读剧情', 'cancel': '结束连续播放', 'cancel_story': '取消未绑定的播放确认，返回入口', 'menu': '打开剧情菜单', 'skip': '跳过剧情', 'skip_confirm': '确认跳过', 'options': '设置连续转盘', 'continuous': '开启连续转动', 'eight_lines': '选择8行宾果', 'save': '保存转盘设置', 'cancel_settings': '返回转盘', 'spin': '使用已有轮盘币连续转动', 'skip_spin': '跳过转盘动画', 'close_bingo': '关闭宾果结果', 'advance_bingo': '继续宾果领奖', 'back': '返回活动主页', 'close': '关闭提示', 'leave_other': '跳过非目标活动', 'tachyon_continue': '速子活动：关闭研究成果奖励', 'tachyon_back': '速子活动：领奖后返回主页'}
INTRO_ACTIONS = {'portrait', 'silent', 'confirm_story', 'play_next', 'cancel', 'menu', 'skip', 'skip_confirm'}
SETTING_FIELDS = {'portrait': 'portrait', 'silent': 'silent', 'continuous': 'continuous', 'eight_lines': 'eight_lines'}
SETTING_PAGES = {'story_settings': ('portrait', 'silent'), 'roulette_settings': ('continuous', 'eight_lines')}

def entry_story_bound(context):
    return context.get('entry_pending') and context.get('entry_banner_id') in STORY_EVENT_IDS

def authorize(action, current, expected, context):
    if current.get('endpoint') != _public_endpoint() or current.get('endpoint') != expected.get('endpoint') or (not expected.get('frame_sha256')) or (current.get('page') != expected.get('page')):
        raise RuntimeError('event_source_not_bound')
    page = current['page']
    allowed = page in CLOSE_PAGES if action == 'close' else page in {'missions', 'roulette'} if action == 'back' else PAGES.get(action) == page
    candidate = current.get('actions', {}).get(action)
    if not allowed or not candidate:
        raise RuntimeError('event_action_not_allowed:' + action)
    if context.get('activity_kind') == 'tachyon' and action not in {'event_banner', 'next_banner', 'tachyon_continue', 'tachyon_back', 'dismiss_tutorial', 'leave_other'}:
        raise RuntimeError('tachyon_unrequested_action:' + action)
    if action == 'event_banner':
        a, b = (current.get('banner_id'), expected.get('banner_id'))
        if a not in WHITELIST or a != b or (context.get('target_event_id') and a != context['target_event_id']):
            raise RuntimeError('event_banner_changed')
    elif action == 'next_banner':
        if current.get('banner_excluded') not in EXCLUDED_BANNERS or current.get('banner_excluded') != expected.get('banner_excluded'):
            raise RuntimeError('event_banner_changed')
    elif action in {'tachyon_continue', 'tachyon_back'}:
        if current.get('activity_kind') != 'tachyon' or expected.get('activity_kind') != 'tachyon' or context.get('activity_kind') != 'tachyon' or (not context.get('event_verified')):
            raise RuntimeError('tachyon_activity_not_verified')
    elif action not in {'leave_other', 'dismiss_tutorial', 'cancel_story'} and page != 'story_unlock':
        intro = entry_story_bound(context) and (action in INTRO_ACTIONS or (action == 'close' and page == 'claim'))
        if not context.get('event_verified') and (not intro):
            raise RuntimeError('event_type_not_verified')
        if context.get('target_event_id') and context.get('activity_id') != context['target_event_id']:
            raise RuntimeError('event_identity_not_bound')
    if action == 'spin':
        if not current.get('eight_mode') or context.get('pending_spin'):
            raise RuntimeError('event_spin_not_bound')
    if action == 'back' and page == 'roulette':
        if not all((state.get('spin_disabled') and 'spin' not in state.get('actions', {}) for state in (expected, current))):
            raise RuntimeError('event_roulette_exit_not_verified')
    if action == 'claim' and current.get('tab') != expected.get('tab'):
        raise RuntimeError('event_tab_changed')
    if action == 'close' and page == 'story_list' and ('play_story' in current['actions']):
        raise RuntimeError('event_unread_story_available')
    if action == 'confirm_story' and (not (current.get('portrait') and current.get('silent'))):
        raise RuntimeError('event_story_settings_not_ready')
    if action == 'save' and (not (current.get('continuous') and current.get('eight_lines'))):
        raise RuntimeError('event_roulette_settings_not_ready')
    if action in SETTING_FIELDS and current.get(SETTING_FIELDS[action]) is not False:
        raise RuntimeError('event_setting_state_not_unselected')
    return candidate

class EventDevice(SupportDevice):

    def __init__(self, endpoint, adb_path, output_dir):
        if endpoint != _public_endpoint():
            raise ValueError('event_test_device_only')
        super().__init__(endpoint, adb_path, output_dir)
        self.context = {}

    def save(self):
        (self.output_dir / 'context.json').write_text(json.dumps(self.context, ensure_ascii=False, indent=2), encoding='utf8')

    def track_banner_arrival(self, state, started, captured):
        previous = getattr(self, '_banner_sample', None)
        fresh = 0 <= captured - started <= 0.75
        continuous = state['page'] == 'home' and fresh and previous and (previous['page'] == 'home') and previous['fresh'] and (0 <= started - previous['started'] < 1)
        target = state.get('banner_id') in WHITELIST
        scores = state.get('banner_scores', {})
        clearly_other = state.get('banner_excluded') in EXCLUDED_BANNERS or (bool(scores) and max(scores.values()) < 0.5)
        anchor = None
        if continuous:
            anchor = previous if previous['other_banner'] or previous.get('banner_id') in WHITELIST else getattr(self, '_banner_transition_anchor', None)
        changed_identity = anchor and (anchor['other_banner'] or anchor.get('banner_id') != state.get('banner_id'))
        if target and changed_identity and (0 <= started - anchor['started'] < 1.5):
            self._banner_switched_at = anchor['started']
        transition = not target and bool(scores) and (0.5 <= max(scores.values()) < 0.9)
        self._banner_transition_anchor = previous if continuous and transition and (previous['other_banner'] or previous.get('banner_id') in WHITELIST) else None
        self._banner_sample = dict(page=state['page'], other_banner=clearly_other, banner_id=state.get('banner_id'), started=started, fresh=fresh)
        if state['page'] != 'home':
            self._banner_switched_at = None

    def capture(self):
        self._banner_frame = None
        started = time.monotonic()
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('event_screenshot_invalid')
        fast = observe_banner(raw)
        self.track_banner_arrival(fast, started, captured)
        rows, _ = ([], None) if fast['page'] == 'home' else self.ocr(image)
        tokens = [dict(text=text, confidence=float(score), bbox=[int(min((p[0] for p in box))), int(min((p[1] for p in box))), int(max((p[0] for p in box))), int(max((p[1] for p in box)))]) for box, text, score in rows or []]
        result = fast if fast['page'] == 'home' else observe(raw, tokens)
        path = self.output_dir / (str(time.time_ns()) + '.png')
        path.write_bytes(raw)
        result.update(endpoint=self.endpoint, captured_monotonic=captured, screenshot_path=str(path))
        path.with_suffix('.json').write_text(json.dumps(dict(result, tokens=tokens), ensure_ascii=False), encoding='utf8')
        if 'event_banner' in fast['actions']:
            self._banner_frame = (started, raw, dict(result))
        return result

    @action_boundary
    def step(self, action, expected, stop):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        if action == 'event_banner':
            return self.step_banner(expected, stop)
        current = self.capture()
        if current['page'] != expected['page']:
            return dict(performed=False, observation=current)
        if current['page'] in SETTING_PAGES and action in {*SETTING_FIELDS, 'save', 'confirm_story'}:
            fields = SETTING_PAGES[current['page']]
            if any((current.get(field) is None for field in fields)):
                stop.wait(0.25)
                current = self.capture()
            if current['page'] != expected['page']:
                return dict(performed=False, observation=current)
            if any((current.get(field) is None for field in fields)):
                return dict(performed=False, reason='event_setting_state_unreadable', observation=current)
            if action in SETTING_FIELDS and current.get(SETTING_FIELDS[action]) is True:
                return dict(performed=False, reason='event_control_changed', observation=current)
            if action in {'save', 'confirm_story'} and (not all((current.get(field) is True for field in fields))):
                return dict(performed=False, reason='event_settings_changed', observation=current)
            if action in SETTING_FIELDS and self.context.get('pending_setting') == action:
                return dict(performed=False, reason='event_setting_unverified', observation=current)
        if action == 'next_banner' and 'next_banner' not in current['actions']:
            return dict(performed=False, observation=current)
        if action == 'back' and current['page'] == 'roulette' and (not current.get('spin_disabled') or 'spin' in current['actions']):
            return dict(performed=False, reason='event_roulette_exit_changed', observation=current)
        if action == 'close' and current['page'] == 'story_list' and ('play_story' in current['actions']):
            return dict(performed=False, reason='event_unread_story_available', observation=current)
        if action == 'cancel' and current['page'] == 'story_next' and ('play_next' in current['actions']):
            return dict(performed=False, reason='event_next_story_available', observation=current)
        if action == 'play_story' and (not current['actions'].get('play_story') or current['actions']['play_story'].get('episode') != expected['actions']['play_story'].get('episode')):
            return dict(performed=False, reason='event_story_candidate_changed', observation=current)
        if action == 'spin' and current['page'] == 'roulette' and current.get('spin_disabled') or (action == 'advance_bingo' and current['page'] == 'bingo' and ('close_bingo' in current['actions'])) or (action == 'continuous' and current['page'] == 'roulette_settings' and current.get('continuous')) or (action == 'eight_lines' and current['page'] == 'roulette_settings' and current.get('eight_lines')):
            return dict(performed=False, reason='event_control_changed', observation=current)
        if action not in current.get('actions', {}):
            current = await_candidate(self.capture, stop, action, current, expected.get('page'))
        candidate = authorize(action, current, expected, self.context)
        if observation_is_stale(current, action) or stop.is_set():
            raise RuntimeError('event_stale_or_stopped')
        if action == 'spin':
            self.context.update(pending_spin=True, spin_result_seen=False)
        self.context['stage'] = action
        if action in SETTING_FIELDS:
            self.context['pending_setting'] = action
        self.save()
        receipt = dict(action_id=action, source_frame_sha256=current['frame_sha256'], performed=False)
        path = self.output_dir / (str(time.time_ns()) + '-receipt.json')
        path.write_text(json.dumps(receipt), encoding='utf8')
        if action == 'next_banner':
            self.command('-s', self.endpoint, 'shell', 'input', 'swipe', *map(str, candidate['swipe']))
            self._banner_switched_at = time.monotonic()
        else:
            self.command('-s', self.endpoint, 'shell', 'input', 'tap', *map(str, candidate['point']))
        receipt['performed'] = True
        path.write_text(json.dumps(receipt), encoding='utf8')
        stop.wait(0.25)
        after = self.capture()
        if action in SETTING_FIELDS and after.get(SETTING_FIELDS[action]) is True:
            self.context.pop('pending_setting', None)
            self.save()
        return dict(receipt, observation=after)

    def step_banner(self, expected, stop):
        switched = getattr(self, '_banner_switched_at', None)
        if switched is None or not 0 <= time.monotonic() - switched < 3:
            return dict(performed=False, reason='event_banner_wait_for_switch', observation=self.capture())
        cached = getattr(self, '_banner_frame', None)
        self._banner_frame = None
        frames = []
        for index in range(2):
            if stop.is_set():
                raise RuntimeError('operator_stop')
            if index == 0 and cached and (switched <= cached[0] <= time.monotonic()) and (cached[2]['frame_sha256'] == expected.get('frame_sha256')):
                started, raw, current = cached
            else:
                started = time.monotonic()
                raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
                current = dict(observe_banner(raw), endpoint=self.endpoint)
            frames.append((raw, current))
            if current.get('banner_id') != expected.get('banner_id') or 'event_banner' not in current['actions']:
                return dict(performed=False, reason='event_banner_changed', observation=self.capture())
            candidate = authorize('event_banner', current, expected, self.context)
            if time.monotonic() - started > 0.75:
                return dict(performed=False, reason='event_banner_stale', observation=self.capture())
            if len(frames) == 1 and stop.wait(0.15):
                raise RuntimeError('operator_stop')
        receipt = dict(action_id='event_banner', source_frame_sha256=current['frame_sha256'], banner_id=current['banner_id'], performed=False)
        path = self.output_dir / (str(time.time_ns()) + '-receipt.json')
        path.write_text(json.dumps(receipt), encoding='utf8')
        if stop.is_set():
            raise RuntimeError('operator_stop')
        if time.monotonic() - started > 0.75:
            return dict(receipt, reason='event_banner_stale', observation=self.capture())
        if time.monotonic() - switched >= 3:
            return dict(receipt, reason='event_banner_wait_for_switch', observation=self.capture())
        self._banner_switched_at = None
        self.command('-s', self.endpoint, 'shell', 'input', 'tap', *map(str, candidate['point']))
        receipt['performed'] = True
        path.write_text(json.dumps(receipt), encoding='utf8')
        for index, (raw, state) in enumerate(frames):
            path.with_name(path.stem + '-' + str(index) + '.png').write_bytes(raw)
        self.context['stage'] = 'event_banner'
        self.context['entry_pending'] = True
        self.context['entry_banner_id'] = current['banner_id']
        self.context['activity_id'] = current['banner_id']
        self.context['activity_kind'] = 'tachyon' if current['banner_id'] == 'tachyon' else 'story'
        self.save()
        stop.wait(0.25)
        return dict(receipt, observation=self.capture())

class EventRunner:

    def __init__(self, device, emit, stop):
        self.device, self.emit, self.stop = (device, emit, stop)

    def run(self, initial=None):
        context = self.device.context
        report = dict(ok=False, device_actions=0, records=[])
        replans = ReobserveBudget()
        phase = context.get('event_phase', 'entry')
        visited, claimed = (set(context.get('visited_tabs', [])), set(context.get('claimed_tabs', [])))
        scan_started = time.monotonic()
        last_progress = scan_started
        last_page = None
        home_count = 0
        expected_tab = None
        story_count = context.get('story_count', 0)
        entry_attempts = 0
        recovering = False
        entry_started = scan_started
        setting_reads = {}
        try:
            current = initial if initial is not None else self.device.capture()
            for _ in range(1200):
                if self.stop.is_set():
                    raise RuntimeError('operator_stop')
                page, actions = (current['page'], current['actions'])
                report['last_observation'] = current
                needs_settings = page in SETTING_PAGES and (not (page == 'roulette_settings' and context.get('settings_saved'))) and (not (page == 'story_settings' and (not context.get('event_verified')) and (not entry_story_bound(context))))
                if needs_settings:
                    pending = context.get('pending_setting')
                    pending_field = SETTING_FIELDS.get(pending)
                    if pending_field and current.get(pending_field) is True:
                        context.pop('pending_setting', None)
                        self.device.save()
                        pending_field = None
                    unknown = any((current.get(field) is None for field in SETTING_PAGES[page]))
                    if unknown or pending_field:
                        key = (page, pending or 'unknown')
                        if setting_reads.get(key, 0) >= 1:
                            raise RuntimeError('event_setting_unverified' if pending_field else 'event_setting_state_unreadable')
                        setting_reads[key] = setting_reads.get(key, 0) + 1
                        self.stop.wait(0.25)
                        current = self.device.capture()
                        continue
                    setting_reads.clear()
                else:
                    setting_reads.clear()
                page_changed = page != last_page
                if page_changed:
                    last_progress, last_page = (time.monotonic(), page)
                if time.monotonic() - last_progress > 45:
                    raise RuntimeError('event_page_timeout:' + page)
                if context.get('pending_spin') and (not context.get('spin_result_seen')) and (page in {'roulette_animation', 'bingo', 'roulette_limit', 'roulette_replace', 'roulette_empty'}):
                    context['spin_result_seen'] = True
                    self.device.save()
                action = None
                if page == 'home' and (not (context.get('stage') in {'confirm_story', 'play_next'} and (context.get('event_verified') or entry_story_bound(context)))):
                    if entry_started is None:
                        entry_started = time.monotonic()
                    if recovering or (page_changed and phase not in {'entry', 'done'}):
                        scan_started = time.monotonic()
                        recovering = False
                        self.emit(dict(type='progress', message='已返回主页，重新选择剧情活动入口'))
                    if context.get('entry_pending') or context.get('event_verified'):
                        context.update(entry_pending=False, event_verified=False)
                        context.pop('entry_banner_id', None)
                        self.device.save()
                    if phase == 'done':
                        home_count += 1
                        if home_count >= 2:
                            return dict(report, ok=True, reason='event_home_verified')
                    elif entry_attempts >= 4 or time.monotonic() - entry_started > 180:
                        return dict(report, ok=False, reason='event_entry_retry_exhausted')
                    elif time.monotonic() - scan_started > 25:
                        self.emit(dict(type='progress', message='未能确认剧情活动入口，本次未完成；请检查入口识别'))
                        return dict(report, ok=False, reason='event_entry_not_verified')
                    elif 'event_banner' in actions and (not context.get('target_event_id') or current.get('banner_id') == context['target_event_id']):
                        action = 'event_banner'
                    elif 'next_banner' in actions:
                        action = 'next_banner'
                elif page in {'tachyon_rewards', 'tachyon_event'}:
                    if context.get('target_event_id') not in (None, 'tachyon'):
                        raise RuntimeError('event_identity_changed')
                    context['activity_id'] = 'tachyon'
                    context.update(event_verified=True, entry_pending=False, activity_kind='tachyon')
                    self.device.save()
                    report['activity_kind'] = 'tachyon'
                    report.setdefault('reward_popup_seen', False)
                    if page == 'tachyon_rewards':
                        report['reward_popup_seen'] = True
                        phase = 'tachyon'
                        if 'tachyon_continue' in actions:
                            action = 'tachyon_continue'
                    else:
                        phase, action = ('done', 'tachyon_back')
                elif page == 'event':
                    target = context.get('target_event_id')
                    if target and (target not in STORY_EVENT_IDS or context.get('activity_id') != target):
                        raise RuntimeError('event_identity_changed')
                    entry_started, entry_attempts, recovering = (None, 0, False)
                    context['event_verified'] = True
                    context['entry_pending'] = False
                    context.pop('entry_banner_id', None)
                    self.device.save()
                    if phase == 'entry':
                        phase = 'missions'
                    action = {'missions': 'missions', 'post_story_missions': 'missions', 'post_wheel_missions': 'missions', 'stories': 'stories', 'roulette': 'roulette', 'done': 'home'}[phase]
                elif page == 'other_event':
                    action = 'leave_other'
                elif page == 'missions':
                    tab = current.get('tab')
                    if expected_tab is not None and tab != expected_tab:
                        action = 'tab_' + str(expected_tab)
                    elif tab is not None:
                        expected_tab = None
                        if 'claim' in actions and tab not in claimed:
                            action = 'claim'
                        else:
                            visited.add(tab)
                            remaining = set(range(3)) - visited
                            if remaining:
                                expected_tab = min(remaining)
                                action = 'tab_' + str(expected_tab)
                            else:
                                phase = 'roulette' if phase == 'post_story_missions' else 'roulette' if phase == 'post_wheel_missions' and claimed else 'done' if phase == 'post_wheel_missions' else 'stories'
                                action = 'back'
                elif page == 'claim':
                    if context.get('stage') == 'claim':
                        claimed.add(context['claim_tab'])
                    action = 'close'
                elif page == 'story_list':
                    phase = 'stories'
                    if 'play_story' in actions:
                        if story_count >= 30:
                            raise RuntimeError('event_story_limit')
                        action = 'play_story'
                    else:
                        action = 'close'
                elif page == 'story_settings':
                    if not context.get('event_verified') and (not entry_story_bound(context)):
                        recovering = True
                        action = 'cancel_story'
                    else:
                        action = 'portrait' if not current['portrait'] else 'silent' if not current['silent'] else 'confirm_story'
                elif page == 'story_next':
                    action = 'play_next' if 'play_next' in actions else 'cancel'
                elif page in {'story_playing', 'story_menu', 'story_skip_confirm'}:
                    action = {'story_playing': 'menu', 'story_menu': 'skip', 'story_skip_confirm': 'skip_confirm'}[page]
                elif page == 'tutorial_help':
                    if not context.get('event_verified'):
                        recovering = True
                        context['entry_pending'] = False
                        context.pop('entry_banner_id', None)
                        self.device.save()
                    action = 'dismiss_tutorial'
                elif page == 'roulette_settings':
                    action = 'cancel_settings' if context.get('settings_saved') else 'continuous' if not current['continuous'] else 'eight_lines' if not current['eight_lines'] else 'save'
                elif page in {'roulette_limit', 'roulette_replace', 'roulette_empty', 'settings_saved', 'story_unlock'}:
                    if page == 'settings_saved':
                        context['settings_saved'] = True
                    action = 'close'
                elif page == 'bingo':
                    action = 'close_bingo' if 'close_bingo' in actions else 'advance_bingo'
                elif page == 'roulette_animation':
                    action = 'skip_spin'
                elif page == 'roulette':
                    if context.get('pending_spin') and 'spin' in actions and context.get('spin_result_seen'):
                        context.update(pending_spin=False, spin_result_seen=False)
                        self.device.save()
                    if current.get('spin_disabled') and 'spin' not in actions:
                        action = 'back'
                    elif not current.get('eight_mode') and 'spin' in actions:
                        action = 'options'
                    elif 'spin' in actions and (not context.get('pending_spin')):
                        action = 'spin'
                if action is not None and action in current.get('actions', {}):
                    if context.get('target_event_id'):
                        context.update(event_phase=phase, visited_tabs=sorted(visited), claimed_tabs=sorted(claimed), story_count=story_count)
                    if action == 'claim':
                        context['claim_tab'] = current['tab']
                    self.emit(dict(type='progress', message='活动收尾：' + LABELS[action]))
                    try:
                        receipt = self.device.step(action, current, self.stop)
                    except ReobserveRequired as error:
                        current = replans.accept(error)
                        continue
                    if receipt.get('execution_status') == 'executed':
                        replans.executed()
                    if action == 'event_banner' and receipt.get('performed') is False and (receipt.get('execution_status') == 'not_executed') and (receipt.get('reason') in {'event_banner_wait_for_switch', 'event_banner_changed', 'event_banner_stale'}) and (receipt.get('observation', {}).get('page') == 'home'):
                        self.stop.wait(0.15 if page == 'home' and phase == 'entry' else 0.5)
                        current = self.device.capture()
                        continue
                    fresh = receipt_replan(receipt, replans)
                    if fresh is not None:
                        current = fresh
                        continue
                    report['records'].append({k: v for k, v in receipt.items() if k != 'observation'})
                    report['device_actions'] += int(receipt.get('performed', False))
                    if action == 'leave_other' and receipt.get('performed'):
                        recovering = True
                        context.update(event_verified=False, entry_pending=False)
                        context.pop('entry_banner_id', None)
                        self.device.save()
                    if action == 'play_story' and receipt.get('performed'):
                        story_count += 1
                    if action == 'close' and page == 'story_list' and receipt.get('performed'):
                        phase = 'post_story_missions' if context.get('target_event_id') else 'roulette'
                        if phase == 'post_story_missions':
                            visited.clear()
                            claimed.clear()
                    if action == 'back' and page == 'roulette' and receipt.get('performed'):
                        phase = 'post_wheel_missions' if context.get('target_event_id') else 'done'
                        if phase == 'post_wheel_missions':
                            visited.clear()
                            claimed.clear()
                        context.update(pending_spin=False, spin_result_seen=False)
                        self.device.save()
                    if action == 'event_banner' and receipt.get('performed'):
                        entry_attempts += 1
                        context['entry_pending'] = True
                        context['entry_banner_id'] = current['banner_id']
                        context['activity_id'] = current['banner_id']
                        context['activity_kind'] = 'tachyon' if current['banner_id'] == 'tachyon' else 'story'
                        self.device.save()
                    current = receipt['observation']
                else:
                    self.stop.wait(0.15 if page == 'home' and phase == 'entry' else 0.5)
                    current = self.device.capture()
            raise RuntimeError('event_step_limit')
        except (RuntimeError, ValueError, OSError) as error:
            return dict(report, reason=str(error), **failure_details(error))
EVENT_FIELDS = {'stage', 'entry_pending', 'entry_banner_id', 'activity_kind', 'activity_id', 'event_verified', 'settings_saved', 'pending_spin', 'spin_result_seen', 'pending_setting', 'claim_tab', 'event_phase', 'visited_tabs', 'claimed_tabs', 'story_count', 'target_event_id', 'action_outcome', 'execution_status'}

class MultiEventRunner:
    """Run each observed reward activity once, then verify a whole Home carousel."""

    def __init__(self, device, emit, stop):
        self.device, self.emit, self.stop = (device, emit, stop)

    def run(self):
        from core.story_event.cycle import CarouselScan, pending
        context = self.device.context
        cycle = context.setdefault('event_cycle', dict(version=1, endpoint=_public_endpoint(), events={}, active_event_id=None))
        report = dict(ok=False, activity_cycle_version=1, device_actions=0, records=[])
        scan = CarouselScan()

        def result(reason, ok=False):
            return dict(report, ok=ok, reason=reason, events=deepcopy(cycle['events']), carousel_scan=scan.report(), pending=pending(context), pending_spin=bool(context.get('pending_spin')), pending_setting=bool(context.get('pending_setting')))

        def run_one(event_id, current):
            previous = cycle['events'].get(event_id, {})
            saved = previous.get('context', {})
            for name in EVENT_FIELDS:
                context.pop(name, None)
            context.update(deepcopy(saved), target_event_id=event_id)
            cycle['active_event_id'] = event_id
            cycle['events'][event_id] = dict(ok=False, reason='event_in_progress', context=deepcopy(saved))
            self.device.save()
            title = {'tachyon': '因子研究', 'autumn_thoughts': '秋日活动', 'farewell': '将离别缀合成诗'}[event_id]
            self.emit(dict(type='progress', message='活动检查：' + title))
            child = EventRunner(self.device, self.emit, self.stop).run(initial=current)
            report['records'].extend((dict(record, activity_id=event_id) for record in child['records']))
            report['device_actions'] += child['device_actions']
            report['last_observation'] = child.get('last_observation', current)
            if child.get('action_outcome'):
                context['action_outcome'] = child['action_outcome']
                report['action_outcome'] = child['action_outcome']
            complete = child.get('ok') is True and child.get('reason') == 'event_home_verified' and (not pending(context)) and (context.get('activity_id') == event_id)
            cycle['events'][event_id] = dict(ok=complete, reason=child.get('reason'), context={k: deepcopy(v) for k, v in context.items() if k in EVENT_FIELDS})
            if complete:
                cycle['active_event_id'] = None
                self.emit(dict(type='progress', message=title + '已完成，继续检查其他活动'))
            self.device.save()
            return (child, complete)
        try:
            if pending(context):
                return result('event_pending_requires_reconciliation')
            active = cycle.get('active_event_id')
            if active and pending(cycle['events'].get(active, {}).get('context', {})):
                context.update(deepcopy(cycle['events'][active]['context']))
                return result('event_pending_requires_reconciliation')
            if self.stop.is_set():
                return result('operator_stop')
            current = self.device.capture()
            started = time.monotonic()
            for _ in range(1200):
                report['last_observation'] = current
                if self.stop.is_set():
                    return result('operator_stop')
                if current['page'] == 'home':
                    event_id = scan.observe(current)
                    if event_id in WHITELIST and cycle['events'].get(event_id, {}).get('ok') is not True:
                        child, complete = run_one(event_id, current)
                        if not complete:
                            return result(child.get('reason') or 'event_incomplete')
                        scan = CarouselScan()
                        started = time.monotonic()
                        current = self.device.capture()
                        continue
                    if scan.complete:
                        details = scan.report()
                        if details['unrecognized_slots']:
                            return result('event_carousel_unrecognized')
                        required = [v for v in scan.slots.values() if v in WHITELIST]
                        if required and all((cycle['events'].get(v, {}).get('ok') is True for v in required)) and all((v.get('ok') is True for v in cycle['events'].values())):
                            return result('event_home_verified', ok=True)
                        return result('event_activity_incomplete' if required else 'event_carousel_no_reward_activity')
                    if time.monotonic() - started > 25:
                        return result('event_carousel_scan_incomplete')
                    if current.get('banner_excluded') in EXCLUDED_BANNERS and event_id is not None:
                        receipt = self.device.step('next_banner', current, self.stop)
                        report['records'].append({k: v for k, v in receipt.items() if k != 'observation'})
                        report['device_actions'] += int(receipt.get('performed', False))
                        if receipt.get('execution_status') == 'uncertain':
                            report['action_outcome'] = receipt
                            context['action_outcome'] = receipt
                            return result('event_navigation_uncertain')
                        current = receipt['observation']
                        continue
                elif cycle.get('active_event_id') or current['page'] in {'tachyon_rewards', 'tachyon_event'}:
                    event_id = cycle.get('active_event_id') or 'tachyon'
                    child, complete = run_one(event_id, current)
                    if not complete:
                        return result(child.get('reason') or 'event_incomplete')
                    scan, started = (CarouselScan(), time.monotonic())
                elif current['page'] in {'other_event', 'tutorial_help', 'story_settings'}:
                    action = {'other_event': 'leave_other', 'tutorial_help': 'dismiss_tutorial', 'story_settings': 'cancel_story'}[current['page']]
                    if action in current['actions']:
                        receipt = self.device.step(action, current, self.stop)
                        report['records'].append({k: v for k, v in receipt.items() if k != 'observation'})
                        report['device_actions'] += int(receipt.get('performed', False))
                        if receipt.get('execution_status') == 'uncertain':
                            context['action_outcome'] = receipt
                            return result('event_navigation_uncertain')
                        current = receipt['observation']
                        continue
                elif current['page'] not in {'unknown', 'loading'}:
                    return result('event_identity_unbound')
                if time.monotonic() - started > 45:
                    return result('event_entry_not_verified')
                self.stop.wait(0.15)
                current = self.device.capture()
            return result('event_step_limit')
        except (RuntimeError, ValueError, OSError) as error:
            return dict(result(str(error)), **failure_details(error))
