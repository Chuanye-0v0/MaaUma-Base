"""Read-only local compatibility API. No device or account operations."""
import json
from pathlib import Path

from core import parent_library as store
from core.parent_compatibility import (CompatibilityCatalog, characters_from_roster,
    entry_summaries, evaluate_entries, evaluate_lineage, suggest_entries)


ROOT = Path(__file__).resolve().parents[1]
RELATIONS = ROOT / 'data/parent_library/compatibility_relations.json'
ROSTER = ROOT / 'core/autonomous_training/plant_roster.json'
PLANNER_CATALOG = ROOT / 'data/parent_library/planner_catalog.json'
FIELDS = {
    'parent_compatibility_catalog': set(),
    'parent_compatibility_evaluate': {'target_character_id', 'parent_entry_ids', 'lineage', 'targets', 'raw_targets'},
    'parent_compatibility_suggest': {'target_character_id', 'candidate_entry_ids', 'limit', 'targets', 'raw_targets'},
}


def _error(code, message):
    return {'ok': False, 'code': code, 'message': message,
            'results': [{'code': code, 'message': message}]}


def _success(value):
    return {'ok': True, 'results': [value]}


def _add_coverage(value, request, library, planner_catalog_path):
    targets, raw_targets = request.get('targets', []), request.get('raw_targets', [])
    if not targets and not raw_targets:
        return
    from core.parent_pair_coverage import pair_coverage
    from core.parent_planner_catalog import ParentPlannerCatalog
    catalog = ParentPlannerCatalog.load(planner_catalog_path or PLANNER_CATALOG)
    value['factor_coverage'] = pair_coverage(library, value['parent_entry_ids'], targets, raw_targets, catalog)


def dispatch(request, *, root=None, relations_path=None, roster_path=None, planner_catalog_path=None):
    if not isinstance(request, dict) or request.get('action') not in FIELDS:
        return _error('parent_compatibility_invalid_request', '不支持这项本地相性操作。')
    action = request['action']
    if set(request) - FIELDS[action] - {'action', 'request_id', 'mode', 'endpoint'}:
        return _error('parent_compatibility_invalid_request', '相性规划只接受角色和本地记录编号，不接受文件路径。')
    try:
        catalog = CompatibilityCatalog.load(relations_path or RELATIONS)
        roster = json.loads(Path(roster_path or ROSTER).read_text(encoding='utf-8-sig'))
        characters = characters_from_roster(roster, catalog)
        # Explicit manual nodes do not depend on the user's saved library.
        if action == 'parent_compatibility_evaluate' and 'lineage' in request:
            if 'parent_entry_ids' in request:
                raise ValueError('请选择本地成品组合或手动七节点中的一种方式。')
            return _success(evaluate_lineage(request.get('target_character_id'),
                                            request['lineage'], catalog, characters))
        # An absent store returns an empty in-memory library; it is never created.
        library = store.load_library(root=root)
        entries = entry_summaries(library, characters, catalog)
        if action == 'parent_compatibility_catalog':
            return _success({'characters': characters, 'entries': entries, **catalog.metadata()})
        target = request.get('target_character_id')
        if action == 'parent_compatibility_evaluate':
            if ('parent_entry_ids' in request) == ('lineage' in request):
                raise ValueError('请选择本地成品组合或手动七节点中的一种方式。')
            value = evaluate_entries(target, request['parent_entry_ids'], entries, catalog, characters)
            _add_coverage(value, request, library, planner_catalog_path)
            return _success(value)
        value = suggest_entries(target, entries, catalog, characters,
            candidate_entry_ids=request.get('candidate_entry_ids'), limit=request.get('limit', 20))
        for suggestion in value['suggestions']:
            _add_coverage(suggestion, request, library, planner_catalog_path)
        return _success(value)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        message = str(error) if isinstance(error, ValueError) and any('\u4e00' <= c <= '\u9fff' for c in str(error)) else '相性资料暂未匹配，请检查角色与本地记录后重试。'
        return _error('parent_compatibility_invalid_data', message)
    except OSError:
        return _error('parent_compatibility_storage_unavailable', '本地相性资料暂不可读，请检查安装资料。')


def main():
    import sys
    raw = sys.stdin.read(1024 * 1024 + 1)
    try:
        if len(raw) > 1024 * 1024:
            raise ValueError()
        result = dispatch(json.loads(raw))
    except (ValueError, TypeError):
        result = _error('parent_compatibility_invalid_request', '相性请求格式不正确或内容过大。')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
