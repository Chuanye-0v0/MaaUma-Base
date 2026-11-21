"""Purchase + training comparisons. Future gains are estimates, never cash in hand."""
from dataclasses import replace
from itertools import combinations
from math import isfinite
from statistics import mean

from core.auto_trainer.larc_adaptation import (LEVEL_TWO_COSTS, TRAINING_UPGRADES,
    build_adaptation_plan, growth_bundle_phase)
from core.auto_trainer.larc_attribute_plan import free_windows
from core.auto_trainer.larc_training_value import gain_value, energy_continuity
from core.auto_trainer.rejection_policy import is_hard_rejected
from core.auto_trainer.state import TRAINING_TYPES
from core.auto_trainer.thresholds import SCORE_TIE_EPSILON


def deck_training_coverage(state, profile=None):
    """Use the run's deck, not the tab an off-type visitor occupies today."""
    profile = (profile
               or state.scenario_state.get("larc_support_card_profile")
               or state.scenario_state.get("support_card_profile")
               or state.scenario_state.get("larc_friend_outing", {}).get("support_card_profile"))
    runs = {c.raw_data.get("capture", {}).get("run_session_id") for c in state.training_options}
    counts = dict.fromkeys(TRAINING_TYPES, 0)
    if (not isinstance(profile, dict) or profile.get("schema_version") != "uma_run_support_profile.v1"
            or not profile.get("run_session_id") or runs != {profile["run_session_id"]}):
        return {"counts": counts, "complete": False, "known_slots": 0, "basis": "deck_unknown"}
    aliases = dict(zip(("速度", "耐力", "力量", "根性", "智力"), TRAINING_TYPES))
    known, slots = 0, set()
    for card in profile.get("cards", []):
        slot = card.get("slot")
        if slot in slots:
            continue
        slots.add(slot)
        kind = card.get("type") or card.get("card_type")
        kind = aliases.get(kind, kind)
        if kind in counts:
            counts[kind] += 1
            known += 1
        elif kind in {"friend", "友人", "group", "团队"}:
            known += 1
    return {"counts": counts, "complete": known == 6, "known_slots": known,
            "basis": "run_support_card_profile; future attendance remains uncertain"}


def overseas_windows(month, half):
    return {(7, "first"): 5, (7, "second"): 4, (8, "first"): 3,
            (8, "second"): 2, (9, "first"): 1, (9, "second"): 1}.get((month, half), 0)


def training_income(candidate):
    effects = (candidate.scenario_mechanics.get("larc") or {}).get("training_effects", {})
    capture = candidate.raw_data.get("capture", {})
    source, amount = effects.get("source", {}), effects.get("adaptation_pt")
    bound = (effects.get("status") == "accepted" and capture.get("run_session_id")
             and all(source.get(k) == capture.get(k) for k in ("run_session_id", "turn_index", "sha256")))
    if bound and type(amount) is int and amount >= 0:
        return amount, "observed_training_preview"
    people = (candidate.scenario_mechanics.get("larc") or {}).get("training_participants") or {}
    count = people.get("visible_portrait_count")
    if (people.get("status") == "accepted"
            and people.get("training_type") == candidate.training_type
            and type(count) is int and 0 <= count <= 5):
        min_rainbows = 1 if candidate.friendship is True and count else 0
        floor_ap = (30 if candidate.training_type == "wisdom" else 50) + 20 * count + 20 * min_rainbows
        return floor_ap, "participant_headcount_floor"
    return 100, "policy_estimate_100_per_overseas_training_not_guaranteed"


def _known_failure_rate(candidate, row):
    rate = candidate.failure_rate
    if (type(rate) in (int, float) and isfinite(rate) and 0 <= rate <= 100
            and 'failure_rate_unreadable' not in (row.get('risk_flags') or ())):
        return rate
    return None


def _improves_purchase_deadline(before, after):
    """Prefer a modeled deadline improvement only when no other due gap grows."""
    if before is None or after is None:
        return False
    differences = [round(before.get(key, 0.) - after.get(key, 0.), 3)
                   for key in before.keys() | after.keys()]
    return any(delta > 0 for delta in differences) and all(delta >= 0 for delta in differences)


def _scheduled_deadline_benefit(schedule):
    def shortfalls(side):
        totals = {}
        for row in schedule['comparisons']:
            for gap in row[side]['deadline_shortfalls']:
                key = (gap['day'], gap['slot'])
                totals[key] = totals.get(key, 0.) + row['weight'] * gap['shortfall']
        return totals
    return _improves_purchase_deadline(shortfalls('before'), shortfalls('after'))


def _purchase_beats_wait(row, baseline):
    score = row['score']
    return (score > baseline + SCORE_TIE_EPSILON
            or (score >= baseline - SCORE_TIE_EPSILON and row['deadline_benefit']))


def _best_purchase(rows):
    highest = max(row['score'] for row in rows)
    tied = (row for row in rows if row['score'] >= highest - SCORE_TIE_EPSILON)
    return min(tied, key=lambda row: (not row['deadline_benefit'], len(row['purchase_phases']), row['cost']))


def gains_after_purchase(state, candidate, phase, *, targets=None, purchase_v3=False):
    from core.auto_trainer.larc_expedition_forecast import forecast_metadata
    from core.auto_trainer.larc_training_forecast import project_gains
    source = forecast_metadata(candidate, state)
    if purchase_v3 or state.scenario_state.get('larc_purchase_model_v3') is True:
        source['purchase_v3'] = True
    levels = list(source.get("levels") or [2] * 10)
    source["levels"] = levels
    upgraded = list(targets or levels)
    if targets is None:
        slot = 5 if phase == "expedition_skill_pt" else 7 if phase == "willpower_three" else TRAINING_UPGRADES[candidate.training_type][1]
        upgraded[slot] = 3
    gains, report = project_gains(state.current_stats, candidate.stats_gain, candidate.training_type,
                                  source, {**source, "levels": upgraded})
    return gains, report["unknown_fields"] + ["shared stage-effect model; post-purchase preview replaces estimate"]


def race_budget(levels, year, month, points_after, covered_slot, windows, income, income_basis, failure, *, skip_language=False):
    # 4-1/4-2 are due at senior Arc, not at entry. Other debuffs can be due
    # earlier, at the prep race. Forecasts receive a discount, not certainty.
    before_prep = month < 9
    near_count = 8 if year == 3 or not before_prep else 5
    near = sum(LEVEL_TWO_COSTS[i] for i in range(near_count) if levels[i] == 1 and i != covered_slot
               and not (skip_language and year == 2 and i == 4))
    final = ((200 if levels[8] == 1 else 0) + (150 if levels[9] == 1 else 0)) if year == 3 else 0
    prep_windows = max(0, windows - 1) if before_prep else windows
    expected = income * (1 - (failure or 0) / 100) * .5
    known = income if income_basis == "observed_training_preview" and failure == 0 else 0
    def gap(cost, remaining):
        credit = known if remaining <= 1 else expected * remaining
        return max(0., cost - points_after - credit)
    near_gap, final_gap = gap(near, prep_windows), gap(near + final, windows)
    hard_gap = near_gap if prep_windows <= 1 else 0
    if windows <= 1:
        hard_gap = max(hard_gap, final_gap)
    penalty = near_gap * .35 / max(1, prep_windows) + final_gap * .35 / max(1, windows)
    return {"near_race_cost": near, "final_arc_cost": final, "deadline_total": near + final,
            "training_windows_before_arc": windows, "training_windows_before_near_race": prep_windows,
            "expected_future_income": round(expected * windows, 2), "income_basis": income_basis,
            "known_current_income": known, "unfunded_estimate": round(max(near_gap, final_gap), 2),
            "penalty": round(penalty, 4), "affordable_at_deadline": hard_gap <= 0}


def _observed_ss_purchase_action(state, goal):
    """Reuse a current bound SS comparison, never synthesize a playable SS."""
    preview=state.scenario_state.get('larc_ss_preview', {})
    source=preview.get('source') or preview.get('participants_observation',{}).get('source',{})
    captures=[item.raw_data.get('capture',{}) for item in state.training_options]
    if (preview.get('status')!='accepted' or preview.get('stage')!='preview'
            or source.get('turn_index')!=state.turn or not source.get('run_session_id')
            or not (source.get('path') or source.get('absolute_path'))
            or not (source.get('sha256') or source.get('source_sha256'))
            or len(captures)!=5 or any(c.get('run_session_id')!=source['run_session_id']
                or c.get('turn_index')!=state.turn for c in captures)):
        return None
    from core.auto_trainer.larc_ss_choice import choose_larc_ss
    comparison=choose_larc_ss(state,goal,preview,ss_executions=None)
    if (comparison.get('reason') not in {'ss_value_preferred','training_value_preferred','urgent_observed_supporter_progress'}
            or type(comparison.get('score')) not in {int,float}):
        return None
    return {'action_id':'ss.current','action':'ss','score':comparison['score'],
            'rejected':False,'planning':comparison.get('planning',{}),
            'basis':'existing_bound_ss_comparison_no_purchase_bonus_to_current_ss'}


def recommend_growth_adaptation(observation, period, *, preferred_training=None, race_day=False,
                                state=None, goal=None, support_profile=None):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    from core.auto_trainer.larc_strategy_rules import first_arc_purchase_scope
    with first_arc_purchase_scope(state, goal, period, support_profile, observation=observation):
        if unified_larc_enabled(goal):
            from core.auto_trainer.larc_return_budget import purchase_scope
            with purchase_scope():
                return _recommend_growth_adaptation_core(observation, period,
                    preferred_training=preferred_training, race_day=race_day,
                    state=state, goal=goal, support_profile=support_profile)
        return _recommend_growth_adaptation_core(observation, period,
            preferred_training=preferred_training, race_day=race_day,
            state=state, goal=goal, support_profile=support_profile)


def _recommend_growth_adaptation_core(observation, period, *, preferred_training=None, race_day=False,
                                      state=None, goal=None, support_profile=None):
    from core.auto_trainer.larc_policy import overseas_purchase_v3_enabled
    from core.auto_trainer.larc_workflow_adaptation import apply as apply_workflow
    tail_diagnostic = None
    if state is not None and goal is not None and not race_day:
        from core.auto_trainer.larc_tail263.production import in_scope, purchase_recommendation, summary
        if in_scope(state, goal) and not observation.get('errors') and observation.get('pending_cost') == 0:
            proposed, detail = purchase_recommendation(observation, period, state, goal, support_profile)
            tail_diagnostic = summary(detail)
            if proposed is not None:
                return apply_workflow(proposed, observation, state, goal, race_day=race_day)
    if overseas_purchase_v3_enabled(goal):
        from core.auto_trainer.larc_purchase_v3 import recommend
        if state is not None:
            state = replace(state, scenario_state={**state.scenario_state, 'larc_purchase_model_v3': True})
        legacy = _recommend_growth_adaptation(observation, period, preferred_training=preferred_training,
            race_day=race_day, state=state, goal=goal, support_profile=support_profile)
        result = recommend(observation, period, state, goal, legacy, support_profile=support_profile, race_day=race_day)
    else:
        result = _recommend_growth_adaptation(observation, period, preferred_training=preferred_training,
            race_day=race_day, state=state, goal=goal, support_profile=support_profile)
    if tail_diagnostic is not None:result['tail263']=tail_diagnostic
    return apply_workflow(result, observation, state, goal, race_day=race_day)


def _recommend_growth_adaptation(observation, period, *, preferred_training=None, race_day=False,
                                state=None, goal=None, support_profile=None):
    from core.auto_trainer.larc_policy import overseas_purchase_v2_enabled, overseas_purchase_v3_enabled
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    v2 = overseas_purchase_v2_enabled(goal) if goal is not None else False
    unified = unified_larc_enabled(goal)
    balanced = unified
    empty = {"phase": None, "reason": "no_growth_upgrade_selected", "policy_kind": "joint_purchase_training"}
    if (not isinstance(period, dict) or period.get("status") != "verified"
            or not isinstance(observation, dict) or observation.get("errors") or observation.get("pending_cost") != 0):
        return {**empty, "reason": "current_grid_or_period_unknown"}
    value = period.get("value") or {}
    year, month, half = value.get("year_index"), value.get("month"), value.get("half")
    levels, points = observation.get("levels"), observation.get("points")
    if (year not in {2, 3} or type(month) is not int or not 1 <= month <= 12 or half not in {"first", "second"}
            or not isinstance(levels, list) or len(levels) != 10
            or any(type(x) is not int or not 0 <= x <= 3 for x in levels) or type(points) is not int or points < 0):
        return {**empty, "reason": "growth_context_unavailable"}
    if race_day:
        if year == 3 and month == 10 and half == "first" and levels[9] == 1:
            reserve = sum(LEVEL_TWO_COSTS[i] for i in range(9) if levels[i] == 1)
            if points >= reserve + 150:
                return {**empty, "phase": "champion_dream_two", "cost": 150,
                        "reason": "final_arc_race_bonus", "reserved_race_points": reserve,
                        "points_after_purchase": points - 150}
        return empty
    if state is None or goal is None:
        return {**empty, "reason": "training_comparison_required"}
    runs = {c.raw_data.get("capture", {}).get("run_session_id") for c in state.training_options}
    if len(runs) == 1 and next(iter(runs)):
        state = replace(state, scenario_state={**state.scenario_state, "larc_expedition_context": {
            "status": "accepted", "run_session_id": next(iter(runs)), "through_turn": state.turn,
            "levels": list(levels), "points": points, "balance_current": True}})
    from core.auto_trainer.larc_opening import LarcOpeningStrategy
    local = LarcOpeningStrategy(goal).evaluate(state)
    rows = [row for row in local.scored_candidates if not is_hard_rejected(row)
            and row.get('allowed') is not False and row.get('eligible') is not False]
    if v2:
        ss_action = _observed_ss_purchase_action(state, goal)
        if ss_action is not None:rows.append(ss_action)
    if not rows:
        return {**empty, "reason": "no_eligible_action"}
    baseline = max(rows, key=lambda row: row["score"])
    base_score = baseline["score"]
    scored = {row["action_id"]: row for row in rows}
    options = [item for item in state.training_options if item.action_id in scored]
    if not options and not balanced:
        return {**empty, "reason": "recovery_preferred_no_eligible_training"}
    # In v2 cash has value through lost modeled purchases/deadlines, not a
    # second flat penalty after their timing difference has already been counted.
    point_rate = 0. if v2 else (.035 if year==3 else .06)
    abroad = 7 <= month <= 9
    strategy_20 = unified
    price_current_courage = (abroad and year == 3 and strategy_20
                             and overseas_purchase_v3_enabled(goal))
    windows = overseas_windows(month, half) if abroad else 0
    free, _ = free_windows(state, goal, {})
    from core.auto_trainer.larc_calendar import period_ordinal as _pord
    from core.auto_trainer.larc_strategy_rules import banned_on_day, first_arc_basket_allowed
    _cur_ord = _pord(value)
    available = []
    if abroad:
        available.extend((phase, slot, tab) for tab, (phase, slot) in TRAINING_UPGRADES.items()
                         if levels[slot] in {1, 2} and not banned_on_day(slot, _cur_ord))
    if free > 1:
        available.extend((phase, slot, None) for phase, slot in (("expedition_skill_pt", 5), ("willpower_three", 7))
                         if levels[slot] in {1, 2})
    if abroad and windows > 1 and levels[6] == 2:
        available.append(("courage_three", 6, None))
    numeric = {item.training_type: gain_value(state, item.stats_gain, goal)["score"] for item in options}
    coverage = deck_training_coverage(state, support_profile)
    support_total = max(1, sum(coverage["counts"].values()), 0 if coverage["complete"] else 5)
    from core.auto_trainer.larc_training_value import _future_numeric_values
    from core.auto_trainer.larc_recovery_routes import recovery_window
    from core.auto_trainer.larc_recovery import estimated_training_cost
    from core.auto_trainer.larc_training_forecast import training_scenarios
    reference = _future_numeric_values(state, goal)
    references = [reference]
    reference_indices = {}
    for item in options:
        # Reuse the direct-training forecast after its current bond/startup
        # progress has been consumed. Equal budgets can still share work.
        used = scored[item.action_id].get("planning", {}).get("energy_horizon", {}).get(
            "future_value_reference", reference)
        if used not in references:
            references.append(used)
        reference_indices[item.action_id] = references.index(used)
    window = recovery_window(state)
    training_cost, _ = estimated_training_cost(state)
    considered = []
    bundles = []
    for size in range(1, min(4, len(available)) + 1):
        for parts in combinations(available, size):
            if sum(part[2] is not None for part in parts) <= 1:
                bundles.append(parts)
    before_case = {"name": "before", "levels": levels, "weight": 1., "cost": 0}
    before_forecast = training_scenarios(state, goal, reference, window, training_cost, cases=[before_case])
    from core.auto_trainer.larc_adaptation_schedule import build_comparison,training_share,canonical_training_share
    share_fn=canonical_training_share if unified else training_share
    from core.auto_trainer.larc_calendar import free_action_days,period_ordinal,period_at,is_overseas
    now=period_ordinal(value);schedule_days=free_action_days(now+1)[:12]
    language_deferred = balanced and year == 2 and 36 <= now <= 42 and levels[4] == 1
    schedule_window={**(window or {}),'steps':len(schedule_days),'future_trainings':len(schedule_days),
        'action_calendar':[{'period':period_at(day),'kind':'overseas' if is_overseas(day) else 'domestic'} for day in schedule_days]}
    schedule_cases=[]
    for pt,friend in ((False,False),(True,False),(False,True),(True,True)):
        target=list(levels)
        if pt:target[5]=3
        if friend:target[7]=3
        schedule_cases.append({'name':str((pt,friend)),'levels':target,'cost':0,'weight':.25})
    schedule_forecast=training_scenarios(state,goal,reference,schedule_window,training_cost,cases=schedule_cases) if schedule_days else {'active':False}
    utilities=[{} for _ in schedule_days]
    masks=((False,False),(True,False),(False,True),(True,True))
    for mask,case in zip(masks,schedule_forecast.get('scenarios',[])):
        for index,layouts in enumerate(case['calendar_layouts']):
            values=[]
            for layout in layouts:
                weights=[coverage['counts'].get(row['tab'],0) if coverage['complete'] else 1 for row in layout]
                values.append(sum(row['numeric']*weight for row,weight in zip(layout,weights))/max(1,sum(weights)))
            utilities[index][mask]=mean(values) if values else 0.
    for utility in utilities:
        baseline_value=utility.get((False,False),0.)
        for mask in masks:utility[mask]=utility.get(mask,baseline_value)-baseline_value
    baseline_item=next((item for item in options if item.action_id==baseline['action_id']),None)
    base_failure = _known_failure_rate(baseline_item, baseline) if baseline_item else 0
    base_income,base_basis=training_income(baseline_item) if baseline_item is not None else (0,'no_current_training')
    if base_failure is None:base_income=0
    # The cash forecast models only its no-income branch for unreadable risk.
    baseline_cash_failure = base_failure if base_failure is not None else 100
    if not abroad and base_basis!='observed_training_preview':base_income=0
    forecast_cache={(tuple(levels),0):before_forecast};continuation_cache={}
    short_energy_cache = {}
    for parts in bundles:
        phases = [part[0] for part in parts]
        phase = growth_bundle_phase(phases)
        tab = next((part[2] for part in parts if part[2]), None)
        try:
            plan = build_adaptation_plan(observation, phase=phase)
        except ValueError:
            considered.append({"phase": phase, "rejected": True, "reason": "unaffordable_or_locked"})
            continue
        after_levels = plan["targets"]
        if not first_arc_basket_allowed(after_levels, levels, points, plan["cost"], now):
            continue
        future_levels = list(after_levels)
        count = coverage["counts"].get(tab, 0)
        repeat_share = count / support_total if tab else 0.
        off_deck_cost = 0.
        if tab and coverage["complete"] and not count:
            # A known off-deck pileup can justify today's purchase, without
            # assuming it repeats in later training layouts.
            future_levels[TRAINING_UPGRADES[tab][1]] = levels[TRAINING_UPGRADES[tab][1]]
            off_deck_cost = 200 * .20 if year == 2 else 0.
        future_levels[5]=levels[5];future_levels[7]=levels[7]
        after_case = {"name": "other_only", "levels": future_levels, "weight": 1., "cost": 0}
        level_key=tuple(future_levels);key=(level_key,0)
        if key not in forecast_cache:
            forecast_cache[key]=training_scenarios(state,goal,reference,window,training_cost,cases=[after_case])
        forecast=forecast_cache[key]
        # A three-speed deck has future speed opportunities even when today's
        # other tab wins every copied layout. This discounted marginal prior
        # replaces, rather than adds to, the route's numeric upgrade benefit.
        prior_values = []
        if forecast["active"] and tab and count:
            for elapsed, (old, new) in enumerate(zip(before_forecast["scenarios"][0]["calendar_layouts"],
                                                     forecast["scenarios"][0]["calendar_layouts"])):
                marginals = [max(0., b["numeric"] - a["numeric"])
                             for first, second in zip(old, new) for a, b in zip(first, second)
                             if a["tab"] == tab]
                prior_values.append(mean(marginals) * repeat_share * .5 * .95 ** (elapsed + 1) if marginals else 0.)
        # Global bonuses survive a recovery/SS action. In v2 their purchase
        # can precede the same eligible action, without inventing training
        # gains or adaptation income for that non-training current turn.
        if v2 and set(phases) <= {"expedition_skill_pt", "willpower_three"}:
            for base in rows:
                if base['action_id'] in {item.action_id for item in options}:
                    continue
                schedule=build_comparison(now,points,levels,schedule_days,utilities,
                    initial_targets=after_levels,initial_cost=plan['cost'],
                    baseline_share=share_fn(baseline),after_share=share_fn(base),
                    baseline_income=base_income,baseline_failure=baseline_cash_failure,
                    after_income=0,after_failure=0,point_rate=point_rate,respect_observed_locks=v2,skip_language=language_deferred)
                from core.auto_trainer.larc_adaptation_schedule import imminent_shortfall
                imminent=imminent_shortfall(schedule,after_levels,now)
                budget={'penalty':round(schedule['incremental_unfunded']*.35,4),
                    'affordable_at_deadline':imminent<=0,'deadline_total':0,
                    'schedule_imminent_shortfall':round(imminent,4),
                    'schedule_future_unfunded_estimate':round(schedule['unfunded_estimate'],4),
                    'income_basis':'non_training_current_action_no_training_income'}
                opportunity=schedule['net_spending']*point_rate
                slots=[5 if name=='expedition_skill_pt' else 7 for name in phases]
                mask=(5 in slots,7 in slots)
                timing_utility=sum(value.get(mask,0.) for value in utilities)
                equivalent=(base['action_id']==baseline['action_id'] and timing_utility>0
                    and abs(schedule['future_difference'])<1e-8 and abs(schedule['net_spending'])<1e-8
                    and schedule['incremental_unfunded']==0 and all(
                        branch['after']['feasible'] and all(slot in branch['before']['purchase_days'] for slot in slots)
                        for branch in schedule['comparisons']))
                considered.append({'phase':phase,'cost':plan['cost'],'training':None,
                    'after_action_id':base['action_id'],'timing_equivalent_purchase':equivalent,
                    'timing_utility':round(timing_utility,4),'score':round(base['score']+
                        schedule['future_difference']-opportunity-budget['penalty'],4),
                    'action_score_after_purchase':base['score'],'training_score_after_purchase':None,
                    'estimated_gains':{},'future_delay_value':0.,'future_value_change':0.,
                    'point_opportunity_cost':round(opportunity,4),'purchase_phases':phases,
                    'purchase_schedule':schedule,'purchase_timing_difference':round(schedule['future_difference'],4),
                    'budget':budget,'rejected':not budget['affordable_at_deadline'],
                    'assumptions':['same eligible non-training action; no current training gain or income',
                                   'normal post-purchase observation replans the next action']})
        for item in options:
            if tab and item.training_type != tab:
                continue
            base = scored[item.action_id]
            failure = _known_failure_rate(item, base)
            if failure is None:
                new_gains = item.stats_gain
                assumptions = ['failure rate unreadable; no current purchase uplift or income credited']
                score = base["score"]
            else:
                new_gains, assumptions = gains_after_purchase(state, item, phase, targets=after_levels)
                uplift = gain_value(state, new_gains, goal)["score"] - numeric[item.training_type]
                score = base["score"] + uplift * (1 - failure / 100)
            energy = base.get("planning", {}).get("training_vitality_percent", 0)
            after = energy * .8 if "courage_three" in phases and energy < 0 else energy
            reference_index = reference_indices[item.action_id]
            old = base.get("planning", {}).get("energy_horizon", {})
            same_current_gains = (not unified or all(
                new_gains.get(key, 0) == item.stats_gain.get(key, 0) for key in (*TRAINING_TYPES, 'skill_pt')))
            # The short energy solver reads the existing reference and post-
            # action HP, regardless of the projected levels or current gains.
            if strategy_20 and old.get("kind") == "symmetric_short_energy_only":
                continuation = old
                if after != energy:
                    short_key = (reference_index, after)
                    if short_key not in short_energy_cache:
                        short_energy_cache[short_key] = energy_continuity(state, goal, after,
                            future_reference=references[reference_index])
                    continuation = short_energy_cache[short_key]
            elif future_levels==levels and after==energy and same_current_gains:
                continuation=old
            else:
                key=(level_key,reference_index)
                if key not in forecast_cache:
                    forecast_cache[key]=training_scenarios(state,goal,references[reference_index],window,training_cost,cases=[after_case])
                continuation_key=(level_key,reference_index,after)
                if unified:
                    continuation_key += (tuple(new_gains.get(key, 0) for key in (*TRAINING_TYPES, 'skill_pt')),)
                if continuation_key not in continuation_cache:
                    continuation_cache[continuation_key]=energy_continuity(state,goal,after,
                        future_forecast=forecast_cache[key],future_reference=references[reference_index],
                        consumed_gains=new_gains)
                continuation=continuation_cache[continuation_key]
            future = continuation.get("future_value_after", 0.) - old.get("future_value_after", 0.)
            current_courage = None
            if (price_current_courage and after != energy and failure is not None
                    and old.get("kind") == continuation.get("kind") == "symmetric_short_energy_only"):
                # The short solver uses the same reference for both outcomes.
                # v3 replaces the legacy future term with a cash route that
                # does not carry the current training's ending energy.
                current_courage = {"before_percent": energy, "after_percent": after,
                                   "score": round(future, 4)}
            # Credit the prior only while the same energy route has consuming
            # training opportunities; don't promise extra turns or rainbows.
            route = continuation.get("future_route_after", [])
            usable = sum(action == "train" for action in route) / max(1, len(route))
            prior = sum(prior_values) * usable
            future = max(future, prior) if after == energy else future
            assumptions.append('same future action window; conditional Pt/friendship dates; no promised rainbows/income/wins')
            income, basis = (training_income(item) if failure is not None
                             else (0, 'current_failure_rate_unreadable_no_income_credit'))
            budget = race_budget(after_levels, year, month, points - plan["cost"], None, windows, income,
                                 basis, failure, skip_language=language_deferred) if abroad else {
                                     "penalty": 0., "affordable_at_deadline": True, "deadline_total": 0}
            current_income=income if abroad or basis=='observed_training_preview' else 0
            schedule=build_comparison(now,points,levels,schedule_days,utilities,initial_targets=after_levels,initial_cost=plan['cost'],
                baseline_share=share_fn(baseline),after_share=share_fn(base),baseline_income=base_income,
                baseline_failure=baseline_cash_failure,after_income=current_income,
                after_failure=failure if failure is not None else 100,
                point_rate=point_rate,respect_observed_locks=v2,skip_language=language_deferred)
            opportunity=schedule['net_spending']*point_rate+off_deck_cost
            schedule_penalty=schedule['incremental_unfunded']*.35
            if v2:
                # A shortfall shared with waiting is not a purchase cost.
                # Keep the absolute budget and its deadline gate for diagnosis.
                budget['absolute_race_penalty']=budget['penalty']
                budget['penalty']=round(schedule_penalty,4)
            else:
                budget['penalty']=round(max(budget['penalty'],schedule_penalty),4)
            # If the next essential deadline is imminent, unknown later income
            # may not fund this purchase. The ordinary current plan still owns execution.
            from core.auto_trainer.larc_adaptation_schedule import imminent_shortfall
            imminent=imminent_shortfall(schedule,after_levels,now)
            budget['schedule_imminent_shortfall']=round(imminent,4)
            budget['schedule_future_unfunded_estimate']=round(schedule['unfunded_estimate'],4)
            if imminent>0:
                budget['affordable_at_deadline']=False
            considered.append({"phase": phase, "cost": plan["cost"], "training": item.training_type,
                "score": round(score + future + schedule['future_difference'] - opportunity - budget["penalty"], 4),
                "training_score_after_purchase": round(score + (current_courage or {}).get("score", 0.), 4),
                "estimated_gains": new_gains,
                **({"current_courage_saving": current_courage} if current_courage is not None else {}),
                "future_delay_value": round(future, 4), "future_value_change": round(future, 4),
                "point_opportunity_cost": round(opportunity, 4), "purchase_phases": phases,
                'purchase_schedule':schedule,'purchase_timing_difference':round(schedule['future_difference'],4),
                "future_delay_values_by_period": [round(value, 4) for value in prior_values],
                "deck_opportunity_prior": round(prior, 4),
                "future_value_basis": forecast.get("basis"),
                "repeat_training_share_estimate": round(repeat_share, 4),
                "off_deck_first_expedition_cost": round(off_deck_cost, 4),
                "budget": budget, "rejected": not budget["affordable_at_deadline"], "assumptions": assumptions})
    eligible = []
    for row in considered:
        if is_hard_rejected(row):
            continue
        row['deadline_benefit'] = _scheduled_deadline_benefit(row['purchase_schedule'])
        if _purchase_beats_wait(row, base_score):
            eligible.append(row)
    if not eligible:
        return {**empty, "baseline_action": baseline["action_id"], "baseline_score": base_score,
                "deck_coverage": coverage, "considered": considered}
    best = _best_purchase(eligible)
    return {**empty, "phase": best["phase"], "cost": best["cost"], "training": best["training"],
            "purchase_phases": best["purchase_phases"],
            "reason": "purchase_training_bundle_preferred", "points_after_purchase": points - best["cost"],
            "reserved_race_points": best["budget"]["deadline_total"], "budget": best["budget"],
            "baseline_action": baseline["action_id"], "baseline_score": base_score,
            "deck_coverage": coverage,
            "score": best["score"], "after_action_id":best.get("after_action_id", "train:"+str(best["training"])),
            "considered": considered,
            "prediction_status": "estimate_replaced_by_normal_post_purchase_observation"}
