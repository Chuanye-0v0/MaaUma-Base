"""Finite full-career purchase routes. Estimates are never current cash.

All alternatives share the same action calendar and opportunity samples. The
beam bounds runtime, not dates: every route reaches the last training action.
No training weights, device inputs, or settled receipts are changed here.
"""
from functools import lru_cache
from statistics import mean

from core.auto_trainer.larc_adaptation import LEVEL_TWO_COSTS, GROWTH_PHASE_SLOTS, TRAINING_PHASE_SLOTS, build_adaptation_plan
from core.auto_trainer.larc_adaptation_schedule import UNLOCK_DAYS, income_at, mandatory_due, training_share,canonical_training_share
from core.auto_trainer.larc_calendar import free_action_days, period_at, period_ordinal, is_overseas
from core.auto_trainer.rejection_policy import is_hard_rejected
from core.auto_trainer.larc_adaptation_strategy import _best_purchase, _improves_purchase_deadline, _purchase_beats_wait
from core.auto_trainer.thresholds import SCORE_TIE_EPSILON

VERSION = 'larc_purchase_plan.v3'
PHASES = {slot: phase for phase, slot in GROWTH_PHASE_SLOTS.items()}
BEAM = 12


def _cost(slot, level):
    if level >= 3 or level <= 0:
        return None
    if slot == 6 and level != 2:
        return None  # The existing courage executor requires Lv2 first.
    return (LEVEL_TWO_COSTS[slot] if level == 1 else 0) + (300 if slot in (6, 7) else 200)


def _identity(state):
    runs = {row.raw_data.get('capture', {}).get('run_session_id') for row in state.training_options} if state else set()
    return next(iter(runs)) if len(runs) == 1 else None


def _first_arc_french_budgets(levels, points):
    pre_arc_prep = sum(LEVEL_TWO_COSTS[i] for i in range(8)
        if i not in {4, 5, 7} and levels[i] in {0, 1}
        and (levels[i] == 1 or UNLOCK_DAYS.get(i, 99) <= 42))
    perm_req = sum(_cost(i, levels[i]) or (500 if i == 7 and levels[i] == 0 else 0)
                   for i in (5, 7) if levels[i] < 3)
    french_cost = LEVEL_TWO_COSTS[4]
    can_afford_french = points >= pre_arc_prep + french_cost
    after_french = points - pre_arc_prep - french_cost
    return {
        'pre_arc_prep': pre_arc_prep,
        'perm_req': perm_req,
        'french_cost': french_cost,
        'can_afford_french': can_afford_french,
        'after_french': after_french,
        'win_cash': after_french + 80,
        'loss_cash': after_french + 50,
        'win_shortfall': max(0, perm_req - (after_french + 80)),
        'loss_shortfall': max(0, perm_req - (after_french + 50)),
    }


def _language(levels, points, now, *, balance_resources=False, arc1_ap_credit=0):
    if now is None or not 36 <= now <= 42 or levels[4] != 1:
        return False
    if balance_resources:
        # Locked friendship still needs its complete 1->3 cost. In particular,
        # a 0 observed after the prep race is not a free permanent upgrade.
        b = _first_arc_french_budgets(levels, points)
        if arc1_ap_credit > 0 and now in (41, 42):
            return not (
                b['can_afford_french']
                and b['after_french'] + arc1_ap_credit >= b['perm_req']
            )
        required = b['pre_arc_prep'] + b['perm_req']
        return points < required + b['french_cost']
    # This is a declared performance tradeoff, not a claim that a race was won.
    required = sum(LEVEL_TWO_COSTS[i] for i in range(8) if i not in {4, 5, 7} and levels[i] == 1)
    goal_cost = sum(_cost(i, levels[i]) or (500 if i == 7 and now < 41 else 0)
                    for i in (5, 7) if levels[i] < 3)
    return points >= required and points < required + goal_cost + LEVEL_TWO_COSTS[4]


def _stage_funding(now, points, levels, current_income, share, *, focused=False):
    """Reserve permanent goals once, without counting their Lv2 cost twice."""
    if not 36 <= now <= 43 or levels[7] == 3:
        return None
    preparation = sum(LEVEL_TWO_COSTS[i] for i in range(8)
                   if i not in {4, 5, 7} and levels[i] in {0, 1}
                   and (levels[i] == 1 or UNLOCK_DAYS.get(i, 99) <= 42))
    required = preparation
    permanent = {}
    for slot in (5, 7):
        permanent[slot] = _cost(slot, levels[slot]) or (500 if slot == 7 and levels[slot] == 0 else 0)
        required += permanent[slot]
    income = current_income + sum(income_at(day, 'ordinary', share) for day in free_action_days(now + 1, 43))
    result = {'deadline_ordinal': 43, 'required_remaining': required,
            'cash_current': points, 'income_estimate_before_deadline': round(income, 3),
            'shortfall_estimate': round(max(0., required - points - income), 3),
            'forecast_only': True}
    if focused:
        result.update(reserve_mode='conditional_goal_timing_not_current_cash_lock',
            other_preparation_remaining=preparation,
            permanent_goals_remaining={str(k): v for k, v in permanent.items()},
            income_is_conditional_not_payment=True)
    return result


def first_arc_language_reserve_decision(state, goal, levels, points, now, *, balance_resources=False):
    """Shared First-Arc language deferral check unifying main purchase & return budget."""
    arc1_branch = None
    arc1_credit = 0.0
    if goal is not None and now in (41, 42) and isinstance(levels, (list, tuple)) and len(levels) > 4 and levels[4] == 1:
        from core.auto_trainer.larc_strategy_extension import active_provider
        from core.auto_trainer.larc_strategy_params import params_for_goal

        params = params_for_goal(goal)
        ext_res = active_provider().first_arc_reserve_decision(
            params, state, goal, levels, points, now, balance_resources
        )
        if ext_res is not None:
            return ext_res
    deferred = _language(levels, points, now, balance_resources=balance_resources, arc1_ap_credit=arc1_credit)
    return deferred, arc1_branch, arc1_credit


def _diagnostic(state, period, levels, points, *, balance_resources=False, goal=None):
    now = period_ordinal(period.get('value', {}))
    remaining = free_action_days(now + 1) if now is not None else []
    friend_cost = _cost(7, levels[7])
    friend = 'owned' if levels[7] == 3 else 'locked' if levels[7] == 0 else 'planned' if points >= friend_cost else 'unfunded'
    deferred, arc1_branch, _ = first_arc_language_reserve_decision(
        state, goal, levels, points, now, balance_resources=balance_resources)
    result = {'schema_version': VERSION, 'run_session_id': _identity(state),
        'through_turn': state.turn if state else None,
        'calendar': [period_at(day) for day in remaining],
        'goals': {'skill_pt': {'target_level': 3, 'status': 'owned' if levels[5] == 3 else 'locked' if levels[5] == 0 else 'planned'},
                  'friendship': {'target_level': 3, 'preferred_ordinal': 41,
                                 'deadline_ordinal': 43, 'status': friend}},
        'language_debuff_deferred': deferred,
        'selected_route': {'actions': []},
        'basis': 'same remaining calendar; observed opportunity samples; conditional future income',
        'forecast_only': True}
    if arc1_branch is not None:
        result['first_arc_ap_branch'] = arc1_branch
    return result


def _deck_upgrade_context(state, goal, period, support_profile):
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
    if not unified_larc_enabled(goal):
        return None
    params = params_for_goal(goal)
    value = period.get('value', {})
    if value.get('year_index') != 2 or value.get('month') not in {7, 8, 9}:
        return None
    from core.auto_trainer.larc_adaptation_strategy import deck_training_coverage
    coverage = deck_training_coverage(state, support_profile)
    enough_known = coverage['complete'] or coverage.get('known_slots', 0) >= params.deck_upgrade_min_known_slots
    return {**coverage, 'active': enough_known and sum(coverage['counts'].values()) > 0,
            'observed_weight': 1.0 - params.deck_prior_weight, 'deck_weight': params.deck_prior_weight,
            'scope': 'future first-expedition facility uplift only',
            'basis': coverage['basis'] + '; bounded deck prior, not measured attendance probability'}


def _facility_value(global_values, upgraded_values, tabs, counts):
    """Replace, never add to, the old copied-layout facility uplift.

    Permanent Pt/friendship utility remains in the global maximum. Current
    gains, cash and all non-facility preferences are evaluated elsewhere.
    """
    weight = sum(counts.get(tab, 0) for tab in tabs)
    if weight <= 0:
        return max(upgraded_values)
    baseline = max(global_values)
    observed = max(upgraded_values) - baseline
    deck = sum(counts.get(tab, 0) * max(0., after - before)
               for tab, before, after in zip(tabs, global_values, upgraded_values)) / weight
    return baseline + .5 * observed + .5 * deck


def _main_pt_exception(row, levels, revised, deck_prior):
    """Only a real main-axis facility may borrow the first-main + Pt exemption.

    This does not reject off-axis purchases: they still use the existing cash
    and permanent-goal comparison, and may win when they do not worsen it.
    """
    allowed = ('expedition_skill_pt' in row['purchase_phases']
        and (not revised or 'french_two' not in row['purchase_phases'])
        and not any(level == 3 for level in levels[:5]))
    if not allowed or not deck_prior or not deck_prior['active']:
        return allowed
    counts = deck_prior['counts']
    from core.auto_trainer.larc_adaptation import TRAINING_UPGRADES
    upgraded = [tab for tab, (phase, _) in TRAINING_UPGRADES.items()
                if phase in row['purchase_phases']]
    return not upgraded or any(counts.get(tab, 0) == max(counts.values()) for tab in upgraded)


def _utility_model(state, goal, levels, days, *, deck_prior=None):
    from core.auto_trainer.larc_training_value import _future_numeric_values
    from core.auto_trainer.larc_training_forecast import training_scenarios, project_gains, numeric_at_period
    from core.auto_trainer.larc_recovery import estimated_training_cost
    from core.auto_trainer.larc_attribute_plan import attribute_plan
    reference = _future_numeric_values(state, goal)
    window = {'steps': len(days), 'future_trainings': len(days), 'kind': 'purchase_full_career',
              'action_calendar': [{'period': period_at(day), 'kind': 'overseas' if is_overseas(day) else 'domestic'} for day in days]}
    cost, _ = estimated_training_cost(state)
    forecast = training_scenarios(state, goal, reference, window, cost,
        cases=[{'name': 'owned', 'levels': levels, 'cost': 0, 'weight': 1.}])
    scenarios = forecast.get('scenarios') or [{}]
    layouts = scenarios[0].get('calendar_layouts', [])
    context = attribute_plan(state, goal)
    @lru_cache(maxsize=16384)
    def projected(index, sample, position, training_level, pt_level, friend_level):
        row = layouts[index][sample][position]
        source = {**row.get('destination_metadata', {}), 'period': period_at(days[index]),
                  'levels': levels, 'purchase_v3': True}
        target_levels = list(levels)
        from core.auto_trainer.larc_expedition_forecast import TRAINING_SLOTS
        target_levels[TRAINING_SLOTS[row['tab']]] = training_level
        target_levels[5], target_levels[7] = pt_level, friend_level
        gains, _ = project_gains(state.current_stats, row['gains'], row['tab'], source,
                                 {**source, 'levels': target_levels})
        return (numeric_at_period(state, goal, gains, period_at(days[index]), attribute_context=context)
                * row.get('forecast_discount', 1.))
    @lru_cache(maxsize=16384)
    def _utility_8(index, owned):
        if index >= len(layouts):
            return 0.
        values = []
        from core.auto_trainer.larc_expedition_forecast import TRAINING_SLOTS
        for sample, layout in enumerate(layouts[index]):
            before, after = [], []
            globals_, tabs = [], []
            for position, row in enumerate(layout):
                before.append(row['numeric'])
                after.append(projected(index, sample, position, owned[TRAINING_SLOTS[row['tab']]], owned[5], owned[7]))
                if deck_prior and deck_prior['active']:
                    globals_.append(projected(index, sample, position,
                        levels[TRAINING_SLOTS[row['tab']]], owned[5], owned[7]))
                    tabs.append(row['tab'])
            if before:
                # Keep absolute future utility so an unfunded mandatory race
                # loses ordinary future training too, not only upgrade deltas.
                # Complete routes cancel their common utility on comparison.
                destination = period_at(days[index])
                if (deck_prior and deck_prior['active'] and destination['year_index'] == 2
                        and is_overseas(days[index])):
                    values.append(_facility_value(globals_, after, tabs, deck_prior['counts']))
                else:
                    values.append(max(after))
        result = mean(values) if values else 0.
        # Cost reduction has no domestic value. It can save only a fraction of
        # a recovery action; it never creates a guaranteed extra rainbow.
        if is_overseas(days[index]) and owned[6] == 3 and levels[6] != 3:
            baseline = [max(row['numeric'] for row in layout) for layout in layouts[index] if layout]
            result += (mean(baseline) if baseline else 0.) * min(.2, max(0., cost) * .2 / 50.)
        return result

    def utility(index, owned):
        return _utility_8(index, owned[:8])
    return utility, forecast.get('unknown_fields', [])


def _same_day_upgrades(owned, cash, day, *, multiple=False):
    """Zero-main upgrades affordable from cash already held on this date.

    Eight fixed slots give at most256 subsets. No income is added while
    expanding this monotone closure; each slot can change toLv3 only once.
    The caller applies its one training opportunity/income after purchases.
    """
    from core.auto_trainer.larc_strategy_rules import banned_on_day, first_arc_basket_allowed
    candidates=[]
    for slot in range(8):
        price=_cost(slot,owned[slot])
        if price is not None and price<=cash and(slot in(5,7)or is_overseas(day)) and not banned_on_day(slot, day):
            candidates.append((slot,price))
    if not multiple:
        options = [((),0)]+[((slot,),price)for slot,price in candidates]
    else:
        options=[((),0)]
        for slot,price in candidates:
            options += [(slots+(slot,),cost+price)for slots,cost in options if cost+price<=cash]
    if 36 <= day <= 41:
        filtered = [((), 0)]
        for slots, cost in options[1:]:
            after = list(owned)
            for s in slots:
                after[s] = 3
            if first_arc_basket_allowed(after, owned, cash, cost, day):
                filtered.append((slots, cost))
        return filtered
    return options


def _imminent_training_income_credit(candidate, income):
    """Original production fact gate, separately adaptable in a model process.

    A model frontend may wrap this pure boundary only after validating its
    own bound public success preview. It must retain simulation provenance;
    no simulator label is accepted as a real-game observation here.
    This credit funds the next preparation, never today's purchase price.
    """
    if candidate is None or getattr(candidate, 'failure_rate', None) != 0:
        return 0
    from core.auto_trainer.larc_adaptation_strategy import training_income
    amount, basis = training_income(candidate)
    if basis == 'observed_training_preview':
        return income
    if basis == 'participant_headcount_floor' and type(amount) is int and amount > 0:
        return min(income, amount)
    return 0


def _route(now, points, original, days, utility, *, initial, initial_cost, current_income, share, branch, phases,
           allow_language_deferral=False, balance_resources=False, focused_first_cycle=False,
           same_day_purchases=False, allow_power_deferral=False, keep_first_arc_cash_route=False):
    owned = tuple(initial)
    actions = ([{'ordinal': now, 'phases': list(phases), 'cost': initial_cost,
                 'points_before': points, 'points_after': points - initial_cost}] if phases else [])
    # cash is never negative. Unfunded obligations stay unpaid, not invented.
    friendship_deadline = not balance_resources or 36 <= now <= 43
    deadline_gap = (None if now < 43 or not friendship_deadline else
                    (_cost(7, owned[7]) or (500 if owned[7] == 0 else 0)))
    states = [(points - initial_cost + current_income, owned, 0., 0., actions, deadline_gap)]
    halted = []
    indices = {day: i for i, day in enumerate(days)}
    option_count=option_max=expanded_states=pruned_owned=pruned_beam=0
    for day in range(now + 1, 67):
        updated = []
        for cash, owned, value, gap, actions, deadline_gap in states:
            owned = list(owned)
            for slot, unlock in UNLOCK_DAYS.items():
                if owned[slot] == 0 and now < unlock <= day:
                    owned[slot] = 1
            due = [slot for slot, level in enumerate(owned) if slot != 9 and level == 1
                   and mandatory_due(slot, now, defer_niel_power=allow_power_deferral) <= day]
            # Language may wait for the two permanent upgrades only on the
            # first expedition. It becomes ordinary preparation afterwards.
            if allow_language_deferral and now <= 42 and day < 64:
                due = [slot for slot in due if slot != 4]
            shortage = max(0., sum(LEVEL_TWO_COSTS[slot] for slot in due) - cash)
            for slot in due:
                price = LEVEL_TWO_COSTS[slot]
                if cash >= price:
                    cash -= price
                    owned[slot] = 2
            gap = max(gap, shortage)
            if shortage > 0:
                halted.append(((cash, tuple(owned), value, gap, actions, deadline_gap),
                    {'ordinal': day, 'unpaid_slots': [slot for slot in due if owned[slot] == 1],
                     'shortfall': round(shortage, 3)}))
                continue
            if day not in indices:
                updated.append((cash, tuple(owned), value, gap, actions, deadline_gap))
                continue
            options = _same_day_upgrades(owned,cash,day,multiple=same_day_purchases)
            if same_day_purchases:
                option_count+=len(options);option_max=max(option_max,len(options));expanded_states+=1
            for slots, price in options:
                target = list(owned)
                log = actions
                balance = cash
                for slot in slots:
                    atomic_price = _cost(slot,target[slot])
                    target[slot] = 3
                    log = log + [{'ordinal': day, 'phases': [PHASES[slot]], 'cost': atomic_price,
                                  'points_before': round(balance, 3), 'points_after': round(balance - atomic_price, 3)}]
                    balance -= atomic_price
                amount = utility(indices[day], tuple(target)) * share
                goal_gap = ((_cost(7, target[7]) or (500 if target[7] == 0 else 0))
                            if day == 43 and friendship_deadline else deadline_gap)
                updated.append((cash - price + income_at(day, branch, share), tuple(target), value + amount, gap, log, goal_gap))
        # Merge states with the same owned levels, keeping solvent/value tradeoffs.
        if not updated:
            states = []
            break
        if friendship_deadline and not focused_first_cycle and day >= 43 and any(row[5] == 0 for row in updated):
            updated = [row for row in updated if row[5] == 0]
        updated.sort(key=lambda row: row[2] - row[3] * .35, reverse=True)
        selected = [];counts = {}
        for position,row in enumerate(updated):
            key = row[1]
            if counts.get(key, 0) >= 2:
                if same_day_purchases:pruned_owned+=1
                continue
            counts[key] = counts.get(key, 0) + 1
            selected.append(row)
            if len(selected) >= BEAM:
                if same_day_purchases:pruned_beam+=len(updated)-position-1
                break
        if keep_first_arc_cash_route and day < 42:
            # Keep a cash-heavy route until first Arc preparation is paid.
            cash_route = max(updated, key=lambda row: row[0])
            if cash_route not in selected:
                if len(selected) < BEAM and counts.get(cash_route[1], 0) < 2:
                    selected.append(cash_route)
                else:
                    replace_at = next((i for i in range(len(selected) - 1, -1, -1)
                                       if selected[i][1] == cash_route[1]), len(selected) - 1)
                    selected[replace_at] = cash_route
        states = selected
    finished = [(row, None) for row in states] + halted
    best, stopped = max(finished, key=lambda pair: pair[0][2] - pair[0][3] * .35)
    result = {'actions': best[4], 'future_value': round(best[2], 4), 'unfunded_estimate': round(best[3], 3),
            'friendship_goal_shortfall': best[5] or 0, 'points_at_end_estimate': round(best[0], 3),
            'final_levels': list(best[1]), 'income_branch': branch, 'forecast_only': True,
            'current_income': current_income, 'conditional_on_continuing': True,
            **({'friendship_deadline_active': friendship_deadline} if balance_resources else {}),
            'continuation_feasible': stopped is None, 'halted_at': stopped}
    if focused_first_cycle:
        friend_day = next((a['ordinal'] for a in best[4] if 'willpower_three' in a['phases']), None)
        result.update(friendship_deadline_mode='soft_timing_comparison',
            friendship_purchase_ordinal=friend_day,
            friendship_already_owned=original[7] == 3,
            friendship_met_reference_day=original[7] == 3 or friend_day is not None and friend_day <= 43,
            friendship_unacquired_at_route_end=best[1][7] < 3,
            permanent_goal_timing_is_conditional=True)
    if same_day_purchases:
        result['future_purchase_action_space']={
            'mode':'same_date_affordable_monotone_subset_closure','optional_slot_limit':8,
            'max_subset_count_per_state':256,'maximum_observed_options':option_max,
            'enumerated_options':option_count,'expanded_date_states':expanded_states,
            'global_beam_limit':BEAM,'per_owned_state_limit':2,
            'same_day_options_prepruned':False,'same_rule_for_wait_and_after_purchase':True,
            'pruned_same_owned_states':pruned_owned,'pruned_at_global_beam':pruned_beam,
            'purchases_use_main_actions':False,'training_utility_once_per_free_date':True,
            'income_once_after_that_dates_purchases':True,'future_income_pays_same_date_purchase':False}
    return result


def _routes(now, points, levels, days, utility, *, initial, initial_cost, current_income,
            failure, share, phases, allow_language_deferral=False, balance_resources=False,
            focused_first_cycle=False,same_day_purchases=False,allow_power_deferral=False):
    probability = max(0., min(1., (failure or 0) / 100.))
    routes = []
    for branch in ('conservative', 'ordinary'):
        for success, chance in ((True, 1 - probability), (False, probability)):
            if chance <= 0:
                continue
            row = _route(now, points, levels, days, utility, initial=initial, initial_cost=initial_cost,
                         current_income=current_income if success else 0, share=share, branch=branch, phases=phases,
                         allow_language_deferral=allow_language_deferral, balance_resources=balance_resources,
                         focused_first_cycle=focused_first_cycle,same_day_purchases=same_day_purchases,
                         allow_power_deferral=allow_power_deferral)
            row.update(current_training_success=success, weight=.5 * chance)
            routes.append(row)
    return routes


def _value(routes):
    return sum(row['weight'] * (row['future_value'] - row['unfunded_estimate'] * .35) for row in routes)


def _representative(routes):
    return next((row for row in routes if row['income_branch'] == 'ordinary'
                 and row['current_training_success']), routes[-1])


def _route_deadline_shortfalls(routes, *, friendship_due):
    totals = {}
    friendship = 0.
    known_friendship = friendship_due
    for route in routes:
        halted = route.get('halted_at')
        if halted:
            key = (halted['ordinal'], 'mandatory')
            totals[key] = totals.get(key, 0.) + route['weight'] * halted['shortfall']
            known_friendship = False
        elif route.get('continuation_feasible') is not True:
            return None, None
        gap = route.get('friendship_goal_shortfall')
        if gap is None:
            known_friendship = False
        elif known_friendship:
            friendship += route['weight'] * gap
    return totals, friendship if known_friendship else None


def _route_deadline_benefit(waiting, after, *, friendship_due):
    before, before_friendship = waiting
    after, after_friendship = _route_deadline_shortfalls(after, friendship_due=friendship_due)
    if before_friendship is not None and after_friendship is not None:
        before = {**before, (43, 'friendship'): before_friendship}
        after[(43, 'friendship')] = after_friendship
    return _improves_purchase_deadline(before, after)


def _targeted_language_rows(rows, observation, now, base_rows):
    """Lv2 removes a race debuff; it does not buy the Lv3 training boost.

    A mixed Lv2 language + Lv3 Pt basket has exactly the Pt basket's current
    training gains. All cash, future utility and deadlines are then evaluated
    by the ordinary shared route comparison below.
    """
    if not is_overseas(now) or observation['levels'][4] != 1:
        return []
    additions = []
    try:
        solo = build_adaptation_plan(observation, phase='french_two')
    except ValueError:
        return []
    for action, base in base_rows.items():
        training = base.get('training_type')
        additions.append({'phase': 'french_two', 'cost': solo['cost'], 'training': training,
            'after_action_id': action, 'purchase_phases': ['french_two'],
            'score': base['score'], 'training_score_after_purchase': base['score'] if training else None,
            'action_score_after_purchase': base['score'], 'estimated_gains': {},
            'level_targets': solo['targets'],
            'assumptions': ['language Lv2 removes race debuff only; no invented training or victory bonus']})
    try:
        combo = build_adaptation_plan(observation, phase='bundle_french_two_pt')
    except ValueError:
        return additions
    for row in rows:
        if row.get('purchase_phases') != ['expedition_skill_pt']:
            continue
        additions.append({**row, 'phase': 'bundle_french_two_pt', 'cost': combo['cost'],
            'purchase_phases': ['french_two', 'expedition_skill_pt'], 'level_targets': combo['targets'],
            'assumptions': [*row.get('assumptions', []),
                'language Lv2 + Pt Lv3; no extra overseas training multiplier from language']})
    return additions


def _early_pt_candidates(eligible):
    """The Pt goal is not a free preference for an extra language purchase."""
    pt = [row for row in eligible if 'expedition_skill_pt' in row['purchase_phases']
          and 'french_two' not in row['purchase_phases']]
    for row in eligible:
        if not {'french_two', 'expedition_skill_pt'} <= set(row['purchase_phases']):
            continue
        # Compare the same subsequent action. A real marginal route benefit
        # may justify the added Lv2 cost; the Pt basket's own gain may not.
        action = row.get('after_action_id') or 'train:' + str(row.get('training'))
        standalone = [other for other in pt if other['purchase_phases'] == ['expedition_skill_pt']
                      and (other.get('after_action_id') or 'train:' + str(other.get('training'))) == action]
        if standalone and row['score'] > max(other['score'] for other in standalone):
            pt.append(row)
    return pt


def _friendship_timing(routes, already_owned=False):
    rows = []
    for route in routes:
        day = next((a['ordinal'] for a in route.get('actions', [])
                    if 'willpower_three' in a.get('phases', [])), None)
        rows.append({'scenario_weight': route.get('weight', 0),
            'income_branch': route.get('income_branch'),
            'current_training_success': route.get('current_training_success'),
            'already_owned': already_owned, 'purchase_ordinal': day,
            'met_reference_43': already_owned or day is not None and day <= 43,
            'unacquired': not already_owned and day is None,
            'missed_free_actions_after_43': (sum(43 <= d < day for d in free_action_days(43, day))
                                           if day is not None and day > 43 else None if day is None and not already_owned else 0),
            'continuation_feasible': route.get('continuation_feasible'),
            'halted_at': route.get('halted_at'), 'forecast_only': True})
    return rows


def _focused_trace(considered, plan, now, points, levels, days, waits, selected):
    """Bounded exporter-facing data, without embedding every forecast layout."""
    first_days = [d for d in free_action_days(now, 43) if 36 <= d <= 41]
    selected_key = (selected or {}).get('phase'), (selected or {}).get('after_action_id')
    ordered = sorted(enumerate(considered), key=lambda pair:
        (0 if (pair[1].get('phase'), pair[1].get('after_action_id')) == selected_key else 1, pair[0]))
    trace = []
    for _, row in ordered[:128]:
        budget = row.get('budget', {})
        trace.append({'phase': row.get('phase'), 'purchase_phases': row.get('purchase_phases', []),
            'after_action_id': row.get('after_action_id'), 'training': row.get('training'),
            'considered': True, 'selected': row is selected,
            'rejected': bool(row.get('rejected')), 'rejection_reason': row.get('v3_rejection_reason'),
            'value_improved': row.get('score', float('-inf')) > plan.get('baseline_score', float('inf')),
            'score': row.get('score'), 'current_score_after': row.get('training_score_after_purchase', row.get('action_score_after_purchase')),
            'future_utility_difference': row.get('future_utility_difference'),
            'funding_risk_difference': row.get('funding_risk_difference'),
            'current_payment': row.get('current_payment', {}),
            **({'level_targets_after': list(row['level_targets']),
                'level_targets_after_basis': 'planned_not_actual_receipt'}
               if isinstance(row.get('level_targets'), list) else {}),
            'conditional_current_action_income': row.get('conditional_current_action_income'),
            'budget': {k: budget.get(k) for k in ('deadline_total', 'affordable_at_deadline',
                'protected_goal_blocked', 'unfunded_estimate', 'penalty')},
            'imminent_preparation_required': row.get('imminent_required'),
            'permanent_goal_comparison': row.get('permanent_budget_comparison'),
            'friendship_timing_after': _friendship_timing(row.get('purchase_routes_v3', []), levels[7] == 3),
            **({'current_training_purchase_coupling': row['current_training_purchase_coupling']}
               if 'current_training_purchase_coupling' in row else {}),
            'training_windows': {'current_action_id': row.get('after_action_id'),
                'first_expedition_free_ordinals': first_days, 'remaining_full_career_action_count': len(days),
                'actual_future_training_or_rainbow_count_unknown': True}})
    action_space=None
    if 'future_purchase_action_space' in plan:
        action_space={**plan['future_purchase_action_space']}
        for label,routes in (('waiting_route_counts',waits),('selected_route_counts',(selected or{}).get('purchase_routes_v3',[]))):
            action_space[label]=[{'income_branch':r.get('income_branch'),
                'current_training_success':r.get('current_training_success'),
                **r['future_purchase_action_space']}for r in routes[:4]if isinstance(r.get('future_purchase_action_space'),dict)]
    return {'schema_version': 'larc_procurement_focus.v1', 'revision': plan.get('focused_purchase_revision','1.7.1'),
        'through_ordinal': now, 'cash_current': points, 'levels_current': list(levels), 'current_payment_only': True,
        'future_income_can_pay_current_purchase': False,
        'candidate_count': len(considered), 'included_count': len(trace), 'omitted_count': max(0, len(considered)-len(trace)),
        'limit': 128, 'baseline_score': plan.get('baseline_score'),
        'friendship_timing_before': _friendship_timing(waits, levels[7] == 3),
        'deck_upgrade_prior': plan.get('deck_upgrade_prior'),
        'income_assumptions': {'future_training': 'existing conservative/ordinary conditional priors; not actual cash',
            'future_SS_and_Mei': 'unknown; no new guaranteed income or retrospective receipts inserted',
            'scenario_weights_are_not_empirically_calibrated_probabilities': True},
        'considered': trace, 'forecast_only': True,
        **({'future_purchase_action_space': action_space}if action_space is not None else {})}


def recommend(observation, period, state, goal, legacy, *, support_profile=None, race_day=False):
    from core.auto_trainer.larc_joint_career.dispatch import enabled as career_enabled, recommend as career_recommend
    from core.auto_trainer.larc_strategy_rules import first_arc_purchase_scope
    with first_arc_purchase_scope(state, goal, period, support_profile, observation=observation):
        if career_enabled(goal):
            return career_recommend(observation, period, state, goal, legacy,
                parent=_recommend_before_career, support_profile=support_profile, race_day=race_day)
        return _recommend_before_career(observation, period, state, goal, legacy,
            support_profile=support_profile, race_day=race_day)


def _recommend_before_career(observation, period, state, goal, legacy, *, support_profile=None, race_day=False):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if unified_larc_enabled(goal):
        from core.auto_trainer.larc_joint_career.low_cash_quote import recommend as joint_recommend
        return joint_recommend(observation, period, state, goal, legacy,
            parent=_recommend_before_joint192_low, support_profile=support_profile, race_day=race_day)
    return _recommend_before_joint192_low(observation, period, state, goal, legacy,
        support_profile=support_profile, race_day=race_day)


def _recommend_before_joint192_low(observation, period, state, goal, legacy, *, support_profile=None, race_day=False, _joint=None):
    utility_model = _utility_model if _joint is None else _joint.utility
    routes_for = _routes if _joint is None else _joint.routes
    value_of = _value if _joint is None else _joint.value
    targeted_rows = _targeted_language_rows if _joint is None else _joint.targeted
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
    unified = unified_larc_enabled(goal)
    params = params_for_goal(goal)
    revised = unified
    balanced = unified
    focused = unified
    followup = unified
    if not isinstance(observation, dict) or not isinstance(period, dict) or period.get('status') != 'verified':
        return legacy
    levels, points = observation.get('levels'), observation.get('points')
    now = period_ordinal(period.get('value', {}))
    if (now is None or not isinstance(levels, list) or len(levels) != 10
            or any(type(level) is not int or not 0 <= level <= 3 for level in levels)
            or type(points) is not int or points < 0
            or observation.get('errors') or observation.get('pending_cost') != 0):
        if focused:
            return {**legacy, 'phase': None, 'cost': 0, 'purchase_phases': [],
                'reason': 'focused_purchase_observation_unusable',
                'procurement_trace': {'schema_version': 'larc_procurement_focus.v1',
                    'status': 'blocked_observation_or_pending', 'pending_cost': observation.get('pending_cost'),
                    'levels_current': list(levels) if isinstance(levels, list) and len(levels) == 10
                        and all(type(level) is int and 0 <= level <= 3 for level in levels) else None,
                    'errors': observation.get('errors'), 'considered': [], 'current_payment_only': True}}
        return legacy
    focused_first_cycle = focused and 36 <= now <= 43
    power_deferred = unified and params.arc1_defer_niel_power and 36 <= now <= 42
    power_route_kwargs = {'allow_power_deferral': True} if power_deferred else {}
    plan = _diagnostic(state, period, levels, points, balance_resources=balanced, goal=goal)
    if power_deferred and levels[2] == 1:
        plan['power_deferral'] = {'slot': 2, 'cost': 100, 'due_ordinal': 42,
                                 'deferred_through_niel': True, 'forecast_only': True}
    if followup:
        plan['future_purchase_action_space']={
            'mode':'same_date_affordable_monotone_subset_closure','max_subset_count_per_state':256,
            'global_beam_limit':BEAM,'same_rule_for_wait_and_after_purchase':True,
            'purchases_use_main_actions':False,'income_once_after_that_dates_purchases':True}
    if focused:
        plan['focused_purchase_revision'] = '1.7.2' if followup else '1.7.1'
        plan['goals']['friendship']['time_is_reference_not_current_cash_lock'] = focused_first_cycle
    result = {**legacy, 'purchase_plan': plan, 'language_debuff_deferred': plan['language_debuff_deferred'],
              'policy_kind': 'full_career_purchase_v3'}
    result['language_deferral_basis'] = ({'kind': 'permanent_upgrade_budget_competition',
        'slot': 4, 'deferred_cost': LEVEL_TWO_COSTS[4], 'points_current': points,
        'race_performance_effect': 'not_quantified; explicit_user_tradeoff',
        'other_unresolved_hazards_allowed': False,
        **({'selected_ordinal': now, 'exclusive_end_ordinal': 43,
            'scope': 'first_expedition_language_only'} if balanced else {})}
        if plan['language_debuff_deferred'] else None)
    if race_day or state is None or now > 65:
        if focused:
            result['procurement_trace'] = _focused_trace([], plan, now, points, levels,
                free_action_days(now + 1), [], None)
            result['procurement_trace']['comparison_status'] = 'no_current_discretionary_training_comparison'
        return result
    from dataclasses import replace
    run_id = _identity(state)
    if run_id:
        state = replace(state, scenario_state={**state.scenario_state, 'larc_expedition_context': {
            'status': 'accepted', 'run_session_id': run_id, 'through_turn': state.turn,
            'levels': list(levels), 'points': points, 'balance_current': True}})
    rows = [row for row in legacy.get('considered', []) if 'score' in row]
    baseline_score = legacy.get('baseline_score')
    if baseline_score is None:
        if focused:
            result['procurement_trace'] = _focused_trace([], plan, now, points, levels,
                free_action_days(now + 1), [], None)
            result['procurement_trace'].update(comparison_status='baseline_action_unavailable',
                reason=legacy.get('reason'))
        return result
    if focused:
        plan['baseline_score'] = baseline_score
    days = free_action_days(now + 1)
    deck_prior = _deck_upgrade_context(state, goal, period, support_profile)
    if deck_prior is not None:
        plan['deck_upgrade_prior'] = deck_prior
        utility, unknown = utility_model(state, goal, levels, days, deck_prior=deck_prior)
    else:
        utility, unknown = utility_model(state, goal, levels, days)
    from core.auto_trainer.larc_adaptation_strategy import _known_failure_rate, training_income
    candidates = {row.action_id: row for row in state.training_options}
    baseline_item = candidates.get(legacy.get('baseline_action'))
    local = __import__('core.auto_trainer.larc_opening', fromlist=['LarcOpeningStrategy']).LarcOpeningStrategy(goal).evaluate(state)
    base_rows = {row['action_id']: row for row in local.scored_candidates if not is_hard_rejected(row)
                 and row.get('allowed') is not False and row.get('eligible') is not False}
    from core.auto_trainer.larc_risk import strategy_v4_enabled
    if strategy_v4_enabled(state, goal) and not (balanced and plan['language_debuff_deferred']):
        rows.extend(targeted_rows(rows, observation, now, base_rows))
    base_action = base_rows.get(legacy.get('baseline_action'), {})
    base_failure = _known_failure_rate(baseline_item, base_action) if baseline_item else 0
    baseline_income = (training_income(baseline_item)[0]
                       if baseline_item and base_failure is not None and is_overseas(now) else 0)
    share_fn=canonical_training_share if unified else training_share
    share = share_fn(base_action)
    if _joint is not None:
        _joint.prepare_current(rows, base_rows, goal)
    stage_funding = _stage_funding(now, points, levels, baseline_income * (1 - (base_failure or 0) / 100.), share,
                                   focused=focused_first_cycle)
    plan['stage_funding'] = stage_funding
    from core.auto_trainer.larc_strategy_extension import active_provider
    provider = active_provider()
    wisdom_gate_ctx = provider.purchase_wisdom_gate_prepare(
        params=params,
        now=now,
        levels=levels,
        points=points,
        baseline_income=baseline_income,
        base_failure=base_failure,
        share=share,
        focused_first_cycle=focused_first_cycle,
        cost_fn=_cost,
        stage_funding_fn=_stage_funding,
    )
    waits = routes_for(now, points, levels, days, utility, initial=levels, initial_cost=0,
                    current_income=baseline_income, failure=base_failure if base_failure is not None else 100,
                    share=share, phases=[],
                    allow_language_deferral=plan['language_debuff_deferred'], balance_resources=balanced,
                    focused_first_cycle=focused_first_cycle,same_day_purchases=followup,**power_route_kwargs)
    wait_value = value_of(waits)
    from core.auto_trainer.larc_strategy_rules import first_arc_basket_allowed
    considered = []
    for row in rows:
        try:
            purchase = build_adaptation_plan(observation, phase=row['phase'])
        except ValueError as error:
            if focused:
                considered.append({'phase': row.get('phase'), 'purchase_phases': row.get('purchase_phases', []),
                    'after_action_id': row.get('after_action_id'), 'training': row.get('training'),
                    'rejected': True, 'v3_rejection_reason': 'current_purchase_plan_unavailable',
                    'v3_rejection_message': str(error),
                    'current_payment': {'cash': points, 'cost': None, 'payable_now': False,
                        'unverified_legacy_quote': row.get('cost'), 'future_income_credited_for_current_payment': 0},
                    'budget': {'model': VERSION, 'affordable_at_deadline': False,
                        'protected_goal_blocked': False}})
            continue
        if not first_arc_basket_allowed(purchase['targets'], levels, points, purchase['cost'], now):
            continue
        action = row.get('after_action_id') or 'train:' + str(row.get('training'))
        item = candidates.get(action)
        failure = _known_failure_rate(item, base_rows.get(action, {})) if item else 0
        income = training_income(item)[0] if item and failure is not None and is_overseas(now) else 0
        routes = routes_for(now, points, levels, days, utility, initial=purchase['targets'], initial_cost=purchase['cost'],
                  current_income=income, failure=failure if failure is not None else 100, share=share,
                  phases=row['purchase_phases'], allow_language_deferral=plan['language_debuff_deferred'],
                  balance_resources=balanced, focused_first_cycle=focused_first_cycle,same_day_purchases=followup,
                  **power_route_kwargs)
        future = value_of(routes) - wait_value
        current = row.get('training_score_after_purchase')
        if current is None:
            current = row.get('action_score_after_purchase', baseline_score)
        # Old v2 rejection can include its limited-window forecast. Recompute
        # only the imminent cash condition; neither model may spend tomorrow's cash today.
        due = [i for i in range(9) if purchase['targets'][i] == 1
               and mandatory_due(i, now, defer_niel_power=power_deferred) <= now + 1]
        if plan['language_debuff_deferred']:
            due = [i for i in due if i != 4]
        required = sum(LEVEL_TWO_COSTS[i] for i in due)
        known_income = _imminent_training_income_credit(item,income)
        currently_payable = 0 <= purchase['cost'] <= points
        future = future + provider.purchase_high_cash_bonus(
            params=params,
            now=now,
            currently_payable=currently_payable,
            points=points,
            cost=purchase['cost'],
            levels=levels,
            purchase_phases=row['purchase_phases'],
            training_phase_slots=TRAINING_PHASE_SLOTS,
            future=future,
            days_count=len(days),
        )
        rejected = (focused and not currently_payable) or points - purchase['cost'] + known_income < required
        after_funding = _stage_funding(now, points - purchase['cost'], purchase['targets'], income * (1 - (failure or 0) / 100.),
                                       share, focused=focused_first_cycle)
        # In 1.7 even the primary facility must not delay the first-Arc
        # permanent goals. The exception remains frozen for older revisions.
        first_main_with_pt = (False if balanced else _main_pt_exception(row, levels, revised, deck_prior))
        protected = bool(stage_funding and after_funding and not first_main_with_pt
            and any((phase in GROWTH_PHASE_SLOTS or (revised and phase == 'french_two'))
                    and phase not in {'expedition_skill_pt', 'willpower_three'}
                    for phase in row['purchase_phases'])
            and after_funding['shortfall_estimate'] > stage_funding['shortfall_estimate'] + 1e-6)
        permanent_shortfall_increased = protected
        if focused_first_cycle:
            # Same finite route utility already prices later friendship and
            # funding shortfalls. A permanent-goal forecast is not locked cash.
            protected = False
        courage_deferred = provider.purchase_courage_deferred(
            params=params,
            now=now,
            purchase_phases=row['purchase_phases'],
            action=action,
        )
        projection = {}
        if item is not None and failure is not None:
            from core.auto_trainer.larc_expedition_forecast import forecast_metadata
            from core.auto_trainer.larc_training_forecast import project_gains
            source = {**forecast_metadata(item, state), 'levels': levels, 'purchase_v3': True}
            _, projection = project_gains(state.current_stats, item.stats_gain, item.training_type,
                                          source, {**source, 'levels': purchase['targets']})
        w_gate = (
            provider.purchase_wisdom_gate_check(
                wisdom_gate_ctx,
                state=state,
                goal=goal,
                levels=levels,
                targets=purchase['targets'],
                points=points,
                cost=purchase['cost'],
                now=now,
                item=item,
                projection=projection,
                failure=failure,
                after_funding=after_funding,
            )
            if wisdom_gate_ctx is not None
            else {'active': False, 'in_band': False, 'allowed': True, 'reason': 'not_applicable'}
        )
        w_blocked = bool(w_gate.get('active') and not w_gate.get('allowed'))
        pre_w_rejected = bool(rejected or protected or courage_deferred)
        rejected = pre_w_rejected or w_blocked
        # Do not attach v2's limited-window budget/timing fields to a v3 score.
        clean = {key: value for key, value in row.items() if key not in {
            'purchase_schedule', 'purchase_timing_difference', 'future_delay_value',
            'future_delay_values_by_period', 'deck_opportunity_prior', 'budget'}}
        after_gap = sum(route['weight'] * route['unfunded_estimate'] for route in routes)
        before_gap = sum(route['weight'] * route['unfunded_estimate'] for route in waits)
        considered.append({**clean,
            **({'cost': purchase['cost'], 'level_targets': purchase['targets']} if focused else {}),
            'score': round(current + future, 4), 'rejected': rejected,
            'w_pre_rejected': pre_w_rejected, 'w_blocked': w_blocked,
            **({'w_entry_wisdom_gate': w_gate} if w_gate.get('active') else {}),
            'future_value_change': round(future, 4), 'purchase_routes_v3': routes,
            'future_utility_difference': round(sum(route['weight'] * route['future_value'] for route in routes)
                - sum(route['weight'] * route['future_value'] for route in waits), 4),
            'funding_risk_difference': round((after_gap - before_gap) * .35, 4),
            'current_cash_after': points - purchase['cost'], 'imminent_required': required,
            'stage_funding_after': after_funding,
            'gain_projection_v3': projection,
            'budget': {'model': VERSION, 'penalty': round(max(0., after_gap - before_gap) * .35, 4),
                       'unfunded_estimate': round(after_gap, 3), 'deadline_total': required,
                       'affordable_at_deadline': (not focused or currently_payable) and points - purchase['cost'] + known_income >= required,
                       'protected_goal_blocked': protected},
            **({'current_payment': {'cash': points, 'cost': purchase['cost'], 'payable_now': currently_payable,
                    'future_income_credited_for_current_payment': 0},
                'conditional_current_action_income': {'action_id': action,
                    'model_credit_after_action': income,
                    'basis': ('non_training_income_unmodeled_not_assumed' if item is None else
                              'current_failure_rate_unreadable_no_income_credit' if failure is None else
                              training_income(item)[1]),
                    'failure_rate_percent': failure if item else None,
                    'conditional_not_received_cash': True, 'usable_for_current_payment': False},
                'permanent_budget_comparison': {'mode': 'soft_timing_and_conditional_funding',
                    'old_aggregate_shortfall_increased': permanent_shortfall_increased,
                    'before': stage_funding, 'after': after_funding}} if focused else {}),
            'v3_rejection_reason': 'unaffordable_current_cash' if focused and not currently_payable else 'protected_first_arc_friendship_budget' if protected else
                'courage_deferred_on_zero_stamina_action' if courage_deferred else w_gate['reason'] if w_blocked else 'imminent_preparation_shortfall' if rejected else None,
            'v3_rejection_message': '当前代币不足，未来收入不能支付本次购买。' if focused and not currently_payable else '为首凯后的20%友情保留代币，暂缓第二项限时升级。' if protected else
                '当前行动不消耗体力，暂缓勇气Lv3升级。' if courage_deferred else '700档智力条件购未满足，先买Pt下一手重算。' if w_blocked else '购买后不足以支付临近比赛的必要费用。' if rejected else None})
    if unified:
        from core.auto_trainer.larc_purchase_training_coupling import couple_purchase_rows
        considered = couple_purchase_rows(considered, purchase_rows=rows, current_rows=base_rows, goal=goal)
        if _joint is not None:
            considered = _joint.bind_current(considered, purchase_rows=rows, current_rows=base_rows, goal=goal)
        plan['current_training_purchase_coupling'] = {
            'enabled': True, 'specific_facility_use_before_payment': True,
            'global_upgrades_exempt': True, 'normal_post_purchase_reobservation_unchanged': True,
            'deferred_candidate_count': sum(r['current_training_purchase_coupling']['active']
                and not r['current_training_purchase_coupling']['allowed'] for r in considered)}
    funded = [row for row in considered if not row['rejected']]
    eligible = []
    friendship_due = 41 <= now <= 43 and levels[7] in (1, 2)
    waiting_deadlines = _route_deadline_shortfalls(waits, friendship_due=friendship_due)
    for row in funded:
        row['deadline_benefit'] = _route_deadline_benefit(
            waiting_deadlines, row['purchase_routes_v3'], friendship_due=friendship_due)
        if _purchase_beats_wait(row, baseline_score):
            eligible.append(row)
    # Early permanent Pt is an explicit purchase goal, not a fabricated score
    # bonus. Protect it from another temporary facility when currently payable
    # and the ordinary remaining-career comparison has positive utility.
    if 36 <= now <= 39 and levels[5] in (1, 2):
        route_priority = unified and not params_for_goal(goal).protect_early_pt50_on_fallback
        pt = (eligible if route_priority else
              _early_pt_candidates(eligible) if revised else
              [row for row in eligible if 'expedition_skill_pt' in row['purchase_phases']])
        if pt:
            eligible = pt
            plan['goals']['skill_pt']['selection_priority'] = (
                'finite_career_route_comparison_no_hard_early_Pt3_filter' if route_priority
                else 'first_expedition_permanent_upgrade')
        elif points < _cost(5, levels[5]):
            eligible = [row for row in eligible if 'expedition_skill_pt' in row['purchase_phases']]
            plan['goals']['skill_pt']['status'] = 'unfunded'
    # Once unlocked after the prep race, compare early friendship first. At
    # the first domestic action it is the user's required budget milestone.
    # For post-Arc1 domestic turns (44 <= now < 60), buy affordable funded
    # willpower_three immediately rather than tying wait indefinitely.
    if 41 <= now <= 43 and levels[7] in (1, 2):
        due_friend = [row for row in funded if 'willpower_three' in row['purchase_phases']
                      and (_purchase_beats_wait(row, baseline_score)
                           or abs(row['score'] - baseline_score) <= SCORE_TIE_EPSILON
                           or ((balanced or now >= 43) and row['score'] < baseline_score - SCORE_TIE_EPSILON))]
        if due_friend:
            eligible = due_friend
    elif 44 <= now < 60 and levels[7] in (1, 2):
        due_friend = [row for row in funded if 'willpower_three' in row['purchase_phases']
                      and (provider.purchase_bugfix_tie_enabled(params) or row['score'] >= baseline_score - SCORE_TIE_EPSILON)
                      and float((row.get('budget') or {}).get('unfunded_estimate') or 0.0) <= 1e-6]
        if due_friend:
            eligible = due_friend
    result['considered'] = considered
    plan['unknown_fields'] = unknown + ['future_income', 'future_attendance', 'future_race_results']
    plan['waiting_routes'] = waits
    best = _best_purchase(eligible) if eligible else None
    if wisdom_gate_ctx is not None:
        provider.purchase_wisdom_gate_finalize(
            wisdom_gate_ctx,
            considered=considered,
            best=best,
            plan=plan,
            result=result,
            unified=unified,
            params=params,
            now=now,
            levels=levels,
            points=points,
            baseline_score=baseline_score,
            revised=revised,
            cost_fn=_cost,
            purchase_beats_wait_fn=_purchase_beats_wait,
            early_pt_candidates_fn=_early_pt_candidates,
            best_purchase_fn=_best_purchase,
        )
    if not eligible:
        result.update(phase=None, cost=0, training=None, purchase_phases=[], points_after_purchase=points,
                      reason='v3_no_funded_purchase_improvement', budget={'model': VERSION, 'purchase_selected': False})
        plan['selected_route'] = _representative(waits)
        if focused:
            result['procurement_trace'] = _focused_trace(considered, plan, now, points, levels, days, waits, None)
        return result
    if 'french_two' in best['purchase_phases']:
        plan['language_debuff_deferred'] = False
        result['language_debuff_deferred'] = False
        result['language_deferral_basis'] = None
    result.update(phase=best['phase'], cost=best['cost'], training=best.get('training'),
                  purchase_phases=best['purchase_phases'], points_after_purchase=points-best['cost'],
                  score=best['score'], reason='v3_full_career_route_preferred',
                  after_action_id=best.get('after_action_id') or 'train:' + str(best.get('training')),
                  budget=best['budget'], reserved_race_points=best['imminent_required'])
    plan['selected_route'] = _representative(best['purchase_routes_v3'])
    if 'current_training_purchase_coupling' in best:
        result['current_training_purchase_coupling'] = best['current_training_purchase_coupling']
    if 'willpower_three' in best['purchase_phases']:
        plan['goals']['friendship']['status'] = 'planned'
    if focused:
        result['procurement_trace'] = _focused_trace(considered, plan, now, points, levels, days, waits, best)
    return result
