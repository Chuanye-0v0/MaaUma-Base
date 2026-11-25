"""Build-first workflow configuration library (V2).

Primary user entity is a named Build ('培养方案', owning trainee, deck,
targets, strategy revision, races, skills, event preferences, AI/failure limits).
The user can freely switch between the 5 built-in workflow modes without losing
or duplicating their Build, while each (build_id, mode_id) pair independently
stores its mode-specific thresholds, handoffs, continuation gates, and budgets
(Option A isolation: no cut/carrot budget leaks into ordinary or cycle runs).

At runtime and preflight validation, any (build, mode_id) compiles deterministically
into an immutable `uma.career_workflow.v1` execution document without altering
v1 `normalize()` semantics or breaking in-flight `runs/`.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile

from core.auto_trainer import larc_strategy_versions as _strategy_versions
from core.auto_trainer.ace_graduation import normalize_ace_cycle
from core.career_workflow import (
    ACE_CYCLE_MODE,
    SS_SIEVE_MODE,
    normalize as normalize_document,
    normalize_ss_sieve,
    templates as workflow_templates,
)
from core.career_workflow_library import (
    LEGACY_WORKFLOW,
    LIBRARY as LIBRARY_V1,
    LibraryStore,
    _read as _read_json_obj,
    _resolved as _resolve_v1_profile,
    deepmerge,
    normalize_library as normalize_library_v1,
)

SCHEMA_V2 = 'uma.career_workflow_library.v2'
LIBRARY_V2 = 'runs/standalone-panel/career-workflow-library-v2.json'
V1_BACKUP = 'runs/standalone-panel/career-workflow-library.v1.bak.json'

BUILTIN_MODES = (
    ('direct_career', '自动育成'),
    ('cut_preserve', '刷切并保留'),
    ('cut_then_career', '出切先停，确认后同局养完'),
    (SS_SIEVE_MODE, 'SS筛局循环'),
    (ACE_CYCLE_MODE, '大赛成品马连开循环'),
)
BUILTIN_MODE_IDS = tuple(mode_id for mode_id, _ in BUILTIN_MODES)
BUILTIN_MODE_NAMES = dict(BUILTIN_MODES)

MODE_SHORT_SUFFIX = {
    'direct_career': '自动育成',
    'cut_preserve': '刷切保留',
    'cut_then_career': '出切先停',
    SS_SIEVE_MODE: 'SS筛局',
    ACE_CYCLE_MODE: '成品循环',
}

SETUP_KEYS = (
    'character_id',
    'character_name',
    'validation_character_name',
    'support_deck',
    'borrow_card_id',
    'borrow_min_level',
    'borrow_min_break',
)

RECIPE_TOP_KEYS = (
    'support_profile',
    'strategy',
    'larc_policy',
    'targets',
    'failure_limit',
    'max_actions',
    'race_retry',
    'running_style',
    'race_running_styles',
    'custom_races',
    'terminal_skills',
    'arc_policy',
    'mid_skill_learning',
    'early_skill_allowlist',
    'mei_breaks',
    'with_ramonu',
    'retain_training',
    'decision_preferences',
    'event_preferences',
    'ai_options',
)

CAREER_PHASE_ONLY_KEYS = {
    'custom_races',
    'terminal_skills',
    'arc_policy',
    'mid_skill_learning',
    'early_skill_allowlist',
    'mei_breaks',
    'with_ramonu',
}

CAREER_MODE_ONLY_KEYS = {'setup', 'manual_handoff', 'manual_pause_dates', 'continuation'}
CUT_INNER_SHARED_KEYS = set(SETUP_KEYS) | {'decision_preferences', 'event_preferences', 'ai_options'}
CUT_INNER_MODE_ONLY_KEYS = {'continuation'}

DOC_RESERVED_KEYS = {'schema_version', 'id', 'label', 'template_id', 'run_graph', 'policies', 'editor', 'career_batch'}

_MIGRATION_PRIORITY = ('direct_career', 'cut_then_career', 'cut_preserve', SS_SIEVE_MODE, ACE_CYCLE_MODE)


def builtin_templates() -> dict[str, dict]:
    """Return canonical built-in v1 template documents keyed by template_id."""
    return {doc['id']: deepcopy(doc) for doc in workflow_templates()}


def default_mode_settings() -> dict[str, dict]:
    """Default per-mode settings for a newly created Build (finite budgets)."""
    return {
        'direct_career': {
            'career_batch': {'max_careers': 1},
        },
        'cut_preserve': {
            'cut_params': {'max_attempts': 20, 'max_tp_items': 0, 'max_carrots': 0},
        },
        'cut_then_career': {
            'cut_params': {'max_attempts': 20, 'max_tp_items': 0, 'max_carrots': 0},
        },
        SS_SIEVE_MODE: {
            'ss_sieve': {'pre_debut': False, 'year1_min': 4, 'allow_ready_five': False},
            'cycle_budget': {'max_attempts': 20, 'max_tp_items': 0, 'max_carrots': 0},
        },
        ACE_CYCLE_MODE: {
            'ace_cycle': normalize_ace_cycle(None),
            'cycle_budget': {'max_attempts': 10, 'max_tp_items': 10, 'max_carrots': 0},
        },
    }


def _graph_signature(document: dict) -> tuple | None:
    graph = document.get('run_graph')
    if not isinstance(graph, dict):
        return None
    nodes = graph.get('nodes')
    edges = graph.get('edges')
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return None
    node_sig = tuple(
        (node.get('id'), node.get('kind'), node.get('policy'))
        for node in nodes
        if isinstance(node, dict)
    )
    if len(node_sig) != len(nodes):
        return None
    edge_sig = tuple(sorted(
        (str(edge.get('from') or ''), str(edge.get('outcome') or ''), str(edge.get('to') or ''))
        for edge in edges
        if isinstance(edge, dict)
    ))
    return (graph.get('entry'), node_sig, edge_sig)


def same_builtin_topology(document: dict, mode_id: str) -> bool:
    """Return True if document structurally matches the built-in template for mode_id."""
    if mode_id not in BUILTIN_MODE_NAMES or not isinstance(document, dict):
        return False
    tpl = builtin_templates().get(mode_id)
    if tpl is None:
        return False
    return _graph_signature(document) == _graph_signature(tpl)


def _strip_phase_horizon(larc_policy, phase: str):
    if not isinstance(larc_policy, dict):
        return deepcopy(larc_policy)
    cleaned = deepcopy(larc_policy)
    default_horizon = 'junior_cutoff_discard' if phase == 'cut' else 'full_career'
    if cleaned.get('ss_planning_horizon') == default_horizon:
        cleaned.pop('ss_planning_horizon', None)
    return cleaned


def _clean_setup_from_dict(src: dict | None) -> dict:
    if not isinstance(src, dict):
        return {}
    return {k: deepcopy(src[k]) for k in SETUP_KEYS if k in src}


def _coerce_int_opt(val):
    if type(val) is int:
        return val
    if isinstance(val, str):
        text = val.strip()
        if re.fullmatch(r'[+-]?\d+', text):
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _clean_cycle_budget(raw_cut: dict | None, defaults_budget: dict) -> dict:
    if not isinstance(raw_cut, dict):
        return deepcopy(defaults_budget)
    budget = {}
    for key in ('max_attempts', 'max_tp_items', 'max_carrots', 'refill_operation_limit'):
        if key in raw_cut:
            val = raw_cut[key]
            # Never inherit unlimited None carrots into cycle modes (N1).
            if key == 'max_carrots' and val is None:
                val = 0
            coerced = _coerce_int_opt(val)
            budget[key] = coerced if coerced is not None else deepcopy(val)
    for key, def_val in defaults_budget.items():
        budget.setdefault(key, deepcopy(def_val))
    return budget


def _extract_career_phase_recipe(career_opts: dict) -> dict:
    recipe: dict = {}
    if isinstance(career_opts.get('setup'), dict):
        recipe['setup'] = deepcopy(career_opts['setup'])
    else:
        recipe['setup'] = {}
    for key in RECIPE_TOP_KEYS:
        if key in career_opts:
            if key == 'larc_policy':
                recipe[key] = _strip_phase_horizon(career_opts[key], 'career')
            else:
                recipe[key] = deepcopy(career_opts[key])
    known = set(RECIPE_TOP_KEYS) | CAREER_MODE_ONLY_KEYS
    extras = {
        k: deepcopy(v)
        for k, v in career_opts.items()
        if k not in known
    }
    if extras:
        recipe['extra_options'] = extras
    return recipe


def _extract_cut_phase_recipe_and_params(cut_opts: dict) -> tuple[dict, dict, dict | None]:
    recipe: dict = {}
    cut_inner = cut_opts.get('cut') if isinstance(cut_opts.get('cut'), dict) else {}
    recipe['setup'] = _clean_setup_from_dict(cut_inner)
    for key in RECIPE_TOP_KEYS:
        if key in cut_opts:
            if key == 'larc_policy':
                recipe[key] = _strip_phase_horizon(cut_opts[key], 'cut')
            else:
                recipe[key] = deepcopy(cut_opts[key])
    # In legacy cut_options, decision_preferences/event_preferences/ai_options live under cut_options['cut'].
    for shared_key in ('decision_preferences', 'event_preferences', 'ai_options'):
        if shared_key not in recipe and shared_key in cut_inner:
            recipe[shared_key] = deepcopy(cut_inner[shared_key])
    known_top = set(RECIPE_TOP_KEYS) | {'cut'}
    extras = {k: deepcopy(v) for k, v in cut_opts.items() if k not in known_top}
    if extras:
        recipe['extra_options'] = extras

    cut_params = {
        k: deepcopy(v)
        for k, v in cut_inner.items()
        if k not in CUT_INNER_SHARED_KEYS and k not in CUT_INNER_MODE_ONLY_KEYS
    }
    continuation = deepcopy(cut_inner['continuation']) if isinstance(cut_inner.get('continuation'), dict) else None
    return recipe, cut_params, continuation


def extract_from_v1_document(mode_id: str, document: dict) -> tuple[dict, dict]:
    """Extract (recipe, mode_setting) losslessly from a compiled/resolved v1 workflow document."""
    doc = deepcopy(document) if isinstance(document, dict) else {}
    policies = doc.get('policies') or {}
    default_policy = policies.get('default') if isinstance(policies.get('default'), dict) else {}
    career_opts = default_policy.get('career_options') if isinstance(default_policy.get('career_options'), dict) else {}
    cut_opts = default_policy.get('cut_options') if isinstance(default_policy.get('cut_options'), dict) else {}
    nodes = (doc.get('run_graph') or {}).get('nodes') or []
    by_id = {n.get('id'): n for n in nodes if isinstance(n, dict) and n.get('id')}
    def_settings = default_mode_settings()

    doc_extensions = {
        k: deepcopy(v)
        for k, v in doc.items()
        if k not in DOC_RESERVED_KEYS
    }

    if mode_id == 'direct_career':
        recipe = _extract_career_phase_recipe(career_opts)
        setting = deepcopy(def_settings['direct_career'])
        batch = doc.get('career_batch')
        if isinstance(batch, dict):
            setting['career_batch'] = deepcopy(batch)
        if 'manual_handoff' in career_opts:
            setting['manual_handoff'] = deepcopy(career_opts['manual_handoff'])
        if 'manual_pause_dates' in career_opts:
            setting['manual_pause_dates'] = deepcopy(career_opts['manual_pause_dates'])
        if 'continuation' in career_opts:
            setting['continuation'] = deepcopy(career_opts['continuation'])
        if doc_extensions:
            setting['doc_extensions'] = doc_extensions
        return recipe, setting

    if mode_id == SS_SIEVE_MODE:
        recipe = _extract_career_phase_recipe(career_opts)
        setting = deepcopy(def_settings[SS_SIEVE_MODE])
        grow = by_id.get('grow') or next((n for n in nodes if isinstance(n, dict) and 'ss_sieve' in n), {})
        if isinstance(grow.get('ss_sieve'), dict):
            setting['ss_sieve'] = normalize_ss_sieve(grow['ss_sieve'])
        cut_inner = cut_opts.get('cut') if isinstance(cut_opts.get('cut'), dict) else {}
        setting['cycle_budget'] = _clean_cycle_budget(cut_inner, def_settings[SS_SIEVE_MODE]['cycle_budget'])
        if 'manual_handoff' in career_opts:
            setting['manual_handoff'] = deepcopy(career_opts['manual_handoff'])
        if 'manual_pause_dates' in career_opts:
            setting['manual_pause_dates'] = deepcopy(career_opts['manual_pause_dates'])
        if 'continuation' in career_opts:
            setting['continuation'] = deepcopy(career_opts['continuation'])
        if doc_extensions:
            setting['doc_extensions'] = doc_extensions
        return recipe, setting

    if mode_id == ACE_CYCLE_MODE:
        recipe = _extract_career_phase_recipe(career_opts)
        setting = deepcopy(def_settings[ACE_CYCLE_MODE])
        grow = by_id.get('grow') or next((n for n in nodes if isinstance(n, dict) and 'ace_cycle' in n), {})
        if isinstance(grow.get('ace_cycle'), dict):
            setting['ace_cycle'] = normalize_ace_cycle(grow['ace_cycle'])
        cut_inner = cut_opts.get('cut') if isinstance(cut_opts.get('cut'), dict) else {}
        setting['cycle_budget'] = _clean_cycle_budget(cut_inner, def_settings[ACE_CYCLE_MODE]['cycle_budget'])
        if 'manual_handoff' in career_opts:
            setting['manual_handoff'] = deepcopy(career_opts['manual_handoff'])
        if 'manual_pause_dates' in career_opts:
            setting['manual_pause_dates'] = deepcopy(career_opts['manual_pause_dates'])
        if 'continuation' in career_opts:
            setting['continuation'] = deepcopy(career_opts['continuation'])
        if doc_extensions:
            setting['doc_extensions'] = doc_extensions
        return recipe, setting

    if mode_id == 'cut_preserve':
        recipe, cut_params, continuation = _extract_cut_phase_recipe_and_params(cut_opts)
        setting = deepcopy(def_settings['cut_preserve'])
        if cut_params:
            setting['cut_params'] = cut_params
        else:
            setting['cut_params'] = {}
        if continuation is not None:
            setting['continuation'] = continuation
        screen = by_id.get('screen') or next((n for n in nodes if isinstance(n, dict) and n.get('kind') == 'cut'), {})
        if 'screening' in screen:
            setting['screening'] = deepcopy(screen['screening'])
        if 'check_at' in screen and screen['check_at'] != 'cut_checkpoint':
            setting['check_at'] = screen['check_at']
        if doc_extensions:
            setting['doc_extensions'] = doc_extensions
        return recipe, setting

    if mode_id == 'cut_then_career':
        cut_recipe, cut_params, continuation = _extract_cut_phase_recipe_and_params(cut_opts)
        career_recipe = _extract_career_phase_recipe(career_opts)
        # Merge career-phase keys (like custom_races, terminal_skills, arc_policy, mid_skill_learning)
        # into the unified recipe; preserve genuine two-stage divergences in grow_overrides.
        recipe = deepcopy(cut_recipe)
        if not recipe.get('setup') and career_recipe.get('setup'):
            recipe['setup'] = deepcopy(career_recipe['setup'])
        grow_overrides = {}
        for key, career_val in career_recipe.items():
            if key == 'setup':
                continue
            if key == 'extra_options':
                merged_extra = {**career_val, **recipe.get('extra_options', {})}
                if merged_extra:
                    recipe['extra_options'] = merged_extra
                continue
            if key not in recipe or recipe[key] is None:
                recipe[key] = deepcopy(career_val)
            elif recipe[key] != career_val:
                grow_overrides[key] = deepcopy(career_val)

        setting = deepcopy(def_settings['cut_then_career'])
        if cut_params:
            setting['cut_params'] = cut_params
        elif isinstance(cut_opts.get('cut'), dict):
            setting['cut_params'] = {}
        if continuation is not None:
            setting['continuation'] = continuation
        if 'manual_handoff' in career_opts:
            setting['manual_handoff'] = deepcopy(career_opts['manual_handoff'])
        if 'manual_pause_dates' in career_opts:
            setting['manual_pause_dates'] = deepcopy(career_opts['manual_pause_dates'])
        screen = by_id.get('screen') or next((n for n in nodes if isinstance(n, dict) and n.get('kind') == 'cut'), {})
        if 'screening' in screen:
            setting['screening'] = deepcopy(screen['screening'])
        if 'check_at' in screen and screen['check_at'] != 'cut_checkpoint':
            setting['check_at'] = screen['check_at']
        if grow_overrides:
            setting['grow_overrides'] = grow_overrides
        if doc_extensions:
            setting['doc_extensions'] = doc_extensions
        return recipe, setting

    raise ValueError('内置育成模式标识不受支持：' + str(mode_id))


def _compile_career_options(recipe: dict, setting: dict, overrides: dict | None = None) -> dict:
    options: dict = {}
    if isinstance(recipe.get('setup'), dict):
        options['setup'] = deepcopy(recipe['setup'])
    for key in RECIPE_TOP_KEYS:
        if key in recipe:
            if key == 'larc_policy':
                options[key] = _strip_phase_horizon(recipe[key], 'career')
            else:
                options[key] = deepcopy(recipe[key])
    if isinstance(recipe.get('extra_options'), dict):
        for k, v in recipe['extra_options'].items():
            if k not in options and k not in CAREER_MODE_ONLY_KEYS:
                options[k] = deepcopy(v)
    if 'manual_handoff' in setting:
        options['manual_handoff'] = deepcopy(setting['manual_handoff'])
    if 'manual_pause_dates' in setting:
        options['manual_pause_dates'] = deepcopy(setting['manual_pause_dates'])
    if 'continuation' in setting:
        options['continuation'] = deepcopy(setting['continuation'])
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k == 'larc_policy':
                options[k] = _strip_phase_horizon(v, 'career')
            else:
                options[k] = deepcopy(v)
    if 'larc_policy' not in options:
        options['larc_policy'] = None
    return options


def _compile_cut_options(recipe: dict, setting: dict) -> dict:
    options: dict = {}
    for key in RECIPE_TOP_KEYS:
        if key in {'decision_preferences', 'event_preferences', 'ai_options'}:
            continue
        if key in CAREER_PHASE_ONLY_KEYS:
            continue
        if key in recipe:
            if key == 'larc_policy':
                options[key] = _strip_phase_horizon(recipe[key], 'cut')
            else:
                options[key] = deepcopy(recipe[key])
    if isinstance(recipe.get('extra_options'), dict):
        for k, v in recipe['extra_options'].items():
            if k not in options and k not in CAREER_PHASE_ONLY_KEYS and k not in CAREER_MODE_ONLY_KEYS and k != 'cut':
                options[k] = deepcopy(v)
    if 'larc_policy' not in options:
        options['larc_policy'] = None

    cut_inner: dict = {}
    if isinstance(recipe.get('setup'), dict):
        for k in SETUP_KEYS:
            if k in recipe['setup']:
                cut_inner[k] = deepcopy(recipe['setup'][k])
    cut_params = setting.get('cut_params')
    if isinstance(cut_params, dict):
        for k, v in cut_params.items():
            if k not in CUT_INNER_SHARED_KEYS and k not in CUT_INNER_MODE_ONLY_KEYS:
                cut_inner[k] = deepcopy(v)
    for shared_key in ('decision_preferences', 'event_preferences', 'ai_options'):
        if shared_key in recipe and recipe[shared_key] is not None:
            cut_inner[shared_key] = deepcopy(recipe[shared_key])
    if 'continuation' in setting and setting['continuation'] is not None:
        cut_inner['continuation'] = deepcopy(setting['continuation'])
    options['cut'] = cut_inner
    return options


def compile_v2_document(build: dict, mode_id: str) -> dict:
    """Compile a V2 (build, mode_id) pair into a normalized executable v1 workflow document."""
    if mode_id not in BUILTIN_MODE_NAMES:
        raise ValueError('内置育成模式标识不受支持：' + str(mode_id))
    tpl = builtin_templates()[mode_id]
    recipe = build.get('recipe') if isinstance(build.get('recipe'), dict) else {}
    def_settings = default_mode_settings()
    all_settings = build.get('mode_settings') if isinstance(build.get('mode_settings'), dict) else {}
    setting = deepcopy(all_settings.get(mode_id) or def_settings[mode_id])
    nodes = tpl['run_graph']['nodes']
    by_id = {n['id']: n for n in nodes}

    if isinstance(setting.get('doc_extensions'), dict):
        for ext_k, ext_v in setting['doc_extensions'].items():
            if ext_k not in DOC_RESERVED_KEYS:
                tpl[ext_k] = deepcopy(ext_v)

    if mode_id == 'direct_career':
        tpl['policies']['default']['career_options'] = _compile_career_options(recipe, setting)
        tpl['policies']['default']['cut_options'] = {}
        batch = setting.get('career_batch')
        if isinstance(batch, dict):
            max_careers = batch.get('max_careers', 1)
            if max_careers != 1 or len(batch) > 1 or 'career_batch' in setting:
                if max_careers != 1 or len(batch) > 1:
                    tpl['career_batch'] = deepcopy(batch)
    elif mode_id == 'cut_preserve':
        tpl['policies']['default']['cut_options'] = _compile_cut_options(recipe, setting)
        tpl['policies']['default']['career_options'] = {}
        screen = by_id.get('screen')
        if screen is not None:
            if 'screening' in setting:
                screen['screening'] = deepcopy(setting['screening'])
            if 'check_at' in setting:
                screen['check_at'] = setting['check_at']
    elif mode_id == 'cut_then_career':
        tpl['policies']['default']['cut_options'] = _compile_cut_options(recipe, setting)
        tpl['policies']['default']['career_options'] = _compile_career_options(
            recipe, setting, overrides=setting.get('grow_overrides')
        )
        screen = by_id.get('screen')
        if screen is not None:
            if 'screening' in setting:
                screen['screening'] = deepcopy(setting['screening'])
            if 'check_at' in setting:
                screen['check_at'] = setting['check_at']
    elif mode_id == SS_SIEVE_MODE:
        tpl['policies']['default']['career_options'] = _compile_career_options(recipe, setting)
        # Strict Option-A budget isolation (N1): ONLY read cycle_budget of ss_sieve_cycle.
        budget = _clean_cycle_budget(setting.get('cycle_budget'), def_settings[SS_SIEVE_MODE]['cycle_budget'])
        tpl['policies']['default']['cut_options'] = {'cut': budget}
        grow = by_id.get('grow')
        if grow is not None:
            grow['ss_sieve'] = normalize_ss_sieve(setting.get('ss_sieve') or def_settings[SS_SIEVE_MODE]['ss_sieve'])
    elif mode_id == ACE_CYCLE_MODE:
        tpl['policies']['default']['career_options'] = _compile_career_options(recipe, setting)
        # Strict Option-A budget isolation (N1): ONLY read cycle_budget of ace_career_cycle.
        budget = _clean_cycle_budget(setting.get('cycle_budget'), def_settings[ACE_CYCLE_MODE]['cycle_budget'])
        tpl['policies']['default']['cut_options'] = {'cut': budget}
        grow = by_id.get('grow')
        if grow is not None:
            grow['ace_cycle'] = normalize_ace_cycle(setting.get('ace_cycle'))

    return normalize_document(tpl)


def recipe_fingerprint(recipe: dict) -> str:
    """Canonical fingerprint comparing core training recipe equivalence across v1 modes."""
    norm = deepcopy(recipe) if isinstance(recipe, dict) else {}
    setup = norm.get('setup') if isinstance(norm.get('setup'), dict) else {}
    cleaned_setup = {}
    for k, v in setup.items():
        if k == 'support_deck' and v in (None, 0, '0'):
            continue
        if k == 'borrow_card_id' and v in (None, 0, '0'):
            continue
        if k == 'borrow_min_level' and v in (None, 1, '1', ''):
            continue
        if k == 'borrow_min_break' and v in (None, 0, '0', ''):
            continue
        if k == 'character_id' and v in (None, '', 'current'):
            continue
        if k == 'character_name' and not v:
            continue
        if k == 'validation_character_name' and not v:
            continue
        cleaned_setup[k] = v
    canonical = {'setup': cleaned_setup}
    for key in sorted(k for k in norm if k not in {'setup'} | CAREER_PHASE_ONLY_KEYS):
        val = norm[key]
        if key == 'larc_policy' and val is None:
            continue
        if key == 'strategy' and val == 'baseline':
            continue
        if val in (None, {}, []):
            continue
        canonical[key] = val
    return json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def is_empty_skeleton_recipe(recipe: dict) -> bool:
    """True when recipe holds no explicit trainee, deck, strategy, targets, or preferences."""
    return recipe_fingerprint(recipe) == '{"setup":{}}' and not any(
        recipe.get(k) for k in CAREER_PHASE_ONLY_KEYS if recipe.get(k) not in (None, {}, [], True)
    )


def _slug_build_id(raw_id: str, used: set[str], index: int) -> str:
    stem = re.sub(r'[^0-9A-Za-z_-]+', '-', str(raw_id or '').strip()).strip('-')
    if not stem:
        stem = f'build_{index}'
    candidate = stem
    suffix = 2
    while candidate in used:
        candidate = f'{stem}_{suffix}'
        suffix += 1
    used.add(candidate)
    return candidate


def migrate_v1_to_v2(v1_library: dict) -> dict:
    """Deterministic, idempotent lossless migration from v1 workflow library to V2 Build-first model."""
    modes = v1_library.get('modes') if isinstance(v1_library.get('modes'), list) else []
    old_selected_mode = v1_library.get('selected_mode_id')
    builtin_by_id: dict[str, dict] = {}
    custom_modes: list[dict] = []

    for mode in modes:
        if not isinstance(mode, dict):
            continue
        mid = mode.get('id')
        doc = mode.get('document') or {}
        if mid in BUILTIN_MODE_NAMES and mid not in builtin_by_id and same_builtin_topology(doc, mid):
            builtin_by_id[mid] = mode
        else:
            custom_modes.append(deepcopy(mode))

    # Resolve built-in modes in priority order; merge identical-name identical-content recipes,
    # and disambiguate same-name different-content names (never merge distinct identities silently).
    ordered_mode_ids = [mid for mid in _MIGRATION_PRIORITY if mid in builtin_by_id]
    entries = []
    for mid in ordered_mode_ids:
        mode = builtin_by_id[mid]
        for profile in mode.get('profiles') or []:
            pid = profile['id']
            pname = str(profile.get('name') or '默认配置').strip() or '默认配置'
            resolved_doc = _resolve_v1_profile(mode, profile)
            recipe, setting = extract_from_v1_document(mid, resolved_doc)
            entries.append({
                'mode_id': mid,
                'profile_id': pid,
                'name': pname,
                'recipe': recipe,
                'setting': setting,
                'fp': recipe_fingerprint(recipe),
                'empty': is_empty_skeleton_recipe(recipe),
                'origin_slot': deepcopy(profile.get('legacy_slots')) if 'legacy_slots' in profile else None,
            })

    builds: list[dict] = []
    used_build_ids: set[str] = set()
    source_map: dict[str, str] = {}

    for item in entries:
        src_key = f"{item['mode_id']}/{item['profile_id']}"
        matched = None
        for b in builds:
            if b['base_name'] != item['name']:
                continue
            has_mode = item['mode_id'] in b['filled_modes']
            if not has_mode and (b['fp'] == item['fp'] or item['empty']):
                matched = b
                break
        if matched is not None:
            matched['build']['mode_settings'][item['mode_id']] = deepcopy(item['setting'])
            matched['filled_modes'].add(item['mode_id'])
            if item['origin_slot'] is not None:
                matched['build'].setdefault('legacy_slots', {})[src_key] = item['origin_slot']
            source_map[src_key] = matched['build']['id']
            continue

        bid = _slug_build_id(item['profile_id'], used_build_ids, len(builds) + 1)
        settings = default_mode_settings()
        settings[item['mode_id']] = deepcopy(item['setting'])
        build_obj = {
            'id': bid,
            'name': item['name'],
            'recipe': deepcopy(item['recipe']),
            'mode_settings': settings,
        }
        if item['origin_slot'] is not None:
            build_obj['legacy_slots'] = {src_key: item['origin_slot']}
        builds.append({
            'base_name': item['name'],
            'mode_id': item['mode_id'],
            'fp': item['fp'],
            'filled_modes': {item['mode_id']},
            'build': build_obj,
        })
        source_map[src_key] = bid

    # Disambiguate colliding display names that hold distinct content.
    by_base_name: dict[str, list[dict]] = {}
    for b in builds:
        by_base_name.setdefault(b['base_name'], []).append(b)
    used_names: set[str] = set()
    for base_name, group in by_base_name.items():
        if len(group) == 1:
            used_names.add(group[0]['build']['name'])
            continue
        for entry in group:
            suffix = MODE_SHORT_SUFFIX.get(entry['mode_id'], entry['mode_id'])
            candidate = f'{base_name}〔{suffix}〕'
            n = 2
            while candidate in used_names:
                candidate = f'{base_name}〔{suffix} {n}〕'
                n += 1
            entry['build']['name'] = candidate
            used_names.add(candidate)

    result_builds = [b['build'] for b in builds]
    if not result_builds:
        result_builds = [{
            'id': 'default',
            'name': '默认配置',
            'recipe': {'setup': {}, 'larc_policy': None},
            'mode_settings': default_mode_settings(),
        }]

    selected_build_id = result_builds[0]['id']
    if old_selected_mode in builtin_by_id:
        old_profile = builtin_by_id[old_selected_mode].get('selected_profile_id')
        mapped = source_map.get(f'{old_selected_mode}/{old_profile}')
        if mapped:
            selected_build_id = mapped
        selected_mode_id = old_selected_mode
    elif any(m.get('id') == old_selected_mode for m in custom_modes):
        selected_mode_id = old_selected_mode
    else:
        selected_mode_id = 'direct_career'

    revision = v1_library.get('revision', 0)
    if type(revision) is not int or revision < 0:
        revision = 0

    result = {
        'schema_version': SCHEMA_V2,
        'revision': revision,
        'selected_build_id': selected_build_id,
        'selected_mode_id': selected_mode_id,
        'builds': result_builds,
        'custom_modes': custom_modes,
        'migration_meta': {
            'migrated_from_schema': v1_library.get('schema_version'),
            'source_map': source_map,
        },
    }
    return normalize_library_v2(result)


def normalize_library_v2(value: dict) -> dict:
    """Validate V2 library integrity, mode settings isolation, and compilability."""
    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA_V2:
        raise ValueError('V2培养方案配置库版本不受支持。')
    result = deepcopy(value)
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise ValueError('V2培养方案配置库需要有效JSON数据。') from error

    if 'revision' in result:
        if type(result['revision']) is not int or result['revision'] < 0:
            raise ValueError('配置库修订号应为非负整数。')
    else:
        result['revision'] = 0

    builds = result.get('builds')
    if not isinstance(builds, list) or not builds:
        raise ValueError('至少需要一套培养方案。')

    def_settings = default_mode_settings()
    build_ids: set[str] = set()
    for build in builds:
        if not isinstance(build, dict):
            raise ValueError('培养方案条目应为对象。')
        bid = build.get('id')
        bname = build.get('name')
        if not isinstance(bid, str) or not bid.strip():
            raise ValueError('培养方案标识不能为空。')
        if not isinstance(bname, str) or not bname.strip():
            raise ValueError('培养方案名称不能为空。')
        if bid in build_ids:
            raise ValueError('培养方案标识重复：' + bid)
        build_ids.add(bid)

        if build.get('recipe') is None:
            build['recipe'] = {'setup': {}}
        if not isinstance(build['recipe'], dict):
            raise ValueError('培养方案 recipe 应为对象。')

        raw_ms = build.get('mode_settings')
        if raw_ms is None:
            raw_ms = {}
        if not isinstance(raw_ms, dict):
            raise ValueError('培养方案 mode_settings 应为对象。')
        ms = deepcopy(def_settings)
        for mid, val in raw_ms.items():
            if not isinstance(val, dict):
                raise ValueError('模式专属参数应为对象：' + str(mid))
            if mid in BUILTIN_MODE_NAMES:
                # Finite budget default protection for cycle modes
                merged = deepcopy(val)
                if isinstance(merged.get('continuation'), dict):
                    from core.auto_trainer.larc_continuation import coerce_continuation_numbers
                    coerce_continuation_numbers(merged['continuation'])
                if isinstance(merged.get('cut_params'), dict):
                    for bkey in ('max_attempts', 'max_tp_items', 'max_carrots', 'refill_operation_limit'):
                        if bkey in merged['cut_params']:
                            coerced = _coerce_int_opt(merged['cut_params'][bkey])
                            if coerced is not None:
                                merged['cut_params'][bkey] = coerced
                if mid in (SS_SIEVE_MODE, ACE_CYCLE_MODE):
                    merged['cycle_budget'] = _clean_cycle_budget(
                        merged.get('cycle_budget'), def_settings[mid]['cycle_budget']
                    )
                    if mid == SS_SIEVE_MODE:
                        merged['ss_sieve'] = normalize_ss_sieve(
                            merged.get('ss_sieve') or def_settings[SS_SIEVE_MODE]['ss_sieve']
                        )
                    else:
                        merged['ace_cycle'] = normalize_ace_cycle(merged.get('ace_cycle'))
                ms[mid] = merged
            else:
                ms[mid] = deepcopy(val)
        build['mode_settings'] = ms

        # Verify every built-in mode compiles into a valid v1 workflow document.
        for mid in BUILTIN_MODE_IDS:
            compile_v2_document(build, mid)

    custom_modes = result.get('custom_modes')
    if custom_modes is None:
        custom_modes = []
        result['custom_modes'] = custom_modes
    if not isinstance(custom_modes, list):
        raise ValueError('自定义流程列表应为数组。')
    custom_ids: set[str] = set()
    for cm in custom_modes:
        if not isinstance(cm, dict):
            raise ValueError('自定义流程应为对象。')
        cid = cm.get('id')
        cname = cm.get('name')
        if not isinstance(cid, str) or not cid.strip() or not isinstance(cname, str) or not cname.strip():
            raise ValueError('自定义流程标识或名称不能为空。')
        if cid in custom_ids or cid in BUILTIN_MODE_NAMES:
            raise ValueError('自定义流程标识冲突：' + cid)
        custom_ids.add(cid)
        if not isinstance(cm.get('document'), dict) or not isinstance(cm.get('profiles'), list) or not cm['profiles']:
            raise ValueError('自定义流程需要执行文档和至少一套配置。')
        normalize_document(cm['document'])

    if result.get('selected_build_id') not in build_ids:
        result['selected_build_id'] = builds[0]['id']

    sel_mode = result.get('selected_mode_id')
    if sel_mode not in BUILTIN_MODE_NAMES and sel_mode not in custom_ids:
        result['selected_mode_id'] = 'direct_career'

    return result


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='wb', dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: object) -> None:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    _atomic_write_bytes(path, raw)


def _v1_selection(custom_modes: list[dict], mode_id: str, profile_id: str | None) -> tuple[dict, dict]:
    mode = next((m for m in custom_modes if m.get('id') == mode_id), None)
    if mode is None:
        raise ValueError('自定义流程模式不存在。')
    profiles = mode.get('profiles') or []
    pid = mode.get('selected_profile_id') if profile_id is None else profile_id
    profile = next((p for p in profiles if p.get('id') == pid), None)
    if profile is None and profile_id is None and profiles:
        profile = profiles[0]
    if profile is None:
        raise ValueError('该流程的命名配置不存在。')
    return mode, profile


def _sanitize_unregistered_revisions(value: dict) -> tuple[dict, list[str]]:
    """Replace unregistered larc_policy.revision values in an in-memory copy only."""
    copied = deepcopy(value)
    replaced: list[str] = []
    known = _strategy_versions.REVISION_POLICY_VERSIONS
    fallback = _strategy_versions.CURRENT_REVISION

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            lp = node.get('larc_policy')
            if isinstance(lp, dict):
                rev = lp.get('revision')
                if isinstance(rev, str) and rev not in known:
                    replaced.append(rev)
                    lp['revision'] = fallback
            for v in node.values():
                if isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    _walk(item)

    _walk(copied)
    return copied, sorted(set(replaced))


class BuildLibraryStore:
    """Build-first V2 library storage. `.load`/`.resolve`/`.payload` are strictly read-only."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / LIBRARY_V2
        self.v1_path = self.root / LIBRARY_V1
        self.v1_backup_path = self.root / V1_BACKUP
        self._last_load_warnings: list[str] = []

    def _has_any_v1_source(self) -> bool:
        panel = self.v1_path.parent
        return (
            self.v1_path.is_file()
            or (panel / 'career-settings.json').is_file()
            or (panel / 'settings.json').is_file()
            or (self.root / LEGACY_WORKFLOW).is_file()
        )

    def _initial_v2(self, initial_policies=None) -> dict:
        v1_store = LibraryStore(self.root)
        v1_initial = v1_store._initial(initial_policies)
        v2 = migrate_v1_to_v2(v1_initial)
        v2.setdefault('migration_meta', {})['migrated_from_schema'] = 'initial'
        return v2

    def load(self, initial_policies=None) -> dict:
        self._last_load_warnings = []
        value = _read_json_obj(self.path)
        if value is not None:
            try:
                return normalize_library_v2(value)
            except ValueError as err:
                if 'larc_policy_revision_unknown' in str(err):
                    sanitized, replaced = _sanitize_unregistered_revisions(value)
                    if replaced:
                        result = normalize_library_v2(sanitized)
                        fallback = _strategy_versions.CURRENT_REVISION
                        self._last_load_warnings.append(
                            f"检测到已保存方案使用了当前未注册的策略版本（{'、'.join(replaced)}），"
                            f"已临时以默认策略 {fallback} 只读载入（原磁盘文件未改写）；安装对应策略包后重启即可恢复。"
                        )
                        return result
                raise
        v1_disk = _read_json_obj(self.v1_path)
        if v1_disk is not None or self._has_any_v1_source():
            try:
                try:
                    v1_loaded = LibraryStore(self.root).load(initial_policies)
                except ValueError:
                    if initial_policies is None:
                        raise
                    v1_loaded = LibraryStore(self.root).load(None)
                return migrate_v1_to_v2(v1_loaded)
            except ValueError as err:
                if 'larc_policy_revision_unknown' in str(err) and v1_disk is not None:
                    sanitized_v1, replaced = _sanitize_unregistered_revisions(v1_disk)
                    if replaced:
                        normalized_v1 = normalize_library_v1(sanitized_v1)
                        result = migrate_v1_to_v2(normalized_v1)
                        fallback = _strategy_versions.CURRENT_REVISION
                        self._last_load_warnings.append(
                            f"检测到已保存方案使用了当前未注册的策略版本（{'、'.join(replaced)}），"
                            f"已临时以默认策略 {fallback} 只读载入（原磁盘文件未改写）；安装对应策略包后重启即可恢复。"
                        )
                        return result
                raise
        try:
            return self._initial_v2(initial_policies)
        except ValueError:
            if initial_policies is None:
                raise
            return self._initial_v2(None)

    def _current_disk_revision(self) -> int:
        existing_v2 = _read_json_obj(self.path)
        if isinstance(existing_v2, dict):
            rev = existing_v2.get('revision', 0)
            return rev if type(rev) is int and rev >= 0 else 0
        existing_v1 = _read_json_obj(self.v1_path)
        if isinstance(existing_v1, dict):
            rev = existing_v1.get('revision', 0)
            return rev if type(rev) is int and rev >= 0 else 0
        return 0

    def save(self, library: dict) -> dict:
        disk_rev = self._current_disk_revision()
        existing_v2 = _read_json_obj(self.path)
        has_existing = existing_v2 is not None or self.v1_path.is_file()
        if has_existing and isinstance(library, dict) and 'revision' in library:
            client_rev = library.get('revision')
            if type(client_rev) is not int or client_rev != disk_rev:
                raise ValueError(
                    f'培养方案库已被其他窗口更新（磁盘 v{disk_rev}，提交 v{client_rev}），请刷新后再保存。'
                )
        value = normalize_library_v2(library)
        value['revision'] = disk_rev + 1

        # Preserve exact byte-for-byte backup of pre-V2 library before first V2 write.
        if self.v1_path.is_file() and not self.v1_backup_path.is_file():
            _atomic_write_bytes(self.v1_backup_path, self.v1_path.read_bytes())

        _atomic_json(self.path, value)
        return deepcopy(value)

    def resolve(
        self,
        mode_id=None,
        build_id=None,
        profile_id=None,
        *,
        library=None,
        initial_policies=None,
    ) -> dict:
        lib = self.load(initial_policies) if library is None else normalize_library_v2(library)
        eff_mode = lib['selected_mode_id'] if mode_id is None else mode_id
        if eff_mode in BUILTIN_MODE_NAMES:
            eff_build = lib['selected_build_id'] if build_id is None else build_id
            build = next((b for b in lib['builds'] if b['id'] == eff_build), None)
            if build is None:
                raise ValueError('所选培养方案不存在：' + str(eff_build))
            return compile_v2_document(build, eff_mode)
        mode, profile = _v1_selection(lib.get('custom_modes') or [], eff_mode, profile_id)
        return _resolve_v1_profile(mode, profile)

    def payload(self, mode_id=None, build_id=None, profile_id=None, *, initial_policies=None) -> dict:
        lib = self.load(initial_policies)
        eff_mode = lib['selected_mode_id'] if mode_id is None else mode_id
        eff_build = lib['selected_build_id'] if build_id is None else build_id
        document = self.resolve(eff_mode, build_id=eff_build, profile_id=profile_id, library=lib)
        eff_profile = None
        if eff_mode not in BUILTIN_MODE_NAMES:
            mode, profile = _v1_selection(lib.get('custom_modes') or [], eff_mode, profile_id)
            eff_profile = profile['id']
        result = {
            'library': lib,
            'document': document,
            'mode_id': eff_mode,
            'build_id': eff_build,
            'profile_id': eff_profile,
            'builtin_modes': [{'id': mid, 'name': mname} for mid, mname in BUILTIN_MODES],
            'builtin_templates': builtin_templates(),
        }
        if self._last_load_warnings:
            result['warnings'] = list(self._last_load_warnings)
        return result
