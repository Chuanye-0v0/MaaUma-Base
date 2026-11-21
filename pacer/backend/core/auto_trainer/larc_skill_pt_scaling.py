"""Marginal base-Pt projection; displayed gains already contain source effects.

Missing support multipliers remain estimates. Ranges describe the stated
shared-multiplier model, not measured probabilities or guaranteed game results.
"""
from math import ceil, floor, isfinite


def _positive(value):
    return type(value) in (int, float) and isfinite(value) and value > 0


def project_skill_pt(amount, base_delta, source, target, *, old_boost, new_boost,
                     old_expect, new_expect, old_levels, new_levels):
    unknown = []
    expectation_change = ((new_expect - old_expect)
                          if old_expect is not None and new_expect is not None else 0.)
    if old_expect is None:
        unknown.append('skill_pt_expectation_unknown')
    friendship = source.get('friendship')
    old_friend = 1.2 if friendship is True and old_levels[7] == 3 else 1.
    new_friend = 1.2 if friendship is True and new_levels[7] == 3 else 1.
    if friendship is None or source.get('friendship_basis') == 'eligible_partner_estimate':
        unknown.append('skill_pt_friendship_multiplier_unobserved')
    effect = source.get('training_effect_percent')
    effect_known = type(effect) in (int, float) and isfinite(effect) and effect >= 0
    # Do not promote the old generic +50% dilution prior to an observed
    # absolute Pt multiplier. Only known scenario effects enter this estimate.
    old_additive = 1. + (effect if effect_known else old_boost + (old_expect or 0.)) / 100
    new_additive = max(.01, old_additive + (new_boost - old_boost + expectation_change) / 100)
    source_estimate, target_estimate = old_additive * old_friend, new_additive * new_friend
    explicit = source.get('skill_pt_multiplier')
    complete = _positive(explicit)
    if complete:
        scale = explicit / source_estimate
        source_estimate, target_estimate = float(explicit), target_estimate * scale
        basis = 'explicit_source_skill_pt_multiplier'
        if not effect_known and old_additive != new_additive:
            unknown.append('skill_pt_target_effect_decomposition_estimated')
    else:
        basis = ('provided_training_effect_with_known_scenario_friendship' if effect_known
                 else 'known_scenario_effects_only_estimate')
        if source.get('training_effect_percent_basis') == 'projected_destination_effect':
            basis = 'projected_destination_effect_not_a_new_observation'
        unknown.append('skill_pt_other_support_multipliers_unknown')
    # Targets commonly copy source metadata. A copied source multiplier is
    # not a new observation at the projected date/levels.
    ratio = target_estimate / source_estimate

    def output(raw, multiplier):
        return max(0., raw + base_delta) * multiplier

    estimate = floor(output(amount / source_estimate, target_estimate) + 1e-9)
    # The original displayed integer hides its fractional remainder. Preserve
    # it as a range rather than pretending every base-Pt upgrade adds 10.
    rounding_lo = floor(output(amount / source_estimate, target_estimate) + 1e-9)
    rounding_hi = max(rounding_lo, ceil(output((amount + 1) / source_estimate,
                                               target_estimate) - 1e-9) - 1)
    same_multiplier = abs(ratio - 1.) < 1e-10
    source_upper = source_estimate if complete else None
    if not complete:
        old_bonus = 10 * (old_levels[5] >= 1) + 10 * (old_levels[5] == 3)
        # If the displayed gain contains this known nonnegative base bonus,
        # it supplies a structural upper bound on a shared multiplier.
        if old_bonus and (amount + 1) / old_bonus >= source_estimate:
            source_upper = (amount + 1) / old_bonus
        else:
            unknown.append('skill_pt_multiplier_upper_bound_unknown')
    if base_delta == 0 and same_multiplier:
        gain_range = [int(amount), int(amount)]
    elif same_multiplier:
        lo_delta = base_delta * source_estimate
        hi_delta = base_delta * source_upper if source_upper is not None else None
        if hi_delta is None:
            gain_range = ([max(0, floor(amount + lo_delta)), None] if base_delta >= 0
                          else [0, max(0, ceil(amount + lo_delta))])
        else:
            gain_range = [max(0, floor(amount + min(lo_delta, hi_delta) + 1e-9)),
                          max(0, ceil(amount + max(lo_delta, hi_delta) - 1e-9))]
    else:
        # A common unknown support effect cancels in a same-layout ratio;
        # its exact split is unavailable. Keep a conservative model envelope.
        ratio_lower = min(1., new_additive / old_additive) * new_friend / old_friend
        ratio_upper = max(1., new_additive / old_additive) * new_friend / old_friend
        target_lower = source_estimate * ratio_lower
        target_upper = source_upper * ratio_upper if source_upper is not None else None
        low_raw = amount / source_upper if source_upper is not None else 0.
        gain_range = [max(0, floor(output(low_raw, target_lower) + 1e-9)),
                      (max(0, ceil(output((amount + 1) / source_estimate,
                                          target_upper) - 1e-9)) if target_upper is not None else None)]
    gain_range[0] = min(gain_range[0], rounding_lo)
    if gain_range[1] is not None:
        gain_range[1] = max(gain_range[1], rounding_hi)
    return estimate, {
        'status': 'bounded_rounding' if complete and not unknown else 'estimated',
        'basis': basis, 'source_multiplier_estimate': round(source_estimate, 6),
        'target_multiplier_estimate': round(target_estimate, 6),
        'relative_multiplier_estimate': round(ratio, 6), 'base_bonus_delta': base_delta,
        'gain_range': gain_range, 'rounding_range_at_estimate': [rounding_lo, rounding_hi],
        'range_basis': 'shared_nonnegative_base_and_multiplier_model_not_confidence_interval',
        'unknown_fields': unknown,
    }
