from __future__ import annotations
from core.skill_planner.models import AvailableSkill, SkillCatalogRow, SkillPurchaseContext

def hint_discounted_cost(display_cost: int, hint_level: int) -> int:
    rates = {0: 0.0, 1: 0.05, 2: 0.1, 3: 0.2, 4: 0.3, 5: 0.4}
    clamped = max(0, min(5, int(hint_level)))
    return int(round(display_cost * (1.0 - rates[clamped])))

def has_recovery_owned(context: SkillPurchaseContext, catalog_rows: dict[str, SkillCatalogRow]) -> bool:
    for skill_id in context.owned_skills:
        row = catalog_rows.get(skill_id)
        if row and row.type == 'recovery':
            return True
    return False

def is_covered_by_owned_upper(skill_id: str, row: SkillCatalogRow, owned_skills: set[str], catalog_rows: dict[str, SkillCatalogRow] | None=None) -> bool:
    if skill_id in owned_skills:
        return False
    return any((upper in owned_skills for upper in row.relations.get('upper', []))) or any((skill_id in owned.relations.get('supersedes', []) for owned_id in owned_skills if (owned := (catalog_rows or {}).get(owned_id)) is not None))

def missing_lower_prerequisite(row: SkillCatalogRow, owned_skills: set[str], available_skill_ids: set[str], allow_gold_without_lower: bool) -> bool:
    if allow_gold_without_lower:
        return False
    lower = row.relations.get('lower', [])
    if not lower:
        return False
    return not any((skill_id in owned_skills or skill_id in available_skill_ids for skill_id in lower))

def condition_matches(context: SkillPurchaseContext, row: SkillCatalogRow) -> bool:
    conditions = row.conditions
    distance = conditions.get('distance')
    if isinstance(distance, list) and 'any' not in distance:
        if context.race_profile.get('distance_type') not in distance:
            return False
    style = conditions.get('style')
    if isinstance(style, list) and 'any' not in style:
        planned_style = context.race_profile.get('style') or context.character.get('planned_style')
        if planned_style not in style:
            return False
    ground = conditions.get('ground_condition')
    if isinstance(ground, list) and 'any' not in ground:
        if context.race_profile.get('ground_condition') not in ground:
            return False
    season = conditions.get('season')
    if isinstance(season, list):
        if context.race_profile.get('season') not in season:
            return False
    multiple = conditions.get('distance_m_multiple_of')
    if multiple is not None:
        distance_m = int(context.race_profile.get('distance_m', 0) or 0)
        if distance_m <= 0 or distance_m % int(multiple) != 0:
            return False
    return True

def eligible_visible_skills(context: SkillPurchaseContext, catalog_rows: dict[str, SkillCatalogRow]) -> tuple[list[tuple[AvailableSkill, SkillCatalogRow]], list[tuple[AvailableSkill, str]]]:
    eligible: list[tuple[AvailableSkill, SkillCatalogRow]] = []
    skipped: list[tuple[AvailableSkill, str]] = []
    owned_skills = set(context.owned_skills)
    available_skill_ids = {item.skill_id for item in context.available_skills}
    for visible in context.available_skills:
        row = catalog_rows.get(visible.skill_id)
        if row is None:
            skipped.append((visible, 'unknown_skill'))
            continue
        if visible.recognition_confidence < context.policy.min_confidence:
            skipped.append((visible, 'recognition_confidence_low'))
            continue
        if visible.skill_id in owned_skills:
            skipped.append((visible, 'already_owned'))
            continue
        if is_covered_by_owned_upper(visible.skill_id, row, owned_skills, catalog_rows):
            skipped.append((visible, 'covered_by_upper'))
            continue
        if missing_lower_prerequisite(row, owned_skills, available_skill_ids, context.policy.allow_gold_without_lower):
            skipped.append((visible, 'missing_prerequisite'))
            continue
        if not row.is_verified and (not context.policy.allow_unverified_skills):
            skipped.append((visible, 'unverified_not_allowed'))
            continue
        if context.policy.require_condition_match and (not condition_matches(context, row)):
            skipped.append((visible, 'condition_mismatch'))
            continue
        eligible.append((visible, row))
    return (eligible, skipped)

def base_score_and_bucket(row: SkillCatalogRow, target_mode: str) -> tuple[float, str]:
    if target_mode == 'parent':
        if row.rarity == 'normal' and row.type == 'speed':
            return (76.0, 'parent_hint')
        if row.type == 'green':
            return (70.0, 'green')
        if row.type == 'recovery':
            return (48.0, 'recovery')
        if row.type == 'acceleration':
            return (45.0, 'core_accel')
        return (30.0, 'cheap_filler')
    if row.type == 'acceleration':
        return (100.0, 'core_accel')
    if row.type == 'speed':
        return (75.0, 'core_speed')
    if row.type == 'recovery':
        return (60.0, 'recovery')
    if row.type == 'green':
        return (55.0, 'green')
    return (20.0, 'cheap_filler')
