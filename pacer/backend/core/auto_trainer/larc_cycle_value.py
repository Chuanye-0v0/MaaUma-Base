"""Marginal future recovery value. Never adds future rewards to current energy."""

from core.auto_trainer.larc_participant_evidence import training_rows
from core.larc_reward_cycle import participant_cycle
from core.auto_trainer.larc_recovery import (
    RECOVERY_KINDS, critical_window, points_to_percent, recovery_weight,
)
from core.auto_trainer.larc_ss_opportunity import ss_opportunity, ss_redemption, WAIT_DISCOUNT


# Soft planning estimates, not promised layouts or a rule to chase these cards.
STEP_DISCOUNT = WAIT_DISCOUNT
SUPPORT_CHARGE_RATE = 1.5
OTHER_CHARGE_RATE = 1.


def _charge_rate(person):
    role, bond = person.get("role", {}), person.get("bond", {})
    identity = person.get("identity", {})
    support = (role.get("kind") == "support" or
               identity.get("entity_kind") == "support_card" and identity.get("card_type")
               in {"speed", "stamina", "power", "guts", "wisdom"})
    started = bond.get("status") == "accepted" and (
        bond.get("is_max") is True or bond.get("color") in {"yellow", "orange"})
    return SUPPORT_CHARGE_RATE if support and started else OTHER_CHARGE_RATE


def _timing(state, cells, ss_steps, rate, *, action_cost=0):
    # At least one training per later empty battery, even at maximum charge.
    first_cells = max(0, cells - 3 * (ss_steps - 1))
    earliest = action_cost + int(first_cells > 0) + (ss_steps - 1) + ss_steps
    window = critical_window(state)
    before_entry = None
    if window and window["kind"] == "expedition_entry":
        before_entry = earliest <= window["free_actions_including_current"]
    calendar = ss_opportunity(state, earliest_actions=earliest)
    if calendar["impossible_before_final_expedition"]:
        return None
    return {"work": cells / rate + ss_steps + calendar["calendar_wait"],
            "earliest_actions_including_current": earliest,
            "can_arrive_before_expedition": before_entry, "overseas_delay": calendar["overseas_delay"],
            "calendar_wait": calendar["calendar_wait"]}


def _progress(state, person, source, *, filled, net=0, consumed=False):
    rewards = participant_cycle(person, source)
    if rewards is None:
        return None
    # Training's current recovery is already priced by cycle_reward_preparation.
    # On SS, also look at the next occurrence after consuming today's recovery.
    limit = 4 if consumed else 3
    index = next((i for i in range(1, limit) if rewards[i % 3]["kind"] in RECOVERY_KINDS
                  and rewards[i % 3].get("earned") is not True), None)
    if index is None:
        return None
    reward = rewards[index % 3]
    points = reward.get("amount")
    points = points if type(points) is int and points >= 0 else 20
    rate = _charge_rate(person)
    before_cells, before_steps = 3 - filled + 3 * index, index + 1
    after_cells = before_cells if consumed else before_cells - net
    after_steps = before_steps - 1 if consumed else before_steps
    before = _timing(state, before_cells, before_steps, rate)
    after = _timing(state, after_cells, after_steps, rate, action_cost=1)
    if after is None:
        return None
    before_opportunity = ss_redemption(state, ss_steps=before_steps,
        earliest_actions=before["earliest_actions_including_current"]) if before else None
    after_opportunity = ss_redemption(state, ss_steps=after_steps,
        earliest_actions=after["earliest_actions_including_current"], consumed_current=consumed)
    # Linear future utility avoids pretending today's fullness predicts the
    # eventual deficit. Only the discounted advancement is credited, not HP.
    percent = points_to_percent(state, points)
    utility = percent * recovery_weight(state, include_held_ss=False)[0]
    # The shared redemption factor includes the calendar wait exactly once.
    # Keep ordinary charge/SS work discounted by the existing cycle model.
    advancement = max(0., after_opportunity["value_factors"]["recovery_cycle"]
                      * STEP_DISCOUNT ** (after["work"] - after["calendar_wait"])
                      - (before_opportunity["value_factors"]["recovery_cycle"]
                         * STEP_DISCOUNT ** (before["work"] - before["calendar_wait"]) if before else 0.))
    return {"member_id": person.get("member_ref", {}).get("member_id"),
            "slot": person.get("slot"), "reward_kind": reward["kind"],
            "recovery_points": points, "recovery_percent_estimate": round(percent, 4),
            "ss_participations_until_recovery": after_steps,
            "charge_cells_until_recovery": after_cells, "charge_rate_estimate": rate,
            "before": before, "after": after, "score": utility * advancement,
            "redemption_opportunity": after_opportunity,
            "current_vitality_credit": 0., "basis": "discounted_cycle_progress_estimate"}


def _result(rows, weights, selection_factor=1.):
    phase_factor = min(1., weights.get('unscaled_net_charge',weights["net_charge"]) / 5.)
    # Keep aggregate future recovery bounded by one full bar per horizon step.
    totals = {}
    for row in rows:
        key = row["ss_participations_until_recovery"]
        totals[key] = totals.get(key, 0.) + row["recovery_percent_estimate"]
    for row in rows:
        capacity_factor = min(1., 100. / max(1., totals[row["ss_participations_until_recovery"]]))
        row["score"] = round(row["score"] * phase_factor * selection_factor * capacity_factor * weights.get('charge_preference',1.), 4)
    return {"score": round(sum(row["score"] for row in rows), 4), "members": rows,
            "model_version": "larc_reward_cycle_progress_v3",
            "phase_factor": phase_factor, "selection_factor": selection_factor,
            "current_vitality_credit": 0., "basis": "future_reward_advancement_only"}


def training_cycle_value(state, candidate, source, weights, *, ready=None, newly_full=0):
    rows = []
    view = training_rows(candidate, require_tab=True)
    if source and weights["net_charge"] > 0 and view is not None:
        for person in view[0]:
            charge = person.get("charge", {})
            filled, net = charge.get("filled_cells"), charge.get("preview_cells")
            if (charge.get("status") != "accepted" or charge.get("capacity") != 3
                    or type(filled) is not int or type(net) is not int
                    or not 0 <= filled < 3 or not 0 < net <= 3 - filled):
                continue
            row = _progress(state, person, source, filled=filled, net=net)
            if row is not None:
                rows.append(row)
    selection = min(1., 5. / max(5, ready + newly_full)) if ready is not None else 1.
    return _result(rows, weights, selection)


def ss_cycle_value(state, preview, weights):
    observation = preview.get("participants_observation", {})
    source = preview.get("source") or observation.get("source", {})
    rows = []
    if observation.get("status") == "accepted" and weights["net_charge"] > 0:
        for person in observation.get("participants", []):
            if person.get("win_guaranteed") is not True:
                continue
            row = _progress(state, person, source, filled=3, consumed=True)
            if row is not None:
                rows.append(row)
    return _result(rows, weights)
