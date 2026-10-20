"""Offline skill reference using the existing complete CN catalog and exact sources."""
from copy import deepcopy
import json
from pathlib import Path
import unicodedata

from core.parent_planner_catalog import ParentPlannerCatalog

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / 'data/skills/cn-20260916/cn_skill_master.json'
SOURCES = ROOT / 'data/parent_library/skill_sources.json'


def _normal(text):
    return ''.join(c.casefold() for c in unicodedata.normalize('NFKC', str(text)) if c.isalnum())


class SkillReference:
    def __init__(self, master, sources, planner):
        self.version = master.get('dataset_version', 'cn-20260916')
        self.source_version = sources.get('data_version', 'larc_end_pre_uaf')
        self.skills = master['skills']
        self.by_id = {str(row['skill_id']): row for row in self.skills}
        self.source_rows = sources.get('skills', {})
        self.targets = {str(row['db_id']): row for row in planner.targets()
                        if row.get('kind') == 'skill' and row.get('db_id')}
        self.planner = planner
        self.index = [(row, _normal(' '.join(str(row.get(k) or '') for k in
                       ('skill_id', 'db_id', 'name_zh_cn', 'name_ja', 'description', 'trigger'))))
                      for row in self.skills]

    @classmethod
    def load(cls, *, master_path=None, sources_path=None, planner=None):
        master = json.loads(Path(master_path or MASTER).read_text('utf-8-sig'))
        sources = json.loads(Path(sources_path or SOURCES).read_text('utf-8-sig'))
        return cls(master, sources, planner or ParentPlannerCatalog.load())

    def _row(self, row):
        target = self.targets.get(str(row.get('db_id', '')))
        return {'id': row['skill_id'], 'db_id': str(row.get('db_id', '')),
                'name': row['name_zh_cn'], 'rarity': row.get('rarity', ''),
                'type': row.get('type', ''), 'description': row.get('description', ''),
                'mapped_target_id': target['target_id'] if target else None}

    def search(self, query='', limit=30):
        if not isinstance(query, str) or len(query) > 240:
            raise ValueError('请填写简短的技能名称、说明或编号。')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('每次显示数量应为 1～100。')
        terms = [_normal(term) for term in query.split() if _normal(term)]
        matches = [row for row, text in self.index if all(term in text for term in terms)]
        exact = _normal(query)
        matches.sort(key=lambda row: (0 if exact and exact in {_normal(row['name_zh_cn']),
                     _normal(row.get('db_id', '')), _normal(row['skill_id'])} else 1,
                     row['name_zh_cn'], row['skill_id']))
        return {'skills': [self._row(row) for row in matches[:limit]], 'total': len(matches),
                'data_version': self.version, 'source_version': self.source_version,
                'message': f'本地技能 {len(self.skills)} 项，来源资料按各自版本展示。'}

    def detail(self, skill_id):
        if not isinstance(skill_id, str) or skill_id not in self.by_id:
            raise ValueError('请从技能查询结果中选择一个技能。')
        row = self.by_id[skill_id]
        target = self.targets.get(str(row.get('db_id', '')))
        source = deepcopy(self.source_rows.get(str(row.get('db_id', '')), {}))
        source.setdefault('trainees', [])
        source.setdefault('support_cards', [])
        source.setdefault('source_notes', [])
        if not source['support_cards']:
            source['source_notes'].append('当前静态目录尚未整理到该技能的支援卡来源。')
        source['source_notes'].append('来源资料范围：凯旋门末期、UAF之前；不表示账号持有或技能必得。')
        own_cost = row.get('upgrade_cost')
        if own_cost is None:
            own_cost = row.get('base_cost')
        value = {**self._row(row), 'trigger': row.get('trigger', ''),
                 'precondition': row.get('precondition', ''), 'cost': own_cost,
                 'total_cost': row.get('total_cost'), 'conditions': deepcopy(row.get('conditions', {})),
                 'duration_seconds': row.get('duration_seconds'),
                 'effects': deepcopy(row.get('effects', [])),
                 'source_url': row.get('source_url', ''), 'relations': deepcopy(row.get('relations', {}))}
        result = {'skill': value, 'sources': source, 'data_version': self.version,
                  'source_version': self.source_version,
                  'mapped_target_id': target['target_id'] if target else None,
                  'mapped_target_name': target.get('display_name') if target else None}
        # Gold/evolved skills may map to a lower white factor. Keep those sources
        # separate: a card giving the lower skill does not directly give the gold.
        if target:
            result['factor_sources'] = self.planner.sources({'factor_key': target['factor_key']})
        return result
