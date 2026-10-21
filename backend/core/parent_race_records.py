"""Local, horse-bound observed wins; a calendar is a vocabulary, never a result."""
from copy import deepcopy
import json
from pathlib import Path
import re
import unicodedata
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / 'data/parent_library/race_catalog.json'
CATALOG_SCHEMA = 'uma.parent_race_catalog.v1'
COVERAGES = frozenset({'unknown', 'partial', 'complete'})
WIN_STATUSES = frozenset({'confirmed', 'unverified'})

def _text(value, label, *, blank=False, limit=500):
    if not isinstance(value, str) or len(value) > limit or (not blank and (not value.strip())):
        raise ValueError(label + '无效。')
    return value.strip()

def _entry_id(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('胜场记录需要明确的成品编号，不能只绑定角色名称。')
    return value

def _fields(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(label + '字段无效。')

def normalize_records(value, entry_id):
    """Validate optional node data without claiming an unscanned empty history."""
    expected = _entry_id(entry_id)
    _fields(value, ('entry_id', 'coverage', 'wins', 'source'), '胜场记录')
    if value.get('entry_id') != expected:
        raise ValueError('胜场记录与当前节点的成品编号不一致。')
    coverage = value.get('coverage', 'unknown')
    if not isinstance(coverage, str) or coverage not in COVERAGES:
        raise ValueError('胜场完整性应为未知、部分或完整。')
    source = _text(value.get('source'), '胜场来源')
    wins = value.get('wins', [])
    if not isinstance(wins, list) or len(wins) > 2000:
        raise ValueError('胜场应为不超过 2000 项的列表。')
    result = []
    seen = set()
    for row in wins:
        _fields(row, ('race_id', 'raw_name', 'status', 'source'), '胜场条目')
        race_id = _text(row.get('race_id'), '赛事编号', limit=160)
        if race_id in seen:
            raise ValueError('同一赛事窗口的胜场重复，请保留一条实际记录。')
        seen.add(race_id)
        status = row.get('status', 'unverified')
        if not isinstance(status, str) or status not in WIN_STATUSES:
            raise ValueError('只可保存已确认或待核对胜场；计划参赛不是实际胜场。')
        result.append({'race_id': race_id, 'raw_name': _text(row.get('raw_name', ''), '赛事原文', blank=True, limit=240), 'status': status, 'source': _text(row.get('source', source), '胜场来源')})
    return {'entry_id': expected, 'coverage': coverage, 'wins': result, 'source': source}

class RaceCatalog:
    """Small projection of the existing local calendar, with source labels intact."""

    def __init__(self, payload):
        if not isinstance(payload, dict) or payload.get('schema_version') != CATALOG_SCHEMA:
            raise ValueError('本地赛事目录版本无效。')
        self.source = deepcopy(payload.get('source', {}))
        rows = payload.get('races')
        if not isinstance(rows, list):
            raise ValueError('本地赛事目录缺少赛事列表。')
        self.by_id = {}
        for row in rows:
            identifier = _text(row.get('race_id'), '目录赛事编号', limit=160)
            if identifier in self.by_id or not isinstance(row.get('event_key'), str):
                raise ValueError('本地赛事目录编号重复或缺少稳定赛事键。')
            self.by_id[identifier] = deepcopy(row)

    @classmethod
    def load(cls, path=None):
        return cls(json.loads(Path(path or DEFAULT_CATALOG).read_text('utf-8-sig')))

    def get(self, race_id):
        row = self.by_id.get(race_id)
        return deepcopy(row) if row is not None else None

    def search(self, query='', limit=100):
        if not isinstance(query, str) or len(query) > 240:
            raise ValueError('赛事查询文字过长。')
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('赛事显示数量应为 1～500。')

        def normal(text):
            return ''.join(unicodedata.normalize('NFKC', str(text)).split()).casefold()
        terms = [normal(term) for term in query.split()]
        rows = []
        for row in self.by_id.values():
            text = normal(' '.join((str(row.get(k) or '') for k in ('race_id', 'event_key', 'name_cn', 'source_name', 'grade', 'surface', 'distance_m', 'venue'))))
            if all((term in text for term in terms)):
                rows.append(deepcopy(row))
        return {'races': rows[:limit], 'total': len(rows), 'source': deepcopy(self.source), 'message': '目录只帮助选择赛事；选择目录项不会生成胜场。原文名称不代表已核对国服译名。'}

def records_summary(records, entry_id, catalog=None):
    expected = _entry_id(entry_id)
    if records is None:
        return {'entry_id': expected, 'coverage': 'unknown', 'status': 'unknown', 'known_win_count': None, 'classified_win_count': 0, 'confirmed_event_count': 0, 'wins': [], 'unverified_count': 0, 'unclassified_count': 0, 'complete': False}
    value = normalize_records(records, expected)
    catalog = catalog or RaceCatalog.load()
    rows = []
    for row in value['wins']:
        reference = catalog.get(row['race_id'])
        rows.append({**row, 'classification': 'catalog_reference' if reference else 'unknown', 'event_key': reference['event_key'] if reference else None, 'race': reference})
    confirmed = [row for row in rows if row['status'] == 'confirmed']
    unverified = sum((row['status'] != 'confirmed' for row in rows))
    unknown = sum((row['status'] == 'confirmed' and row['race'] is None for row in rows))
    complete = value['coverage'] == 'complete' and (not unverified)
    known = len(confirmed) if confirmed or complete else None
    return {'entry_id': expected, 'coverage': value['coverage'], 'status': 'complete' if complete else 'partial' if rows else 'unknown', 'known_win_count': known, 'classified_win_count': sum((row['race'] is not None for row in confirmed)), 'confirmed_event_count': len({row['event_key'] for row in confirmed if row['event_key']}), 'wins': rows, 'unverified_count': unverified, 'unclassified_count': unknown, 'complete': complete, 'source': value['source']}

def common_wins(left_records, right_records, catalog=None, grades=None, *, left_entry_id=None, right_entry_id=None):
    """Return an observed intersection only; no compatibility bonus or probability."""
    catalog = catalog or RaceCatalog.load()
    summaries = []
    for value, identifier in ((left_records, left_entry_id), (right_records, right_entry_id)):
        identifier = identifier or (value.get('entry_id') if isinstance(value, dict) else None)
        if identifier is None and value is None:
            summaries.append({'coverage': 'unknown', 'wins': [], 'complete': False, 'unverified_count': 0, 'unclassified_count': 0})
        else:
            summaries.append(records_summary(value, identifier, catalog))
    if grades is not None:
        if not isinstance(grades, (list, tuple, set, frozenset)) or any((grade not in {'G1', 'G2', 'G3', 'OP'} for grade in grades)):
            raise ValueError('共同赛事等级筛选无效。')
        grades = set(grades)
    events = []
    unknown_ids = []
    for side in summaries:
        index = {}
        unknown = set()
        for row in side['wins']:
            if row['status'] != 'confirmed':
                continue
            if row['race'] is None:
                unknown.add(row['race_id'])
            elif grades is None or row['race'].get('grade') in grades:
                index.setdefault(row['event_key'], []).append(row)
        events.append(index)
        unknown_ids.append(unknown)
    common = []
    for key in sorted(events[0].keys() & events[1].keys()):
        left, right = (events[0][key], events[1][key])
        reference = deepcopy(left[0]['race'])
        common.append({'event_key': key, 'race': reference, 'left_race_ids': [row['race_id'] for row in left], 'right_race_ids': [row['race_id'] for row in right]})
    complete = all((side['complete'] and (not side['unclassified_count']) for side in summaries))
    return {'status': 'complete' if complete else 'partial' if any((side['wins'] for side in summaries)) else 'unknown', 'complete': complete, 'confirmed_common_count': len(common), 'common_races': common, 'count_status': 'exact' if complete else 'lower_bound', 'left_coverage': summaries[0]['coverage'], 'right_coverage': summaries[1]['coverage'], 'unclassified_shared_ids': sorted(unknown_ids[0] & unknown_ids[1]), 'unknown_catalog_count': sum((side['unclassified_count'] for side in summaries)), 'compatibility_bonus': None, 'inheritance_probability': None, 'message': '只比较绑定实际成品的已确认胜场；未知赛事不参与分级共同胜场，未读取不等于零胜场。'}
