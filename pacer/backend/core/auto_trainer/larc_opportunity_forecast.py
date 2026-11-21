"""Numerical opportunity estimates, never a prescribed future training order.

Choose within each observed layout before averaging. A forecast shortfall is
diagnostic and cannot feed back into the marginal score that generated it.
"""
from core.auto_trainer.state import TRAINING_TYPES


MAX_GAP_PREMIUM = .6


def raw_gain(current, gain):
    return gain + max(0., current + gain - max(1200., current))


def display_gain(current, raw):
    below = min(raw, max(0., 1200. - current))
    return below + (raw - below) / 2.


def soft_gaps(current, target, rates, budget, stage_weight, rewards=None):
    """Bounded preference for an observed deficit, independent of allocation."""
    rows = {}
    for key in TRAINING_TYPES:
        gap = max(0., target[key] - current[key] - (rewards or {}).get(key, 0.))
        rate = max((display_gain(current[key], row.get(key, 0.))
                    for row in rates.values()), default=0.)
        needed = gap / rate if rate > 0 else None
        pressure = needed / (max(1., budget) + needed) if needed is not None else float(gap > 0)
        premium = min(MAX_GAP_PREMIUM,
                      .15 * min(1., gap / max(1., target[key]))
                      + .45 * stage_weight * pressure) if gap else 0.
        rows[key] = {"target": target[key], "active_gap": gap, "estimated_trainings_needed": needed,
                     "gap_premium": premium}
    return rows


def effective_far_surplus_edge(eff_target: float, buffer: float, exact_buffer: bool = False) -> float:
    """Return the far-surplus inflection threshold in display-stat units."""
    if not exact_buffer:
        return eff_target + max(buffer, .1 * eff_target)
    eff_buf = min(buffer, max(10., 1590. - eff_target)) if eff_target >= 1540. else buffer
    return eff_target + eff_buf


def marginal_gains(current, gains, gaps, *, attribute_weights=None, after_target_credit=1.,
                   high_stat_scarcity=True, restore_high_stat_value=False, adaptive_high_stat_value=False,
                   far_surplus_credit=None, far_surplus_buffer=100., far_surplus_exact_buffer=False,
                   scarcity_factors=None):
    """Same attribute scale for current training and hypothetical layouts."""
    base = premium = scarcity = 0.
    equivalent = {};far_discount=0.
    for key in TRAINING_TYPES:
        amount = max(0., gains.get(key, 0.))
        row = gaps[key]
        eff_target = row.get("effective_target", row["target"])
        relative = (attribute_weights or {}).get(key, 1.)
        below = min(amount, max(0., eff_target - current.get(key, 0.))) if attribute_weights is not None else amount
        base += relative * (below + (amount - below) * after_target_credit)
        if far_surplus_credit is not None and attribute_weights is not None:
            edge=effective_far_surplus_edge(eff_target, far_surplus_buffer, far_surplus_exact_buffer)
            far=max(0.,current.get(key,0.)+amount-max(current.get(key,0.),edge))
            loss=relative*far*max(0.,after_target_credit-far_surplus_credit)
            base-=loss;far_discount+=loss
        premium += relative * min(amount, row["active_gap"]) * row["gap_premium"]
        extra = max(0., min(current.get(key, 0.) + amount,
                           current.get(key, 0.) + row["active_gap"])
                    - max(1200., current.get(key, 0.)))
        scarcity_scale = (scarcity_factors or {}).get(key, 1.)
        if restore_high_stat_value:
            high_target = max(0., min(current.get(key, 0.) + amount, eff_target)
                              - max(1200., current.get(key, 0.)))
            high_gap = min(high_target, extra)
            scarcity += relative * scarcity_scale * (high_target + high_gap * row["gap_premium"])
            equivalent[key] = amount + high_target * scarcity_scale
        else:
            if high_stat_scarcity:
                scarcity += relative * scarcity_scale * extra * (1 + row["gap_premium"])
            equivalent[key] = amount + extra * scarcity_scale
    return {"base": base, "gap_premium": premium, "scarcity": scarcity,
            **({'far_surplus_discount':far_discount}if far_surplus_credit is not None else{}),
            "training_equivalent_gains": equivalent}


def project_opportunities(current, target, rates, layouts, budget, *,
                          stage_weight=1., pt_weight=.85, scale=1., rewards=None, attribute_weights=None,
                          after_target_credit=1., high_stat_scarcity=True, restore_high_stat_value=False,
                          adaptive_high_stat_value=False,far_surplus_credit=None,far_surplus_buffer=100.,
                          far_surplus_exact_buffer=False):
    values = {key: current[key] + (rewards or {}).get(key, 0.) for key in TRAINING_TYPES}
    counts = {tab: 0. for tab in rates}
    collateral = {key: 0. for key in TRAINING_TYPES}
    layouts = [layout for layout in layouts if layout.get("gains")]
    if not layouts and rates:
        layouts = [{"gains": rates, "skill_points": {}}]
    # Calendar groups often contain identical numerical training options.
    # Evaluate each option once per projection step, retaining the original
    # layout order and accumulation order (including floating-point sums).
    option_indices, unique_options, prepared_layouts = {}, [], []
    for layout in layouts:
        indices = []
        for tab, raw in layout['gains'].items():
            points = layout.get('skill_points', {}).get(tab, 0.)
            key = (tab, tuple(raw.get(stat, 0.) for stat in TRAINING_TYPES), points)
            if key not in option_indices:
                option_indices[key] = len(unique_options)
                unique_options.append((tab, raw, points))
            indices.append(option_indices[key])
        prepared_layouts.append((layout.get('weight', 1.), indices))
    left = max(0., budget)
    while left > .0001 and layouts:
        weight = min(1., left)
        gaps = soft_gaps(values, target, rates, left, stage_weight)
        evaluated = []
        for tab, raw, points in unique_options:
            gains = {key: display_gain(values[key], max(0., raw.get(key, 0.) * scale * weight))
                     for key in TRAINING_TYPES}
            components = marginal_gains(values, gains, gaps, attribute_weights=attribute_weights,
                after_target_credit=after_target_credit, high_stat_scarcity=high_stat_scarcity,
                restore_high_stat_value=restore_high_stat_value,
                adaptive_high_stat_value=adaptive_high_stat_value,
                far_surplus_credit=far_surplus_credit,far_surplus_buffer=far_surplus_buffer,
                far_surplus_exact_buffer=far_surplus_exact_buffer)
            utility = sum(components[key] for key in ("base", "gap_premium", "scarcity"))
            utility += max(0., points or 0.) * pt_weight * scale * weight
            evaluated.append((utility, tab, gains))
        choices = []
        for layout_weight, indices in prepared_layouts:
            choices.append((layout_weight, max((evaluated[index] for index in indices), key=lambda row: (row[0], row[1]))))
        # These fractions describe opportunity shares, not scheduled actions
        # or known probabilities of next turn's rainbow positions.
        total = sum(layout_weight for layout_weight, _ in choices)
        for layout_weight, (_, tab, gains) in choices:
            share = layout_weight / total
            counts[tab] = counts.get(tab, 0.) + weight * share
            for key, gain in gains.items():
                values[key] += gain * share
                if key != tab:
                    collateral[key] += gain * share
        left -= weight
    return {"stats": {key: round(value, 3) for key, value in values.items()},
            "allocation": {key: round(value, 6) for key, value in counts.items()},
            "collateral": {key: round(value, 3) for key, value in collateral.items()}}
