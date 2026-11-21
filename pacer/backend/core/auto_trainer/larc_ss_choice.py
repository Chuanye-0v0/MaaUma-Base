"""Local SS opportunity-cost comparison; this module cannot execute actions."""
from core.auto_trainer.rejection_policy import is_hard_rejected
from dataclasses import replace

from core.auto_trainer.larc_opening import LarcOpeningStrategy
from core.auto_trainer.larc_supporter_goal import deadline_larc_supporter_goal
from core.auto_trainer.state import STAT_KEYS
from core.auto_trainer.larc_recovery import POLICY_VERSION, points_to_percent, recovery_value, ss_recovery
from core.auto_trainer.larc_cycle_value import ss_cycle_value
from core.auto_trainer.larc_charming_value import charming_value
from core.auto_trainer.larc_capacity import recovery_delta
from core.auto_trainer.larc_charge_value import ss_battery_release_value
from core.auto_trainer.larc_policy import policy_version, adaptation_timing_value, enabled, strategy_v4_enabled
from core.auto_trainer.larc_attribute_plan import planning_evaluation
from core.auto_trainer.larc_ss_visible import visible_ss_participants, ss_victory_evidence
from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
from core.auto_trainer.larc_user_preferences import values as user_preferences, configured as preferences_configured


def _release_after_explicit_cycle(release, continuity, goal):
    """Replace only the overlap already represented by conditional SS routes."""
    cycle = continuity.get('ss_resource_cycle') or {}
    fraction = cycle.get('replaces_battery_release_fraction')
    if (not unified_larc_enabled(goal) or cycle.get('replaces_battery_release_credit') is not True
            or type(fraction) not in (int, float) or not 0 < fraction <= 1):
        return release
    return {**release, 'score': round(release['score'] * (1 - fraction), 4),
            'score_before_cycle_overlap': release['score'], 'explicit_cycle_replaced_fraction': fraction,
            'basis_after_cycle': 'covered release opportunity already in common before/after resource routes'}


def visible_full_group_wait(preview,candidates,fallback_actions):
    """A currently executable layout can bound the held-slot delay on success."""
    fallback=min(4,max(1,fallback_actions));count=preview.get('ready_count')
    result={'available':False,'hold_actions':fallback,'fallback_actions':fallback,
        'basis':'energy_route_without_visible_full_group_path','guaranteed_future_points':None}
    if type(count) is not int or not 1<=count<5:return result
    eligible=[]
    for row in candidates:
        full=row.get('features',{}).get('newly_full',{}).get('known_value')
        failure=row.get('failure_rate');energy=row.get('evidence',{}).get('vitality_preview')
        after=energy.get('after_percent') if isinstance(energy,dict) and energy.get('status')=='accepted' else None
        if (is_hard_rejected(row) or not row.get('action_id','').startswith('train:')
                or type(full) is not int or count+full<5 or type(failure) not in {int,float}
                or not 0<=failure<100 or failure>row.get('failure_rate_limit',0)
                or type(after) not in {int,float} or not 0<=after<=100):continue
        eligible.append(row)
    if not eligible:return result
    candidate=max(eligible,key=lambda row:row['score']);probability=1-candidate['failure_rate']/100
    return {**result,'available':True,'candidate':candidate['training_type'],'visible_newly_full_lower_bound':
        candidate['features']['newly_full']['known_value'],'actions_on_success':1,'success_probability':probability,
        'hold_actions':round(probability+(1-probability)*fallback,4),'basis':'current_visible_charge_on_success_else_energy_route',
        'training_after_percent':candidate['evidence']['vitality_preview']['after_percent'],
        'training_energy_basis':'same_training_observed_success_preview',
        'unknown_fields':['future_ss_roster','future_ss_rewards','future_ss_win_guarantee','current_recovery_member_selection']}


def _bound_supporter_goal(state):
    goal = state.scenario_state.get("larc_supporter_goal")
    if not isinstance(goal, dict) or goal.get("status") != "accepted":
        return None
    source = goal.get("source", {})
    captures = [candidate.raw_data.get("capture", {}) for candidate in state.training_options]
    if (type(source.get("turn_index")) is not int or source["turn_index"] != state.turn
            or any(not isinstance(source.get(key), str) or not source[key]
                   for key in ("run_session_id", "path", "sha256"))
            or not captures or not all(c.get("run_session_id") == source["run_session_id"]
                                     and c.get("turn_index") == state.turn for c in captures)):
        return None
    return goal


def _phase_post_full(state,goal,preview):
    if not unified_larc_enabled(goal):return False
    from core.auto_trainer.larc_ss_deadline import deadline_status
    status=deadline_status(state,goal,preview)
    return (status.get('phase')=='post_first_arc_domestic'and status.get('current_full')is True
        and isinstance(preview,dict)and preview.get('stage')=='preview')


def _local_identity(row):
    if row.get('action')in {'outing','rest'}:
        return row['action'],row.get('target')
    if row.get('training_type')in {'speed','stamina','power','guts','wisdom'}:
        return 'train',row['training_type']
    raise ValueError('phase_local_row_action_unknown')


@planning_evaluation
def choose_larc_ss(state, goal, preview, *, ss_executions, preflight_only=False):
    from core.auto_trainer.larc_year1_engine import enabled as b197_enabled, choose as choose_b197, decorate_rows as decorate_b197_rows
    if b197_enabled(goal) and not preflight_only:
        from dataclasses import replace
        context = dict(state.scenario_state)
        context.pop('larc_ss_preview', None)
        local = LarcOpeningStrategy(goal).evaluate_baseline(replace(state, scenario_state=context))
        selected = choose_b197(state, goal, local.scored_candidates, ss_preview=preview, ss_executions=ss_executions)
        if selected is not None:
            aid = selected['action_id']
            b197_rows = decorate_b197_rows(local.scored_candidates, selected)
            row = next((r for r in b197_rows if r.get('action_id') == aid), None)
            action, target = _local_identity(row) if row is not None else ('stop', None)
            return {'selected': aid == 'ss', 'reason': selected['reason'],
                    'local_action': action, 'local_target': target, 'local_reason': selected['reason'],
                    'local_risk_flags': list((row or {}).get('risk_flags', [])),
                    'training_candidates': b197_rows, 'training_score': selected['values'].get(aid),
                    'score': selected['values'].get('ss'), 'components': {},
                    'kind': preview.get('kind'), 'ready_count': preview.get('ready_count'),
                    'unknown_fields': [], 'policy_version': policy_version(goal), 'year1_b197': selected}
    result = _choose_larc_ss_matched(state, goal, preview,
        ss_executions=ss_executions, preflight_only=preflight_only)
    from core.auto_trainer.larc_year1_engine import fallback_reason as b197_fallback_reason
    fallback = b197_fallback_reason() if b197_enabled(goal) else None
    if fallback:
        result = {**result, 'effective_policy': 'parent193_fallback', 'fallback_reason': fallback}
    return result


def _choose_larc_ss_matched(state, goal, preview, *, ss_executions, preflight_only=False):
    if unified_larc_enabled(goal):
        from core.auto_trainer.larc_ss_matched_stock import choose
        return choose(state, goal, preview, ss_executions=ss_executions,
            preflight_only=preflight_only, original=_choose_larc_ss_original,
            scored=_choose_larc_ss_scored)
    return _choose_larc_ss_original(state, goal, preview,
        ss_executions=ss_executions, preflight_only=preflight_only)


def _choose_larc_ss_original(state, goal, preview, *, ss_executions, preflight_only=False):
    from core.auto_trainer.larc_ss_deadline_execution import current_deadline_evidence,requires_ss_observation
    evidence = current_deadline_evidence(state, goal, None if preflight_only else preview)
    deadline = evidence['status']
    if not deadline['enabled']:
        return _choose_larc_ss_scored(state, goal, preview,
            ss_executions=ss_executions, preflight_only=preflight_only)
    if preflight_only and deadline['reason'] not in {'ss_deadline_domain_unavailable', 'current_turn_unavailable'}:
        return {'selected': False, 'status': 'eligible', 'reason': 'comparison_deferred_until_preview',
            'policy_version': policy_version(goal), 'ss_deadline': deadline}
    if not preflight_only and requires_ss_observation(deadline):
        executable = evidence['executable']
        return {'selected': executable,
            'reason': 'user_full_group_ss_deadline' if executable else 'user_full_group_ss_deadline_blocked',
            'local_action': 'stop', 'local_target': None,
            'local_reason': ('Before the first Arc prefer the current full group; require current executable evidence'
                if deadline.get('phase')=='pre_first_arc_domestic'else
                'Full SS grace spent; require a current executable full preview'),
            'local_risk_flags': [] if executable else ['full_ss_deadline_unresolved'],
            'training_candidates': [], 'training_score': None, 'score': None, 'components': {},
            'kind': preview.get('kind'), 'ready_count': preview.get('ready_count'),
            'unknown_fields': ['ss_numeric_gain_not_required_for_user_deadline'],
            'ss_deadline': deadline, 'policy_version': policy_version(goal),
            'planning': {'ss_victory': {'guaranteed': evidence['guaranteed'],
                'risk_known': evidence['risk_known'], 'risk_bands': evidence['risk_bands'],
                'basis': 'user_deadline_accepts_known_ss_risk_without_claiming_victory'}}}
    result = _choose_larc_ss_scored(state, goal, preview,
        ss_executions=ss_executions, preflight_only=preflight_only)
    year1=deadline.get('year1_full_comparison')or{}
    if year1.get('enabled'):
        result['year1_full_comparison']=year1
        # An unread local numeric page cannot turn the earlier executable full
        # SS into a new stop. This is the old advancement fallback, not a score
        # of zero for unknown training, nor permission for an unsafe page/run.
        if (year1.get('active') and not result.get('deadline_ss_only')
                and result.get('reason')=='local_training_unavailable'
                and set(result.get('local_risk_flags')or[])=={'larc_numeric_state_unreliable'}):
            result.update(selected=True,reason='first_year_local_numeric_unavailable_ss_fallback',
                score=None,selection_basis='legacy_guaranteed_full_ss_advance_with_unread_local_numeric')
            result['year1_full_comparison']={**year1,'comparison_result':'local_numeric_unavailable_legacy_fallback'}
    if deadline.get('phase')=='post_first_arc_domestic':
        result['ss_phase']={'phase':deadline['phase'],'historical_deferral_cap_applied':False,
            'non_guaranteed_ss_not_priced':result.get('reason')=='ss_victory_unverified'}
    return {**result, 'ss_deadline': deadline}


def _choose_larc_ss_scored(state, goal, preview, *, ss_executions, preflight_only=False):
    from core.auto_trainer.larc_ss_opportunity import with_planning_horizon
    state=with_planning_horizon(state,goal)
    from core.auto_trainer.larc_training_value import energy_continuity, gain_value, support_bond_units
    from core.auto_trainer.larc_policy import strategy_refinement_enabled, training_balance_v2_enabled
    refined = strategy_refinement_enabled(goal)
    full_group_only = training_balance_v2_enabled(goal)
    post_full=_phase_post_full(state,goal,preview)
    from core.auto_trainer.larc_ss_phase import year1_full_comparison_status
    year1_full=year1_full_comparison_status(state,goal,preview)['active']

    supporter_goal = _bound_supporter_goal(state)
    unknown = ["ss_support_bond_value", "ss_battery_release_value"]
    if preview.get("stage") == "preview":
        observation = preview.get("participants_observation", {})
        participants = visible_ss_participants(preview)
        if not participants:
            unknown.extend(("ss_participants", "ss_current_rewards"))
        elif any(row.get("current_reward", {}).get("status") != "accepted" for row in participants):
            unknown.append("ss_current_rewards")
    if (goal.scenario == "larc" and state.scenario_state.get("scenario") == "larc"
            and state.screen == "training_menu" and deadline_larc_supporter_goal(supporter_goal)):
        count, points = preview.get("ready_count"), preview.get("supporter_points_if_all_win")
        sufficient = (preview.get("status") == "accepted" and preview.get("stage") == "preview"
                      and preview.get("kind") in {"SS", "SSS"}
                      and type(count) is int and 1 <= count <= 5
                      and type(points) is int and points >= supporter_goal["remaining_points"])
        available = (preview.get("status") == "accepted" and preview.get("stage") == "preview"
                     and preview.get("kind") in {"SS", "SSS"} and type(count) is int and 1 <= count <= 5)
        if full_group_only and available and (count < 5 or not sufficient):
            # These rewards remain in a full group. A deadline is not a reason
            # to spend a small SS or an already-insufficient full SS as rescue.
            known_shortfall = count < 5 or type(points) is int and points < supporter_goal["remaining_points"]
            resolution = {"status": "unreachable" if known_shortfall else "unknown",
                "reason": "no_full_group" if count < 5 else "full_ss_points_insufficient" if known_shortfall else "ss_points_unknown",
                "ready_count": count, "points_if_all_win": points}
            return {"selected": False, "deadline_ss_only": True,
                "reason": "goal_deadline_unresolved", "deadline_resolution": resolution,
                "local_action": "stop", "local_target": None,
                "local_reason": "No verified sufficient full-group SS at the supporter deadline",
                "local_risk_flags": ["goal_deadline_unresolved"], "goal_completion_claimed": False,
                "training_candidates": [], "training_score": None, "score": None, "components": {},
                "unknown_fields": unknown + ([] if type(points) is int else ["supporter_points_if_all_win"]),
                "policy_version": policy_version(goal), "supporter_goal": supporter_goal}
        victory=ss_victory_evidence(preview)
        if available and not victory['guaranteed']:
            rates=(preview.get('participants_observation') or {}).get('win_rates') or []
            known_risk=(full_group_only and count==5
                and (preview.get('participants_observation') or {}).get('win_rates_status')=='accepted'
                and len(rates)==5 and [row.get('slot') for row in rates]==list(range(5))
                and all(row.get('band') in {'blue','green'} for row in rates)
                and any(row.get('band')=='green' for row in rates))
            return {'selected':False,'reason':'ss_victory_unverified','deadline_ss_only':True,
                'local_action':'stop','local_target':None,'local_reason':'Ordinary SS lacks a guaranteed win at the goal deadline',
                'local_risk_flags':['ss_victory_unverified'],'training_candidates':[],
                'training_score':None,'score':None,'components':{},'unknown_fields':unknown+['ss_win_guarantee'],
                **({'deadline_resolution':{'status':'unreachable','reason':'no_safe_full_group',
                    'ready_count':count,'points_if_all_win':points,'win_rates':rates}} if known_risk else {}),
                'planning':{'ss_victory':victory},'goal_completion_claimed':False}
        return {"selected": available, "deadline_ss_only": True,
                "reason": "deadline_ss_covers_gap" if sufficient else "deadline_ss_best_effort" if available else "goal_deadline_unresolved",
                "local_action": "stop", "local_target": None,
                "local_reason": "Ordinary training/rest forbidden at supporter deadline",
                "local_risk_flags": ([] if sufficient else ["goal_deadline_projected_shortfall"] if available else ["goal_deadline_unresolved"]),
                "goal_completion_claimed": False, "training_candidates": [],
                "training_score": None, "score": None, "components": {}, "unknown_fields": unknown,
                "supporter_goal": {"remaining_turns": 1, "remaining_points": supporter_goal["remaining_points"],
                                   "points_if_all_win": points, "guaranteed_future_points": None}}
    # The deadline blocks ordinary actions, not an independently sufficient SS.
    context = dict(state.scenario_state)
    if isinstance(ss_executions, (list, tuple)) or type(ss_executions) is int:
        context["larc_ss_execution_count"] = len(ss_executions) if isinstance(ss_executions, (list, tuple)) else ss_executions
    context.pop("larc_supporter_goal", None)
    if preview.get("status") == "accepted" and preview.get("stage") == "preview":
        context["larc_ss_preview"] = preview
    strategy = LarcOpeningStrategy(goal)
    local_state = replace(state, scenario_state=context)
    if preflight_only:
        local = strategy.validation_stop(local_state)
        if local is None:
            return {"selected": False, "status": "eligible",
                    "reason": "comparison_deferred_until_preview", "policy_version": policy_version(goal)}
    else:
        local = strategy.evaluate(local_state)
    if post_full or year1_full:
        # Opening may have made an old close-score/timing pick. Rebuild the
        # current local incumbent from every freshly scored eligible row;
        # keep explicit user action tiers and synchronize the actual target.
        from core.auto_trainer.larc_timeline_workflow import selection_key
        fresh=[r for r in local.scored_candidates if not is_hard_rejected(r)]
        if fresh:
            best=min(fresh,key=selection_key);action,target=_local_identity(best)
            local=replace(local,action=action,target=target,
                confidence=best.get('source_confidence',local.confidence),
                reason='Compared all current eligible local action scores after the first Arc',
                risk_flags=list(best.get('risk_flags',[])))
    from core.auto_trainer.larc_recovery_policy import NO_LOCAL_ACTION_REASON
    mei_priority_empty = (unified_larc_enabled(goal) and local.action == 'stop'
        and set(local.risk_flags) == {NO_LOCAL_ACTION_REASON})
    from core.auto_trainer.larc_score_tuning import apply as tune_scores, original_rows, without_tuning
    fallback=next((r['score_tuning'] for r in local.scored_candidates
                   if (r.get('score_tuning') or {}).get('status')=='fallback'),None)
    if fallback:
        plain=choose_larc_ss(state,without_tuning(goal),preview,ss_executions=ss_executions,preflight_only=preflight_only)
        plain['score_tuning']=fallback
        return plain
    model_candidates=original_rows(local.scored_candidates)
    selected_id = (f"train:{local.target}" if local.action == "train" else
                   f"outing.{local.target.replace(':', '.')}" if local.action == "outing" else "rest.normal")
    selected = next((row for row in local.scored_candidates if row["action_id"] == selected_id), {})
    training_score = selected.get("score", 0.0) if local.action == "rest" else selected.get("score")
    if mei_priority_empty:
        selected = {'action_id': 'no_eligible_local_action', 'score': 0.}
        training_score = 0.
    result = {"selected": False, "reason": "local_training_unavailable", "local_action": local.action,
              "local_target": local.target, "local_reason": local.reason, "local_risk_flags": local.risk_flags,
              "training_candidates": local.scored_candidates,
              "training_score": training_score, "score": None, "components": {}, "unknown_fields": unknown,
              "policy_version": policy_version(goal)}
    if preferences_configured(goal):result['decision_preferences']=user_preferences(goal)
    from core.auto_trainer.larc_recovery import period_value
    period = period_value(state)
    if enabled(goal) and period.get("year_index") in {2,3} and period.get("month") in {7,8,9}:
        return dict(result, reason="ss_unavailable_overseas")
    if local.action not in {"train", "rest", "outing"} and not mei_priority_empty:
        return result
    if preview.get("status") != "accepted" or preview.get("stage") != "preview":
        return dict(result, reason="ss_preview_unknown")
    count = preview.get("ready_count")
    if mei_priority_empty and count != 5:
        return dict(result, reason='no_local_action_requires_safe_full_ss')
    if type(count) is not int or not 1 <= count <= 5 or preview.get("kind") not in {"SS", "SSS"}:
        return dict(result, reason="ss_preview_unknown")
    victory=ss_victory_evidence(preview)
    if not victory['guaranteed']:
        return dict(result,reason='ss_victory_unverified',planning={'ss_victory':victory},
                    unknown_fields=unknown+['ss_win_guarantee'])
    gains = preview.get("stats_gain", {})
    value = gain_value(state, gains, goal)
    missing = [key for key in STAT_KEYS if type(gains.get(key)) is not int or gains[key] < 0]
    result["unknown_fields"] += [f"stats_gain.{key}" for key in missing]
    observation = preview.get("participants_observation", {})
    visible = visible_ss_participants(preview)
    bond_units, bond_details = 0., []
    if visible:
        for row in visible:
            # This bar exists only for our support cards. NPC portraits and
            # inferred identities do not acquire a support-bond preference.
            units = support_bond_units(row.get("bond")) if row.get("win_guaranteed") is True else None
            if refined and row.get("win_guaranteed") is True:
                from core.auto_trainer.larc_ss_refinement import ss_bond_units
                units = ss_bond_units(row.get("bond"), count)
            if units is not None:
                bond_units += units
            bond_details.append({"slot": row.get("slot"), "units": units,
                                 "bond": row.get("bond")})
    bond_score = round(bond_units * value["weights"]["effective_bond"], 4)
    # Credit only the observed portion; hidden/absent bars stay unresolved.
    if len(bond_details) == count and all(row["units"] is not None for row in bond_details):
        result["unknown_fields"].remove("ss_support_bond_value")
    rewards = [row.get("current_reward", {}) for row in visible]
    charming = bool(visible) and any(
        reward.get("status") == "accepted" and reward.get("kind") == "charming"
        and reward.get("earned") is not True for reward in rewards)
    recovery = ss_recovery(preview)
    recovery_percent = recovery_delta(state, recovery["points"], recovery["max_increase"])
    recovery_score = recovery_value(state, recovery_percent, include_held_ss=False)
    continuity = energy_continuity(local_state, goal, recovery_percent, consumed_held_ss=True,
        consumed_skill_pt=max(0,gains.get("skill_pt",0)), consumed_gains=gains)
    if any(reward.get("kind") in {"motivation_and_vitality", "max_vitality_and_recovery"}
           and reward.get("amount") is None for reward in rewards):
        result["unknown_fields"].append("reward_vitality_amount")
    turns = gap = None
    if supporter_goal is not None:
        turns, gap = supporter_goal.get("remaining_turns"), supporter_goal.get("remaining_points")
    valid_goal = type(turns) is int and turns > 0 and type(gap) is int and gap > 0
    points = preview.get("supporter_points_if_all_win")
    known_points = type(points) is int and points > 0
    goal_preference = (max(0, 7 - turns) * 8 * min(points / gap, 1.0)
                       if valid_goal and known_points else 0.0)
    params = params_for_goal(goal)
    preference = ((params.full_sss_base_preference if preview["kind"] == "SSS" else params.full_ss_base_preference)
                  * min(1., value["weights"].get('unscaled_net_charge',value["weights"]["net_charge"]) / 6)
                  * value['weights'].get('charge_preference',1.)) if count == params.full_group_min_count else 0
    if post_full:preference=0. # Do not restore a junior bonus by skipping old returned-phase code.
    # Prior SS executions may have contained no charming reward. A currently
    # offered, unearned charming reward still deserves the startup preference.
    charming_plan = charming_value(state, ss_executions)
    first_charming = charming and count >= 3 and charming_plan["score"] > 0
    charming_score = charming_plan["score"]*user_preferences(goal)['bond'] if first_charming else 0.
    cycle = ss_cycle_value(state, preview, value["weights"])
    held_route = continuity.get("future_route_before", [])
    hold_actions = 1 + next((i for i, action in enumerate(held_route) if action.startswith("held_ss")),
                            len(held_route))
    wait=visible_full_group_wait(preview,model_candidates,hold_actions)
    hold_actions=wait['hold_actions']
    # A small visible gap need not be rescued several turns early when this
    # layout can charge a full group before the last ordinary action. Failure
    # retains urgency; a larger gap still gets the original shortage pressure.
    relief=(wait['success_probability']*min(1.,max(0,turns-2)/max(1.,wait['hold_actions']))
        if valid_goal and known_points and points>=gap and wait['available'] else 0.)
    goal_preference*=1-relief
    from core.auto_trainer.larc_mei_cycle import enabled as mei_cycle_enabled
    release = ss_battery_release_value(state, preview, value["weights"], hold_actions=hold_actions,
        cycle_model=mei_cycle_enabled(state, goal),goal=goal)
    release = _release_after_explicit_cycle(release, continuity, goal)
    from core.auto_trainer.larc_fan_goal import fan_route_value, fan_action_reference
    fan = fan_route_value(state, expectation_delta=sum(p.get("win_guaranteed") is True for p in observation.get("participants", []))
                          * (.5 if preview["kind"] == "SS" else 1.),
                          reference_value=fan_action_reference(state, goal))
    from core.auto_trainer.larc_training_forecast import ss_expectation_value
    expectation = ss_expectation_value(state, goal, preview, deferred_actions=hold_actions)
    adaptation = adaptation_timing_value(local_state, goal, preview, continuity)
    phase_preference = None
    phase_opportunity = 0.
    if refined and not post_full:
        from core.auto_trainer.larc_ss_refinement import phase_comparison, best_executable_training, NUMERIC_COMPONENTS
        phase_training = best_executable_training(model_candidates,
            ss_numeric=(sum(value["components"].get(k, 0.) for k in NUMERIC_COMPONENTS)
                        if period.get("year_index") == 1 else None))
        phase_preference = phase_comparison(state, preview, value, phase_training,
            preference=preference, recovery_percent=recovery_percent, recovery_points=recovery["points"],
            strategy_v4=strategy_v4_enabled(goal))
        preference = phase_preference["full_group_preference"]
        phase_opportunity = phase_preference["strong_training_opportunity"]
    if release["complete"]:
        result["unknown_fields"].remove("ss_battery_release_value")
    ss_cont_fix = ss_conserve_bonus = 0.
    ss_conserve_detail = None
    if count == params.full_group_min_count and victory['guaranteed']:
        from core.auto_trainer.larc_strategy_rules import conserve_full_ss
        from core.auto_trainer.larc_timeline_workflow import selection_key as _sel_key
        eligible_local = [r for r in local.scored_candidates if not is_hard_rejected(r)]
        incumbent_local = min(eligible_local, key=_sel_key) if eligible_local else selected
        incumbent_cand = next((c for c in local_state.training_options if c.action_id == (incumbent_local or {}).get('action_id')), None)
        ss_cont_fix, ss_conserve_bonus, ss_conserve_detail = conserve_full_ss(
            incumbent_local, incumbent_cand, local_state,
            continuity_score=continuity["score"], recovery_percent=recovery_percent,
            ss_numeric_score=value["score"], goal=goal)
    result.update(components={**value["components"], "full_group_preference": preference,
                              "effective_bond": bond_score,
                              "battery_release_opportunity": release["score"],
                              "expectation_training_progress": expectation["score"],
                              "conditional_fan_route": fan["score"],
                              **({"adaptation_early_use": adaptation["score"]} if adaptation["score"] else {}),
                              **({"future_recovery_cycle_progress": cycle["score"]} if cycle["score"] else {}),
                              "energy_continuity": continuity["score"],
                              **({"full_ss_continuity_correction": ss_cont_fix} if ss_cont_fix else {}),
                              **({"full_ss_vitality_conservation": ss_conserve_bonus} if ss_conserve_bonus else {}),
                              **({"phase_strong_training_opportunity": phase_opportunity} if refined and not post_full else {}),
                              **({"known_recovery" if not recovery["rule_points"] else "rule_recovery":
                                  round(recovery_score, 4)} if recovery_score > 0 else {}),
                              "supporter_deadline_preference": round(goal_preference, 4),
                              "first_charming_preference": charming_score},
                  gain_value=value, kind=preview["kind"], ready_count=count, recovery=recovery,
                  planning={"energy_horizon": continuity, "reward_cycle": cycle, "charming": charming_plan,
                            "ss_victory":victory,
                            "visible_full_group_wait":wait,
                            "battery_release": release, "fan_route": fan, "adaptation_resource": adaptation,
                            "expectation_training_progress": expectation,
                            **({"full_ss_vitality_conservation": ss_conserve_detail} if ss_conserve_detail is not None else {}),
                            "support_bond": {"basis": "visible_support_bars_shared_contact_value",
                                             "units": bond_units, "participants": bond_details}},
                  supporter_goal={"remaining_turns": turns, "remaining_points": gap,
                                  "deadline_relief_from_visible_charge":round(relief,4),
                                  "points_if_all_win": points, "guaranteed_future_points": None})
    if refined and not post_full:
        result["planning"]["phase_comparison"] = phase_preference
        result["planning"]["support_bond"]["basis"] = "visible_support_bars_with_partial_group_policy_discount_not_bond_points"
        result["planning"]["support_bond"]["partial_group_policy_factor"] = count / 5. if count < 5 else 1.
    from core.auto_trainer.larc_strategy_extension import active_provider
    urgent_supporter_progress = valid_goal and turns <= 2 and count >= 4 and known_points
    early_4p_exception = bool(
        active_provider().early_4p_ss_allowed(params)
        and count == 4
        and (first_charming or urgent_supporter_progress or adaptation["score"] > 0)
    )
    if full_group_only and count < 5 and not early_4p_exception:
        return dict(result, reason="wait_for_full_ss_group", partial_group_policy={
            "basis": "user_prefers_retaining_current_rewards_for_full_group",
            "minimum_ready_count": 5, "ready_count": count,
            "recovery_or_charming_alone_is_exception": False, "deadline_rescue": False,
            "fifth_member_rewards": None, "sss_probability": None})
    workflow_effects=next(((r.get('score_workflow') or {}).get('effects',{})
        for r in local.scored_candidates if (r.get('score_workflow') or {}).get('status')=='applied'),{})
    workflow_customization=bool(workflow_effects.get('preferred_actions')) or any(
        amount!=1 for group in ('training_multipliers','component_factors')
        for amount in workflow_effects.get(group,{}).values())
    if urgent_supporter_progress and not workflow_customization:
        return dict(result, selected=True, reason="urgent_observed_supporter_progress")
    # Two-turn urgency allowed this SS before ordinary numeric/group checks.
    # Keep that eligibility, but compare it with the user's current workflow.
    if count < 5 and not first_charming and recovery_percent <= 0 and not urgent_supporter_progress and not early_4p_exception:
        return dict(result, reason="small_ss_without_confirmed_exception")
    if missing and not urgent_supporter_progress:
        return dict(result, reason="ss_numeric_gain_unknown")
    score = (value["score"] + preference + goal_preference + charming_score + recovery_score
             + continuity["score"] + ss_cont_fix + ss_conserve_bonus + cycle["score"] + bond_score + release["score"] + expectation["score"] + fan["score"] + adaptation["score"] + phase_opportunity)
    ss_row={**result,'action_id':'ss','action':'ss','score':round(score,4),'rejected':False}
    tuning=tune_scores(state,goal,[ss_row],preview)
    if tuning.get('status')=='fallback':
        plain=choose_larc_ss(state,without_tuning(goal),preview,ss_executions=ss_executions,preflight_only=preflight_only)
        plain['score_tuning']=tuning
        return plain
    if tuning['active']:
        result['score_tuning']=ss_row.get('score_tuning');result['components']=ss_row['components']
        if ss_row.get('score_workflow'):result['score_workflow']=ss_row['score_workflow']
        score=ss_row['score']
    customized=tuning['changed'] or any((r.get('score_tuning') or {}).get('adjustment')
        or (r.get('score_workflow') or {}).get('adjustment')
        or (r.get('score_workflow') or {}).get('priority_active') for r in local.scored_candidates)
    from core.auto_trainer.larc_timeline_workflow import prefers_ss
    ss_preferred=(score > 0 if mei_priority_empty else
        training_score is not None and prefers_ss(ss_row,selected or {'score':training_score}))
    result.update(score=round(score, 4), selected=ss_preferred,
                  reason="ss_value_preferred" if ss_preferred else "training_value_preferred")
    if not mei_priority_empty and (ss_row.get('score_workflow') or {}).get('priority_rank',0)!=(selected.get('score_workflow') or {}).get('priority_rank',0):
        result.update(reason='workflow_action_priority',selection_basis='calendar_workflow_action_tier_then_score')
    if refined and not post_full and not year1_full and not customized and phase_preference.get("selection_preference") == "full_ss_first":
        result.update(selected=True, reason="junior_full_group_tempo_preferred",
                      selection_basis=phase_preference["selection_basis"])
    if post_full or year1_full:
        # This is one final score/tier comparison, not a second hidden
        # phase/rainbow rule. SS retains its existing numeric-score tie break.
        from core.auto_trainer.larc_timeline_workflow import selection_key
        pool=[r for r in local.scored_candidates if not is_hard_rejected(r)]
        if not mei_priority_empty or score>0:pool.append(ss_row)
        if pool:
            chosen=min(pool,key=lambda r:(*selection_key(r)[:2],0 if r['action_id']=='ss'else 1,r['action_id']))
            is_ss=chosen['action_id']=='ss'
            phase_name='first_year_full'if year1_full else'post_first_arc'
            result.update(selected=is_ss,reason=phase_name+'_ss_value_preferred'if is_ss else phase_name+'_training_value_preferred',
                selection_basis='current_all_candidate_tier_then_score')
            if not is_ss:
                action,target=_local_identity(chosen)
                result.update(local_action=action,local_target=target,training_score=chosen['score'],
                    local_reason=('Compared the complete current action pool with the bound full SS'if year1_full else
                        'Compared the complete current action pool after the first Arc'),
                    local_risk_flags=list(chosen.get('risk_flags',[])))
            result.setdefault('planning',{})['ss_phase']={'phase':'first_year_domestic'if year1_full else'post_first_arc_domestic',
                'candidate_action_ids':[r['action_id']for r in pool],
                'selected_action_id':chosen['action_id'],'legacy_phase_override_applied':False,
                'legacy_boundary_override_applied':False,'full_group_startup_bonus':preference if year1_full else 0.,
                **({'symmetric_energy_scope':'first_year_bound_full','member_inventory_modelled':False}
                    if year1_full else{})}
        return result
    # Resolve the raw local+SS pool together. Reusing a locally tie-selected
    # incumbent would make the cross-family tie preference order-dependent.
    from core.auto_trainer.larc_boundary_readiness import boundary_readiness
    boundary = boundary_readiness(local_state, goal, [*local.scored_candidates, ss_row], ss_preview=preview)
    if boundary.get('reason') not in {'revision_not_enabled','not_last_free_domestic_action'}:
        result.setdefault('planning',{})['boundary_readiness']=boundary
    if boundary.get('changed'):
        chosen_id=boundary['selected_action_id']
        result.update(selected=chosen_id=='ss',reason='pre_expedition_close_energy_preferred',
                      selection_basis='existing_close_score_band_and_known_entry_energy')
        if chosen_id!='ss':
            chosen=next(row for row in local.scored_candidates if row['action_id']==chosen_id)
            action=chosen.get('action')or('train'if chosen_id.startswith('train:')else None)
            target=chosen.get('target',chosen.get('training_type'))
            if action not in {'train','rest','outing'}:
                raise ValueError('boundary_choice_missing_existing_local_action')
            result.update(local_action=action,local_target=target,training_score=chosen['score'],
                          local_reason='Close-score action preserves known energy for the next overseas window',
                          local_risk_flags=list(chosen.get('risk_flags',[])))
    return result
