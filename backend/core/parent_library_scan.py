"""Bounded, injectable hall-of-fame traversal; no ADB, files or background jobs.

Driver coordinates are raw capture pixels. Observations use canonical 720x1280.
on_entry(entry, evidence) receives one new UUID entry per visited detail; it must
persist before returning. Optional driver.save_evidence(image,page,entry_id)
returns the relative screenshot path for that accepted factor page.
"""
from copy import deepcopy
import time
import uuid
from core.parent_library import KINDS, new_library, upsert_entry
from core.parent_library_observer import observe_parent_library
MAX_PAGES = 30

def _distance(a, b):
    try:
        if not a.startswith('dhash128:') or not b.startswith('dhash128:'):
            return None
        return (int(a.split(':', 1)[1], 16) ^ int(b.split(':', 1)[1], 16)).bit_count()
    except (AttributeError, ValueError):
        return None

def _same(a, b):
    distance = _distance(a.get('visual_signature'), b.get('visual_signature'))
    return a.get('column') == b.get('column') and a.get('kind') == b.get('kind') and (not (a.get('factor_key') and b.get('factor_key') and (a['factor_key'] != b['factor_key']))) and (distance is not None) and (distance <= 20)

def _soft_same(a, b):
    distance = _distance(a.get('visual_signature'), b.get('visual_signature'))
    return a.get('column') == b.get('column') and a.get('kind') == b.get('kind') and bool(a.get('factor_key')) and (a['factor_key'] == b.get('factor_key')) and (a.get('stars') is not None) and (a['stars'] == b.get('stars')) and (distance is not None) and (distance <= 24)

def _y(row):
    return (row['bbox'][1] + row['bbox'][3]) / 2

def _page_rows(view):
    rows = [deepcopy(row) for row in view.get('rows', []) if not row.get('clipped')]
    for row in rows:
        row['_start'] = False
        row['_gaps'] = []
    for boundary in view.get('group_boundaries', []):
        if boundary.get('full') is not True:
            continue
        top, bottom = (boundary['bbox'][1], boundary['bbox'][3])
        header = [i for i, row in enumerate(rows) if top - 5 <= _y(row) <= bottom + 15 and row.get('kind') in {'blue', 'red'}]
        if not header:
            return (rows, 'group_header_unreadable')
        first = min(header)
        rows[first]['_start'] = True
        colors = {rows[i]['kind'] for i in header}
        rows[first]['_gaps'] = [kind + '_header_unreadable' for kind in ('blue', 'red') if kind not in colors]
    return (rows, None)

class FactorPages:
    """Ordered scroll overlap with physical row evidence, never key-set union."""

    def __init__(self):
        self.rows = []
        self.pages = []
        self.reason = None
        self.at_bottom = False
        self.last_rows = []
        self.unassigned = []

    @staticmethod
    def factor_end(view, candidate, current_rows):
        end = view.get('factor_end') or {}
        box = end.get('bbox') or []
        return end.get('reason') == 'inheritance_history_heading' and len(box) == 4 and bool(current_rows) and (sum((row['_start'] for row in candidate)) == 3) and all((row['bbox'][3] <= box[1] for row in current_rows))

    def add(self, view, page):
        self.reason = None
        rows, problem = _page_rows(view)
        if problem:
            self.reason = problem
            return (False, False)
        if not rows:
            self.reason = 'factor_rows_unreadable'
            return (False, False)
        if not self.rows:
            if view.get('at_top') is not True or not rows[0]['_start']:
                self.reason = 'factor_top_unverified'
                return (False, False)
            overlap = 0
        else:
            repeated = len(rows) == len(self.last_rows) and all((_same(a, b) and abs(_y(a) - _y(b)) <= 6 for a, b in zip(self.last_rows, rows)))
            if repeated:
                self.at_bottom = view.get('at_bottom') is True or self.factor_end(view, self.rows, rows)
                return (False, True)
            matches = []
            ambiguous_pair = False
            for length in range(2, min(len(self.rows), len(rows)) + 1):
                old, fresh = (self.rows[-length:], rows[:length])
                strong = [_same(a, b) for a, b in zip(old, fresh)]
                if not all(strong):
                    span = max((_y(r) for r in old)) - min((_y(r) for r in old))
                    row_height = max((r['bbox'][3] - r['bbox'][1] for r in old))
                    if sum(strong) < 4 or sum(strong) / length < 0.8 or length - sum(strong) > 2 or (span < 2 * row_height) or (not all((ok or _soft_same(a, b) for ok, a, b in zip(strong, old, fresh)))):
                        continue
                deltas = [_y(b) - _y(a) for a, b in zip(old, fresh)]
                if max(deltas) - min(deltas) > 12 or not -490 <= sum(deltas) / length <= 8:
                    continue
                if max((_y(r) for r in old)) - min((_y(r) for r in old)) < 25:
                    pair = length == 2 and old[0]['_start'] and fresh[0]['_start'] and ([r.get('kind') for r in old] == ['blue', 'red']) and ([r.get('column') for r in old] == [0, 1]) and all((r.get('factor_key') for r in [*old, *fresh])) and (sum(deltas) / length < -6)
                    if not pair:
                        continue
                    keys = [r['factor_key'] for r in old]
                    occurrences = [i for i in range(len(self.rows) - 1) if self.rows[i]['_start'] and [r.get('factor_key') for r in self.rows[i:i + 2]] == keys]
                    if occurrences != [len(self.rows) - 2]:
                        ambiguous_pair = True
                        continue
                matches.append(length)
            if len(matches) != 1:
                self.reason = 'scroll_overlap_ambiguous' if matches or ambiguous_pair else 'scroll_overlap_missing'
                return (False, False)
            overlap = matches[0]
        candidate = deepcopy(self.rows)
        for i, fresh in enumerate(rows):
            source = {'page': page, 'screenshot': None}
            if i < overlap:
                index = len(candidate) - overlap + i
                old = candidate[index]
                fresh['_start'] = old['_start'] or fresh['_start']
                fresh['_gaps'] = list(dict.fromkeys(old['_gaps'] + fresh['_gaps']))
                if old.get('stars') is not None and fresh.get('stars') is not None and (old['stars'] != fresh['stars']):
                    fresh['stars'] = None
                    fresh['_gaps'].append('conflicting_stars')
                elif fresh.get('stars') is None:
                    fresh['stars'] = old.get('stars')
                if fresh.get('factor_key') is None and old.get('factor_key') is not None:
                    fresh['factor_key'] = old['factor_key']
                    fresh['raw_name'] = old['raw_name']
                fresh['_sources'] = old['_sources'] + [source]
                candidate[index] = fresh
            else:
                fresh['_sources'] = [source]
                candidate.append(fresh)
        if sum((row['_start'] for row in candidate)) > 3:
            self.reason = 'too_many_factor_groups'
            return (False, False)
        self.rows = candidate
        self.last_rows = deepcopy(rows)
        self.pages.append(page)
        self.at_bottom = view.get('at_bottom') is True or self.factor_end(view, candidate, rows)
        if self.at_bottom and any((row.get('clipped') and row['bbox'][3] >= view['bbox'][3] - 2 for row in view.get('rows', []))):
            self.reason = 'bottom_factor_row_clipped'
        return (True, False)

    def source(self, page, screenshot):
        for row in self.rows:
            for source in row['_sources']:
                if source['page'] == page:
                    source['screenshot'] = screenshot

    def entry(self, entry_id, identity, training_date=None):
        groups = []
        for row in self.rows:
            if row['_start']:
                groups.append([])
            if groups:
                groups[-1].append(row)
        coverage = self.at_bottom and len(groups) == 3 and (self.reason is None)
        reason = self.reason or (None if coverage else 'factor_groups_incomplete')
        nodes = []
        for index, group in enumerate(groups):
            missing = list(dict.fromkeys((gap for row in group for gap in row['_gaps'])))
            if reason:
                missing.append(reason)
            factors = []
            for row in group:
                if row.get('kind') not in KINDS:
                    missing.append('unknown_factor_color')
                    self.unassigned.append({'group_index': index, 'raw_name': row.get('raw_name'), 'stars': row.get('stars'), 'sources': deepcopy(row['_sources'])})
                    continue
                factors.append({'kind': row['kind'], 'factor_key': row.get('factor_key'), 'raw_name': row.get('raw_name') or '', 'stars': row.get('stars'), 'sources': row['_sources']})
            nodes.append({'status': 'complete' if coverage and (not missing) else 'partial', 'scope': list(KINDS) if coverage else list(dict.fromkeys((f['kind'] for f in factors))), 'missing': list(dict.fromkeys(missing)), 'factors': factors})
        own = nodes[0] if nodes else {'status': 'partial', 'scope': [], 'missing': [reason or 'not_scanned'], 'factors': []}
        value = {'entry_id': entry_id, 'character_name': identity.get('name'), 'variant_name': identity.get('outfit'), 'trained_on': training_date.get('raw') if isinstance(training_date, dict) else None, 'self': own, 'parents': [nodes[i] if i < len(nodes) else None for i in (1, 2)]}
        normalized = upsert_entry(new_library(), value)['entries'][0]
        return (normalized, coverage, len(groups))

class _Stopped(Exception):
    pass

class _NavigationError(Exception):
    pass

class _Scan:

    def __init__(self, driver, observe, on_entry, on_progress, stop, limit, sleep):
        self.driver, self.observe, self.on_entry, self.on_progress = (driver, observe, on_entry, on_progress)
        self.stop, self.limit, self.sleep = (stop, limit, sleep)
        self.frame = None
        self.observed = None
        self.stats = {'status': 'partial', 'scanned': 0, 'total': None, 'complete': False, 'coverage_complete': False, 'fully_recognized': False, 'reason': None, 'entries': []}

    def checkpoint(self):
        stopped = self.stop.is_set() if hasattr(self.stop, 'is_set') else bool(self.stop()) if callable(self.stop) else False
        if stopped:
            raise _Stopped()

    def capture(self):
        self.checkpoint()
        try:
            self.frame = self.driver.capture()
            self.observed = self.observe(self.frame)
        except Exception as error:
            raise _NavigationError('capture_or_observation_failed') from error
        return self.observed

    def point(self, point):
        height, width = self.frame.shape[:2]
        return (round(point[0] * width / 720), round(point[1] * height / 1280))

    def tap(self, name=None, cell=None):
        self.checkpoint()
        controls = [c for c in self.observed.get('controls', []) if c.get('name') == name] if cell is None else [cell]
        if len(controls) != 1:
            raise _NavigationError('observed_control_missing:' + str(name))
        self.driver.tap(*self.point(controls[0]['center']))
        self.sleep(0.2)

    def swipe(self, box, *, down=False, half=False):
        self.checkpoint()
        left, top, right, bottom = box
        x = (left + right) / 2
        height = bottom - top
        a, b = ([x, bottom - height * 0.12], [x, bottom - height * 0.47])
        if half:
            b[1] = (a[1] + b[1]) / 2
        if down:
            a, b = (b, a)
        self.driver.swipe(*self.point(a), *self.point(b), 800)
        self.sleep(0.2)

    def wait_page(self, pages):
        for _ in range(5):
            observed = self.capture()
            if observed.get('page') in pages:
                return observed
            self.sleep(0.15)
        raise _NavigationError('detail_navigation_unconfirmed')

    @staticmethod
    def list_signature(observed):
        value = observed.get('library') or {}
        return [(c.get('row'), c.get('column'), c.get('score')) for c in value.get('cells', [])]

    @staticmethod
    def first_cell(cells):
        if not cells:
            return None
        cell = cells[0]
        point = cell.get('center') or []
        if len(point) == 2 and 35 <= point[0] <= 145 and (110 <= point[1] <= 245) and (cell.get('row', 0) == 0) and (cell.get('column', 0) == 0):
            return cell
        return None

    def to_list_top(self):
        previous = None
        repeats = 0
        for _ in range(MAX_PAGES):
            if self.observed.get('page') != 'library':
                raise _NavigationError('library_page_required')
            library = self.observed.get('library') or {}
            if library.get('filter') is True:
                raise _NavigationError('library_filter_active')
            if library.get('filter') is not False:
                raise _NavigationError('library_filter_unreadable')
            if type(library.get('count')) is not int or library['count'] < 0:
                raise _NavigationError('library_count_unreadable')
            if self.stats['total'] is None:
                self.stats['total'] = library['count']
            if library['count'] != self.stats['total']:
                raise _NavigationError('library_count_changed')
            if library['count'] == 0:
                return None
            cells = library.get('cells') or []
            first = self.first_cell(cells)
            if library.get('at_top') is True:
                if first is None:
                    raise _NavigationError('library_first_cell_unreadable')
                return first
            if not cells:
                raise _NavigationError('library_cells_unreadable')
            if library.get('at_top') is None and len(cells) == library['count']:
                if first is None:
                    raise _NavigationError('library_first_cell_unreadable')
                return first
            signature = self.list_signature(self.observed)
            repeats = repeats + 1 if previous == signature else 0
            if repeats >= 2:
                if library.get('at_top') is False:
                    raise _NavigationError('library_top_unconfirmed')
                if first is None:
                    raise _NavigationError('library_first_cell_unreadable')
                return first
            previous = signature
            box = [min((c['bbox'][0] for c in cells)), min((c['bbox'][1] for c in cells)), max((c['bbox'][2] for c in cells)), max((c['bbox'][3] for c in cells))]
            self.swipe(box, down=True)
            self.capture()
        raise _NavigationError('library_top_unconfirmed')

    def to_factor_top(self):
        self.wait_page({'detail', 'detail_info', 'detail_inheritance'})
        date = self.observed.get('training_date')
        if self.observed['page'] != 'detail_inheritance':
            self.tap('inheritance')
            self.wait_page({'detail_inheritance'})
        previous = None
        repeats = 0
        for _ in range(MAX_PAGES):
            view = self.observed.get('viewport') or {}
            if view.get('at_top') is True:
                return date
            signature = view.get('signature')
            repeats = repeats + 1 if previous == signature else 0
            if repeats >= 2 or not view.get('bbox'):
                raise _NavigationError('factor_top_unverified')
            previous = signature
            self.swipe(view['bbox'], down=True)
            self.wait_page({'detail_inheritance'})
        raise _NavigationError('factor_top_unverified')

    def header_pixels(self):
        height, width = self.frame.shape[:2]
        return [self.frame[round(t * height / 1280):round(b * height / 1280), round(l * width / 720):round(r * width / 720)].copy() for l, t, r, b in ([340, 105, 650, 181], [65, 258, 210, 300])]

    def stable_header(self, reference):
        for old, fresh in zip(reference, self.header_pixels()):
            if old.shape != fresh.shape or not old.size:
                return False
            difference = abs(old.astype('int16') - fresh.astype('int16'))
            if difference.mean() > 1 or (difference > 12).mean() > 0.01:
                return False
        return True

    def same_detail(self, identity, header):
        fresh = self.observed.get('identity') or {}
        known = [key for key in ('name', 'outfit', 'score') if identity.get(key) is not None]
        return self.observed.get('page') == 'detail_inheritance' and (self.stable_header(header) or (bool(known) and all((fresh.get(key) == identity[key] for key in known))))

    def one(self, ordinal):
        entry_id = uuid.uuid4().hex
        pages = FactorPages()
        frames = []
        sources = []
        date = None
        identity = deepcopy(self.observed.get('identity') or {})
        stopped = False
        recoveries = {'captures': 0, 'rescans': 0}
        fallback = None
        self.stats['current'] = {'ordinal': ordinal, 'page': 0}
        self.on_progress(deepcopy(self.stats))
        try:
            date = self.to_factor_top()
            identity = deepcopy(self.observed.get('identity') or {})
            header = self.header_pixels()
            for attempt in range(2):
                repeats = 0
                for page in range(1, MAX_PAGES + 1):
                    self.checkpoint()
                    self.stats['current'] = {'ordinal': ordinal, 'page': page}
                    self.on_progress(deepcopy(self.stats))
                    for retry in range(3):
                        fresh = self.observed.get('identity') or {}
                        changed = any((identity.get(k) is not None and fresh.get(k) is not None and (identity[k] != fresh[k]) for k in ('name', 'outfit', 'score')))
                        if self.observed.get('page') != 'detail_inheritance' or (changed and (not self.stable_header(header))):
                            pages.reason = 'detail_changed_during_scroll'
                            accepted = repeated = False
                        else:
                            view = self.observed.get('viewport') or {}
                            accepted, repeated = pages.add(view, page)
                        if pages.reason not in {'detail_changed_during_scroll', 'scroll_overlap_missing', 'scroll_overlap_ambiguous'} or retry == 2:
                            break
                        recoveries['captures'] += 1
                        self.sleep(0.15)
                        self.capture()
                    if accepted:
                        saver = getattr(self.driver, 'save_evidence', None)
                        if callable(saver):
                            screenshot = saver(self.frame, page, entry_id)
                            pages.source(page, screenshot)
                            sources.append({'page': page, 'screenshot': screenshot})
                        else:
                            frames.append({'page': page, 'image': self.frame.copy()})
                    if pages.reason or pages.at_bottom:
                        break
                    repeats = repeats + 1 if repeated else 0
                    if repeats >= 2:
                        pages.reason = 'factor_scroll_stalled'
                        break
                    if page == MAX_PAGES:
                        pages.reason = 'factor_page_limit'
                        break
                    self.swipe(view['bbox'], half=bool(attempt))
                    self.wait_page({'detail_inheritance'})
                if attempt or pages.reason not in {'detail_changed_during_scroll', 'scroll_overlap_missing', 'scroll_overlap_ambiguous'} or (not self.same_detail(identity, header)):
                    break
                fallback = (pages, frames, sources)
                recoveries['rescans'] += 1
                try:
                    self.to_factor_top()
                except _NavigationError:
                    break
                if not self.same_detail(identity, header):
                    break
                pages = FactorPages()
                frames = []
                sources = []
        except _Stopped:
            pages.reason = 'operator_stop'
            stopped = True
        except _NavigationError as error:
            pages.reason = str(error)
        if fallback and (not pages.rows):
            reason = pages.reason
            pages, frames, sources = fallback
            pages.reason = reason or pages.reason
        entry, coverage, group_count = pages.entry(entry_id, identity, date)
        if not coverage and pages.reason is None:
            pages.reason = 'factor_groups_incomplete'
        evidence = {'ordinal': ordinal, 'identity': {key: identity.get(key) for key in ('name', 'outfit', 'score')}, 'page_count': len(pages.pages), 'group_count': group_count, 'reason': pages.reason, 'coverage_complete': coverage, 'frames': frames, 'frame_sources': sources, 'unassigned_rows': pages.unassigned, 'recoveries': recoveries}
        self.on_entry(entry, evidence)
        full = all((node is not None and node['status'] == 'complete' for node in [entry['self'], *entry['parents']]))
        self.stats['entries'].append({'entry_id': entry_id, 'ordinal': ordinal, 'coverage_complete': coverage, 'complete': full, 'reason': pages.reason})
        self.stats['scanned'] += 1
        self.on_progress(deepcopy(self.stats))
        if stopped:
            raise _Stopped()
        if not coverage:
            raise _NavigationError(pages.reason or 'factor_groups_incomplete')

    @staticmethod
    def detail_signature(observed):
        identity = observed.get('identity') or {}
        view = observed.get('viewport') or {}
        return (identity.get('name'), identity.get('outfit'), identity.get('score'), view.get('at_top'), view.get('signature'))

    def next_detail(self):
        previous = self.detail_signature(self.observed)
        self.tap('next')
        for _ in range(5):
            self.capture()
            if self.observed.get('page') in {'detail', 'detail_info', 'detail_inheritance'} and self.detail_signature(self.observed) != previous:
                return
            self.sleep(0.15)
        raise _NavigationError('next_detail_unconfirmed')

    def run(self):
        try:
            if self.limit is not None and (type(self.limit) is not int or self.limit < 1):
                raise ValueError('扫描数量应为正整数。')
            self.capture()
            cell = self.to_list_top()
            target = min(self.stats['total'], self.limit) if self.limit is not None else self.stats['total']
            if cell is not None:
                self.tap(cell=cell)
                self.wait_page({'detail', 'detail_inheritance', 'detail_info'})
            for ordinal in range(1, target + 1):
                self.one(ordinal)
                if ordinal < target:
                    self.next_detail()
            self.stats['coverage_complete'] = self.stats['scanned'] == self.stats['total'] and all((r['coverage_complete'] for r in self.stats['entries']))
            self.stats['fully_recognized'] = all((r['complete'] for r in self.stats['entries']))
            self.stats['complete'] = self.stats['coverage_complete'] and self.stats['fully_recognized']
            self.stats['status'] = 'complete' if self.stats['complete'] else 'partial'
            self.stats['reason'] = 'scan_complete' if self.stats['complete'] else 'scan_limit' if target < self.stats['total'] else 'scan_contains_incomplete_entries'
        except _Stopped:
            self.stats.update(status='stopped', reason='operator_stop')
        except _NavigationError as error:
            self.stats['reason'] = str(error)
        self.on_progress(deepcopy(self.stats))
        return deepcopy(self.stats)

def run_scan(driver, *, observe=observe_parent_library, on_entry, on_progress, stop, limit=None, sleep=time.sleep):
    return _Scan(driver, observe, on_entry, on_progress, stop, limit, sleep).run()
