"""Opt-in SS phase utility and explicit first-year full-group ordering."""
from core.auto_trainer.larc_recovery import period_value
from core.auto_trainer.larc_training_value import support_bond_units
from core.auto_trainer.larc_ss_visible import ss_victory_evidence


NUMERIC_COMPONENTS = (
    "stats", "dynamic_attribute_gap", "skill_pt", "first_arc_speed_preparation",
    "high_stat_target_scarcity",
)


def ss_bond_units(bond, count):
    """Discount partial-group startup utility, never observed bond increments.

    A bar at 64% does not deserve the same near-startup premium as 79%.
    The smooth premium is a policy estimate; no exact SS bond gain is assumed.
    Full groups retain the shared contact estimate used by existing training.
    """
    shared = support_bond_units(bond)
    if shared is None or shared == 0 or count == 5:
        return shared
    ratio = bond.get("fill_ratio_estimate")
    proximity = max(0., min(1., (ratio - .73) / .07)) if type(ratio) in {int, float} else 0.
    return (1. + .25 * proximity) * count / 5.


def executable_training(training):
    """Existing eligibility and observed risk, independent of recovery ranking."""
    if (training.get("rejected") is not False
            or not training.get("action_id", "").startswith("train:")):
        return False
    failure, limit = training.get("failure_rate"), training.get("failure_rate_limit")
    return (type(failure) in {int, float} and 0 <= failure < 100
            and type(limit) in {int, float} and failure <= limit)


def current_training_numeric(training):
    components = training.get("components", {})
    if not executable_training(training) or not all(
            type(components.get(k)) in {int, float} for k in NUMERIC_COMPONENTS):
        return None
    return sum(components[k] for k in NUMERIC_COMPONENTS) * (1 - training["failure_rate"] / 100)


def effective_training_charge(training):
    charge = training.get("features", {}).get("net_charge", {}).get("known_value")
    return (type(charge) in {int, float} and charge > 0
            and training.get("components", {}).get("net_charge", 0.) > 0)


def best_executable_training(candidates, *, ss_numeric=None):
    """Find a real strong/charged exception before ordinary total ranking."""
    eligible = [row for row in candidates if executable_training(row)]
    if type(ss_numeric) in {int, float} and ss_numeric > 0:
        strong = [row for row in eligible if current_training_numeric(row) is not None
                  and current_training_numeric(row) >= .8 * ss_numeric and effective_training_charge(row)]
        if strong:
            return min(strong, key=lambda row: (-current_training_numeric(row), -row["score"], row["action_id"]))
    return min(eligible, key=lambda row: (-row["score"], row["action_id"])) if eligible else {}


def phase_comparison(state, preview, value, training, *, preference, recovery_percent, recovery_points,
                     strategy_v4=False):
    """First-year full-group ordering and later training opportunity utility.

    Replaces the existing full-group preference rather than stacking another
    full-match bonus. Existing energy, released capacity and recovery-cycle
    terms remain intact. No second charged group or future layout is invented.
    """
    result = {"active": False, "full_group_preference": preference,
              "strong_training_opportunity": 0., "basis": "phase_or_comparison_unknown",
              "score_kind": "policy_preference_not_additional_game_rewards"}
    period = period_value(state)
    year, month = period.get("year_index"), period.get("month")
    junior = year == 1
    returned = (year == 2 and type(month) is int and 10 <= month <= 12
                or year == 3 and type(month) is int and 1 <= month <= 6)
    if returned:
        # Removing a startup preference depends on the known phase, not on
        # recovery winning the local ranking or optional energy OCR succeeding.
        result.update(full_group_preference=0.,phase='returned_domestic',
            basis='returned_domestic_has_no_early_full_group_preference')
    people = preview.get("participants_observation", {})
    source = preview.get("source") or people.get("source", {})
    captures = [o.raw_data.get("capture", {}) for o in state.training_options]
    bound = (isinstance(source, dict) and source.get("run_session_id")
             and source.get("turn_index") == state.turn and captures
             and all(c.get("run_session_id") == source["run_session_id"]
                     and c.get("turn_index") == state.turn for c in captures))
    if (not (junior or returned) or preview.get("ready_count") != 5
            or not bound
            or not ss_victory_evidence(preview)['guaranteed']
            or value["unknown_fields"] or any(type(preview.get("stats_gain", {}).get(k)) is not int
                                             or preview["stats_gain"][k] < 0
                                             for k in (*("speed", "stamina", "power", "guts", "wisdom"), "skill_pt"))):
        return result
    energy = training.get("evidence", {}).get("vitality_preview")
    known_energy = (isinstance(energy, dict) and energy.get("status") == "accepted"
                    and type(energy.get("after_percent")) in {int, float}
                    and 0 <= energy["after_percent"] <= 100)
    ss_numeric = sum(value["components"].get(k, 0.) for k in NUMERIC_COMPONENTS)
    training_numeric = current_training_numeric(training)
    known_training = training_numeric is not None
    # The energy preview describes overflow; missing it must not restore an
    # early-phase bonus. Training risk/value still uses an executable candidate.
    effective_charge = known_training and effective_training_charge(training)
    strong = known_training and ss_numeric > 0 and training_numeric >= .8 * ss_numeric
    recovery_overflow = (recovery_points > 0 and recovery_percent <= 0 and energy["after_percent"] < state.vitality
                         if known_energy else None)
    result.update(active=True, phase="junior" if junior else "returned_domestic",
                  ss_current_numeric=round(ss_numeric, 4),
                  training_current_numeric=round(training_numeric, 4) if known_training else None,
                  strong_numeric_training=strong, effective_observed_charge=effective_charge,
                  recovery_overflow_if_used_now=recovery_overflow,
                  strong_numeric_threshold_ratio=.8,
                  basis="current_numeric_opportunity_and_observed_charge_with_existing_future_cost")
    if junior:
        # Explicit ordering, not a score-margin patch: free the five occupied
        # slots, then reassess the actual event recovery and next layout.
        # Strong training with observed effective charge keeps score ordering.
        result.update(selection_preference="score_comparison" if strong and effective_charge else "full_ss_first",
                      training_action_id=training.get("action_id") or None,
                      reassess_after_ss=True,
                      selection_basis="strong_executable_training_with_effective_charge" if strong and effective_charge else
                      "junior_full_group_releases_filled_slots_before_recovery_or_ordinary_training")
        if known_training and not (strong and effective_charge):
            result["full_group_preference"] = round(max(preference, .35 * max(0., ss_numeric - training_numeric)), 4)
        result["tempo_preference_fraction"] = .35
    else:
        # After returning, the early startup preference yields to current
        # strong numeric training. Useful SS healing remains in the shared
        # energy route, including its timing and the cost of delaying it.
        result["full_group_preference"] = 0.
        result["training_action_id"] = training.get("action_id") or None
        from core.auto_trainer.larc_training_forecast import strategy_v4_enabled
        if strategy_v4 or strategy_v4_enabled(state):
            # The ordinary comparison already forgoes this same training.
            # Its numeric value is not a second, independent loss of SS.
            result.update(strong_training_opportunity=0.,
                strong_training_opportunity_fraction=0.,
                basis="same_action_comparison_without_extra_training_opportunity_penalty")
        elif strong:
            result["strong_training_opportunity"] = round(-.2 * max(0., training_numeric - .6 * ss_numeric), 4)
        if not (strategy_v4 or strategy_v4_enabled(state)):
            result["strong_training_opportunity_fraction"] = .2
    return result
