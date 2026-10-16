"""Read-only local planning service; remote pages cannot call this interface."""
from pathlib import Path
from core import parent_library as store
from core.parent_planner import plan_materials
from core.parent_planner_catalog import ParentPlannerCatalog
ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'data/parent_library/planner_catalog.json'
FIELDS = {'parent_planner_catalog': set(), 'parent_planner_evaluate': {'targets', 'raw_targets', 'scope'}}

def _error(code, message):
    return {'ok': False, 'code': code, 'message': message, 'results': [{'code': code, 'message': message}]}

def _success(value):
    return {'ok': True, 'results': [value]}

def _int(value, default, lower, upper, message):
    value = default if value is None else value
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(message)
    return value

def _goals(request, catalog):
    chosen = request.get('targets', [])
    raw = request.get('raw_targets', [])
    if not isinstance(chosen, list) or not isinstance(raw, list) or len(chosen) + len(raw) > 200:
        raise ValueError('请检查目标清单，一次最多规划 200 个目标。')
    groups = {}

    def add(requirement, label, input_id, display_name):
        identity = ('key', requirement['factor_key']) if 'factor_key' in requirement else ('raw', requirement['raw_name'])
        if identity not in groups:
            groups[identity] = {'requirement': requirement, 'names': [], 'display_names': [], 'input_ids': []}
        group = groups[identity]
        for field, value in (('names', label), ('display_names', display_name), ('input_ids', input_id)):
            if value not in group[field]:
                group[field].append(value)
        for field in ('min_stars', 'priority'):
            group['requirement'][field] = max(group['requirement'][field], requirement[field])
    for row in chosen:
        store._fields(row, ('target_id', 'min_stars', 'priority'), '规划目标')
        identifier = store._text(row.get('target_id'))
        target = catalog.get(identifier)
        if target is None:
            raise ValueError('所选目标未匹配本地目录，请重新选择；网站新增技能可先核对原文。')
        requirement = {'factor_key': target['factor_key'], 'min_stars': _int(row.get('min_stars'), 1, 1, 9, '最低星数应为 1～9。'), 'priority': _int(row.get('priority'), 3, 1, 5, '优先级应为 1～5。')}
        add(requirement, target['name'], identifier, target.get('display_name') or target['name'])
    for row in raw:
        store._fields(row, ('name', 'min_stars', 'priority'), '原文目标')
        name = store._text(row.get('name'))
        requirement = {'raw_name': name, 'min_stars': _int(row.get('min_stars'), 1, 1, 9, '最低星数应为 1～9。'), 'priority': _int(row.get('priority'), 3, 1, 5, '优先级应为 1～5。')}
        add(requirement, name, 'raw:' + name, '原文：' + name)
    if not groups:
        raise ValueError('请先确认至少一个目标技能或因子。')
    return list(groups.values())

def _project(plan, groups):
    names = {}
    targets = []
    for target, group in zip(plan['targets'], groups):
        value = {**target, 'name': ' / '.join(group['display_names']), 'input_target_ids': group['input_ids']}
        names[target['target_id']] = value['name']
        targets.append(value)
    materials = []
    for candidate in plan['candidates']:
        if candidate['status'] == 'no_material':
            continue
        materials.append({**candidate, 'title': ' · '.join(filter(None, (candidate.get('character_name') or '未识别角色', candidate.get('variant_name')))), 'matched_targets': [names[c['target_id']] for c in candidate['checks'] if c['satisfied']], 'missing_targets': [names[c['target_id']] for c in candidate['checks'] if not c['satisfied']], 'uncertain_targets': [names[c['target_id']] for c in candidate['checks'] if not c['satisfied'] and c['uncertain']], 'completeness': candidate['label'], 'rank_explanation': candidate['rank_explanation']['text']})
    by_id = {target['target_id']: target for target in targets}
    missing = []
    for availability in plan['target_availability']:
        if availability['state'] == 'available':
            continue
        target = by_id[availability['target_id']]
        message = '已有部分素材，仍需补齐目标星数。' if availability['state'] == 'partial' else '资料尚未读全，需要核对候选因子。' if availability['state'] == 'unknown' else '本地库尚未发现所需素材。'
        missing.append({**availability, 'name': target['name'], 'status': availability['state'], 'message': message, 'sources': target['sources']})
    return {'targets': targets, 'materials': materials, 'missing_targets': missing, 'material_count': len(materials), 'library_entry_count': plan['candidate_count'], 'scope': plan['scope'], 'plan': plan, 'message': '本地素材匹配完成；星数表示已保存素材，来源仅供补种参考。'}

def dispatch(request, *, root=None, catalog_path=None):
    if not isinstance(request, dict) or request.get('action') not in FIELDS:
        return _error('parent_planner_invalid_request', '不支持这项种马规划操作。')
    action = request['action']
    if set(request) - FIELDS[action] - {'action', 'request_id', 'mode', 'endpoint'}:
        return _error('parent_planner_invalid_request', '规划仅接受目标清单与范围，不接受文件路径。')
    try:
        catalog = ParentPlannerCatalog.load(catalog_path or CATALOG)
        if action == 'parent_planner_catalog':
            metadata = getattr(catalog, 'metadata', {})
            return _success({'targets': catalog.targets(), 'version': metadata.get('dataset_version', 'local-v1'), 'scope_options': ['self', 'pedigree']})
        groups = _goals(request, catalog)
        library = store.load_library(root=root)
        plan = plan_materials(library, [g['requirement'] for g in groups], request.get('scope', 'pedigree'), source_resolver=catalog.sources)
        return _success(_project(plan, groups))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        message = str(error) if isinstance(error, ValueError) and any(('一' <= c <= '鿿' for c in str(error))) else '目标或本地资料暂未匹配，请重新选择后重试。'
        return _error('parent_planner_invalid_data', message)
    except OSError:
        return _error('parent_planner_storage_unavailable', '本地规划资料暂不可读，请检查安装资料与种马库。')

def main():
    import json
    import sys
    raw = sys.stdin.read(1024 * 1024 + 1)
    try:
        if len(raw) > 1024 * 1024:
            raise ValueError()
        result = dispatch(json.loads(raw))
    except (ValueError, TypeError):
        result = _error('parent_planner_invalid_request', '规划请求格式不正确或内容过大。')
    print(json.dumps(result, ensure_ascii=False))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
