"""Daily navigation with ordinal/template binding before the existing race gate."""
import json
import cv2
import numpy as np
import re
import hashlib
from functools import lru_cache
from pathlib import Path
from core.daily_legend.catalog import opponent
from core.daily_legend.listing import locate, authorize_list, help_close
from core.legend_race.live import LegendRaceDevice
from core.legend_race.observer import observe
from core.legend_selection.observer import read_tokens

def crop_tokens(image, ocr, box):
    x, y, right, bottom = box
    rows, _ = ocr(image[y:bottom, x:right])
    return [dict(text=text, confidence=float(confidence), bbox=[int(min((p[0] for p in points))) + x, int(min((p[1] for p in points))) + y, int(max((p[0] for p in points))) + x, int(max((p[1] for p in points))) + y]) for points, text, confidence in rows or []]

@lru_cache(maxsize=6)
def display_template(name):
    try:
        raw = (Path(__file__).with_name('assets') / (name + '.png')).read_bytes()
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE) if raw else None
    except (OSError, cv2.error):
        return None

def fast_display(raw, image, ocr, event=None, pending=False):
    """Recognize static display anchors; no full-screen OCR or consuming actions."""
    if image.shape[:2] != (1280, 720):
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    def matches(name, x, y, margin=3):
        template = display_template(name)
        if template is None:
            return False
        h, w = template.shape
        region = gray[max(0, y - margin):y + h + margin, max(0, x - margin):x + w + margin]
        return cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))[1] >= 0.97
    result = dict(actions={}, tokens=[], frame_sha256=hashlib.sha256(raw).hexdigest(), perception='fast_display')

    def ready(observation):
        from core.jjc.observer import observe_loading_fast
        return observe_loading_fast(raw, image, ocr) or observation
    if matches('help-title', 100, 50, 8):
        if matches('help-close', 230, 1180):
            return ready(dict(result, page='daily_legend_help', actions={'close_legend_help': {'point': [360, 1207]}}))
        return dict(result, page='loading')
    if not pending:
        return None
    if matches('rank-click', 315, 1025) and matches('rank-suffix', 412, 746):
        return ready(dict(result, page='rank_overlay', actions={'dismiss_rank': {'point': [360, 1055]}}))
    if matches('trophy-title', 200, 270) and matches('trophy-close', 285, 885):
        return ready(dict(result, page='trophy', victory=True, actions={'close_trophy': {'point': [360, 920]}}))
    return None

def daily_observation(raw, image, tokens, number, event=None, listing=None):
    result = observe(raw, tokens, navigation=True, event=event, daily=True)
    if image.shape[:2] != (1280, 720) or result['page'] in ('blocked', 'loading', 'connection_interrupted', 'connection_conflict'):
        return result

    def region(left, top, right, bottom, confidence=0.95):
        return [t for t in tokens if t['confidence'] >= confidence and left <= t['bbox'][0] < t['bbox'][2] <= right and (top <= t['bbox'][1] < t['bbox'][3] <= bottom)]
    if any((t['text'].replace(' ', '') == '继续比赛' for t in region(240, 360, 480, 450))) and any((t['text'].replace(' ', '') == '参赛中的优俊少女' for t in region(15, 520, 250, 575))):
        courses = [re.fullmatch('(.+?)困难', t['text'].replace(' ', '')) for t in region(20, 450, 450, 525, 0.9)]
        courses = [m.group(1) for m in courses if m]
        names = region(250, 610, 510, 650)
        scores = [t['text'].replace(',', '') for t in region(380, 650, 520, 720) if re.fullmatch('[\\d,]+', t['text'])]
        buttons = [t for t in region(390, 760, 680, 900) if t['text'] == '继续']
        result.update(page='race_resume', actions={}, event={})
        if len(courses) == len(names) == len(scores) == len(buttons) == 1:
            x0, y0, x1, y1 = buttons[0]['bbox']
            result.update(event={'course': courses[0]}, resume_horse_name=names[0]['text'], resume_horse_score=int(scores[0]), actions={'resume_race': {'point': [(x0 + x1) // 2, (y0 + y1) // 2]}})
        return result
    for action in ('view_result', 'start_race', 'run_race'):
        if action in result['actions']:
            result['actions']['resume_' + action] = result['actions'][action]
    close = help_close(tokens)
    if result['page'] == 'rewards' and 'continue_rewards' in result['actions']:
        result['actions']['continue_sale_rewards'] = result['actions']['continue_rewards']
    if close:
        result.update(page='daily_legend_help', actions={'close_legend_help': close})
        return result
    if result['page'] == 'events':
        good = [t for t in tokens if t['confidence'] >= 0.95]
        exhausted = any((re.fullmatch('0/[1-3]', t['text']) and 570 <= t['bbox'][0] < t['bbox'][2] <= 690 and (810 <= t['bbox'][1] < t['bbox'][3] <= 890) for t in good))
        completed = any((re.sub('\\s+', '', t['text']).rstrip('!！。.') == '本日内容已完成' and t['bbox'][0] > 400 and (930 <= t['bbox'][1] < t['bbox'][3] <= 980) for t in good))
        home = [t for t in good if t['text'] == '主页' and 290 <= t['bbox'][0] < t['bbox'][2] <= 430 and (t['bbox'][1] >= 1200)]
        if exhausted and completed:
            result.update(page='daily_legend_complete', tickets=0, actions={})
            if len(home) == 1:
                x0, y0, x1, y1 = home[0]['bbox']
                result['actions']['return_home'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
            elif not home:
                footer = region(290, 1200, 430, 1280)
                first = [t for t in footer if t['text'] == '主']
                second = [t for t in footer if t['text'] in {'页', '贝'}]
                neighbors = [('强化编成', (0, 1200, 170, 1280)), ('赛事', (470, 1200, 580, 1280)), ('招募', (590, 1200, 720, 1280))]
                if len(first) == len(second) == 1 and all((len([t for t in region(*box) if t['text'] == label]) == 1 for label, box in neighbors)):
                    a, b = (first[0]['bbox'], second[0]['bbox'])
                    if -10 <= b[0] - a[2] <= 20 and abs(a[1] + a[3] - (b[1] + b[3])) <= 12 and (a[0] < b[0]):
                        result['actions']['return_home'] = dict(point=[(a[0] + b[2]) // 2, (min(a[1], b[1]) + max(a[3], b[3])) // 2])
    if result['page'] == 'legend':
        listing = locate(image, number) if listing is None else listing
        if listing:
            result.update(page='daily_legend_list', listing=listing)
            result['actions'] = {k: v for k, v in result['actions'].items() if k == 'return_home'}
            if result.get('tickets', 0) and listing.get('action'):
                key = 'choose_opponent' if listing.get('found') else 'scroll_opponents'
                result['actions'][key] = listing['action']
        else:
            backs = [t for t in tokens if t['confidence'] >= 0.95 and t['text'].replace(' ', '') == '返回' and (t['bbox'][0] < 150) and (1030 < t['bbox'][1] < 1120)]
            if len(backs) == 1:
                x0, y0, x1, y1 = backs[0]['bbox']
                result['actions']['back_to_opponents'] = dict(point=[(x0 + x1) // 2, (y0 + y1) // 2])
    if result['page'] == 'unknown':

        def glyph(text, left, right):
            return any((t['confidence'] >= 0.95 and t['text'] == text and (left <= t['bbox'][0] < t['bbox'][2] <= right) and (710 <= t['bbox'][1] < t['bbox'][3] <= 910) for t in tokens))
        gray = cv2.cvtColor(image[740:875, 310:410], cv2.COLOR_BGR2GRAY)
        visible_rank = float(gray.std()) > 30 and (gray > 210).mean() > 0.15
        rank_number = any((t['confidence'] >= 0.95 and re.fullmatch('[1-9]|1[0-8]', t['text']) and (270 <= t['bbox'][0] < t['bbox'][2] <= 465) and (710 <= t['bbox'][1] < t['bbox'][3] <= 910) for t in tokens))
        rank_suffix = any((t['confidence'] >= 0.9 and t['text'] == '名' and (380 <= t['bbox'][0] < t['bbox'][2] <= 560) and (710 <= t['bbox'][1] < t['bbox'][3] <= 910) for t in tokens))
        merged_rank = any((t['confidence'] >= 0.95 and re.fullmatch('(?:[1-9]|1[0-8])名', t['text']) and (260 <= t['bbox'][0] < t['bbox'][2] <= 560) and (710 <= t['bbox'][1] < t['bbox'][3] <= 910) for t in tokens))
        click_hint = any((t['confidence'] >= 0.95 and t['text'] == '点击' and (290 <= t['bbox'][0] < t['bbox'][2] <= 430) and (1000 <= t['bbox'][1] < t['bbox'][3] <= 1100) for t in tokens))
        if visible_rank and (glyph('第', 180, 335) and (glyph('名', 380, 560) or merged_rank) or ((rank_number and rank_suffix or merged_rank) and click_hint)):
            result.update(page='rank_overlay', actions={'dismiss_rank': dict(point=[360, 820])})
            return result
        from core.daily_tasks.observer import observe as observe_daily
        playback = observe_daily(raw, tokens)
        if playback['page'] == 'limited_sale':
            result.update(page='limited_sale', actions={k: v for k, v in playback['actions'].items() if k == 'cancel_limited_sale'})
            return result
        if playback['page'] in ('animation', 'orientation'):
            result.update(page=playback['page'], portrait_selected=playback.get('portrait_selected'))
            result['actions'] = {k: v for k, v in playback['actions'].items() if k in ('skip_race', 'select_portrait', 'confirm_portrait')}
    return result

class DailyLegendDevice(LegendRaceDevice):
    additional_predecessors = {action: ('start_race',) for action in ('continue_victory', 'close_trophy', 'continue_rewards')}
    additional_predecessors['dismiss_rank'] = ('view_result', 'run_race', 'start_race')
    additional_predecessors['continue_sale_rewards'] = ('continue_rewards',)
    additional_predecessors['resume_race'] = ('enter_race',)
    for _action in ('resume_view_result', 'resume_start_race', 'resume_run_race'):
        additional_predecessors[_action] = ('resume_race',)
    for _action in ('continue_victory', 'continue_defeat', 'close_trophy', 'continue_rewards', 'dismiss_rank'):
        additional_predecessors[_action] = additional_predecessors.get(_action, ()) + ('resume_race',)
    navigation_actions = LegendRaceDevice.navigation_actions | {'choose_opponent', 'scroll_opponents', 'back_to_opponents', 'close_legend_help', 'cancel_limited_sale', 'skip_race', 'select_portrait', 'confirm_portrait'}

    def __init__(self, endpoint, adb_path, output_dir, number):
        super().__init__(endpoint, adb_path, output_dir)
        self.target = opponent(number)
        self.target_verified = False

    def resumed_playback_bound(self):
        marker = self.output_dir / 'resume_race-submitted.json'
        if not getattr(self, 'pending_resume_verified', False) or not marker.exists():
            return False
        prior = json.loads(marker.read_text(encoding='utf8'))
        if prior.get('action_id') != 'resume_race' or prior.get('horse') != self.horse or prior.get('event') != self.event:
            return False
        for path in self.output_dir.glob('*-receipt.json'):
            receipt = json.loads(path.read_text(encoding='utf8'))
            observed = receipt.get('observation', {})
            if receipt.get('action_id') == 'resume_race' and receipt.get('source_frame_sha256') == prior.get('source_frame_sha256') and (observed.get('endpoint') == self.endpoint):
                return True
        return False

    def read_observation(self, raw, image, navigation):
        if image.shape[:2] != (1280, 720):
            raise RuntimeError('portrait_720_required')
        pending = getattr(self, 'target_verified', False) and bool(getattr(self, 'horse', None))
        fast = fast_display(raw, image, self.ocr, self.event, pending)
        if fast and fast['frame_sha256'] != getattr(self, '_last_display_dispatch', None):
            return fast
        if pending:
            from core.legend_race.items import fast_items
            fast = fast_items(raw, image, self.ocr, daily=True)
            if fast:
                return fast
        header = crop_tokens(image, self.ocr, (0, 0, 720, 65))
        daily = any((t['text'].replace(' ', '') == '日常传奇赛事' and t['confidence'] >= 0.95 for t in header))
        listing = locate(image, self.target['number']) if daily else None
        tokens = header + crop_tokens(image, self.ocr, (0, 1200, 720, 1280)) if listing else read_tokens(image, self.ocr)
        return daily_observation(raw, image, tokens, self.target['number'], self.event, listing)

    def preflight(self, action, expected):
        super().preflight(action, expected)
        if action in ('resume_view_result', 'resume_start_race', 'resume_run_race'):
            if not self.resumed_playback_bound():
                raise RuntimeError('daily_legend_resume_not_bound')
        if action == 'continue_sale_rewards':
            receipts = [json.loads(p.read_text(encoding='utf8')) for p in self.output_dir.glob('*-receipt.json')]
            if not any((r.get('action_id') == 'cancel_limited_sale' and r.get('observation', {}).get('page') == 'rewards' and (r.get('observation', {}).get('endpoint') == self.endpoint) for r in receipts)):
                raise RuntimeError('daily_legend_sale_return_not_bound')
        if action in ('skip_race', 'select_portrait', 'confirm_portrait') and (not self.resumed_playback_bound()):
            marker = self.output_dir / 'start_race-submitted.json'
            prior = json.loads(marker.read_text(encoding='utf8')) if marker.exists() else {}
            if prior.get('horse') != self.horse or prior.get('event') != self.event:
                raise RuntimeError('daily_legend_playback_not_bound')

    def authorize(self, action, current, expected):
        if action == 'close_legend_help':
            if current.get('page') != 'daily_legend_help' or expected.get('page') != 'daily_legend_help' or current.get('endpoint') != self.endpoint or (expected.get('endpoint') != self.endpoint) or (action not in current.get('actions', {})):
                raise RuntimeError('daily_legend_help_not_bound')
            return current['actions'][action]
        if current.get('endpoint') != self.endpoint or expected.get('endpoint') != self.endpoint:
            raise RuntimeError('daily_legend_endpoint_not_bound')
        if action == 'resume_race':
            if not getattr(self, 'pending_resume_verified', False) or not self.target_verified or (not self.horse) or (not self.event) or (action not in current.get('actions', {})) or any((o.get('page') != 'race_resume' or o.get('event') != {'course': self.event['course']} or o.get('resume_horse_name') != self.horse['name'] or (o.get('resume_horse_score') != self.horse['score']) for o in (current, expected))):
                raise RuntimeError('daily_legend_resume_not_bound')
            return current['actions'][action]
        if action in ('resume_view_result', 'resume_start_race', 'resume_run_race'):
            if not getattr(self, 'pending_resume_verified', False):
                raise RuntimeError('daily_legend_resume_not_bound')
            return super().authorize(action.removeprefix('resume_'), current, expected)
        if action == 'continue_sale_rewards':
            return super().authorize('continue_rewards', current, expected)
        if action == 'cancel_limited_sale':
            if current.get('page') != 'limited_sale' or expected.get('page') != 'limited_sale' or action not in current.get('actions', {}):
                raise RuntimeError('daily_legend_sale_not_bound')
            return current['actions'][action]
        if action == 'dismiss_rank':
            if current.get('page') != 'rank_overlay' or expected.get('page') != 'rank_overlay' or (not self.target_verified) or (not self.horse) or (not self.event) or (action not in current.get('actions', {})):
                raise RuntimeError('daily_legend_rank_not_bound')
            return current['actions'][action]
        if action in ('skip_race', 'select_portrait', 'confirm_portrait'):
            page = 'animation' if action == 'skip_race' else 'orientation'
            if current.get('page') != page or expected.get('page') != page or action not in current['actions'] or (not self.target_verified) or (not self.horse) or (not self.event):
                raise RuntimeError('daily_legend_playback_not_bound')
            if action == 'confirm_portrait' and current.get('portrait_selected') is not True:
                raise RuntimeError('daily_legend_portrait_not_selected')
            return current['actions'][action]
        if action in ('choose_opponent', 'scroll_opponents'):
            return authorize_list(action, current, expected, self.target['number'])
        if action == 'back_to_opponents':
            if current.get('page') != 'legend' or expected.get('page') != 'legend':
                raise RuntimeError('daily_legend_back_not_bound')
            candidate = current['actions'].get(action)
            if not candidate:
                raise RuntimeError('daily_legend_back_not_visible')
            return candidate
        if action == 'return_home' and current.get('page') in ('daily_legend_list', 'daily_legend_complete'):
            if expected.get('page') != current['page'] or action not in current['actions']:
                raise RuntimeError('daily_legend_empty_not_bound')
            return current['actions'][action]
        if action in ('open_selection', 'confirm_selected', 'enter_race'):
            if not self.target_verified or not self.event or self.event.get('opponent') != self.target['name']:
                raise RuntimeError('daily_legend_opponent_not_verified')
        return super().authorize(action, current, expected)

    def step(self, action_id, stop_event, expected):
        receipt = super().step(action_id, stop_event, expected)
        if action_id in {'close_legend_help', 'dismiss_rank', 'close_trophy'}:
            self._last_display_dispatch = receipt['source_frame_sha256']
        if action_id == 'choose_opponent':
            self.target_verified = receipt['observation'].get('page') == 'legend'
        return receipt
