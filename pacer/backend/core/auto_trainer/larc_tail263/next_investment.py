"""Two bounded alternatives for a facility deferred by current-use ranking."""
from dataclasses import replace


def enabled(goal, current):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled

    return (current.calendar in (60, 61, 62) and bool(current.future_free_days)
            and current.future_free_days[0] == current.calendar + 1 and unified_larc_enabled(goal))


def compare(prepared, basket_results, original_best, soft_actions, *, state, owned, cash,
            expectation, records, annotations, scenario, incomes, K, F, S, max_ops,
            optional_next_facility=False, excluded_future_tabs=frozenset()):
    """Return extra proposals without changing the original paid baskets or scores."""
    from core.auto_trainer.larc_adaptation import TRAINING_PHASE_SLOTS, TRAINING_UPGRADES

    def single_facility(spec):
        phases = spec.get('purchase_phases') or []
        if len(phases) != 1 or phases[0] not in TRAINING_PHASE_SLOTS:
            return False
        changed = [slot for slot, target in enumerate(spec['targets']) if target != owned[slot]]
        return changed == [TRAINING_PHASE_SLOTS[phases[0]]]

    excluded_slots = {TRAINING_UPGRADES[tab][1] for tab in excluded_future_tabs if tab in TRAINING_UPGRADES}
    deferred = []
    for basket in basket_results:
        spec = basket['basket']
        use = basket.get('current_facility_use') or {}
        if (single_facility(spec)
                and TRAINING_PHASE_SLOTS[spec['purchase_phases'][0]] not in excluded_slots
                and use.get('deferred_until_current_use') is True
                and use['matching_score'] > original_best['winner']['score'] + 1e-7):
            deferred.append((spec, use))
    if not deferred:
        return [], None
    deferred.sort(key=lambda item: (-item[1]['matching_score'], item[0]['cost'], item[0]['basket_id']))
    future, use = deferred[0]
    by_id = {spec['basket_id']: (spec, quoted, legacy) for spec, quoted, legacy, _ in prepared}
    original_actions = {row['basket']['basket_id']: max(row['raw_ranking'], key=lambda value: value['score'])['action_id']
                        for row in basket_results if row.get('raw_ranking')}
    entries = [(by_id['empty'], original_actions['empty'])]
    tab = use['best_paid_action'].removeprefix('train:')
    if tab in TRAINING_UPGRADES:
        phase, slot = TRAINING_UPGRADES[tab]
        if (phase in by_id and owned[slot] < 3 and single_facility(by_id[phase][0])
                and original_actions.get(phase) == use['best_paid_action']):
            entries.append((by_id[phase], use['best_paid_action']))

    diag = {'status': 'evaluated', 'trigger_basket': future['basket_id'],
            'trigger_matching_score': use['matching_score'], 'original_best_score': original_best['winner']['score'],
            'attempts': []}
    proposals = []
    try:
        for (spec, quoted, held_legacy), current_action in entries:
            attempt = {'current_basket': spec['basket_id'], 'original_action': current_action,
                       'kernel_calls': 0, 'cash_calls': 0,
                       'kernel_operations': 0, 'cash_operations': 0}
            diag['attempts'].append(attempt)
            proposal = _score_plan(spec, quoted, current_action, future, attempt, soft_actions, state=state, owned=owned,
                cash=cash, expectation=expectation, records=records, annotations=annotations, scenario=scenario,
                incomes=incomes, K=K, F=F, S=S, max_ops=max_ops, held_legacy=held_legacy,
                optional_next_facility=optional_next_facility)
            if proposal is not None:
                proposals.append(proposal)
    except Exception as exc:
        attempt.update(status='fallback', reason=str(exc) or type(exc).__name__)
        diag.update(status='fallback', reason=attempt['reason'])
        proposals = []
    diag['extra_operations'] = sum(row['kernel_operations'] + row['cash_operations'] for row in diag['attempts'])
    return proposals, diag


def _score_plan(spec, quoted, original_action, future, attempt, soft_actions, *, state, owned, cash,
                expectation, records, annotations, scenario, incomes, K, F, S, max_ops,
                held_legacy=None, optional_next_facility=False):
    from core.auto_trainer.larc_adaptation import TRAINING_PHASE_SLOTS, TRAINING_UPGRADES, selected_upgrade_cost
    from .consumer import _menus, discount_soft_ranking
    from .strategy182 import rank_pool

    total = spec['cost'] + future['cost']
    attempt['total_committed_from_current_cash'] = total
    if total > cash:
        attempt.update(status='skipped', reason='combined_commitment_exceeds_current_wallet')
        return None
    phase = future['purchase_phases'][0]
    slot = TRAINING_PHASE_SLOTS[phase]
    joined = list(spec['targets'])
    joined[slot] = future['targets'][slot]
    if selected_upgrade_cost(owned, joined) != total:
        raise ValueError('combined_commitment_price_mismatch')
    next_day = quoted.future_free_days[0]
    menus, _ = _menus(records, annotations, state, joined, expectation, scenario['capacity_points'],
                      {**scenario, 'calendar': next_day}, K)
    menu_sources = [(menu.source_id, menu.source_calendar) for menu in quoted.menus]
    if [(menu.source_id, menu.source_calendar) for menu in menus] != menu_sources:
        raise ValueError('deferred_investment_changed_history_sources')
    proposal_basket = {**spec, 'basket_id': f"{spec['basket_id']}+next{slot}"}
    optional = {}
    if optional_next_facility:
        tab = next(tab for tab, (_, index) in TRAINING_UPGRADES.items() if index == slot)
        optional['deferred_facility'] = {'slot': slot, 'cost': future['cost'],
            'matching_action_id': 'future:' + tab, 'menus': tuple(menus), 'levels': joined}
        future_quote, price_spec, legacy = quoted, proposal_basket, held_legacy
    else:
        future_quote = replace(quoted, menus=menus)
        price_spec = {**proposal_basket, 'targets': joined, 'cost': total}
        attempt['kernel_calls'] = 1
        legacy = K.rank(future_quote, max_ops=max_ops)
        attempt['kernel_operations'] = legacy.get('operations') or 0
    if not legacy.get('active'):
        raise ValueError(legacy.get('reason') or 'deferred_investment_kernel_unavailable')

    attempt['cash_calls'] = 1
    ranked, cash_result = rank_pool([(price_spec, future_quote, legacy, [])], state, owned, cash,
                                   records, incomes, scenario, K, S, max_ops, **optional)
    if not cash_result.get('active'):
        raise ValueError(cash_result.get('reason') or 'deferred_investment_cash_unavailable')
    cash_info = cash_result['basket_diagnostics'][0]
    cash_detail = cash_info['cash_continuation']
    attempt['cash_operations'] = cash_detail.get('operations') or 0
    if cash_detail.get('candidate_cost_deducted_once') != price_spec['cost']:
        raise ValueError('deferred_investment_commitment_not_deducted_once')
    balance_basis = ('today_cash_only_future_facility_paid_at_first_use' if optional_next_facility
                     else 'uncommitted_cash_excludes_earmarked_future_facility_cost')
    ranking = [{**row, 'cash_outcome_balance_basis': balance_basis}
               for row in discount_soft_ranking(ranked[0]['ranking'], soft_actions)]
    winner = max(ranking, key=lambda row: row['score'])
    if winner['action_id'] != original_action:
        attempt.update(status='skipped', reason='reserved_plan_changes_current_action',
                       original_action=original_action, reserved_plan_best_action=winner['action_id'])
        return None
    paid = winner.get('deferred_facility')
    if optional_next_facility:
        attempt['conditional_payment'] = paid
        if not paid or paid.get('paid_probability', 0.) <= 0:
            attempt.update(status='skipped', reason='deferred_facility_unused_in_projected_next_menus')
            return None

    action_id = winner['action_id']
    action = next(action for action in quoted.actions if action.action_id == action_id)
    route = (winner.get('representative_not_forecast') or {}).get('route', [])
    share = sum(row['action'].startswith('future:') and row['action'] != 'future:rest' for row in route) / len(route) if route else 0.
    if optional_next_facility:
        share = paid['paid_path_training_share']
    income = cash_info['current_income_quotes'][action_id]['conditional_amount']
    reserve = F.reserve_for_action(quoted.calendar, joined, income, action.p_percent, share,
                                  fixed_race_incomes=None, cash_before=cash, basket_cost=total)
    if reserve.get('conditional_forecast_funded') is not True:
        attempt.update(status='skipped', reason='combined_commitment_does_not_preserve_reserve')
        return None
    plan = {'status': 'conditional_reserved_investment', 'current_basket': spec['basket_id'],
            'current_purchase_cost': spec['cost'], 'current_action_id': action_id, 'future_calendar': next_day,
            'future_phase': phase, 'reserved_future_cost': future['cost'], 'total_committed_from_current_cash': total,
            'current_cash_after_only_current_purchase': cash - spec['cost'], 'uncommitted_cash': cash - total,
            'levels_for_current_action': list(spec['targets']),
            'projected_levels_from_next_free_action': joined, 'required_reserve': reserve['required_post_basket_reserve'],
            'extra_operations': attempt['kernel_operations'] + attempt['cash_operations'],
            'commitment_deducted_once': True, 'execution_authorized': False}
    if optional_next_facility:
        plan.update(timing_basis='first_matching_use_in_next_two_free_actions',
                    possible_payment_days=list(quoted.future_free_days[:2]),
                    conditional_payment=paid, reserved_future_cost_is_not_paid=True)
    attempt.update(status='eligible', score=winner['score'], future_facility_plan=plan)
    return {'basket': proposal_basket, 'winner': {**winner, 'future_facility_plan': plan},
            'forecast': {'basket': proposal_basket, 'raw_ranking': ranking, 'active': True,
                         'future_facility_plan': plan, 'operations': plan['extra_operations']}}
