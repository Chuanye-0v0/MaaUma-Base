"""Shared capture/input boundary with per-round one-shot marker namespaces."""
import re
from pathlib import Path
import cv2
from core.legend_race.live import LegendRaceDevice
from core.legend_selection.observer import read_tokens
from core.ocr_regions import read_regions
from core.champions_race.observer import observe
from core.champions_race.policy import admission_resource
from core.champions_race.task_restart import round_phase
PREVIEW_RETRY_ACTIONS = {f'retry_view_match_result_{attempt}' for attempt in range(1, 4)}
MATCH_ACTIONS = {'launch_match', 'resume_match', 'continue_standings', 'resume_continue_standings', 'continue_match_result', 'resume_continue_match_result', 'view_match_result', 'continue_viewed_standings', 'dismiss_match_outcome', 'dismiss_group_promotion', 'retry_continue_match_result'} | PREVIEW_RETRY_ACTIONS
ACTION_PAGES = {'open_races': 'home', 'open_events': 'races', 'open_champions': 'events', 'open_current_round': 'champions_active_hub', 'open_entry': 'champions_hub', 'cancel_entry': 'champions_entry_confirm', 'confirm_entry': 'champions_entry_confirm', 'auto_lineup': 'champions_lineup', 'confirm_auto_lineup': 'champions_auto_confirm', 'confirm_lineup': 'champions_lineup', 'confirm_team': 'champions_team_confirm', 'match_round': 'champions_board', 'resume_matching': 'champions_board', 'launch_match': 'champions_board', 'resume_match': 'champions_board', 'continue_standings': 'champions_standings', 'continue_match_result': 'champions_match_result', 'resume_continue_standings': 'champions_standings', 'resume_continue_match_result': 'champions_match_result', 'retry_continue_match_result': 'champions_match_result', 'view_match_result': 'champions_preview', 'continue_viewed_standings': 'champions_standings', 'dismiss_match_outcome': 'champions_outcome', 'dismiss_group_promotion': 'champions_promotion', 'claim_round': 'champions_board', 'advance_round_reward': 'champions_round_reward', 'continue_round_reward': 'champions_round_reward', 'close_claim': 'champions_claim_receipt', 'return_home': 'champions_hub'}
ACTION_PAGES.update({role: 'champions_preview' for role in PREVIEW_RETRY_ACTIONS})

class ChampionsRaceDevice(LegendRaceDevice):
    loading_stall_seconds = 90
    navigation_actions = {'open_races', 'open_events', 'open_champions', 'open_entry', 'open_current_round', 'cancel_entry', 'return_home'}
    candidate_replacements = {'advance_round_reward': ('continue_round_reward',)}

    def __init__(self, endpoint, adb_path, output_dir, division=None):
        if division not in (None, 'intermediate', 'advanced'):
            raise ValueError('champions_division_invalid')
        super().__init__(endpoint, adb_path, output_dir)
        self.division = division
        self.context = {}
        self.match_index = None
        self.event = dict(competition='champions_race', division=division)
        self.book = {}
        self.max_rounds = 4
        self.allow_final_jewels = False
        self.allow_jewels = False
        self.adopted = False
        self.additional_predecessors = {}

    def read_observation(self, raw, image, navigation):
        if image.shape[:2] != (1280, 720):
            raise RuntimeError('portrait_720_required')
        anchors = read_regions(image, self.ocr, [(200, 270, 550, 455)])
        titles = [t for t in anchors if t.get('confidence', 0) >= 0.95 and re.sub('\\s+', '', t['text']) in {'确认联赛选择', '报名确认', '自动编成确认', '参赛登记确认', '领取成功'}]
        if len(titles) > 1:
            return observe(raw, image, [])
        modal = len(titles) == 1
        tokens = anchors + read_regions(image, self.ocr, [(30, 450, 690, 795), (60, 795, 670, 980), (500, 35, 720, 110)]) if modal else read_tokens(image, self.ocr)
        refined = [] if modal else read_regions(image, self.ocr, [(200, 1000, 550, 1240)])
        for token in refined:
            same = [t for t in tokens if t['text'].replace(' ', '') == token['text'].replace(' ', '') and abs(t['bbox'][1] - token['bbox'][1]) < 20]
            if same and max((t['confidence'] for t in same)) >= token['confidence']:
                continue
            tokens = [t for t in tokens if t not in same]
            tokens.append(token)
        book = getattr(self, 'book', {}) or {}
        active = book.get('active_round') or {}
        match = getattr(self, 'match_index', None)
        stage = re.fullmatch('(?:view_match_result|retry_view_match_result_[1-3]|dismiss_match_outcome)_([1-5])', str(book.get('stage', '')))
        outcome_expected = bool(not modal and book.get('started') is True and stage and (int(stage.group(1)) == match == active.get('match_index')) and active.get('number') and (self.output_dir.resolve() == Path(book['run']).resolve() / f"round-{active['number']:03d}") and (self.output_dir / f'view_match_result_{match}-submitted.json').exists())
        result = observe(raw, image, tokens, outcome_expected=outcome_expected)
        if result.get('page') == 'champions_preview' and 'view_match_result' in result['actions']:
            for role in PREVIEW_RETRY_ACTIONS:
                result['actions'][role] = dict(result['actions']['view_match_result'])
        if result.get('page') == 'champions_board' and result.get('unmatched') is True and ('match_round' in result['actions']):
            result['actions']['resume_matching'] = dict(result['actions']['match_round'])
        if result['page'] in {'champions_standings', 'champions_board'} and (not result.get('actions')):
            box = (230, 1120, 530, 1230) if result['page'] == 'champions_standings' else (210, 1030, 540, 1160)
            x, y, r, b = box
            rows, _ = self.ocr(cv2.resize(image[y:b, x:r], None, fx=2, fy=2), use_det=False, use_cls=False)
            tokens += [dict(text=text, confidence=float(score), bbox=list(box)) for text, score in rows or []]
            result = observe(raw, image, tokens)
        if self.match_index:
            for action in MATCH_ACTIONS & result['actions'].keys():
                result['actions'][f'{action}_{self.match_index}'] = result['actions'][action]
            if 'launch_match' in result['actions']:
                result['actions'][f'resume_match_{self.match_index}'] = result['actions']['launch_match']
            if 'continue_standings' in result['actions']:
                result['actions'][f'resume_continue_standings_{self.match_index}'] = result['actions']['continue_standings']
                result['actions'][f'continue_viewed_standings_{self.match_index}'] = result['actions']['continue_standings']
            if 'continue_match_result' in result['actions']:
                result['actions'][f'resume_continue_match_result_{self.match_index}'] = result['actions']['continue_match_result']
                result['actions'][f'retry_continue_match_result_{self.match_index}'] = result['actions']['continue_match_result']
        return result

    def configure_round(self, book, number, match_index=None):
        self.book = book
        self.match_index = match_index
        active = book.get('active_round') or {}
        self.adopted = active.get('adopted') is True
        previous = {} if self.adopted else {'confirm_lineup': ('confirm_entry',), 'confirm_team': ('confirm_lineup',), 'match_round': ('confirm_team', 'confirm_entry')}
        previous['confirm_auto_lineup'] = ('auto_lineup',)
        previous['resume_matching'] = ('match_round',)
        for i in range(1, 6):
            for attempt in range(1, 4):
                previous[f'retry_view_match_result_{attempt}_{i}'] = (f'view_match_result_{i}' if attempt == 1 else f'retry_view_match_result_{attempt - 1}_{i}',)
            launch = f'launch_match_{i}'
            standing = f'continue_standings_{i}'
            result = f'continue_match_result_{i}'
            if not self.adopted:
                previous[launch] = ('match_round', 'confirm_team', 'confirm_entry') if i == 1 else (f'continue_match_result_{i - 1}', f'launch_match_{i - 1}')
            previous[standing] = (launch,)
            previous[f'view_match_result_{i}'] = (standing, f'resume_continue_standings_{i}')
            previous[f'dismiss_match_outcome_{i}'] = (f'view_match_result_{i}',)
            previous[f'dismiss_group_promotion_{i}'] = (f'dismiss_match_outcome_{i}',)
            previous[f'continue_viewed_standings_{i}'] = (f'dismiss_match_outcome_{i}', f'view_match_result_{i}')
            previous[result] = (f'dismiss_match_outcome_{i}', f'continue_viewed_standings_{i}', f'view_match_result_{i}', standing, launch)
            previous[f'retry_continue_match_result_{i}'] = (result,)
            previous[f'resume_match_{i}'] = (launch,)
            previous[f'resume_continue_standings_{i}'] = (f'resume_match_{i}',)
            previous[f'resume_continue_match_result_{i}'] = (f'resume_continue_standings_{i}', f'resume_match_{i}')
        previous['claim_round'] = ('continue_match_result_5', 'launch_match_5') if not self.adopted else ()
        previous['advance_round_reward'] = ('claim_round',)
        previous['continue_round_reward'] = ('claim_round',)
        previous['close_claim'] = ('continue_round_reward', 'claim_round', 'advance_round_reward')
        self.additional_predecessors = previous

    def _verify_round_reward_claim(self):
        import hashlib
        import json
        import numpy as np
        active = self.book.get('active_round') or {}
        directory = self.output_dir.resolve()
        if not self.book.get('started') or directory != Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}":
            raise RuntimeError('champions_round_reward_owner_unverified')
        try:
            read = lambda path: json.loads(path.read_text(encoding='utf8'))
            snapshots = [read(p) for p in directory.glob('*.json') if p.stem.isdigit()]
            receipts = [read(p) for p in directory.glob('*-receipt.json')]

            def parsed_frame(observed):
                path = Path(observed['screenshot_path']).resolve()
                raw = path.read_bytes()
                if path.parent != directory or observed.get('endpoint') != self.endpoint or hashlib.sha256(raw).hexdigest() != observed.get('frame_sha256'):
                    raise ValueError('frame_binding')
                return observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), observed.get('tokens', []))
            for action in ('claim_round', 'advance_round_reward'):
                path = directory / (action + '-submitted.json')
                if action == 'advance_round_reward' and (not path.exists()):
                    continue
                marker = read(path)
                if marker.get('action_id') != action or marker.get('event') != self.event:
                    raise ValueError('claim_scope')
                source = next((s for s in snapshots if s.get('frame_sha256') == marker.get('source_frame_sha256')), None)
                if source is None:
                    raise ValueError('claim_source_missing')
                before = parsed_frame(source)
                if action == 'claim_round' and (before.get('page') != 'champions_board' or before.get('table_verified') is not True or before.get('completed_matches') != 5) or (action == 'advance_round_reward' and before.get('page') != 'champions_round_reward') or (round_phase(self.book) is not None and before.get('event_phase') not in (None, round_phase(self.book))):
                    raise ValueError('claim_role')
                receipt = next((r for r in receipts if r.get('action_id') == action and r.get('source_frame_sha256') == marker.get('source_frame_sha256')), None)
                if receipt is None or receipt.get('execution_status') in {'uncertain', 'unknown', 'not_executed', 'partial'}:
                    raise ValueError('claim_receipt_missing')
                parsed_frame(receipt['observation'])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise RuntimeError('champions_round_reward_claim_unverified') from error

    def preflight(self, action, expected):
        super().preflight(action, expected)
        if action == 'resume_matching':
            from core.champions_race.resume import verify_matching_resume_source
            verify_matching_resume_source(self.output_dir, self.endpoint, self.event)
        match = re.fullmatch('(.+)_([1-5])', action)
        role = match.group(1) if match and match.group(1) in MATCH_ACTIONS else action
        index = int(match.group(2)) if match else self.match_index
        active = self.book.get('active_round') or {}
        if role in PREVIEW_RETRY_ACTIONS:
            from core.champions_race.resume import verify_preview_retry_source
            verify_preview_retry_source(self.output_dir, self.endpoint, self.event, index, round_phase(self.book), int(role[-1]))
        if role == 'confirm_auto_lineup':
            from core.champions_race.resume import verify_auto_lineup_source
            verify_auto_lineup_source(self.output_dir, self.endpoint, self.event)
        if role == 'continue_round_reward':
            self._verify_round_reward_claim()
        if role == 'dismiss_group_promotion':
            from core.champions_race.resume import verify_preview_source
            verify_preview_source(self.output_dir, self.endpoint, self.event, index, round_phase(self.book), dismissed=True)
        if role in {'view_match_result', 'continue_viewed_standings', 'dismiss_match_outcome', 'continue_match_result'}:
            if role != 'continue_match_result' or (self.output_dir / f'dismiss_match_outcome_{index}-submitted.json').exists():
                from core.champions_race.resume import verify_preview_source
                verify_preview_source(self.output_dir, self.endpoint, self.event, index, round_phase(self.book), viewed=role in {'continue_viewed_standings', 'dismiss_match_outcome'}, dismissed=role == 'continue_match_result')
        if role == 'retry_continue_match_result':
            from core.champions_race.resume import verify_continue_retry_source
            verify_continue_retry_source(self.output_dir, self.endpoint, self.event, index)
        if role in {'resume_match', 'resume_continue_standings', 'resume_continue_match_result'}:
            import json
            original = f'launch_match_{index}' if role == 'resume_match' else f'resume_match_{index}'
            marker = json.loads((self.output_dir / f'{original}-submitted.json').read_text(encoding='utf8'))
            if not any(((r := json.loads(p.read_text(encoding='utf8'))).get('action_id') == original and r.get('source_frame_sha256') == marker.get('source_frame_sha256') and (r.get('observation', {}).get('endpoint') == self.endpoint) for p in self.output_dir.glob('*-receipt.json'))):
                raise RuntimeError('champions_original_match_receipt_missing' if role == 'resume_match' else 'champions_resumed_presentation_receipt_missing')

    def authorize(self, action, current, expected):
        match = re.fullmatch('(.+)_([1-5])', action)
        role = match.group(1) if match and match.group(1) in MATCH_ACTIONS else action
        if role not in ACTION_PAGES or action not in current.get('actions', {}) or any((o.get('page') != ACTION_PAGES[role] or o.get('endpoint') != self.endpoint for o in (current, expected))):
            raise RuntimeError('champions_race_action_not_bound')
        if self.division is not None and any((o.get('division') not in (None, self.division) for o in (current, expected))):
            raise RuntimeError('champions_race_division_changed')
        active = self.book.get('active_round') or {}
        if active.get('event') is not None and self.event != active['event']:
            raise RuntimeError('champions_race_owner_changed')
        if role == 'open_current_round' and any((o.get('round_registered') is not True for o in (current, expected))):
            raise RuntimeError('champions_current_round_not_registered')
        if role in {'match_round', 'resume_matching'} and (not self.book.get('started') or self.output_dir.resolve() != Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}" or any((o.get('table_verified') is not True or o.get('unmatched') is not True or o.get('completed_matches') != 0 for o in (current, expected)))):
            raise RuntimeError('champions_matching_scope_unverified')
        if role == 'confirm_entry':
            left = admission_resource(current, self.book, max_rounds=self.max_rounds, allow_final_jewels=self.allow_final_jewels, allow_jewels=getattr(self, 'allow_jewels', False))
            right = admission_resource(expected, self.book, max_rounds=self.max_rounds, allow_final_jewels=self.allow_final_jewels, allow_jewels=getattr(self, 'allow_jewels', False))
            if left != right:
                raise RuntimeError('champions_race_cost_changed')
        if role == 'confirm_lineup' and any((o.get('lineup_count') != 3 for o in (current, expected))):
            raise RuntimeError('champions_lineup_not_complete')
        if role == 'auto_lineup' and any((type(o.get('lineup_count')) is not int or o['lineup_count'] >= 3 for o in (current, expected))):
            raise RuntimeError('champions_existing_lineup_preserved')
        if role == 'confirm_auto_lineup':
            active = self.book.get('active_round') or {}
            expected_directory = Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}"
            if not self.book.get('started') or self.output_dir.resolve() != expected_directory:
                raise RuntimeError('champions_auto_lineup_round_not_bound')
        if role == 'confirm_team' and any((o.get('team_complete') is not True for o in (current, expected))):
            raise RuntimeError('champions_team_not_complete')
        if role == 'continue_round_reward' and (not self.book.get('started') or self.output_dir.resolve() != Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}"):
            raise RuntimeError('champions_round_reward_owner_unverified')
        if role in MATCH_ACTIONS:
            index = int(match.group(2)) if match else self.match_index
            if index != self.match_index or not self.book.get('started'):
                raise RuntimeError('champions_match_scope_unverified')
            if role in {'launch_match', 'resume_match'} and any((o.get('next_match') != index or o.get('table_verified') is not True for o in (current, expected))):
                raise RuntimeError('champions_pending_match_changed')
            if role in {'continue_standings', 'resume_continue_standings', 'view_match_result', 'continue_viewed_standings'} and any((o.get('match_index') != index for o in (current, expected))):
                raise RuntimeError('champions_standings_changed')
            if role in {'view_match_result', 'continue_viewed_standings'} | PREVIEW_RETRY_ACTIONS:
                active = self.book.get('active_round') or {}
                if self.output_dir.resolve() != Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}" or (round_phase(self.book) is not None and any((o.get('event_phase') not in (None, round_phase(self.book)) for o in (current, expected)))):
                    raise RuntimeError('champions_preview_scope_changed')
                if role in {'view_match_result'} | PREVIEW_RETRY_ACTIONS and any((o.get('match_index') != index or o.get('preview', {}).get('view_result_locked') is not False for o in (current, expected))):
                    raise RuntimeError('champions_preview_result_locked')
            if role == 'dismiss_match_outcome':
                active = self.book.get('active_round') or {}
                if self.output_dir.resolve() != Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}" or active.get('match_index') != index or any((o.get('match_index') not in (None, index) or (round_phase(self.book) is not None and o.get('event_phase') not in (None, round_phase(self.book))) for o in (current, expected))):
                    raise RuntimeError('champions_outcome_scope_changed')
            if role == 'dismiss_group_promotion':
                if self.output_dir.resolve() != Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}" or active.get('match_index') != index or any((o.get('promotion_group') not in {'A', 'B'} for o in (current, expected))):
                    raise RuntimeError('champions_promotion_scope_changed')
            if role == 'continue_match_result' and (self.output_dir / f'dismiss_match_outcome_{index}-submitted.json').exists():
                active = self.book.get('active_round') or {}
                if self.output_dir.resolve() != Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}" or active.get('match_index') != index:
                    raise RuntimeError('champions_result_scope_changed')
            if role == 'retry_continue_match_result':
                active = self.book.get('active_round') or {}
                if self.output_dir.resolve() != Path(self.book.get('run', '')).resolve() / f"round-{active.get('number', 0):03d}" or active.get('match_index') != index or any((o.get('match_index') not in (None, index) or (round_phase(self.book) is not None and o.get('event_phase') not in (None, round_phase(self.book))) for o in (current, expected))):
                    raise RuntimeError('champions_continue_retry_scope_changed')
        if role == 'claim_round' and any((o.get('completed_matches') != 5 or o.get('table_verified') is not True for o in (current, expected))):
            raise RuntimeError('champions_round_not_complete')
        return current['actions'][action]
