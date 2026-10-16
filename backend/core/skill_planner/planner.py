from __future__ import annotations
from core.skill_planner.candidate_set import build_skill_candidate_set
from core.skill_planner.catalog import SkillCatalog
from core.skill_planner.global_optimizer import optimize_final_skill_build
from core.skill_planner.models import AvailableSkill, PlannedPurchase, SkillCatalogRow, SkillPurchaseContext, SkillPurchasePlan, SkippedSkill
from core.skill_planner.scoring import base_score_and_bucket, eligible_visible_skills, has_recovery_owned, hint_discounted_cost
from core.skill_planner.preferences import SkillPreferenceProfile, default_skill_preference_profile
PLAN_SCHEMA = 'uma_skill_purchase_plan.v1'

def _visible_hint_and_cost(context: SkillPurchaseContext, visible: AvailableSkill) -> tuple[int, int]:
    if visible.cost_basis == 'observed_final':
        hint_level = visible.observed_hint_level
        if hint_level is None:
            hint_level = context.hint_levels.get(visible.skill_id, 0)
        return (int(hint_level), visible.display_cost)
    hint_level = context.hint_levels.get(visible.skill_id, 0)
    return (hint_level, hint_discounted_cost(visible.display_cost, hint_level))

def plan_skill_purchases(context: SkillPurchaseContext, catalog: SkillCatalog, *, preference_profile: SkillPreferenceProfile | None=None) -> SkillPurchasePlan:
    if context.trigger == 'scheduled_learning':
        return _plan_scheduled_learning(context, catalog)
    if context.trigger == 'support_activation':
        return _plan_support_activation(context, catalog)
    if context.trigger == 'final_build':
        return _plan_final_build(context, catalog, preference_profile=preference_profile)
    return _empty_plan(context, status='blocked', warnings=['unsupported_trigger'], explain=f'Unsupported trigger: {context.trigger}')

def _empty_plan(context: SkillPurchaseContext, *, status: str='ok', skipped: list[SkippedSkill] | None=None, warnings: list[str] | None=None, risk_flags: list[str] | None=None, explain: str='') -> SkillPurchasePlan:
    return SkillPurchasePlan(schema_version=PLAN_SCHEMA, target_mode=context.target_mode, trigger=context.trigger, status=status, skipped=skipped or [], reserved_skill_pt=context.policy.reserve_skill_pt, remaining_skill_pt=context.current_skill_pt, warnings=warnings or [], risk_flags=risk_flags or [], explain=explain)

def _skipped_from_raw(raw: list[tuple[AvailableSkill, str]], catalog: SkillCatalog) -> list[SkippedSkill]:
    skipped: list[SkippedSkill] = []
    for visible, reason in raw:
        row = catalog.maybe_get(visible.skill_id)
        skipped.append(SkippedSkill(skill_id=visible.skill_id, name=row.name_zh_cn if row else visible.skill_id, reason_code=reason, details=reason))
    return skipped

def _long_race_recovery_needed(context: SkillPurchaseContext, catalog: SkillCatalog) -> bool:
    distance_type = context.race_profile.get('distance_type')
    distance_m = int(context.race_profile.get('distance_m', 0) or 0)
    stamina = int(context.current_stats.get('stamina', 9999))
    return distance_type == 'long' and distance_m >= 2500 and (stamina < 650) and (not has_recovery_owned(context, catalog.rows))

def _plan_scheduled_learning(context: SkillPurchaseContext, catalog: SkillCatalog) -> SkillPurchasePlan:
    eligible, skipped_raw = eligible_visible_skills(context, catalog.rows)
    skipped = _skipped_from_raw(skipped_raw, catalog)
    recovery_needed = _long_race_recovery_needed(context, catalog)
    force_buy = context.policy.scheduled_learning_mode == 'force_buy'
    if not recovery_needed and (not force_buy) and context.policy.allow_skip_purchase:
        return _empty_plan(context, skipped=skipped, explain='Scheduled learning risk is acceptable; preserve points.')
    candidates: list[tuple[float, int, str, AvailableSkill, SkillCatalogRow, int, str, list[str]]] = []
    for visible, row in eligible:
        if recovery_needed and row.type != 'recovery':
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'scheduled_learning_recovery_required', 'Long target race with low stamina prioritizes recovery.'))
            continue
        hint_level, final_cost = _visible_hint_and_cost(context, visible)
        if recovery_needed:
            score = 85.0
            bucket = 'recovery'
            reasons = ['scheduled_long_race_recovery']
        else:
            score, bucket = base_score_and_bucket(row, context.target_mode)
            if row.type == 'green':
                score += 10.0
            reasons = ['force_buy_policy', bucket]
        candidates.append((-score, final_cost, row.skill_id, visible, row, hint_level, bucket, reasons))
    return _buy_from_candidates(context, candidates, skipped, max_count=1, explain_when_bought='Scheduled learning selected a survival skill.', explain_when_empty='No eligible scheduled learning purchase.')

def _plan_support_activation(context: SkillPurchaseContext, catalog: SkillCatalog) -> SkillPurchasePlan:
    eligible: list[tuple[AvailableSkill, SkillCatalogRow]] = []
    skipped: list[SkippedSkill] = []
    owned_skills = set(context.owned_skills)
    for visible in context.available_skills:
        row = catalog.maybe_get(visible.skill_id)
        if row is None:
            skipped.append(SkippedSkill(visible.skill_id, visible.skill_id, 'unknown_skill', 'unknown_skill'))
            continue
        if visible.recognition_confidence < context.policy.min_confidence:
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'recognition_confidence_low', 'recognition_confidence_low'))
            continue
        if visible.skill_id in owned_skills:
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'already_owned', 'already_owned'))
            continue
        if not row.is_verified and (not context.policy.allow_unverified_skills):
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'unverified_not_allowed', 'unverified_not_allowed'))
            continue
        eligible.append((visible, row))
    candidates: list[tuple[float, int, str, AvailableSkill, SkillCatalogRow, int, str, list[str]]] = []
    for visible, row in eligible:
        hint_level, final_cost = _visible_hint_and_cost(context, visible)
        if row.rarity != 'normal':
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'support_activation_gold_excluded', 'Support activation buys normal skills only.'))
            continue
        if row.type != 'speed':
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'support_activation_speed_only', 'Support activation buys speed skills only.'))
            continue
        if hint_level < 5:
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'hint_not_full_discount', 'Support activation requires full discount.'))
            continue
        candidates.append((-65.0, final_cost, row.skill_id, visible, row, hint_level, 'support_activation', ['support_activation_full_discount_speed']))
    return _buy_from_candidates(context, candidates, skipped, max_count=1, explain_when_bought='Support activation selected a full-discount normal speed skill.', explain_when_empty='No support activation skill selected.')

def _plan_final_build(context: SkillPurchaseContext, catalog: SkillCatalog, *, preference_profile: SkillPreferenceProfile | None) -> SkillPurchasePlan:
    profile = preference_profile
    if profile is None:
        base_profile = context.target_mode if context.target_mode in {'parent', 'race'} else 'parent'
        profile = default_skill_preference_profile(base_profile)
    candidate_set = build_skill_candidate_set(context, profile_sha256=profile.profile_sha256)
    return optimize_final_skill_build(context, catalog, profile, candidate_set)

def _buy_from_candidates(context: SkillPurchaseContext, candidates: list[tuple[float, int, str, AvailableSkill, SkillCatalogRow, int, str, list[str]]], skipped: list[SkippedSkill], *, max_count: int | None, explain_when_bought: str, explain_when_empty: str) -> SkillPurchasePlan:
    candidates.sort()
    purchases: list[PlannedPurchase] = []
    spent = 0
    max_allowed = max_count
    for neg_score, final_cost, _skill_id, visible, row, hint_level, bucket, reasons in candidates:
        if max_allowed is not None and len(purchases) >= max_allowed:
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'max_purchase_count_reached', 'Purchase count cap reached.'))
            continue
        if context.current_skill_pt - spent - final_cost < context.policy.reserve_skill_pt:
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'insufficient_pt_after_reserve', 'Reserve would be broken.'))
            continue
        if context.policy.trigger_budget is not None and spent + final_cost > context.policy.trigger_budget:
            skipped.append(SkippedSkill(row.skill_id, row.name_zh_cn, 'trigger_budget_exceeded', 'Trigger budget would be exceeded.'))
            continue
        purchases.append(PlannedPurchase(skill_id=row.skill_id, name=row.name_zh_cn, type=row.type, rarity=row.rarity, display_cost=visible.display_cost, final_cost=final_cost, hint_level=hint_level, score=round(-neg_score, 2), priority_bucket=bucket, reasons=list(reasons), risk_flags=['manual_verification_required'] if not row.is_verified else []))
        spent += final_cost
    status = 'ok' if purchases or context.policy.allow_skip_purchase else 'partial'
    risk_flags = ['uses_unverified_catalog_rows'] if any((p.risk_flags for p in purchases)) else []
    return SkillPurchasePlan(schema_version=PLAN_SCHEMA, target_mode=context.target_mode, trigger=context.trigger, status=status, purchases=purchases, skipped=skipped, reserved_skill_pt=context.policy.reserve_skill_pt, spent_skill_pt=spent, remaining_skill_pt=context.current_skill_pt - spent, risk_flags=risk_flags, explain=explain_when_bought if purchases else explain_when_empty)
