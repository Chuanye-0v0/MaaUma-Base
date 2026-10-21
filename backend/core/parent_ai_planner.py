"""Explicit advisory planning: bounded facts out, locally rechecked drafts back."""
from copy import deepcopy
import json
import re
import uuid
from core.parent_compatibility import character_id
from core.parent_aptitude import red_node_summary
from core.parent_local_route import normalize_local_route
from core.parent_race_records import records_summary
SCHEMA = 'uma.parent_ai_context.v1'
MAX_MATERIALS = 120
MAX_CONTEXT_BYTES = 192 * 1024
_LOCAL_PATH = re.compile('(?:[A-Za-z]:[\\\\/]|\\\\\\\\)[^\\s，。；]*|(?<!\\w)/(?:Users|home|tmp|mnt|var|etc|private)/[^\\s，。；]*')
_SECRET = re.compile('(?i)Bearer\\s+[^\\s，。；]+|\\bsk-[A-Za-z0-9_-]{6,}|\\b(?:api[_-]?key|token|authorization|password)\\s*[:=]\\s*[^\\s，。；]+')
_PROBABILITY = re.compile('\\d+(?:\\.\\d+)?\\s*[%％]|百分之\\s*[零一二三四五六七八九十百\\d.]+|保证继承|必定继承')

class AIPlanningError(ValueError):

    def __init__(self, code, message):
        self.code, self.message = (code, message)
        super().__init__(code)

def safe_text(value, limit=500, *, private_values=(), model_text=False):
    if not isinstance(value, str):
        return ''
    value = re.sub('(?is)<(?:think|analysis)\\b[^>]*>.*?(?:</(?:think|analysis)>|\\Z)', '', value)
    value = _LOCAL_PATH.sub('[本地路径已隐去]', value)
    value = _SECRET.sub('[凭据已隐去]', value)
    for text in sorted({text for text in private_values if isinstance(text, str) and len(text) >= 3}, key=len, reverse=True):
        value = value.replace(text, '[账号资料已隐去]')
    if model_text:
        value = _PROBABILITY.sub('[未经本地验证的概率表述未采用]', value)
    return ''.join((char for char in value if char in '\n\t' or ord(char) >= 32)).strip()[:limit]

def _identifier(value):
    try:
        return character_id(value)
    except ValueError:
        return None

def _node_facts(node, allowed_factors, race_catalog, expected_entry_id=None, target_factor='red:泥地'):
    if not isinstance(node, dict):
        return {'character_id': None, 'factors': [], 'factor_coverage': 'unknown', 'factor_scope': [], 'wins_coverage': 'unknown', 'wins_complete': False, 'unverified_wins': 0, 'wins': []}
    factors = []
    seen = set()
    for row in node.get('factors', []):
        key, stars = (row.get('factor_key'), row.get('stars'))
        identity = (key, stars)
        if key not in allowed_factors or type(stars) is not int or (not 1 <= stars <= 3) or (identity in seen):
            continue
        seen.add(identity)
        factors.append({'factor_key': key, 'stars': stars})
    values = {}
    for row in factors:
        values.setdefault(row['factor_key'], set()).add(row['stars'])
    conflicts = {key for key, stars in values.items() if len(stars) > 1}
    red_rows = [{'kind': 'red', 'factor_key': row['factor_key'], 'raw_name': '', 'stars': row['stars']} for row in factors if row['factor_key'].startswith('red:')]
    if red_rows:
        probe = red_node_summary({'status': 'partial', 'scope': ['red'], 'factors': red_rows}, target_factor)
        if probe['conflict']:
            conflicts.update((row['factor_key'] for row in red_rows))
    factors = [row for row in factors if row['factor_key'] not in conflicts]
    factors.sort(key=lambda row: (0 if row['factor_key'] == target_factor else 1 if row['factor_key'].startswith('blue:') else 2 if row['factor_key'].startswith('red:') else 3 if row['factor_key'].startswith('green:') else 4, row['factor_key']))
    records = node.get('race_records') or {}
    identifier = expected_entry_id or node.get('entry_id')
    summary = records_summary(records or None, identifier, race_catalog) if identifier is not None else {'status': 'unknown', 'coverage': 'unknown', 'complete': False, 'unverified_count': 0}
    wins, unclassified, events = ([], 0, set())
    for row in records.get('wins', []):
        if row.get('status') != 'confirmed':
            continue
        reference = race_catalog.get(row.get('race_id'))
        if reference is None:
            unclassified += 1
        elif reference['event_key'] not in events:
            events.add(reference['event_key'])
            wins.append({key: reference.get(key) for key in ('event_key', 'grade')})
    variant = node.get('variant_id')
    complete_wins = summary['complete'] and len(wins) <= 20 and (not unclassified)
    return {'character_id': _identifier(node.get('character_id')), 'variant_id': variant if isinstance(variant, str) and variant.isascii() and variant.isdigit() else None, 'factors': factors[:8], 'factor_coverage': node.get('status', 'partial'), 'factor_conflicts': sorted(conflicts), 'factors_omitted': max(0, len(factors) - 8), 'unmapped_factor_rows': sum((row.get('factor_key') not in allowed_factors for row in node.get('factors', []))), 'factor_scope': [kind for kind in node.get('scope', []) if kind in {'blue', 'red', 'white', 'green'}], 'wins_coverage': 'partial' if summary['status'] == 'complete' and (not complete_wins) else summary['status'], 'wins_declared_coverage': summary['coverage'], 'wins_omitted': max(0, len(wins) - 20), 'wins_complete': complete_wins, 'unverified_wins': summary['unverified_count'], 'wins': wins[:20], 'unclassified_confirmed_wins': unclassified}

def build_context(library, projected_entries, characters, target_character_id, local_route, request_text, *, factor_keys, race_catalog, private_values=(), local_goal_assessment=None, aptitude_catalog=None):
    """Keep account/record identity maps local; never forward raw store records."""
    if not isinstance(request_text, str) or len(request_text) > 2000:
        raise AIPlanningError('parent_ai_invalid_request', '补充需求最多 2000 字。')
    target = character_id(target_character_id)
    by_character = {row['character_id']: row for row in characters}
    if target not in by_character:
        raise AIPlanningError('parent_ai_missing_target', '请先选择本地目录中的育成目标角色。')
    route = normalize_local_route(local_route) or normalize_local_route({})
    available = [row for row in projected_entries if row.get('available') is True and row.get('availability') in {'owned', 'borrowed'}]
    selected = {row['entry_id'] for row in route['nodes'] if row and row['kind'] == 'entry'}
    available.sort(key=lambda row: (row['entry_id'] not in selected, row['entry_id']))
    bindings = {'material_' + str(i + 1): row['entry_id'] for i, row in enumerate(available[:MAX_MATERIALS])}
    reverse = {identifier: name for name, identifier in bindings.items()}
    facts, missing = ([], ['尚未掌握账号可育成角色和支援卡持有信息；待培养角色只是候选，不代表已拥有。'])
    allowed_factors = set(factor_keys)
    for material, row in zip(bindings, available[:MAX_MATERIALS]):
        own = deepcopy(row.get('self') or {})
        for key in ('character_id', 'variant_id'):
            own[key] = own.get(key) or row.get(key)
        facts.append({'material_id': material, 'availability': row['availability'], 'self': _node_facts(own, allowed_factors, race_catalog, row['entry_id'], route['aptitude_goal']['factor_key']), 'parents': [_node_facts(node, allowed_factors, race_catalog, target_factor=route['aptitude_goal']['factor_key']) for node in row.get('parents', [None, None])]})
    if not available:
        missing.append('当前账号范围没有已确认自有或可借成品；可以提出待培养草稿，不能当作现成材料。')
    safe_route = deepcopy(route)
    for index, node in enumerate(safe_route['nodes']):
        if node and node['kind'] == 'entry':
            if node['entry_id'] not in reverse:
                safe_route['nodes'][index] = None
                missing.append('当前草稿含未确认可用的成品，本次未发送该条记录。')
            else:
                node['entry_id'] = reverse[node['entry_id']]
    roster = [{'character_id': row['character_id'], 'name': safe_text(row['name'], 100), 'variant_ids': [str(value['variant_id']) for value in row.get('outfits', [])], 'ownership': 'unknown'} for row in characters]
    if aptitude_catalog is not None:
        aptitude_rows = {row['variant_id']: row for row in aptitude_catalog.rows}
        for row in roster:
            row['aptitude_reference'] = [{'variant_id': variant, 'aptitudes': deepcopy(aptitude_rows[variant]['aptitudes'])} for variant in row['variant_ids'] if variant in aptitude_rows and aptitude_rows[variant]['character_id'] == row['character_id']]
            row['aptitude_source_status'] = 'reference'
            row['cn_live_calibrated'] = False
    payload = {'schema_version': SCHEMA, 'target_character_id': target, 'current_draft': safe_route, 'request_text': safe_text(request_text, 2000, private_values=private_values), 'materials': facts, 'character_catalog': roster, 'missing_data': list(dict.fromkeys(missing)), 'constraints': {'maximum_candidates': 3, 'slot_count': 6, 'existing_material_ids': list(bindings), 'keep_aptitude_goal_unchanged': True, 'planned_is_not_owned': True, 'probabilities_supported': False}}
    if isinstance(local_goal_assessment, dict):
        payload['local_goal_assessment'] = {'baseline': {key: deepcopy(local_goal_assessment.get('baseline', {}).get(key)) for key in ('rank', 'status', 'variant_id')}, 'plan': {key: deepcopy(local_goal_assessment.get('plan', {}).get(key)) for key in ('status', 'required_stars', 'known_stars', 'unknown_nodes', 'additional_stars')}}
    mandatory = sum((row['entry_id'] in selected for row in available[:MAX_MATERIALS]))

    def scope_note():
        payload['missing_data'] = [text for text in payload['missing_data'] if not text.startswith('本次发送 ')]
        if len(facts) < len(available):
            payload['missing_data'].append('本次发送 ' + str(len(facts)) + '/' + str(len(available)) + ' 条可用成品摘要；未发送全部库存，当前草稿已选材料优先。')
        payload['available_material_count'], payload['sent_material_count'] = (len(available), len(facts))
    scope_note()
    while len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf8')) > MAX_CONTEXT_BYTES and len(facts) > mandatory:
        removed = facts.pop()['material_id']
        bindings.pop(removed)
        payload['constraints']['existing_material_ids'].remove(removed)
        for index, node in enumerate(safe_route['nodes']):
            if node and node['kind'] == 'entry' and (node['entry_id'] == removed):
                safe_route['nodes'][index] = None
        scope_note()
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf8')) > MAX_CONTEXT_BYTES:
        raise AIPlanningError('parent_ai_context_too_large', '本次资料摘要过大，请缩小可用素材范围后再请求。')
    return {'payload': payload, 'bindings': bindings, 'goal': route['aptitude_goal'], 'characters': by_character, 'missing_data': payload['missing_data'], 'private_values': tuple(private_values)}

def validate_suggestions(reply, bundle, evaluate):
    """Reject invented inventory/facts and preserve local re-evaluation as authority."""
    if not isinstance(reply, dict) or set(reply) - {'candidates', 'missing_data'}:
        raise AIPlanningError('parent_ai_invalid_output', 'AI 未返回约定的结构化方案，本次未采用。')
    candidates = reply.get('candidates')
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 3:
        raise AIPlanningError('parent_ai_invalid_output', 'AI 应返回 1～3 条方案，本次未采用。')
    extra = reply.get('missing_data', [])
    if not isinstance(extra, list) or len(extra) > 12 or any((not isinstance(text, str) for text in extra)):
        raise AIPlanningError('parent_ai_invalid_output', 'AI 缺失资料说明格式不正确，本次未采用。')
    accepted, rejected = ([], [])
    for proposal in candidates:
        try:
            if not isinstance(proposal, dict) or set(proposal) != {'title', 'summary', 'local_route', 'training_steps'}:
                raise ValueError()
            if any((not isinstance(proposal[key], str) or not proposal[key].strip() for key in ('title', 'summary'))):
                raise ValueError()
            steps = proposal['training_steps']
            if not isinstance(steps, list) or len(steps) > 12 or any((not isinstance(text, str) for text in steps)):
                raise ValueError()
            route = normalize_local_route(proposal['local_route'])
            if route is None or route['aptitude_goal'] != bundle['goal']:
                raise ValueError()
            for node in route['nodes']:
                if node is None:
                    continue
                if node['kind'] == 'entry':
                    if node['entry_id'] not in bundle['bindings']:
                        raise ValueError()
                    node['entry_id'] = bundle['bindings'][node['entry_id']]
                else:
                    identifier = node['character_id']
                    if identifier not in bundle['characters']:
                        raise ValueError()
                    variants = {str(row['variant_id']) for row in bundle['characters'][identifier].get('outfits', [])}
                    if node['variant_id'] is not None and node['variant_id'] not in variants:
                        raise ValueError()
            review = evaluate(route)
            if not isinstance(review, dict) or review.get('selection_issues'):
                raise ValueError()
            warnings = ['AI 只提供草稿；角色持有、胜场、星数与开局缺口以本地复核为准。']
            if any((node and node['kind'] == 'planned' for node in route['nodes'])):
                warnings.append('待培养角色持有信息未知；计划红因子不是已生成因子。')
            if not review.get('ready_to_start'):
                warnings.append('当前方案仍有待培养或未确认项目，尚不能作为已备齐材料。')
            if review.get('aptitude', {}).get('plan', {}).get('status') == 'unreachable_at_start':
                warnings.append('本地规则显示该适性目标在开局不可达；不得按补齐星数即可保证来使用。')
            text = lambda value, limit: safe_text(value, limit, private_values=bundle['private_values'], model_text=True)
            accepted.append({'candidate_id': 'ai-' + uuid.uuid4().hex, 'title': text(proposal['title'], 80) or '本地复核方案', 'summary': text(proposal['summary'], 500) or '请查看本地复核与待培养步骤。', 'local_route': route, 'local_review': review, 'warnings': warnings, 'training_steps': [clean for raw in steps if (clean := text(raw, 240))]})
        except (ValueError, TypeError, KeyError, AttributeError):
            rejected.append('有一条 AI 候选未通过成品编号、固定目标或本地角色与家系校验，已忽略。')
    if not accepted:
        raise AIPlanningError('parent_ai_invalid_output', 'AI 候选均未通过本地资料校验，原草稿未修改。')
    return {'candidates': accepted, 'missing_data': list(dict.fromkeys([*bundle['missing_data'], *rejected, *(safe_text(text, 300, private_values=bundle['private_values'], model_text=True) for text in extra)]))}
