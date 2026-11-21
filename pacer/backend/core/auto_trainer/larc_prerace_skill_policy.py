"""Pure first-Arc preferences; reuse D1's catalog, optimizer and purchase path.

Discount savings and race urgency are policy estimates, not promised future
hints or a race simulator. This module deliberately has no project imports:
the isolated worker can load it while D1 owns the core/scripts namespaces.
"""
from copy import deepcopy


DEFAULTS = {"spend_fraction": .60, "urgent_extra_fraction": .25,
            "preference_credit": .25, "future_hint_credit": .65,
            "saved_point_value": .7, "discount_preference": .6,
            "maximum_hint_discount": 40., "survival_preference": 80.}


def classic_skill_preference(profile, inventory, catalog, *, context):
    settings = {**DEFAULTS, **context.get("larc_prerace_policy", {})}
    result = deepcopy(profile)
    rows = {r["skill_id"]: r for r in catalog.get("skills", []) if isinstance(r, dict) and r.get("skill_id")}
    visible = {r["skill_id"]: r for r in inventory.get("candidates", []) if isinstance(r, dict) and r.get("skill_id")}
    owned = set(inventory.get("owned_skill_ids", []))
    points = inventory.get("current_skill_points", 0)
    if type(points) is not int or points < 0:
        points = 0
    stats = context.get("current_stats", {})
    default_speed = 1000 if context.get("race_profile", {}).get("style") == "leader" else 900

    def urgency(key, target):
        actual = stats.get(key)
        return min(1., max(0., (target - actual) / 200.)) if type(actual) in {int, float} else 0.

    speed_need = urgency("speed", settings.get("speed_soft_target", default_speed))
    # These are tunable preparation preferences, never minimum winning stats.
    # Missing stamina remains unknown instead of inventing a recovery deficit.
    stamina_need = urgency("stamina", settings.get("stamina_soft_target", 700))
    if any(rows.get(x, {}).get("type") == "recovery" for x in owned):
        stamina_need *= .25

    def remaining_base(skill_id, seen=()):
        if skill_id in owned or skill_id in seen:
            return 0.
        row = rows.get(skill_id, {})
        lower = row.get("relations", {}).get("lower", [])
        total = row.get("base_cost", 0) or 0
        upgrade = row.get("knowledge", {}).get("upgrade_cost")
        if type(upgrade) not in {int, float}:
            upgrade = max(0., total - sum(rows.get(x, {}).get("base_cost", 0) or 0 for x in lower))
        return upgrade + sum(remaining_base(x, (*seen, skill_id)) for x in lower)

    rules = result.setdefault("rules", [])
    for rule in rules:
        if rule.get("mode") == "prefer" and rule.get("weight", 0) > 0:
            rule["weight"] *= settings["preference_credit"]
    required = set(settings.get("required_now_skill_ids", []))
    required.update(r["selector"]["value"] for r in rules
                    if r.get("mode") == "must_buy_if_safe" and r.get("selector", {}).get("kind") == "skill_id")
    required.difference_update(r["selector"]["value"] for r in rules
                    if r.get("mode") == "avoid" and r.get("selector", {}).get("kind") == "skill_id")
    diagnostics = []
    for skill_id, item in visible.items():
        cost, row = item.get("display_cost"), rows.get(skill_id, {})
        if skill_id in owned or type(cost) is not int or cost <= 0 or not row:
            continue
        base = remaining_base(skill_id)
        # The displayed price is already discounted and includes unowned lower
        # skills. Use that whole chain; never apply the gold's discount twice.
        discount = max(0., 100. * (1 - cost / base)) if base > 0 else None
        potential = max(0., cost - base * (1 - settings["maximum_hint_discount"] / 100.)) if base > 0 else 0.
        saved_estimate = potential * settings["future_hint_credit"]
        need = speed_need if row.get("type") == "acceleration" else stamina_need if row.get("type") == "recovery" else 0.
        weight = ((discount or 0.) * settings["discount_preference"]
                  - saved_estimate * settings["saved_point_value"]
                  + need * settings["survival_preference"])
        if skill_id in required:
            weight = max(0., weight)
            if not any(r.get("mode") in {"avoid", "must_buy_if_safe"}
                       and r.get("selector") == {"kind": "skill_id", "value": skill_id} for r in rules):
                rules.append({"selector": {"kind": "skill_id", "value": skill_id},
                              "mode": "must_buy_if_safe", "priority": 0, "weight": 0.})
        existing = next((r for r in rules if r.get("mode") == "prefer"
                         and r.get("selector") == {"kind": "skill_id", "value": skill_id}), None)
        if existing is not None:
            existing["weight"] = round(existing.get("weight", 0.) + weight, 4)
        else:
            rules.append({"selector": {"kind": "skill_id", "value": skill_id},
                          "mode": "prefer", "priority": 0, "weight": round(weight, 4)})
        diagnostics.append({"skill_id": skill_id, "displayed_chain_cost": cost,
            "remaining_undiscounted_chain_cost": base,
            "effective_chain_discount_percent": round(discount, 2) if discount is not None else None,
            "lower_skills": row.get("relations", {}).get("lower", []),
            "future_hint_savings_estimate": round(saved_estimate, 2),
            "survival_urgency_estimate": round(need, 4), "preference_adjustment": round(weight, 4)})
    fraction = min(1., max(0., settings["spend_fraction"]
                          + settings["urgent_extra_fraction"] * max(speed_need, stamina_need)))
    covered = {x for skill_id in required for x in rows.get(skill_id, {}).get("relations", {}).get("lower", [])}
    required_cost = sum(visible.get(x, {}).get("display_cost", 0) or 0 for x in required - covered - owned)
    budget = min(points, max(round(points * fraction), required_cost))
    result["reserve_skill_pt"] = max(result.get("reserve_skill_pt", 0), points - budget)
    result["filler_policy"] = "safe_useful_only"
    result["profile_id"] += "-classic-hint-timing"
    return result, {"stage": "first_arc", "spending_budget": points - result["reserve_skill_pt"],
        "reserved_skill_points": result["reserve_skill_pt"], "settings": settings, "skills": diagnostics,
        "observed_stats": stats, "stamina_need_status": "estimated" if type(stats.get("stamina")) in {int, float} else "unknown",
        "basis": "whole observed skill-chain price; discounted possible future hints; tunable early-race budget"}
