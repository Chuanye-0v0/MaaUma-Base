"""Public/private panel adapter for the same local parent library.

No Pacer authorization, device calls, file pickers or arbitrary request paths.
The UI owns import/export files; this adapter exchanges JSON values only.
"""
from pathlib import Path
from core import parent_library as store
ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'data/parent_library/factor_catalog.json'
SCAN_ACTIONS = frozenset({'parent_library_scan_start', 'parent_library_scan_status', 'parent_library_scan_stop'})
FIELDS = {'parent_library_list': {'query', 'requirements', 'scope', 'match_mode'}, 'parent_library_get': {'entry_id', 'requirements', 'scope', 'match_mode'}, 'parent_library_import': {'library', 'expected_revision'}, 'parent_library_export': set(), 'parent_library_planner_snapshot': {'requirements', 'scope', 'match_mode'}, 'parent_library_correct': {'entry_id', 'node', 'factor_index', 'changes', 'note', 'expected_revision'}, 'parent_library_catalog': set()}

def supports(action):
    return action in FIELDS or action in SCAN_ACTIONS

def _success(payload):
    return {'ok': True, 'results': [payload]}

def _error(code, message):
    return {'ok': False, 'code': code, 'message': message, 'error': message, 'results': [{'code': code, 'message': message, 'error': {'code': code, 'message': message}}]}

def _metadata(library):
    return {key: library[key] for key in ('schema_version', 'library_id', 'name', 'revision')}

def _scan_status(root=None):
    from desktop.parent_library_jobs import scan_status
    return scan_status(root)

def dispatch_scan(request, *, root=None):
    from desktop.parent_library_jobs import start_scan, stop_scan, scan_status
    allowed = {'action', 'request_id', 'mode', 'endpoint', 'adb_path', 'scan_id', 'limit', 'owner_pid'}
    if set(request) - allowed:
        return _error('parent_library_invalid_request', '扫描只接受当前设备和数量参数。')
    try:
        action = request['action']
        if action == 'parent_library_scan_start':
            value = start_scan(request, root)
        elif action == 'parent_library_scan_stop':
            value = stop_scan(request, root)
        else:
            value = scan_status(root)
        return _success({'scan_status': value})
    except ValueError as error:
        return _error('parent_library_scan_unavailable', str(error))
    except OSError:
        return _error('parent_library_scan_unavailable', '无法保存扫描任务，请检查本机目录是否可写。')

def dispatch(request, *, root=None, catalog_path=None):
    """Return the shared panel result envelope."""
    if not isinstance(request, dict) or not isinstance(request.get('action'), str):
        return _error('parent_library_invalid_request', '种马库请求格式不正确。')
    action = request['action']
    if action in SCAN_ACTIONS:
        return dispatch_scan(request, root=root)
    if action not in FIELDS:
        return _error('parent_library_action_unknown', '不支持这项种马库操作。')
    allowed = FIELDS[action] | {'action', 'request_id', 'mode', 'endpoint'}
    if set(request) - allowed:
        return _error('parent_library_invalid_request', '种马库仅接受对应操作的参数和 JSON 内容，不接受文件路径。')
    try:
        if action == 'parent_library_catalog':
            path = catalog_path if catalog_path is not None else CATALOG
            factors = store.catalog_choices(path)
            return _success({'factors': factors, 'count': len(factors), 'scenario_names': store.catalog_scenario_names(path)})
        library = store.load_library(root=root)
        if action == 'parent_library_list':
            return _success({'library': _metadata(library), 'version': library['revision'], 'entries': store.list_entries(library, request.get('query', ''), request.get('requirements'), request.get('scope', 'self'), request.get('match_mode', 'all')), 'scan_status': _scan_status(root)})
        if action == 'parent_library_get':
            from desktop.parent_library_images import entry_portraits
            entry = store.get_entry(library, request.get('entry_id'))
            return _success({'library': _metadata(library), 'version': library['revision'], 'entry': entry, 'portraits': entry_portraits(entry, root=root), 'assessment': store.assess_entry(entry, request.get('requirements'), request.get('scope', 'self'), request.get('match_mode', 'all')), 'duplicate_candidates': store.duplicate_candidates(library, entry['entry_id'])})
        if action == 'parent_library_export':
            return _success({'library': store.export_library(library)})
        if action == 'parent_library_planner_snapshot':
            return _success({'snapshot': store.planner_snapshot(library, request.get('requirements'), request.get('scope', 'self'), request.get('match_mode', 'all'))})
        if type(request.get('expected_revision')) is not int or request['expected_revision'] < 0:
            return _error('parent_library_revision_required', '修改种马库前请先读取当前版本，再提交对应版本号。')
        if action == 'parent_library_import':
            updated = store.import_library(request.get('library'), catalog_path=catalog_path)
        else:
            updated = store.correct_factor(library, request.get('entry_id'), request.get('node'), request.get('factor_index'), request.get('changes'), note=request.get('note'))
        saved = store.save_library(updated, root=root, expected_revision=request['expected_revision'])
        payload = {'library': saved, 'version': saved['revision']}
        if action == 'parent_library_correct':
            payload['entry'] = store.get_entry(saved, request['entry_id'])
        return _success(payload)
    except store.LibraryConflict:
        return _error('parent_library_revision_conflict', '种马库已被另一面板修改或正在保存，请重新读取后再提交。')
    except OSError:
        if action == 'parent_library_catalog':
            return _error('parent_library_catalog_unavailable', '因子目录暂不可用，未识别名称仍可保留为待校对。')
        return _error('parent_library_storage_unavailable', '本地种马库暂时无法读写，请检查存储位置是否可用。')
    except (ValueError, KeyError, TypeError, AttributeError):
        return _error('parent_library_invalid_data', '种马库数据或筛选条件不正确，请刷新后检查条目、因子和版本。')

def main():
    import json
    import sys
    raw = sys.stdin.read(32 * 1024 * 1024 + 1)
    try:
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError()
        value = dispatch(json.loads(raw))
    except (ValueError, TypeError):
        value = _error('parent_library_invalid_request', '种马库请求格式不正确或内容过大。')
    print(json.dumps(value, ensure_ascii=False))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
