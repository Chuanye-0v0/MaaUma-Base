"""Bounded timing preferences, separate from observed game rewards."""
from core.auto_trainer.rejection_policy import is_hard_rejected
from core.auto_trainer.larc_charge_unlock import charge_unlock_window
from core.auto_trainer.larc_policy import training_balance_v2_enabled
from core.auto_trainer.larc_recovery_timing import _imminent_progress, _unit


def _energy(row):
    # A policy estimate may value training normally, but cannot prove a
    # dominance/timing comparison about the current visible alternatives.
    value = row.get("features", {}).get("vitality_delta_percent")
    return value if type(value) in {int, float} else None


def _numeric(row):
    parts = row["components"]
    return sum(parts.get(key, 0.) for key in ("stats", "dynamic_attribute_gap", "skill_pt"))


def _add(row, name, credit, evidence):
    row["components"][name] = round(credit, 4)
    row["planning"][name] = {
        **evidence, "score_before_preference": row["score"],
        "score_kind": "policy_preference_not_additional_game_rewards",
        "score": round(credit, 4),
    }
    row["score"] = round(sum(row["components"].values()), 4)


def apply_training_preferences(state, goal, scored):
    if not training_balance_v2_enabled(goal):
        return
    eligible = [r for r in scored if not is_hard_rejected(r)]
    if not eligible:
        return
    best = max(eligible, key=lambda r: r["score"])
    if not best.get("training_type"):
        return  # Do not displace a useful concentrated recovery.
    cost_delta = _energy(best)
    if cost_delta is None or cost_delta >= 0:
        return
    candidate = next(o for o in state.training_options if o.training_type == best["training_type"])
    _opening_preparation(state, goal, eligible, best, candidate, cost_delta)
    _healing_near_tie(state, eligible, best, candidate, cost_delta)


def _opening_preparation(state, goal, eligible, best, candidate, cost_delta):
    opening = charge_unlock_window(state, goal)
    if not opening or opening["game_action"] != 2 or state.vitality >= 100:
        return
    horizon = best["planning"].get("energy_horizon", {})
    cost = horizon.get("estimated_training_cost_percent", 0.)
    reference = horizon.get("future_training_value", 0.)
    if not horizon.get("active") or cost <= 0 or reference <= 0:
        return
    # Once charging starts, the next layout can use the energy saved now.
    # This is a one-time preference worth less than one discounted charging
    # training, not another predicted heal or extra charge in the forecast.
    reference += opening["forecast_credit"]
    imminent = _imminent_progress(candidate, best["components"])
    credit = (.9 * min(40., reference) * _unit(-cost_delta / cost)
              * _unit((100 - state.vitality) / 10.) * reference / (reference + imminent))
    for row in eligible:
        heal = _energy(row)
        is_training = row.get("training_type") is not None
        ordinary_outing = row["action_id"] == "outing.normal"
        if not (ordinary_outing or is_training and heal is not None and heal > 0):
            continue
        if is_training and row.get("failure_rate", 100) > best.get("failure_rate", 0):
            continue
        _add(row, "opening_preparation_preference", credit, {
            "compared_training": best["action_id"], "game_action": 2,
            "charge_unlock": opening, "future_opportunity_reference": round(reference, 4),
            "saved_training_cost_percent": -cost_delta, "imminent_progress_value": imminent,
            "normal_outing_guaranteed_recovery": 0,
        })


def _healing_near_tie(state, eligible, best, candidate, cost_delta):
    if state.vitality >= 95 or best["score"] <= 0:
        return
    charge = best.get("features", {}).get("net_charge", {}).get("known_value")
    full = best.get("features", {}).get("newly_full", {}).get("known_value")
    if type(charge) not in {int, float} or type(full) not in {int, float}:
        return
    if any(best["features"].get(key, {}).get("unknown_slots") for key in ("net_charge", "newly_full")):
        return  # A lower bound cannot prove the consuming option is no better.
    # Keep the existing utility scale. Only a near tie (at most 3 points/5%)
    # can prefer a real heal with no loss of charge or newly ready members.
    budget = min(3., .05 * best["score"])
    for row in eligible:
        if not row.get("training_type"):
            continue
        heal = _energy(row)
        gap = best["score"] - row["score"]
        features = row.get("features", {})
        other_charge = features.get("net_charge", {}).get("known_value")
        other_full = features.get("newly_full", {}).get("known_value")
        if (heal is None or heal <= 0 or not 0 < gap < budget
                or type(other_charge) not in {int, float} or other_charge < charge
                or type(other_full) not in {int, float} or other_full < full
                or row.get("failure_rate", 100) > best.get("failure_rate", 0)
                or _numeric(row) < .65 * _numeric(best)):
            continue
        other = next(o for o in state.training_options if o.training_type == row["training_type"])
        if _imminent_progress(candidate, best["components"]) > _imminent_progress(other, row["components"]):
            continue
        _add(row, "healing_charge_tie_preference", budget, {
            "compared_training": best["action_id"], "original_score_gap": round(gap, 4),
            "max_score_fraction": .05, "max_score_points": 3.,
            "observed_heal_percent": heal, "avoided_cost_percent": -cost_delta,
            "net_charge": other_charge, "compared_net_charge": charge,
        })
