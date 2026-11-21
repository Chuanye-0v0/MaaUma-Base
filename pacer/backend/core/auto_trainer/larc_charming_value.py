"""Charming's remaining bond work, not a permanent first-reward bonus."""
from __future__ import annotations

from core.auto_trainer.larc_participant_evidence import training_rows


def charming_value(state, ss_executions=None):
    seen = {}
    for candidate in state.training_options:
        view = training_rows(candidate)
        if view is None:
            continue
        people, complete = view
        for person in people:
            role = person.get("role", {})
            identity = person.get("identity", {})
            support = role.get("status") == "accepted" and role.get("kind") == "support"
            support |= (identity.get("status") == "known" and identity.get("entity_kind") == "support_card"
                        and identity.get("card_type") not in {"friend", "友人", None})
            if not support:
                continue
            key = person.get("member_ref", {}).get("member_id") or identity.get("matched_card_id")
            if not key:
                if not complete:
                    continue  # This aggregate needs a distinct card across pages.
                key = person.get("appearance_id") or f"{candidate.training_type}:{person.get('slot')}"
            bond = person.get("bond", {})
            ratio = bond.get("fill_ratio_estimate")
            if bond.get("status") != "accepted":
                continue
            if bond.get("is_max") is True or bond.get("color") in {"orange", "yellow"}:
                gap = 0.
            elif type(ratio) in {int, float} and 0 <= ratio <= 1:
                gap = max(0., .8 - ratio)
            else:
                continue
            seen[key] = gap
    # Unseen cards stay unknown, with a modest prior; never label NPCs as bonds.
    unknown = max(0, 5 - len(seen))
    gap_units = sum(min(1., gap / .4) for gap in seen.values()) + unknown * .6
    bond_factor = min(1., gap_units / 3)
    if ss_executions is None:
        ss_executions = state.scenario_state.get("larc_ss_execution_count",
            state.scenario_state.get("larc_capacity", {}).get("ss_execution_count", 0))
    count = len(ss_executions) if isinstance(ss_executions, (list, tuple)) else ss_executions
    count = count if type(count) is int and count >= 0 else 0
    # First/second cycle preferred, but no hard cut: remaining unfinished bonds
    # can still make later charming useful.
    timing = 1. / (1. + .25 * max(0, count - 1))
    value = state.scenario_state.get("larc_period", {}).get("value", {})
    phase = {1: 1., 2: .7, 3: .25}.get(value.get("year_index"), .8)
    factor = bond_factor * timing * phase
    return {"factor": round(factor, 4), "score": round(20 * factor, 4),
            "observed_supports": len(seen), "unknown_supports": unknown,
            "remaining_bond_fraction_sum": round(sum(seen.values()), 4), "ss_count": count}


def charming_charge_preparation(state, people, plan, *, ready, newly_full, earliest_actions,
                               bond_preference=1.):
    """A bounded net-cell share of the same reward valued by the SS scorer.

    This is progress credit, not an observed payout. Calendar feasibility and
    known queue pressure may discount it; neither predicts future attendance.
    All three cells have the same marginal budget, including the final cell.
    """
    from core.auto_trainer.larc_ss_opportunity import ss_redemption, WAIT_DISCOUNT

    budget = max(0., plan['score']) * bond_preference
    rows, total = [], 0.
    for person in people:
        reward = person['current_reward']
        net, filled = person.get('net_charge'), person.get('filled_cells')
        if (reward.get('status') != 'accepted' or reward.get('kind') != 'charming'
                or reward.get('earned') is True or type(net) is not int or net <= 0
                or type(filled) is not int or not 0 <= filled < 3 or filled + net > 3):
            continue
        remaining = 3 - filled - net
        # Existing readiness prior uses two additional full members per later
        # training. Keep its uncertainty; never assert Gold joins the next SS.
        queue_size = (ready + newly_full + int(person['newly_full'] != 1)
                      if ready is not None else None)
        cycles = max(1, (queue_size + 4) // 5) if queue_size is not None else 1
        actions = max(earliest_actions, 2 + remaining + cycles - 1)
        redemption = ss_redemption(state, ss_steps=cycles, earliest_actions=actions)
        # A known overfull queue needs another opportunity. Use the existing
        # waiting prior once; do not also apply the generic five-person delay.
        queue_factor = WAIT_DISCOUNT ** (cycles - 1)
        share = net / 3
        score = budget * share * redemption['value_factors']['reward_preparation'] * queue_factor
        total += score
        rows.append({'slot': person['slot'], 'member_id': person.get('member_id'),
            'net_cells': net, 'remaining_cells_after_action': remaining,
            'budget_share': round(share, 6), 'score': round(score, 4),
            'queue_cycles_estimate': cycles, 'queue_status': 'unknown' if ready is None else 'observed_count_prior',
            'queue_factor': round(queue_factor, 6), 'redemption': redemption,
            'participation_guaranteed': False})
    return {'score': round(min(budget, total), 4), 'reward_budget': budget,
        'participants': rows, 'basis': 'net_charge_share_of_current_charming_budget',
        'guaranteed_reward': False, 'ordinary_preparation_delay_applied': False}
