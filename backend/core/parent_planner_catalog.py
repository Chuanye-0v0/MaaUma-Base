"""Small static planning catalog; runtime needs no master/support libraries."""
from copy import deepcopy
import json
from pathlib import Path
SCHEMA = 'uma.parent_planner_catalog.v1'
DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / 'data/parent_library/planner_catalog.json'
SOURCE_NOTES = ['历史范围为凯旋门末期、UAF之前；未逐项确认国服当前开放情况。', '支援训练启发池与事件来自静态公开参考，未表示当前账号已拥有或一定获得。', '金技能目标可对应下位白因子；来源仅列直接普通/固有技能，不将金技能事件视为白因子保证。', '不计算因子生成、继承或事件发生概率。']

def _key(value):
    if not isinstance(value, str):
        return None
    kind, sep, name = value.partition(':')
    kind = 'red' if kind == 'pink' else kind
    return kind + ':' + name if sep and kind in {'blue', 'red', 'white', 'green'} and name else None

def _empty_sources(note=None):
    return {'skills': [], 'support_cards': [], 'trainees': [], 'source_notes': [*SOURCE_NOTES, *([note] if note else [])]}

class ParentPlannerCatalog:

    def __init__(self, payload):
        if payload.get('schema_version') != SCHEMA:
            raise ValueError('种马规划目录版本无效。')
        self._payload = deepcopy(payload)
        self._targets = {row['target_id']: row for row in self._payload['targets']}
        if len(self._targets) != len(self._payload['targets']):
            raise ValueError('种马规划目标编号重复。')

    @classmethod
    def load(cls, path=DEFAULT_CATALOG):
        return cls(json.loads(Path(path).read_text(encoding='utf-8-sig')))

    def targets(self):
        return deepcopy(list(self._targets.values()))

    def get(self, target_id):
        try:
            return deepcopy(self._targets[target_id])
        except (KeyError, TypeError):
            raise ValueError('规划目标未匹配目录，请明确选择标准因子或原文。') from None

    def sources(self, requirement):
        key = _key(requirement.get('factor_key'))
        if key is None and isinstance(requirement.get('raw_name'), str):
            name = requirement['raw_name'].strip()
            matches = {row['factor_key'] for row in self._targets.values() if row['kind'] == 'factor' and row['name'] == name}
            if len(matches) == 1:
                key = next(iter(matches))
        return deepcopy(self._payload['sources_by_factor'].get(key, _empty_sources('当前原文或因子未匹配静态来源目录；不推断来源。')))

def build_catalog(harvest, factor_catalog, skill_master, support_library):
    """Project explicitly bound identities; ambiguous references remain unmatched."""
    factors = {}
    for row in factor_catalog.get('factors', []):
        key = _key(row.get('key'))
        if key is None or key in factors:
            raise ValueError('标准因子目录编号无效或重复。')
        factors[key] = {'target_id': 'factor:' + key, 'kind': 'factor', 'name': row['name'], 'display_name': row['name'], 'aliases': [row['name'], key], 'factor_key': key, 'mapping': 'canonical_factor'}
        if row.get('is_scenario') is True:
            factors[key]['is_scenario'] = True
    master = {}
    by_id = {}
    for row in skill_master.get('skills', []):
        db_id = str(row['db_id'])
        if db_id in master or row['skill_id'] in by_id:
            raise ValueError('技能目录稳定编号重复。')
        master[db_id] = row
        by_id[row['skill_id']] = row
    references = {}
    for row in harvest.get('skills', []):
        key = _key(row.get('factor_key'))
        if key in factors:
            references.setdefault(str(row['db_id']), set()).add(key)

    def direct(row):
        keys = references.get(str(row['db_id']), set())
        if len(keys) > 1:
            return None
        key = next(iter(keys)) if keys else None
        skill_kind = row.get('skill_kind')
        rarity = row.get('rarity')
        if skill_kind in {'native_unique', 'inherited_unique'}:
            exact = 'green:' + row['name_zh_cn']
            if exact in factors and rarity in {'unique', 'normal'} and (key is None or key == exact):
                return exact
            return None
        if key and key.startswith('white:'):
            return key
        exact = 'white:' + row['name_zh_cn']
        return exact if rarity == 'normal' and exact in factors else None

    def lower_candidates(row, seen=()):
        identity = row['skill_id']
        if identity in seen:
            return set()
        result = set()
        for lower_id in row.get('relations', {}).get('lower', []):
            lower = by_id.get(lower_id)
            if lower is None:
                continue
            key = direct(lower)
            if key and key.startswith('white:') and (lower.get('rarity') == 'normal'):
                result.add(key)
            else:
                result.update(lower_candidates(lower, (*seen, identity)))
        return result
    mapped = {}
    skill_targets = []
    unmatched = []
    for db_id, row in sorted(master.items()):
        key = direct(row)
        lowers = lower_candidates(row)
        if key is None and len(lowers) == 1:
            key = next(iter(lowers))
            mapping = 'lower_relation'
        elif key is not None:
            mapping = 'native_unique_exact' if row.get('skill_kind') == 'native_unique' else 'inherited_unique_exact' if row.get('skill_kind') == 'inherited_unique' else 'lower_relation' if lowers == {key} else 'reference_lower_factor' if row.get('rarity') == 'gold' else 'confirmed_reference'
        else:
            unmatched.append({'db_id': db_id, 'name': row['name_zh_cn'], 'reason': 'ambiguous_lower_factors' if len(lowers) > 1 else 'no_confirmed_factor_mapping'})
            continue
        mapped[row['skill_id']] = key
        lower_mapping = mapping in {'lower_relation', 'reference_lower_factor'}
        name = row['name_zh_cn']
        factor_name = factors[key]['name']
        skill_targets.append({'target_id': 'skill:' + db_id, 'kind': 'skill', 'name': name, 'display_name': name + ' → ' + factor_name + '（下位因子）' if lower_mapping else name, 'aliases': sorted({str(v) for v in [name, row.get('name_ja'), row['skill_id'], db_id] if v}), 'factor_key': key, 'db_id': db_id, 'skill_id': row['skill_id'], 'mapping': mapping})
    sources = {key: _empty_sources() for key in factors}
    source_rows = {identity: by_id[identity] for identity, key in mapped.items() if key.startswith('white:') and by_id[identity].get('rarity') == 'normal' and (by_id[identity].get('skill_kind') not in {'evolved', 'negative'}) or (key.startswith('green:') and by_id[identity].get('skill_kind') in {'native_unique', 'inherited_unique'})}
    cards = {str(row['card_id']): row for row in support_library.get('cards', [])}
    card_sources = {key: {} for key in factors}
    trainee_sources = {key: {} for key in factors}
    for identity, row in source_rows.items():
        key = mapped[identity]
        sources[key]['skills'].append({'db_id': str(row['db_id']), 'name': row['name_zh_cn']})
        for origin in row.get('character_sources', []):
            card_id = str(origin['card_id'])
            marker = (card_id, origin.get('awakening_rank'))
            trainee_sources[key][marker] = {'card_id': card_id, 'character_id': str(origin.get('chara_id', '')), 'name': origin.get('name_zh_cn'), 'outfit': origin.get('outfit_zh_cn'), 'awakening_rank': origin.get('awakening_rank')}

    def add_card(key, card_id, identity, method, note=None):
        card = cards.get(str(card_id))
        if card is None:
            return
        result = card_sources[key].setdefault(str(card_id), {'card_id': str(card_id), 'name': card.get('name_zh_cn') or card.get('name_ja'), 'rarity': card.get('rarity'), 'type': card.get('type'), 'methods': set(), 'skill_ids': set(), 'event_notes': set()})
        result['methods'].add(method)
        result['skill_ids'].add(identity)
        if note:
            result['event_notes'].add(note)

    def linked_identity(skill):
        identity = skill.get('skill_id')
        row = source_rows.get(identity)
        return identity if skill.get('status') == 'linked' and row is not None and (str(skill.get('db_id')) == str(row['db_id'])) else None
    for card_id, card in cards.items():
        for skill in card.get('training_hint_skills', []):
            identity = linked_identity(skill)
            if identity is not None:
                add_card(mapped[identity], card_id, identity, '训练启发')
    operations = {'hint', 'grant_skill', 'random_hint', 'random_hint_related'}
    for event in support_library.get('events', []):
        title = event.get('titles', {}).get('zh_cn') or event.get('titles', {}).get('ja') or event['event_id']
        for option in event.get('options', []):
            for branch in option.get('branches', []):
                for skill in branch.get('skills', []):
                    identity = linked_identity(skill)
                    operation = skill.get('operation')
                    if identity is None or operation not in operations:
                        continue
                    random = operation.startswith('random') or event.get('kind') == 'shared_random'
                    method = '随机事件技能来源' if random else '事件技能来源'
                    note = f"{title}；选项{option.get('index')}；{by_id[identity]['name_zh_cn']}；静态条件来源，非获得保证"
                    add_card(mapped[identity], event.get('support_card_id'), identity, method, note)
    for key in factors:
        sources[key]['skills'] = sorted(sources[key]['skills'], key=lambda row: row['db_id'])
        for card in card_sources[key].values():
            sources[key]['support_cards'].append({**card, **{field: sorted(card[field]) for field in ('methods', 'skill_ids', 'event_notes')}})
        sources[key]['support_cards'].sort(key=lambda row: row['card_id'])
        sources[key]['trainees'] = sorted(trainee_sources[key].values(), key=lambda row: (row['card_id'], str(row['awakening_rank'])))
    return {'schema_version': SCHEMA, 'scope': 'larc_end_pre_uaf', 'cn_live_availability_verified': False, 'targets': [*factors.values(), *skill_targets], 'sources_by_factor': sources, 'unmatched_skills': unmatched, 'scenario_names': list(factor_catalog.get('scenario_names', [])), 'mapping_summary': {'factor_count': len(factors), 'skill_count': len(skill_targets), 'harvest_valid_reference_count': sum((str(row['db_id']) in references for row in harvest.get('skills', []))), 'unmatched_skill_count': len(unmatched)}, 'source_boundaries': {'harvest': deepcopy(harvest.get('boundary', {})), 'skills': deepcopy(skill_master.get('boundary', {})), 'support_scope': support_library.get('scope'), 'support_master_ref': support_library.get('boundary_master_ref')}, 'source_notes': list(SOURCE_NOTES)}
