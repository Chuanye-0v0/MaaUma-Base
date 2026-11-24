"""Bounded charge opportunities, separate from observed batteries and rewards."""

from core.auto_trainer.larc_participant_evidence import (
    effective_domestic_charge, is_mei, training_rows, visible_charge,
)
from core.auto_trainer.larc_ss_opportunity import ss_redemption


# User teaching prior, not a measured trigger rate. The optional scenario input
# larc_charge_planning.mei_trigger_probability can override it, including zero.
MEI_TRIGGER_PROBABILITY = .4
MEI_EXTRA_MEMBERS = 5
ROSTER_SIZE_PRIOR = 15


def _bound(source, state, run_id):
    return (isinstance(source, dict) and source.get("run_session_id") == run_id
            and type(source.get("turn_index")) is int and source["turn_index"] == state.turn
            and bool(source.get("absolute_path") or source.get("path"))
            and bool(source.get("sha256") or source.get("source_sha256")))


def battery_inventory(state, run_id):
    """Union of current-page observations; preview increments stay tab-local."""
    members, conflicts = {}, set()
    for option in state.training_options:
        source = option.raw_data.get("capture", {})
        view = training_rows(option, typed=True)
        if not _bound(source, state, run_id) or view is None:
            continue
        for person in view[0]:
            ref = person.get("member_ref", {})
            member_id = ref.get("member_id")
            resolved = effective_domestic_charge(
                person, state=state, tab_participants=view[0], candidate=option
            )
            if (ref.get("status") != "accepted" or ref.get("run_session_id") != run_id or not member_id
                    or resolved is None):
                continue
            filled, net, _ = resolved
            if not (0 <= filled <= 3 and 0 <= net <= 3 - filled):
                continue
            if member_id in members and members[member_id]["filled"] != filled:
                conflicts.add(member_id)
            row = members.setdefault(member_id, {"filled": filled, "person": person, "previews": {}})
            previous = row["previews"].get(option.training_type)
            if previous is not None and previous != net:
                conflicts.add(member_id)
            row["previews"][option.training_type] = net
    return {key: value for key, value in members.items() if key not in conflicts}


def mei_charge_value(state, candidate, source, weights, *, cycle_model=False, ready_after_base=None,
                     base_progress=None, base_actions=3, goal=None):
    result = {"score": 0., "expected_extra_cells": 0., "expected_extra_full_members": 0.,
              "basis": "user_trigger_prior_and_uniform_member_draw_estimate",
              "certainty": "estimate", "guaranteed_extra_cells": 0,
              "reason": "mei_not_identified_on_current_training"}
    if source is None or weights["net_charge"] <= 0:
        return dict(result, reason="no_domestic_charge_value")
    if cycle_model and not _bound(source, state, source.get('run_session_id')):
        return dict(result, reason='training_source_not_current')
    view = training_rows(candidate, typed=True)
    if view is None:
        return result
    people, complete = view
    # An unnamed friend is not Mei. Presence is one observation per action.
    if not any(is_mei(person) for person in people):
        return dict(result, reason="mei_presence_unknown_on_partial_training") if not complete else result
    if cycle_model:
        from core.auto_trainer.larc_mei_cycle import contact_stage
        stage = contact_stage(state, source)
        if stage != 'subsequent':
            return dict(result, reason='first_contact_has_separate_event' if stage == 'first_contact_unconfirmed'
                        else 'mei_contact_stage_unknown', contact_stage=stage)
    settings = state.scenario_state.get("larc_charge_planning", {})
    probability = settings.get("mei_trigger_probability", MEI_TRIGGER_PROBABILITY) if isinstance(settings, dict) else MEI_TRIGGER_PROBABILITY
    if type(probability) not in {int, float} or not 0 <= probability <= 1:
        probability = MEI_TRIGGER_PROBABILITY
    from core.auto_trainer.larc_mei_charge_forecast import model_rule_value
    model = model_rule_value(state, candidate, source, weights, result, goal=goal,
        probability=probability, cycle_model=cycle_model, base_progress=base_progress, base_actions=base_actions)
    if model is not None:
        return model
    inventory = battery_inventory(state, source["run_session_id"])
    # Unknown members get no credit. Treat full members as wasted draws rather
    # than assuming the game always redirects every extra cell to empty ones.
    pool = max(ROSTER_SIZE_PRIOR, len(inventory))
    eligible, one_short = 0, 0
    for row in inventory.values():
        after_base = row["filled"] + row["previews"].get(candidate.training_type, 0)
        eligible += after_base < 3
        one_short += after_base == 2
    if not complete:
        # Unlinked local slots can establish a floor but may overlap the
        # current roster estimate. Take the larger count, never their sum.
        local_after = [sum(cells) for person in people if (cells := visible_charge(person)) is not None]
        eligible = max(eligible, sum(value < 3 for value in local_after))
        one_short = max(one_short, sum(value == 2 for value in local_after))
        result["current_participants_partial"] = True
    expected = probability * MEI_EXTRA_MEMBERS * eligible / pool
    if cycle_model:
        return _mei_cycle_value(state, candidate, weights, result, inventory, probability, pool,
            eligible, one_short, ready_after_base, base_progress or {}, base_actions)
    # No uncertain readiness/charming/recovery bonus on top of this estimate.
    # Three actions conservatively allow training, another charge, then SS.
    redemption = ss_redemption(state, earliest_actions=3)
    result.update(score=round(expected * weights["net_charge"] * redemption["value_factors"]["net_charge"], 4),
                  expected_extra_cells=round(expected, 4),
                  expected_extra_full_members=round(probability * MEI_EXTRA_MEMBERS * one_short / pool, 4),
                  trigger_probability=probability, trigger_probability_basis="user_teaching_prior_or_scenario_override",
                  members_drawn_if_triggered=MEI_EXTRA_MEMBERS, observed_members=len(inventory),
                  eligible_observed_members=eligible, member_pool_estimate=pool,
                  redemption=redemption, reason="bounded_optional_extra_charge")
    return result


def _mei_cycle_value(state, candidate, weights, result, inventory, probability, pool,
                     eligible, one_short, ready_after_base, base_progress, base_actions):
    """E[value(after ordinary charge + optional proc)] - value(ordinary).

    Draws use the existing conservative uniform-pool estimate. Unknown members
    never become invented nearly-full batteries. Only the hit branch that can
    finish five takes the training -> SS two-action path.
    """
    from math import comb

    known_ready = sum(row['filled'] + row['previews'].get(candidate.training_type, 0) == 3
                      for row in inventory.values())
    ready_known = type(ready_after_base) is int and 0 <= ready_after_base <= 5
    ready = min(5, max(known_ready, ready_after_base if ready_known else 0))
    before = ss_redemption(state, earliest_actions=base_actions)
    ordinary = {key: base_progress.get(key, 0.) for key in ('net_charge', 'newly_full', 'ss_preparation')}
    ordinary_value = sum(ordinary[key] * before['value_factors'][key] for key in ordinary)
    draws = min(MEI_EXTRA_MEMBERS, pool)
    denominator = comb(pool, draws)
    branches, total, completes = [], 0., 0.
    proof = result.get('current_priority_proof')
    if proof:
        joint = proof['branches']
    else:
        joint = []
        for hits in range(max(0, draws - (pool - one_short)), min(draws, one_short) + 1):
            conditional = comb(one_short, hits) * comb(pool - one_short, draws - hits) / denominator
            other_cells = ((draws - hits) * (eligible - one_short) / (pool - one_short)
                           if pool > one_short else 0.)
            joint.append((hits, hits + other_cells, conditional))
    expected_cells = expected_full = 0.
    for hits, cells, conditional in joint:
        branch_probability = probability * conditional
        expected_cells += branch_probability * cells
        expected_full += branch_probability * hits
        missing = max(0, 5 - ready - hits)
        actions = min(base_actions, max(2 if hits else 3, 2 + (missing + 1) // 2))
        redemption = ss_redemption(state, earliest_actions=actions)
        # Batteries beyond the next five need another SS, so a late cutoff
        # cannot give immediate-readiness credit for an unspendable overflow.
        later = ss_redemption(state, ss_steps=2, earliest_actions=max(4, actions + 2))
        immediate = min(hits, max(0, 5 - ready))
        full_score = (immediate * weights['newly_full'] * redemption['value_factors']['newly_full']
            + .25 * (hits - immediate) * weights['newly_full'] * later['value_factors']['newly_full'])
        prep = weights['ss_preparation'] if ready < 5 <= ready + hits else 0.
        # Once five are ready only the new cells belonging to those five can
        # be spent next. The rest must survive to a second group, not sneak
        # through the same near-cutoff two-action discount.
        cells_now = immediate if ready + hits >= 5 else cells
        cell_score = weights['net_charge'] * (cells_now * redemption['value_factors']['net_charge']
            + (cells - cells_now) * later['value_factors']['net_charge'])
        new_value = (cell_score
            + full_score + prep * redemption['value_factors']['ss_preparation'])
        # The normal charging progress is already scored once. Only its
        # acceleration, if this proc makes an earlier SS possible, is new.
        accelerated = max(0., sum(ordinary[key] * redemption['value_factors'][key] for key in ordinary)
                          - ordinary_value)
        marginal = new_value + accelerated
        completes += branch_probability if ready < 5 <= ready + hits else 0.
        total += branch_probability * marginal
        branches.append({'probability': round(branch_probability, 8), 'additional_full_members': hits,
            'expected_effective_cells_if_branch': round(cells, 6),
            'current_cycle_cells': round(cells_now, 6), 'later_cycle_cells': round(cells - cells_now, 6),
            'ready_after_proc_lower_bound': min(5, ready + hits),
            'redemption': redemption, 'additional_full_credit': round(full_score, 6),
            'additional_preparation_credit': round(prep * redemption['value_factors']['ss_preparation'], 6),
            'ordinary_progress_acceleration_credit': round(accelerated, 6),
            'marginal_value_if_branch': round(marginal, 6)})
    result.update(score=round(total, 4), expected_extra_cells=round(expected_cells if proof else probability * draws * eligible / pool, 4),
        expected_extra_full_members=round(expected_full if proof else probability * draws * one_short / pool, 4),
        trigger_probability=probability, trigger_probability_basis='user_teaching_prior_or_scenario_override',
        members_drawn_if_triggered=proof['members_drawn_if_triggered'] if proof else draws, observed_members=len(inventory), eligible_observed_members=eligible,
        member_pool_estimate=pool, contact_stage='subsequent',
        ready_after_base_lower_bound=ready,
        readiness_basis='same_turn_entry_and_observed_base_charge' if ready_known else 'observed_battery_lower_bound',
        completes_five_probability_estimate=round(completes, 8),
        no_trigger_probability=1 - probability, base_redemption=before,
        conditional_branches=branches, redemption=before,
        current_ss_reward_credit=0., current_vitality_credit=0.,
        reason='conditional_mei_charge_marginal_progress')
    if proof:
        low, high = proof['effective_cells_lower'], proof['effective_cells_upper']
        result['physical_extra_cells_if_triggered'] = {'lower': low, 'upper': high}
        result['physical_extra_cells_expectation_interval'] = {'lower': probability * low, 'upper': probability * high}
    return result


def _confirmed_current_full_recharge(participant):
    """Current public refill only; unknown icons/amounts retain the old prior."""
    reward=participant.get('current_reward')or{}
    if reward.get('status')!='accepted' or reward.get('kind')not in {'charge_gain','recharge'}:
        return None
    if type(reward.get('amount'))is int and reward['amount']>=3:
        return 'current_visible_charge_gain_refills_capacity'
    # The model adapter already attaches each public current participant's
    # direct result. Read that field, never a reward queue or future layout.
    public=participant.get('simulation_truth')or{}
    if (participant.get('source_kind')=='simulation' and isinstance(public,dict)
            and (public.get('current_reward')or{}).get('kind')=='recharge'
            and type(public.get('charge_after_on_member_success'))is int
            and public['charge_after_on_member_success']==3 and public.get('charge_max')==3):
        return 'current_public_model_member_success_charge_after_equals_capacity'
    return None


def ss_battery_release_value(state, preview, weights, *, hold_actions=1, cycle_model=False, goal=None):
    """Marginal recharge opportunity across a bounded held-SS delay.

    Encounter rates and forecast credit are conservative policy priors. The next
    layout, its rewards, and the next SS are not promised or written to state.
    """
    result = {"score": 0., "members": [], "complete": False,
              "basis": "bounded_recharge_lost_while_holding_full_slots",
              "forecast_credit": .8, "current_reward_credit": 0., "current_vitality_credit": 0.}
    observation = preview.get("participants_observation", {})
    source = preview.get("source") or observation.get("source", {})
    run_id = source.get("run_session_id")
    if (preview.get("status") != "accepted" or preview.get("stage") != "preview"
            or not run_id or not _bound(source, state, run_id) or observation.get("status") != "accepted"
            or not state.training_options or any(o.raw_data.get("capture", {}).get("run_session_id") != run_id
                or o.raw_data.get("capture", {}).get("turn_index") != state.turn for o in state.training_options)):
        return result
    inventory = battery_inventory(state, run_id)
    from core.auto_trainer.larc_ss_phase import year1_full_comparison_status
    correct_current_recharge=year1_full_comparison_status(state,goal,preview)['active']
    mei_encounter, trigger = None, 0.
    if cycle_model:
        from core.auto_trainer.larc_mei_cycle import contact_stage, encounter_prior
        if contact_stage(state, source) == 'subsequent':
            mei_encounter = encounter_prior(state, source)
        settings = state.scenario_state.get('larc_charge_planning') or {}
        trigger = settings.get('mei_trigger_probability', MEI_TRIGGER_PROBABILITY) if isinstance(settings, dict) else MEI_TRIGGER_PROBABILITY
        if type(trigger) not in {int, float} or not 0 <= trigger <= 1:
            trigger = MEI_TRIGGER_PROBABILITY
    rows = observation.get("participants", [])
    seen, details = set(), []
    hold_actions = min(4, max(1, hold_actions))
    for participant in rows:
        ref = participant.get("member_ref", {})
        mid = ref.get("member_id")
        # Preview readiness proves a full slot even when its identity cannot
        # be joined to the training inventory. Keep identity and reward unknown;
        # only the generic roster encounter prior is allowed in that case.
        if (participant.get("win_guaranteed") is not True
                or ref.get("run_session_id") not in {None, run_id}):
            continue
        matched = (ref.get("status") == "accepted" and ref.get("run_session_id") == run_id
                   and mid in inventory and mid not in seen)
        if matched and inventory[mid]["filled"] != 3:
            continue
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        person = inventory[mid]["person"] if matched else {}
        support = person.get("role", {}).get("status") == "accepted" and person["role"].get("kind") == "support"
        bond = person.get("bond", {})
        started = support and bond.get("status") == "accepted" and (
            bond.get("is_max") is True or bond.get("color") in {"yellow", "orange"})
        encounter, rate = (.25 if support else .15), (1.5 if started else 1.)
        # SS now, at least two charging trainings, and a later SS. This also
        # removes credit at the final domestic deadline rather than hoarding it.
        redemption = ss_redemption(state, earliest_actions=4 if started else 5, consumed_current=True)
        cells = min(3., encounter * rate * hold_actions)
        extra = (min(3., cells + mei_encounter * trigger * MEI_EXTRA_MEMBERS
                     / max(ROSTER_SIZE_PRIOR, len(inventory)) * hold_actions) - cells
                 if mei_encounter is not None else 0.)
        retained_basis=_confirmed_current_full_recharge(participant)if correct_current_recharge else None
        if retained_basis:
            # This member is still full after the current SS. There is no
            # freed slot to recharge; do not invent a replacement future value.
            cells=extra=0.
        details.append({"member_id": mid if matched else None, "identity_matched": matched,
                        "slot": participant.get("slot"), "expected_recharge_cells": round(cells, 4),
                        "encounter_probability_prior": encounter, "charge_rate_prior": rate,
                        "redemption": redemption,
                        "score": round((cells + extra) * weights["net_charge"] * .8 * redemption["value_factors"]["net_charge"], 4),
                        **({'retained_full':True if retained_basis else None,
                            'released_slot':False if retained_basis else None,
                            'release_basis':retained_basis or'legacy_release_prior_current_refill_not_confirmed'}
                            if correct_current_recharge else {}),
                        **({'mei_marginal_recharge_cells': round(extra, 6),
                            'mei_encounter_probability_prior': mei_encounter,
                            'mei_trigger_probability_prior': trigger} if cycle_model else {})})
    result.update(score=round(sum(r["score"] for r in details), 4), members=details,
                  complete=bool(rows) and len(details) == len(rows) == preview.get("ready_count")
                  and all(r["identity_matched"] for r in details),
                  hold_actions_estimate=hold_actions,
                  anonymous_ready_slots=sum(not r["identity_matched"] for r in details))
    return result
