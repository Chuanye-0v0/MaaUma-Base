"""Navigate, sort and leave the first B/B-or-better horse selected. No racing."""
from core.legend_selection.observer import same_list, same_candidate, grid_ready
from core.action_recovery import ReobserveRequired, ReobserveBudget
NAVIGATION = dict(home=('open_races', 'races'), races=('open_events', 'events'), events=('open_legend', 'legend'), legend=('open_details', 'details'), details=('open_selection', 'selection'))

def qualifies(observation):
    grades = [observation.get('surface'), observation.get('distance')]
    if any((grade not in tuple('SABCDEFG') for grade in grades)):
        return None
    return all((grade in 'SAB' for grade in grades))

def next_list_overlap(before, after):
    a, b = (before.get('candidates', []), after.get('candidates', []))
    if not a or not b or (not grid_ready(after)):
        return None
    matches = []
    for count in range(1, min(len(a), len(b))):
        if all((same_candidate(x, y) for x, y in zip(a[-count:], b[:count]))) and any((type(x.get('score')) is int and x.get('score') == y.get('score') for x, y in zip(a[-count:], b[:count]))):
            if b[count]['score'] is None or a[-1]['score'] is None or b[count]['score'] <= a[-1]['score']:
                matches.append(count)
    return matches[0] if len(matches) == 1 else None

class LegendSelectionRunner:

    def __init__(self, device, emit, stop_event):
        self.device, self.emit, self.stop = (device, emit, stop_event)
        self.current = {}
        self.checked = self.actions = 0
        self.rejected = 0
        self.reobserve = ReobserveBudget(limit=3, seconds=30)

    def _check_stop(self):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')

    def _act(self, action):
        self._check_stop()
        if self.actions >= 1200:
            raise RuntimeError('action_limit')
        self.emit(dict(type='progress', phase=f'传奇选马：{action}；已检查 {self.checked}', message=action))
        expected = self.current
        while True:
            try:
                receipt = self.device.step(action, self.stop, expected)
                break
            except ReobserveRequired as error:
                if getattr(error, 'reason', None) != 'stale_frame' or expected.get('page') != 'selection' or error.observation.get('page') != 'selection':
                    raise
                self.current = self.reobserve.accept(error)
                if not same_list(expected, self.current):
                    raise RuntimeError('candidate_list_changed') from error
                if self.stop.wait(0.3):
                    raise RuntimeError('operator_stop')
        self.current = receipt['observation']
        self.reobserve.executed()
        self.actions += 1

    def _wait(self, predicate, reason):
        for _ in range(12):
            self._check_stop()
            if predicate(self.current):
                return
            if self.stop.wait(0.4):
                raise RuntimeError('operator_stop')
            self.current = self.device.capture()
        raise RuntimeError(reason)

    def _selection(self):
        if self.current.get('page') != 'selection':
            raise RuntimeError('selection_page_changed')
        if self.current.get('sort_field') != '评价' or self.current.get('direction') != '降序':
            raise RuntimeError('selection_not_sorted')
        if self.current.get('filter_off') is not True:
            raise RuntimeError('filter_not_off')
        if not grid_ready(self.current):
            raise RuntimeError('candidate_list_uncertain')
        scores = [candidate['score'] for candidate in self.current['candidates'] if candidate.get('score') is not None]
        if not scores or any((type(score) is not int for score in scores)):
            raise RuntimeError('candidate_list_uncertain')
        if scores != sorted(scores, reverse=True):
            raise RuntimeError('evaluation_order_unverified')

    def _stable_horse(self, index, expected):
        previous = None
        for attempt in range(4):
            self._check_stop()
            page = self.current.get('page')
            if page not in ('selection', 'unknown', 'loading'):
                raise RuntimeError('selection_page_changed')
            if page == 'selection':
                if self.current.get('sort_field') not in (None, '评价') or self.current.get('direction') not in (None, '降序'):
                    raise RuntimeError('selection_not_sorted')
                if self.current.get('filter_off') is False and ('filter_state' not in self.current or self.current.get('filter_state') == 'on'):
                    raise RuntimeError('filter_not_off')
            unsuitable = qualifies(self.current) is False
            key = (self.current.get('selected'), None if unsuitable else self.current.get('name'), self.current.get('surface'), self.current.get('distance'), self.current['candidates'][index].get('score')) if page == 'selection' and same_list(expected, self.current) and (self.current.get('sort_field') == '评价') and (self.current.get('direction') == '降序') and (self.current.get('filter_off') is True) and (unsuitable or self.current.get('name')) else None
            if key is not None and key[0] != index and (self.current.get('selection_rejection') == 'opponent_duplicate'):
                return False
            if key is not None and key[0] == index and (key == previous):
                if qualifies(self.current) is not None:
                    self._selection()
                    return True
            previous = key
            if attempt == 3:
                break
            if self.stop.wait(0.4):
                raise RuntimeError('operator_stop')
            self.current = self.device.capture()
        if same_list(expected, self.current) and self.current.get('selected') == index and (qualifies(self.current) is None):
            raise RuntimeError('aptitude_uncertain')
        raise RuntimeError('selected_horse_not_stable' if same_list(expected, self.current) else 'candidate_list_changed')

    def _run(self):
        self._check_stop()
        self.current = self.device.capture()
        self._wait(lambda o: o['page'] not in ('unknown', 'loading'), 'page_unverified')
        for _ in range(8):
            page = self.current['page']
            if page not in NAVIGATION:
                break
            action, target = NAVIGATION[page]
            if self.current.get('zero_tickets'):
                raise RuntimeError('no_tickets')
            if action not in self.current['actions']:
                raise RuntimeError('event_unavailable_or_uncertain')
            self._act(action)
            self._wait(lambda o: o['page'] == target, 'navigation_not_observed')
        if self.current['page'] == 'selection' and self.current.get('sort_field') != '评价':
            self._act('open_sort')
            self._wait(lambda o: o['page'] == 'sort', 'sort_dialog_not_observed')
        if self.current['page'] == 'sort':
            if self.current.get('evaluation_selected') is not True:
                self._act('sort_evaluation')
                self._wait(lambda o: o.get('evaluation_selected') is True, 'sort_not_selected')
            self._act('apply_sort')
            self._wait(lambda o: o['page'] == 'selection' and o.get('sort_field') == '评价', 'sort_not_applied')
        if self.current['page'] == 'selection' and self.current.get('direction') == '升序':
            self._act('descending')
            self._wait(lambda o: o.get('direction') == '降序', 'descending_not_observed')
        self._selection()
        for _ in range(200):
            if self.current.get('at_top'):
                break
            before = self.current
            self._act('scroll_top')
            self._wait(lambda o: o.get('page') == 'selection' and (o.get('at_top') or not same_list(before, o)), 'scroll_top_not_observed')
            self._selection()
        else:
            raise RuntimeError('list_top_unverified')
        first_unchecked = 0
        for _ in range(200):
            self._selection()
            viewport = self.current
            for index in range(first_unchecked, len(viewport['candidates'])):
                self._act('horse_' + str(index))
                if self._stable_horse(index, viewport) is False:
                    self.rejected += 1
                    self._wait(lambda o: o.get('page') == 'selection' and (not o.get('selection_rejection')), 'selection_rejection_not_cleared')
                    continue
                self.checked += 1
                if qualifies(self.current):
                    if type(self.current['candidates'][index].get('score')) is not int:
                        raise RuntimeError('selected_score_uncertain')
                    return dict(name=self.current.get('name'), score=self.current['candidates'][index]['score'], surface=self.current['surface'], distance=self.current['distance'])
            if self.current.get('at_bottom'):
                raise RuntimeError('no_qualified_horse')
            before = self.current
            self._act('scroll_next')
            self._wait(lambda o: o.get('page') == 'selection' and (not same_list(before, o)), 'scroll_not_observed')
            previous = None

            def settled(observation):
                nonlocal previous
                stable = observation.get('page') == 'selection' and grid_ready(observation) and (previous is not None) and same_list(previous, observation)
                previous = observation
                return stable
            self._wait(settled, 'candidate_list_not_settled')
            first_unchecked = next_list_overlap(before, self.current) or 0
        raise RuntimeError('page_limit')

    def run(self):
        selected = None
        try:
            selected = self._run()
            reason = 'selected_without_racing'
        except RuntimeError as exc:
            reason = str(exc)
        return dict(kind='legend_selection', ok=selected is not None, reason=reason, no_race_started=True, checked=self.checked, selected=selected, rejected=self.rejected, device_actions=self.actions, last_page=self.current.get('page'), screenshot_path=self.current.get('screenshot_path'))
