"""Local named workflow configurations. No host, device or network dependency."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

from core.career_workflow import ACE_CYCLE_MODE, SS_SIEVE_MODE, normalize, templates

SCHEMA = 'uma.career_workflow_library.v1'
LIBRARY = 'runs/standalone-panel/career-workflow-library.json'
LEGACY_WORKFLOW = 'runs/standalone-panel/career-workflow.json'


def deepmerge(base, override):
    """Objects merge recursively; lists and scalar values are complete snapshots."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return deepcopy(override)
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = deepmerge(result[key], value) if key in result else deepcopy(value)
    return result


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + '不能为空。')
    return value


def _selection(library, mode_id=None, profile_id=None):
    mode_id = library['selected_mode_id'] if mode_id is None else mode_id
    mode = next((m for m in library['modes'] if m['id'] == mode_id), None)
    if mode is None:
        raise ValueError('工作流模式不存在。')
    profile_id = mode['selected_profile_id'] if profile_id is None else profile_id
    profile = next((p for p in mode['profiles'] if p['id'] == profile_id), None)
    if profile is None:
        raise ValueError('该模式的命名配置不存在。')
    return mode, profile


def _resolved(mode, profile):
    legacy = profile.get('document', {})
    document = deepmerge(mode['document'], {k: v for k, v in legacy.items() if k not in {'run_graph', 'policies'}})
    graph = deepcopy(mode['document'].get('run_graph'))
    parameters = profile.get('node_parameters', {})
    if not isinstance(parameters, dict):
        raise ValueError('节点参数应按节点标识保存为对象。')
    if isinstance(graph, dict) and isinstance(graph.get('nodes'), list):
        known = {node.get('id') for node in graph['nodes'] if isinstance(node, dict)}
        legacy_graph = legacy.get('run_graph', {})
        legacy_nodes = legacy_graph.get('nodes', []) if isinstance(legacy_graph, dict) else []
        legacy_parameters = {node['id']: {k: deepcopy(v) for k, v in node.items() if k not in {'id', 'kind', 'policy'}}
            for node in legacy_nodes if isinstance(node, dict) and node.get('id') in known}
        for node in graph['nodes']:
            if not isinstance(node, dict):
                continue
            values = parameters.get(node.get('id'), {})
            if not isinstance(values, dict) or any(key in values for key in ('id', 'kind', 'policy')):
                raise ValueError('配置只能覆盖节点参数，不能改变节点标识、类型或策略引用。')
            node.update(deepcopy(legacy_parameters.get(node.get('id'), {})))
            node.update(deepcopy(values))
    document['run_graph'] = graph
    policies = deepcopy(mode['document'].get('policies', {}))
    if not isinstance(policies, dict) or not isinstance(legacy.get('policies', {}), dict):
        raise ValueError('工作流需要policies参数对象。')
    # A provided policy ID is a complete immutable parameter snapshot. Merging
    # inside it could revive intentionally deleted workflow/AI/resource fields.
    policies.update(deepcopy(legacy.get('policies', {})))
    policies.update(deepcopy(profile['policies']))
    document['policies'] = policies
    return normalize(document)


def normalize_library(value):
    """Validate executable content while retaining every unknown metadata field."""
    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA:
        raise ValueError('本地工作流配置库版本不受支持。')
    result = deepcopy(value)
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise ValueError('本地工作流配置库需要有效JSON数据。') from error
    if 'revision' in result and (type(result['revision']) is not int or result['revision'] < 0):
        raise ValueError('配置库修订号应为非负整数。')
    modes = result.get('modes')
    if not isinstance(modes, list) or not modes:
        raise ValueError('配置库至少需要一个工作流模式。')
    mode_ids = set()
    for mode in modes:
        if not isinstance(mode, dict):
            raise ValueError('工作流模式应为对象。')
        identifier = _text(mode.get('id'), '模式标识')
        _text(mode.get('name'), '模式名称')
        if identifier in mode_ids:
            raise ValueError('工作流模式标识重复。')
        mode_ids.add(identifier)
        if not isinstance(mode.get('document'), dict):
            raise ValueError('工作流模式需要执行文档。')
        profiles = mode.get('profiles')
        if not isinstance(profiles, list) or not profiles:
            raise ValueError('每个模式至少需要一套命名配置。')
        profile_ids = set()
        for profile in profiles:
            if not isinstance(profile, dict):
                raise ValueError('命名配置应为对象。')
            profile_id = _text(profile.get('id'), '配置标识')
            _text(profile.get('name'), '配置名称')
            if profile_id in profile_ids:
                raise ValueError('同一模式的配置标识重复。')
            profile_ids.add(profile_id)
            if not isinstance(profile.get('policies'), dict):
                raise ValueError('命名配置需要policies参数快照。')
            if 'document' in profile and not isinstance(profile['document'], dict):
                raise ValueError('配置的执行文档覆盖应为对象。')
            # Materialize inherited policy values once, so future edits to the
            # mode defaults cannot change an already saved profile's options.
            effective = _resolved(mode, profile)
            profile['policies'] = deepcopy(effective['policies'])
        mode.setdefault('selected_profile_id', profiles[0]['id'])
        if mode['selected_profile_id'] not in profile_ids:
            raise ValueError('所选命名配置不存在。')
    result.setdefault('selected_mode_id', modes[0]['id'])
    if result['selected_mode_id'] not in mode_ids:
        raise ValueError('所选工作流模式不存在。')
    return result


def _read(path):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise ValueError('本地工作流配置或旧配置无法读取：' + path.name) from error
    if not isinstance(value, dict):
        raise ValueError('本地工作流配置或旧配置应为对象：' + path.name)
    return value


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class LibraryStore:
    """Only .save writes. .load/.resolve/.payload never initialize devices/files."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / LIBRARY

    def _legacy_slots(self, phase):
        panel = self.path.parent
        source = panel / ('career-settings.json' if phase == 'career' else 'settings.json')
        value = _read(source)
        if value is None:
            return {}, None
        expected = 'uma.career_panel_presets.v1' if phase == 'career' else 'uma.cut_panel_presets.v1'
        if value.get('schema_version') != expected or not isinstance(value.get('slots'), dict):
            raise ValueError('旧命名配置格式不正确：' + source.name)
        preferences = _read(panel / 'decision-preferences.json') or {}
        ai = _read(panel / 'ai-options.json') or {}
        rows = {}
        for identifier, entry in value['slots'].items():
            if identifier not in {'1', '2', '3'} or not isinstance(entry, dict) or not isinstance(entry.get('settings'), dict):
                raise ValueError('旧配置槽格式不正确：' + source.name)
            options = deepcopy(entry['settings'])
            nested = options.setdefault('cut', {}) if phase == 'cut' else options
            if not isinstance(nested, dict):
                raise ValueError('旧刷切参数应为对象。')
            if identifier in preferences:
                nested['decision_preferences'] = deepcopy(preferences[identifier])
            if identifier in ai:
                nested['ai_options'] = deepcopy(ai[identifier])
            rows[identifier] = dict(options=options, original=deepcopy(entry), source=str(source))
        selected = str(value.get('selected_id', 1))
        return rows, selected if selected in rows else None

    def _initial(self, initial_policies=None):
        if initial_policies is not None and not isinstance(initial_policies, dict):
            raise ValueError('初始policies快照应为对象。')
        documents = {d['template_id']: d for d in templates()}
        cut, selected_cut = self._legacy_slots('cut')
        career, selected_career = self._legacy_slots('career')
        modes = []
        for identifier, name, phase in (
                ('direct_career', '自动育成', 'career'),
                ('cut_preserve', '刷切并保留', 'cut'),
                ('cut_then_career', '出切先停，确认后同局养完', 'both'),
                (SS_SIEVE_MODE, 'SS筛局循环', 'career_cycle'),
                (ACE_CYCLE_MODE, '大赛成品马连开循环', 'career_cycle')):
            document = deepcopy(documents[identifier])
            if phase == 'career_cycle':
                # Never overwrite finite cycle budgets with cut/initial cut_options.
                career_initial = {
                    pid: {'career_options': supplied['career_options']}
                    for pid, supplied in (initial_policies or {}).items()
                    if isinstance(supplied, dict) and 'career_options' in supplied
                }
                document['policies'] = deepmerge(document['policies'], career_initial)
                for policy_id, supplied in career_initial.items():
                    document['policies'][policy_id]['career_options'] = deepcopy(supplied['career_options'])
                source_rows = career
            else:
                document['policies'] = deepmerge(document['policies'], initial_policies or {})
                # Explicit phase options are saved snapshots, including a missing
                # policy. Do not retain the new-template null inside an old save.
                for policy_id, supplied in (initial_policies or {}).items():
                    if isinstance(supplied, dict):
                        for option in ('career_options', 'cut_options'):
                            if option in supplied:
                                document['policies'][policy_id][option] = deepcopy(supplied[option])
                source_rows = career if phase == 'career' else cut
                if phase == 'both':
                    source_rows = {key: None for key in sorted(set(cut) | set(career))}
            profiles = []
            for slot in sorted(source_rows):
                policies = deepcopy(document['policies'])
                default = policies.setdefault('default', {})
                origins = {}
                if phase in {'cut', 'both'} and slot in cut:
                    default['cut_options'] = deepcopy(cut[slot]['options'])
                    origins['cut'] = {k: deepcopy(v) for k, v in cut[slot].items() if k != 'options'}
                if phase in {'career', 'both', 'career_cycle'} and slot in career:
                    default['career_options'] = deepcopy(career[slot]['options'])
                    origins['career'] = {k: deepcopy(v) for k, v in career[slot].items() if k != 'options'}
                profiles.append({'id': 'legacy-' + slot, 'name': '原配置' + slot,
                    'policies': policies, 'node_parameters': {}, 'legacy_slots': origins})
            if not profiles:
                profiles = [{'id': 'default', 'name': '默认配置', 'policies': deepcopy(document['policies']), 'node_parameters': {}}]
            selected = selected_career if phase in {'career', 'career_cycle'} else selected_cut
            selected_id = 'legacy-' + selected if selected is not None else profiles[0]['id']
            if selected_id not in {p['id'] for p in profiles}:
                selected_id = profiles[0]['id']
            modes.append({'id': identifier, 'name': name, 'document': document,
                'profiles': profiles, 'selected_profile_id': selected_id})
        selected_mode = 'cut_preserve' if (cut and not career) else 'direct_career'
        legacy = _read(self.root / LEGACY_WORKFLOW)
        if legacy is not None:
            # Keep its complete graph, editor layout and extension fields. It
            # remains a separate mode instead of replacing a default template.
            document = normalize(legacy)
            label = document.get('label')
            modes.append({'id': 'legacy_workflow', 'name': label if isinstance(label, str) and label.strip() else '原已保存工作流',
                'document': deepcopy(document), 'profiles': [{'id': 'imported', 'name': '原工作流配置',
                    'document': deepcopy(document), 'policies': deepcopy(document['policies'])}],
                'selected_profile_id': 'imported', 'legacy_source': str(self.root / LEGACY_WORKFLOW)})
            selected_mode = 'legacy_workflow'
        return normalize_library({'schema_version': SCHEMA, 'revision': 0, 'modes': modes, 'selected_mode_id': selected_mode})

    def load(self, initial_policies=None):
        value = _read(self.path)
        if value is None:
            return self._initial(initial_policies)
        library = normalize_library(value)
        library.setdefault('revision', 0)
        tpls = {row['id']: row for row in templates()}
        direct_career_mode = next((m for m in library['modes'] if m['id'] == 'direct_career'), None)
        for missing_mode_id in (SS_SIEVE_MODE, ACE_CYCLE_MODE):
            if any(mode['id'] == missing_mode_id for mode in library['modes']):
                continue
            # A read exposes newly added cycle modes without overwriting disk,
            # copying career_options from direct_career ONLY (never cut_options
            # from cut_preserve, preventing unlimited-carrot leaks).
            document = deepcopy(tpls[missing_mode_id])
            policy = document['policies']['default']
            profile_name = '默认配置'
            if direct_career_mode is not None:
                direct_profile = next(
                    (p for p in direct_career_mode['profiles'] if p['id'] == direct_career_mode['selected_profile_id']),
                    direct_career_mode['profiles'][0],
                )
                existing = _resolved(direct_career_mode, direct_profile)['policies']['default']
                if 'career_options' in existing:
                    policy['career_options'] = deepcopy(existing['career_options'])
                profile_name = direct_profile['name']
            library['modes'].append({'id': missing_mode_id, 'name': document['label'], 'document': document,
                'profiles': [{'id': 'default', 'name': profile_name, 'policies': deepcopy(document['policies']),
                              'node_parameters': {}}], 'selected_profile_id': 'default'})
        return library

    def save(self, library):
        existing = _read(self.path)
        disk_rev = existing.get('revision', 0) if isinstance(existing, dict) and type(existing.get('revision')) is int else 0
        if existing is not None and isinstance(library, dict) and 'revision' in library:
            client_rev = library.get('revision')
            if type(client_rev) is not int or client_rev != disk_rev:
                raise ValueError(f'配置库已被其他窗口更新（磁盘 v{disk_rev}，提交 v{client_rev}），请刷新后再保存。')
        value = normalize_library(library)
        value['revision'] = disk_rev + 1
        _atomic_json(self.path, value)
        return deepcopy(value)

    def resolve(self, mode_id=None, profile_id=None, *, library=None, initial_policies=None):
        value = self.load(initial_policies) if library is None else normalize_library(library)
        return _resolved(*_selection(value, mode_id, profile_id))

    def payload(self, mode_id=None, profile_id=None, *, initial_policies=None):
        value = self.load(initial_policies)
        mode, profile = _selection(value, mode_id, profile_id)
        return {'library': value, 'document': _resolved(mode, profile),
                'mode_id': mode['id'], 'profile_id': profile['id']}
