"""Late first-expedition spending preference with a separate friendship forecast."""

from core.auto_trainer.larc_adaptation_schedule import income_at, obligations
from core.auto_trainer.larc_calendar import is_overseas
from .engine import number


FRIENDSHIP_DAY = 43
FIRST_ARC_LAST_TRAINING_DAY = 41
REST_REFERENCE_AP = 100
# Conditional on completing each scheduled race. The pinned L'Arc reference
# credits 50 at Niel and 50 at the first Arc without assuming first place.
# These are future planning inputs, never wallet or same-day payment credit.
RACE_COMPLETION_AP = {40: 50, 42: 50}


def _budget(row, now, cash, share, defer_language, current_income, *, rest_next=False,
            defer_niel_power=False, arc1_race_ap=None):
    """Reserve mandatory Lv2 and friendship Lv3 before any more optional buys."""
    levels = row['targets']
    debts = obligations(levels, now, skip_language=defer_language,defer_niel_power=defer_niel_power)
    cash_after_purchase = cash - row['cost']
    projected = cash_after_purchase + current_income
    friendship_cost = 0 if levels[7] == 3 else (300 if levels[7] == 2 else 500)
    preparation = 0
    race_income = 0
    for day in range(now + 1, FRIENDSHIP_DAY):
        due = sum(cost for _, deadline, cost in debts if deadline == day)
        preparation += due
        if projected < due:
            return {'preparation_shortfall': due - projected,
                    'friendship_cash': None, 'friendship_cost': friendship_cost}
        projected -= due
        if day in RACE_COMPLETION_AP:
            amount = float(arc1_race_ap) if (day == 42 and arc1_race_ap is not None) else RACE_COMPLETION_AP[day]
            race_income += amount
        elif day == FIRST_ARC_LAST_TRAINING_DAY and rest_next:
            amount = REST_REFERENCE_AP
        elif is_overseas(day):
            # Overseas rest yields a fixed 100 AP; conservative overseas training floor is 80 AP.
            clamped_share = min(1.0, max(0.0, float(share)))
            amount = 80.0 * clamped_share + REST_REFERENCE_AP * (1.0 - clamped_share)
        else:
            amount = income_at(day, 'conservative', share)
        projected += amount
    return {'preparation_shortfall': 0, 'friendship_cash': projected,
            'friendship_cost': friendship_cost, 'preparation_cost': preparation,
            'fixed_race_reference_income': race_income, 'cash_after_purchase': cash_after_purchase,
            'conditional_current_income': current_income}


def _next_action_evidence(row):
    horizon = (row.get('planning') or {}).get('energy_horizon') or {}
    route = horizon.get('future_route_after')
    next_action = route[0] if horizon.get('active') is True and isinstance(route, list) and route else None
    hp = horizon.get('after_vitality_percent')
    return {'planned_action': next_action if isinstance(next_action, str) else None,
            'post_action_vitality_percent': hp if number(hp) and 0 <= hp <= 100 else None,
            'basis': 'same_current_action_energy_horizon'}


def _funded(success, failure):
    return (success['friendship_cash'] is not None
            and success['friendship_cash'] >= success['friendship_cost']
            and not failure['preparation_shortfall'])


def apply_reserve(result, *, now, cash, share, defer_language, current_rows=(), defer_niel_power=False, state=None, goal=None):
    """Keep the current action pool and prefer funded purchases at the first Arc boundary."""
    if now not in (39, 41):
        return
    ranking = result['ranking']
    original = result['selected']
    current = {row['action_id']: row for row in current_rows}
    budgets = {}
    rest_budgets = {}
    arc1_branches = {}
    from core.auto_trainer.larc_strategy_extension import active_provider

    provider = active_provider()
    use_arc1_branch = False
    prob_weighted = False
    if goal is not None:
        from core.auto_trainer.larc_strategy_params import params_for_goal
        p_goal = params_for_goal(goal)
        use_arc1_branch, prob_weighted = provider.first_arc_race_ap_config(p_goal)

    def _branch_for(row):
        key = row['offer_id']
        if key not in arc1_branches and use_arc1_branch:
            arc1_branches[key] = provider.first_arc_win_ap_expectation(state, goal, row['targets'])
        return arc1_branches.get(key)

    def calculate(row, *, rest_next=False, race_ap=None):
        if race_ap is None and use_arc1_branch:
            b = _branch_for(row)
            race_ap = b['expected_ap'] if prob_weighted else b['credit_ap_for_reserve']
        success = _budget(row, now, cash, share, defer_language, row['current_income']['amount'],
                          rest_next=rest_next, defer_niel_power=defer_niel_power, arc1_race_ap=race_ap)
        # If a legal nonzero-risk course fails, future income from that course
        # is zero. Preserve the forecast for mandatory race preparations.
        failure = (_budget(row, now, cash, share, defer_language, 0, rest_next=rest_next,
                           defer_niel_power=defer_niel_power, arc1_race_ap=race_ap)
                   if row['current_risk'] else success)
        return success, failure

    for row in ranking:
        budgets[row['offer_id']] = calculate(row)

    speed_pair = {tuple(row['phases']): row for row in ranking
                  if row['action_id'] == original['action_id'] == 'train:speed'
                  and row['phases'] in ([], ['speed_expedition'])}
    paid_speed = speed_pair.get(('speed_expedition',))
    speed_evidence = _next_action_evidence(current.get('train:speed', {}))
    can_use_rest = (now == 39 and paid_speed is not None and () in speed_pair
                    and speed_evidence['planned_action'] == 'rest')

    def rest_budget(row):
        key = row['offer_id']
        if key not in rest_budgets:
            rest_budgets[key] = calculate(row, rest_next=True)
        return rest_budgets[key]

    def funding_basis(row):
        success, failure = budgets[row['offer_id']]
        if _funded(success, failure):
            branch = _branch_for(row)
            if branch:
                credit = branch.get('expected_ap') if prob_weighted else branch.get('credit_ap_for_reserve')
                if credit and credit > 50:
                    std_s, std_f = calculate(row, race_ap=50)
                    if not _funded(std_s, std_f):
                        return 'cn_arc1_prob_weighted_branch' if prob_weighted else 'cn_arc1_win80_branch'
            return 'conservative'
        if can_use_rest and row is paid_speed and _funded(rest_budget(row)[0], failure):
            return 'conditional_planned_rest'
        return None

    best = original
    reason = None
    if original['cost'] and funding_basis(original) is None:
        best = next((row for row in ranking if funding_basis(row) is not None), original)
        if best is not original:
            reason = 'first_arc_friendship_reserve'

    if best is speed_pair.get(()) and paid_speed is not None:
        # Retain the course that the full-career comparison already chose.
        # With the first Arc budget covered, take the same-course gain now.
        if paid_speed['direct_score'] > best['direct_score'] and funding_basis(paid_speed) is not None:
            best, reason = paid_speed, 'funded_same_course_speed_upgrade'

    if use_arc1_branch:
        result['arc1_ap_branch'] = _branch_for(best)

    if best is original and not (paid_speed is not None and () in speed_pair):
        return
    success, failure = budgets[best['offer_id']]
    if best is not original:
        result['selected'] = best
        result['ranking'] = [best, *(row for row in ranking if row is not best)]
        result['selection_reason'] = reason
    compared = {}
    for row in (original, best, *speed_pair.values()):
        key = row['offer_id']
        if key in compared:
            continue
        conservative_success, conservative_failure = budgets[key]
        comparison = {'conservative': {'success': conservative_success, 'failure': conservative_failure},
                      'funding_basis': funding_basis(row)}
        if key in arc1_branches:
            comparison['arc1_ap_branch'] = arc1_branches[key]
        if now == 39:
            rest_success, rest_failure = rest_budget(row)
            comparison['if_next_free_action_is_rest'] = {
                **_next_action_evidence(current.get(row['action_id'], {})),
                'forecast_only': True, 'rest_reference_ap': REST_REFERENCE_AP,
                'may_inform_selection': can_use_rest and row is paid_speed,
                'success': rest_success, 'failure': rest_failure}
        compared[key] = comparison
    result['first_arc_reserve'] = {
        'reason': reason or 'same_course_speed_budget_compared',
        'deadline_ordinal': FRIENDSHIP_DAY, 'forecast_only': True,
        'original_offer_id': original['offer_id'], 'selected_offer_id': best['offer_id'],
        'original_career_score': original['score'], 'selected_career_score': best['score'],
        'success': success, 'failure': failure, 'language_deferred': defer_language,
        'selected_funding_basis': compared[best['offer_id']]['funding_basis'],
        **({'arc1_ap_branch': arc1_branches[best['offer_id']]} if best['offer_id'] in arc1_branches else {}),
        'compared_budgets': compared,
    }
