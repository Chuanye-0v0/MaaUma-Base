"""Build an offline skill-planner dry-run from a freshly saved screenshot set.

This module intentionally has no ADB, executor, or click dependency.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.skill_acquisition_parser import build_skill_planner_observation, parse_skill_acquisition_pages
from core.skill_name_resolver import load_skill_catalog, resolve_skill_manifest
from core.skill_planner.catalog import SkillCatalog
from core.skill_planner.models import SkillPurchaseContext
from core.skill_planner.planner import plan_skill_purchases
from core.skill_planner.preferences import SkillPreferenceProfile, default_terminal_skill_preference_profile, load_skill_preference_profile, parse_skill_preference_profile
from core.skill_planner.replay import build_replay_verdict, capture_fingerprint, validate_capture_binding
from core.skill_batch_purchase import build_skill_batch_plan, build_skill_inventory_packet
from core.skill_scroll import SKILL_OVERLAP_SWIPE
from core.skill_ocr_performance import harvest_skill_ocr

def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def _build_metadata_known(context: dict[str, Any] | None) -> bool:
    if not isinstance(context, dict) or not context.get('target_mode'):
        return False
    race_profile = context.get('race_profile')
    if not isinstance(race_profile, dict):
        return False
    required = ('distance_type', 'style')
    return all((race_profile.get(key) not in (None, '', 'unknown') for key in required))

def _planner_policy_from_build_context(build_context: dict[str, Any] | None) -> dict[str, Any]:
    requested_policy = build_context.get('policy') if isinstance(build_context, dict) else None
    allow_gold_without_lower = isinstance(requested_policy, dict) and (requested_policy.get('allow_gold_without_lower') is True or requested_policy.get('harvest_only') is True)
    maximum = requested_policy.get('max_purchase_count') if isinstance(requested_policy, dict) else None
    policy = {'min_confidence': 0.75, 'allow_unverified_skills': False, 'require_condition_match': not _harvest_spenddown_requested(build_context), 'allow_gold_without_lower': allow_gold_without_lower}
    if type(maximum) is int and maximum > 0:
        policy['max_purchase_count'] = maximum
    return policy

def _harvest_spenddown_requested(build_context: dict[str, Any] | None) -> bool:
    if not isinstance(build_context, dict) or build_context.get('target_mode') != 'race':
        return False
    policy, race = (build_context.get('policy'), build_context.get('race_profile'))
    return isinstance(policy, dict) and policy.get('harvest_only') is True and isinstance(race, dict) and (race.get('distance_type') == 'harvest_unbound') and (race.get('style') in {'leader', 'front', 'betweener', 'chaser'})

def _planner_available_skills(observation: dict[str, Any], build_context: dict[str, Any] | None, catalog: SkillCatalog | None=None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep a harvest allowlist local to planner input, not captured inventory."""
    available = observation.get('available_skills')
    if not isinstance(available, list) or not all((isinstance(item, dict) and isinstance(item.get('skill_id'), str) and item['skill_id'] for item in available)):
        raise ValueError('planner_observation_available_skills_invalid')
    policy = build_context.get('policy') if isinstance(build_context, dict) else None
    if not isinstance(policy, dict) or policy.get('harvest_only') is not True:
        return (list(available), {'mode': 'unfiltered'})
    allowed = build_context.get('harvest_planner_allowed_skill_ids')
    if not isinstance(allowed, list) or any((not isinstance(skill_id, str) or not skill_id for skill_id in allowed)) or len(allowed) != len(set(allowed)):
        raise ValueError('harvest_planner_allowed_skill_ids_invalid')
    allowed_ids = set(allowed)
    filtered = [item for item in available if item['skill_id'] in allowed_ids]
    deferred_gold = []
    return (filtered, {'mode': 'harvest_allowlist', 'allowed_skill_ids': sorted(allowed_ids), 'observed_available_skill_ids': sorted((item['skill_id'] for item in available)), 'planner_candidate_ids': sorted((item['skill_id'] for item in filtered)), 'deferred_gold_until_lower_owned': deferred_gold})

def _planner_preference_from_build_context(build_context: dict[str, Any], catalog: SkillCatalog) -> SkillPreferenceProfile:
    profile = default_terminal_skill_preference_profile(str(build_context['target_mode']))
    if not _harvest_spenddown_requested(build_context):
        return profile
    rules = []
    for skill_id, row in catalog.rows.items():
        conditions = row.conditions
        styles, distances = (conditions.get('style', []), conditions.get('distance', []))
        if row.type == 'green' and 'any' in styles or not ('any' in styles or build_context['race_profile']['style'] in styles) or set(conditions) - {'distance', 'style', 'phase_hint'}:
            continue
        rules.append({'selector': {'kind': 'skill_id', 'value': skill_id}, 'mode': 'prefer', 'priority': 0, 'weight': 100000 if 'any' in styles and 'any' in distances else 10000 if 'any' in distances else 1000})
    profile = parse_skill_preference_profile({'schema_version': profile.schema_version, 'profile_id': 'harvest-terminal-spenddown', 'base_profile': 'race', 'reserve_skill_pt': 0, 'filler_policy': 'spend_all_safe', 'max_batch_size': 48, 'rules': rules})
    from core.harvest_presets import prioritize_skills
    return prioritize_skills(profile, build_context.get('harvest_skill_list'), catalog)

def _plan_harvest_styles(context_data, build_context, catalog, preference_profile=None):
    styles = build_context.get('harvest_style_candidates') if _harvest_spenddown_requested(build_context) else None
    if not styles:
        return plan_skill_purchases(SkillPurchaseContext.from_dict(context_data), catalog, preference_profile=preference_profile or _planner_preference_from_build_context(build_context, catalog))
    if not isinstance(styles, list) or len(set(styles)) != len(styles) or any((s not in {'leader', 'front', 'betweener', 'chaser'} for s in styles)):
        raise ValueError('harvest_style_candidates_invalid')
    options = []
    for style in styles:
        build = {**build_context, 'race_profile': {**build_context['race_profile'], 'style': style}}
        data = {**context_data, 'race_profile': build['race_profile']}
        plan = plan_skill_purchases(SkillPurchaseContext.from_dict(data), catalog, preference_profile=preference_profile or _planner_preference_from_build_context(build, catalog))
        if plan.optimizer_status not in {'ok', 'bounded_best_available'}:
            raise ValueError('harvest_style_comparison_incomplete')
        general = [p for p in plan.purchases if 'any' in catalog.rows[p.skill_id].conditions.get('style', []) and 'any' in catalog.rows[p.skill_id].conditions.get('distance', [])]
        utility = sum((p.marginal_utility if p.marginal_utility is not None else p.score for p in plan.purchases))
        general_utility = sum((p.marginal_utility if p.marginal_utility is not None else p.score for p in general))
        options.append(((general_utility, utility, utility / max(1, plan.spent_skill_pt)), style, plan))
    _, style, plan = max(options, key=lambda item: item[0])
    build_context['race_profile'] = {**build_context['race_profile'], 'style': style}
    build_context['harvest_style_decision'] = {'selected': style, 'comparisons': [{'style': s, 'general_utility': rank[0], 'total_utility': rank[1], 'utility_per_pt': rank[2], 'spent_pt': p.spent_skill_pt} for rank, s, p in options]}
    return plan

@harvest_skill_ocr()
def run_fresh_dry_run(*, pages: list[Path], resolver_catalog: Path, planner_catalog: Path | None, output_dir: Path, turn_id: str, build_context: dict[str, Any] | None, preference_profile: SkillPreferenceProfile | None=None, prior_owned_skill_ids: list[str] | None=None, capture_manifest: dict[str, Any] | None=None) -> dict[str, Any]:
    """Parse fresh files and issue an offline-only, fail-closed planner report."""
    binding = capture_fingerprint(pages, turn_id=turn_id, capture_manifest=capture_manifest)
    parsed = parse_skill_acquisition_pages(pages, output_dir=output_dir / 'parser_output', assume_continuous_scroll=True)
    resolved = resolve_skill_manifest(parsed, load_skill_catalog(resolver_catalog), manifest_dir=output_dir / 'parser_output')
    mapping = {item['name']: item['skill_id'] for item in resolved.get('skills', []) if item.get('name') and item.get('skill_id')}
    observation = build_skill_planner_observation(resolved, name_to_skill_id=mapping)
    if prior_owned_skill_ids is not None:
        if not isinstance(prior_owned_skill_ids, list) or any((not isinstance(item, str) or not item for item in prior_owned_skill_ids)) or len(prior_owned_skill_ids) != len(set(prior_owned_skill_ids)):
            raise ValueError('prior_owned_skill_ids_invalid')
        observation['owned_skills'] = sorted(set(observation['owned_skills']) | set(prior_owned_skill_ids))
    planner_available_skills, planner_candidate_filter = _planner_available_skills(observation, build_context, SkillCatalog.load(planner_catalog) if planner_catalog is not None else None)
    verdict = build_replay_verdict(parsed_manifest=parsed, resolved_manifest=resolved, planner_observation=observation, binding=validate_capture_binding(binding, binding))
    if not _build_metadata_known(build_context):
        verdict['planner_verdict'] = 'PLANNER_BLOCKED_BUILD_METADATA_UNKNOWN'
        dry_run = {'executed': True, 'purchases': [], 'skipped': [], 'spent_skill_pt': 0, 'remaining_skill_pt': parsed.get('current_skill_points'), 'reason': 'target_mode_distance_type_and_style_are_required'}
    elif parsed.get('current_skill_points') is None:
        verdict['planner_verdict'] = 'PLANNER_BLOCKED_NO_FRESH_INPUT'
        dry_run = {'executed': False, 'purchases': [], 'skipped': [], 'spent_skill_pt': 0, 'remaining_skill_pt': None, 'reason': 'fresh_parser_current_skill_points_unreadable'}
    elif planner_catalog is None:
        verdict['planner_verdict'] = 'PLANNER_BLOCKED_CATALOG_MISSING'
        dry_run = {'executed': False, 'purchases': [], 'skipped': [], 'spent_skill_pt': 0, 'remaining_skill_pt': parsed['current_skill_points'], 'reason': 'planner_catalog_required_when_build_metadata_is_known'}
    else:
        context_data = dict(build_context)
        context_data.update({'schema_version': 'uma.skill_purchase_context.v1', 'trigger': 'final_build', 'source_capture_sha256': binding['capture_sha256'], 'current_skill_pt': parsed['current_skill_points'], 'owned_skills': observation['owned_skills'], 'available_skills': planner_available_skills, 'policy': _planner_policy_from_build_context(build_context)})
        catalog = SkillCatalog.load(planner_catalog)
        plan = _plan_harvest_styles(context_data, build_context, catalog, preference_profile)
        dry_run = {'executed': True, **plan.to_dict()}
    inventory_packet: dict[str, Any] | None = None
    batch_plan: dict[str, Any] | None = None
    batch_plan_omitted_action_ids: list[str] = []
    if isinstance(parsed.get('current_skill_points'), int) and capture_manifest is not None:
        inventory_packet = build_skill_inventory_packet(capture_binding=binding, planner_observation=observation, planner_plan={**dry_run, 'resource_checks': {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}}, build_context=build_context or {})
        local_action_ids = [str(item['skill_id']) for item in dry_run.get('purchases', []) if isinstance(item, dict) and isinstance(item.get('skill_id'), str)]
        allowed = set(inventory_packet['allowed_action_ids'])
        batch_plan_omitted_action_ids = [action_id for action_id in local_action_ids if action_id not in allowed]
        batch_plan = build_skill_batch_plan(inventory=inventory_packet, selected_action_ids=[action_id for action_id in local_action_ids if action_id in allowed], decision_source='local_auto', reverse_scroll_shifts=_harvest_reverse_shifts(capture_manifest, build_context))
    report = {'schema_version': 'uma.fresh_skill_acquisition_dry_run.v1', 'mode': 'offline_dry_run_only', 'execution_allowed': False, 'currency_scope': 'skill_points_only', 'capture_binding': binding, 'verdict': verdict, 'build_context_provided': _build_metadata_known(build_context), 'harvest_style_decision': (build_context or {}).get('harvest_style_decision'), 'parser_manifest': parsed, 'resolver_summary': resolved['resolution_summary'], 'planner_observation': observation, 'planner_candidate_filter': planner_candidate_filter, 'inventory_packet': inventory_packet, 'batch_plan': batch_plan, 'batch_plan_omitted_action_ids': batch_plan_omitted_action_ids, 'planner_dry_run': {**dry_run, 'resource_checks': {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}}, 'notes': ['The supplied pages are the only recognition input for this report.', 'This is not a purchase command and cannot invoke a live executor.']}
    _write_json(output_dir / 'fresh_skill_dry_run.report.json', report)
    return report

def replan_fresh_report(*, source_report_path: Path, planner_catalog: Path, output_dir: Path, build_context: dict[str, Any], preference_profile: SkillPreferenceProfile | None=None) -> dict[str, Any]:
    """Replan one SHA-bound fresh report without parsing its screenshots again."""
    if source_report_path.resolve() == (output_dir / 'planner' / 'fresh_skill_dry_run.report.json').resolve():
        raise ValueError('source_report_output_conflict')
    raw = source_report_path.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    source = json.loads(raw.decode('utf-8'))
    if not isinstance(source, dict) or source.get('schema_version') != 'uma.fresh_skill_acquisition_dry_run.v1':
        raise ValueError('source_report_schema_invalid')
    parsed = source.get('parser_manifest')
    observation = source.get('planner_observation')
    binding = source.get('capture_binding')
    if not isinstance(parsed, dict) or not isinstance(observation, dict) or (not isinstance(binding, dict)) or (not isinstance(parsed.get('current_skill_points'), int)) or (observation.get('current_skill_pt') != parsed['current_skill_points']):
        raise ValueError('source_report_observation_invalid')
    if binding.get('schema_version') != 'uma.skill_capture_binding.v1' or not isinstance(binding.get('capture_sha256'), str) or len(binding['capture_sha256']) != 64 or (not isinstance(binding.get('page_hashes'), list)) or (not binding['page_hashes']) or (not isinstance(binding.get('captured_at_unix_ms'), int)) or (not isinstance(binding.get('expires_at_unix_ms'), int)) or (binding['expires_at_unix_ms'] <= binding['captured_at_unix_ms']):
        raise ValueError('source_report_capture_binding_invalid')
    if not _build_metadata_known(build_context):
        raise ValueError('build_context_invalid')
    planner_available_skills, planner_candidate_filter = _planner_available_skills(observation, build_context, SkillCatalog.load(planner_catalog))
    context_data = dict(build_context)
    context_data.update({'schema_version': 'uma.skill_purchase_context.v1', 'trigger': 'final_build', 'source_capture_sha256': binding['capture_sha256'], 'current_skill_pt': parsed['current_skill_points'], 'owned_skills': observation.get('owned_skills'), 'available_skills': planner_available_skills, 'policy': _planner_policy_from_build_context(build_context)})
    catalog = SkillCatalog.load(planner_catalog)
    plan = _plan_harvest_styles(context_data, build_context, catalog, preference_profile)
    dry_run = {'executed': True, **plan.to_dict(), 'resource_checks': {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}}
    inventory = build_skill_inventory_packet(capture_binding=binding, planner_observation=observation, planner_plan=dry_run, build_context=build_context)
    selected = [item['skill_id'] for item in dry_run.get('purchases', []) if isinstance(item, dict) and item.get('skill_id') in set(inventory['allowed_action_ids'])]
    batch = build_skill_batch_plan(inventory=inventory, selected_action_ids=selected, decision_source='local_auto', reverse_scroll_shifts=(source.get('batch_plan') or {}).get('reverse_scroll_shifts'))
    verdict = dict(source.get('verdict', {}))
    verdict['planner_verdict'] = 'PLANNER_READY' if dry_run.get('status') == 'ok' else 'PLANNER_BLOCKED_NO_FRESH_INPUT'
    planned_ids = [item['skill_id'] for item in dry_run.get('purchases', []) if isinstance(item, dict) and isinstance(item.get('skill_id'), str)]
    report = {'schema_version': 'uma.fresh_skill_acquisition_dry_run.v1', 'mode': 'offline_dry_run_only', 'execution_allowed': False, 'currency_scope': 'skill_points_only', 'source_report_sha256': source_sha256, 'source_capture_sha256': binding['capture_sha256'], 'capture_binding': binding, 'verdict': verdict, 'build_context_provided': True, 'parser_manifest': parsed, 'harvest_style_decision': build_context.get('harvest_style_decision'), 'resolver_summary': source.get('resolver_summary', {}), 'planner_observation': observation, 'planner_candidate_filter': planner_candidate_filter, 'inventory_packet': inventory, 'batch_plan': batch, 'batch_plan_omitted_action_ids': [item for item in planned_ids if item not in set(inventory['allowed_action_ids'])], 'planner_dry_run': dry_run, 'notes': ['Replanned from one SHA-bound fresh report without OCR or device access.']}
    _write_json(output_dir / 'planner' / 'fresh_skill_dry_run.report.json', report)
    return report

def _harvest_reverse_shifts(manifest: dict[str, Any], context: dict[str, Any] | None) -> list[float] | None:
    if manifest.get('swipe_profile') != SKILL_OVERLAP_SWIPE:
        return None
    overlaps = manifest.get('overlap_evidence')
    count = manifest.get('captured_page_count')
    if type(count) is not int or count < 1 or (not isinstance(overlaps, list)) or (len(overlaps) != count - 1) or any((not isinstance(item, dict) or item.get('verified') is not True or item.get('page_index') != index for index, item in enumerate(overlaps, 1))):
        raise ValueError('harvest_reverse_overlap_unverified')
    return [item['shift'] for item in overlaps]

def _read_context(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('build context must contain a JSON object')
    return data

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Offline fresh skill-page parse and planner dry-run.')
    parser.add_argument('--pages', nargs='+', type=Path, required=True)
    parser.add_argument('--resolver-catalog', type=Path, required=True)
    parser.add_argument('--planner-catalog', type=Path)
    parser.add_argument('--turn-id', required=True)
    parser.add_argument('--build-context', type=Path)
    parser.add_argument('--preference-profile', type=Path)
    parser.add_argument('--capture-manifest', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    return parser

def main(argv: list[str] | None=None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = load_skill_preference_profile(args.preference_profile) if args.preference_profile is not None else None
        report = run_fresh_dry_run(pages=args.pages, resolver_catalog=args.resolver_catalog, planner_catalog=args.planner_catalog, output_dir=args.output_dir, turn_id=args.turn_id, build_context=_read_context(args.build_context), preference_profile=profile, capture_manifest=_read_context(args.capture_manifest))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f'fresh skill dry-run input error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(report['verdict'], ensure_ascii=False, indent=2))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
