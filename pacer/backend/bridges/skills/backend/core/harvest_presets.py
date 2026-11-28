"""Named harvest lists. User preferences select verified candidates, not taps."""
from __future__ import annotations
import copy
import json
import math
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / 'runs/harvest-presets.json'
CATALOG = ROOT / 'data/harvest/catalog.json'
SELECTION = dict(skill_list_id='', factor_profile_id='', rerolls=0, tp_budget=0, jewel_budget=0)

def empty_store():
    return dict(version=1, skills=[], factors=[], selection=dict(SELECTION))

def catalog(path=CATALOG):
    value = json.loads(Path(path).read_text(encoding='utf8'))
    if value.get('version') != 1 or not isinstance(value.get('skills'), list) or (not isinstance(value.get('factors'), list)):
        raise ValueError('harvest_catalog_invalid')
    return value

def _text(value, reason):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError(reason)
    return value.strip()

def validate_store(value, library=None):
    library = library or catalog()
    if not isinstance(value, dict) or set(value) != {'version', 'skills', 'factors', 'selection'} or value['version'] != 1:
        raise ValueError('harvest_presets_invalid')
    result = empty_store()
    skill_ids = {s['id'] for s in library['skills']}
    factor_keys = {f['key'] for f in library['factors']}
    for kind in ('skills', 'factors'):
        rows = value[kind]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError('harvest_presets_too_many')
        seen_ids, seen_names = (set(), set())
        for row in rows:
            fields = {'id', 'name', 'skill_ids'} if kind == 'skills' else {'id', 'name', 'weights', 'star_weights'}
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError('harvest_preset_fields_invalid')
            identity, name = (_text(row['id'], 'harvest_preset_id_invalid'), _text(row['name'], 'harvest_preset_name_invalid'))
            if not re.fullmatch('[A-Za-z0-9_-]{1,64}', identity) or identity in seen_ids or name.casefold() in seen_names:
                raise ValueError('harvest_preset_duplicate')
            seen_ids.add(identity)
            seen_names.add(name.casefold())
            if kind == 'skills':
                ids = row['skill_ids']
                if not isinstance(ids, list) or any((not isinstance(i, str) or i not in skill_ids for i in ids)) or len(ids) != len(set(ids)):
                    raise ValueError('harvest_skill_list_invalid')
                result[kind].append(dict(id=identity, name=name, skill_ids=list(ids)))
            else:
                stars = row['star_weights']
                if not isinstance(stars, list) or len(stars) != 3 or any((type(s) not in (int, float) or not math.isfinite(s) or (not 0 <= s <= 1000000) for s in stars)) or (stars != sorted(stars)):
                    raise ValueError('harvest_star_weights_invalid')
                weights = row['weights']
                if not isinstance(weights, list) or len(weights) > 2000:
                    raise ValueError('harvest_factor_weights_invalid')
                seen, clean = (set(), [])
                for rule in weights:
                    if not isinstance(rule, dict) or set(rule) != {'key', 'weight', 'min_stars'}:
                        raise ValueError('harvest_factor_rule_invalid')
                    key = _text(rule['key'], 'harvest_factor_key_invalid')
                    custom = key.startswith('custom:') and bool(key[7:].strip())
                    w, minimum = (rule['weight'], rule['min_stars'])
                    if key in seen or not (key in factor_keys or custom) or type(w) not in (int, float) or (not math.isfinite(w)) or (not 0 <= w <= 1000000) or (type(minimum) is not int) or (not 1 <= minimum <= 3):
                        raise ValueError('harvest_factor_rule_invalid')
                    seen.add(key)
                    clean.append(dict(key=key, weight=w, min_stars=minimum))
                result[kind].append(dict(id=identity, name=name, weights=clean, star_weights=list(stars)))
    selection = value['selection']
    if not isinstance(selection, dict) or set(selection) != set(SELECTION):
        raise ValueError('harvest_selection_invalid')
    for kind, field in [('skills', 'skill_list_id'), ('factors', 'factor_profile_id')]:
        if selection[field] not in {''} | {r['id'] for r in result[kind]}:
            raise ValueError('harvest_selected_preset_missing')
    for field, maximum in [('rerolls', 3), ('tp_budget', 9999), ('jewel_budget', 9999)]:
        if type(selection[field]) is not int or not 0 <= selection[field] <= maximum:
            raise ValueError('harvest_resource_limit_invalid')
    result['selection'] = dict(selection)
    return result

def load_store(path=STORE, library=None):
    path = Path(path)
    return validate_store(json.loads(path.read_text(encoding='utf8')), library) if path.exists() else empty_store()

def save_store(value, path=STORE, library=None):
    from uuid import uuid4
    result = validate_store(value, library)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return result

def snapshot(value):
    """Freeze at dispatch. Later editor changes cannot enlarge a running budget."""
    selected = value['selection']
    return dict(version=1, skill_list=copy.deepcopy(next((r for r in value['skills'] if r['id'] == selected['skill_list_id']), None)), factor_profile=copy.deepcopy(next((r for r in value['factors'] if r['id'] == selected['factor_profile_id']), None)), rerolls=selected['rerolls'], tp_budget=selected['tp_budget'], jewel_budget=selected['jewel_budget'])

def validate_snapshot(value, library=None):
    if not isinstance(value, dict) or set(value) != {'version', 'skill_list', 'factor_profile', 'rerolls', 'tp_budget', 'jewel_budget'}:
        raise ValueError('harvest_snapshot_invalid')
    store = dict(version=value['version'], skills=[value['skill_list']] if value['skill_list'] is not None else [], factors=[value['factor_profile']] if value['factor_profile'] is not None else [], selection=dict(skill_list_id=(value['skill_list'] or {}).get('id', ''), factor_profile_id=(value['factor_profile'] or {}).get('id', ''), **{k: value[k] for k in ('rerolls', 'tp_budget', 'jewel_budget')}))
    return snapshot(validate_store(store, library))

def prioritize_skills(profile, skill_list, planner_catalog):
    """Existing optimizer's lexicographic must targets outrank every filler."""
    from dataclasses import asdict
    from core.skill_planner.preferences import parse_skill_preference_profile
    if not skill_list:
        return profile
    ids = skill_list['skill_ids']
    usable = [i for i in ids if planner_catalog.maybe_get(i) is not None]
    priorities = {}
    for n, identity in enumerate(usable):
        pending = [identity]
        seen = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            row = planner_catalog.maybe_get(current)
            if row is None:
                continue
            priorities[current] = max(priorities.get(current, 0), len(usable) - n)
            pending.extend(row.relations.get('lower', []))
    rules = [dict(selector=asdict(r.selector), mode=r.mode, priority=r.priority, weight=r.weight) for r in profile.rules if not (r.selector.kind == 'skill_id' and r.selector.value in priorities)]
    rules += [dict(selector=dict(kind='skill_id', value=i), mode='must_buy_if_safe', priority=p) for i, p in priorities.items()]
    return parse_skill_preference_profile(dict(schema_version=profile.schema_version, profile_id=profile.profile_id, base_profile=profile.base_profile, reserve_skill_pt=profile.reserve_skill_pt, filler_policy=profile.filler_policy, max_batch_size=profile.max_batch_size, rules=rules))

def import_white_factors(skill_list, library, weight=10):
    by_id = {row['id']: row for row in library['skills']}
    result, seen = ([], set())
    for identity in skill_list['skill_ids']:
        key = by_id.get(identity, {}).get('factor_key')
        if key and key not in seen:
            seen.add(key)
            result.append(dict(key=key, weight=weight, min_stars=1))
    return result
