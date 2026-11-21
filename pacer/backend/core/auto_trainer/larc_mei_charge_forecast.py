"""Explicit model-only nonfull/current-training-first Mei charge forecast.

The packet supplies current public batteries, not future layouts. This branch
is not a CN mechanism claim; absent or unbound packets retain legacy values.
"""
from math import comb, isfinite

KEYS = ('source_kind', 'run_session_id', 'turn_index', 'observation_id', 'step_count')
MEMBERS = frozenset('sim_person_' + str(i) for i in range(15))


def _number(x):
    return type(x) in (int, float) and isfinite(x)


def prearc_model_charge_scope(state, goal):
    """Ordinary classic January-June Mei charge packet scope.

    Packet production may reuse this pure scope. The consumer still binds the
    full current packet below; this helper alone never makes an input valid.
    """
    from core.auto_trainer.larc_calendar import free_action_days, period_ordinal
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if not unified_larc_enabled(goal):
        return False
    context=getattr(state,'scenario_state',{})or{}
    period=context.get('larc_period')or{};value=period.get('value')or{}
    if (getattr(state,'screen',None)!='training_menu'or context.get('scenario')!='larc'
            or period.get('status')!='verified'or type(value.get('year_index'))is not int
            or value['year_index']!=2 or value.get('stage')!='classic'
            or type(value.get('month'))is not int or not 1<=value['month']<=6
            or value.get('half')not in{'first','second'}):
        return False
    day=period_ordinal(value)
    return day is not None and 24<=day<=35 and bool(free_action_days(day,day+1))


def forecast_branches(after_base, current_training_ids, probability, maximum_members=5):
    """Exact priority plus hypergeometric conditional recipient distribution."""
    if (set(after_base) != MEMBERS or any(type(v) is not int or not 0 <= v <= 3 for v in after_base.values())
            or not _number(probability) or not 0 <= probability <= 1
            or type(maximum_members) is not int or maximum_members != 5):
        return None
    current = set(current_training_ids)
    if not current <= MEMBERS:
        return None
    nonfull = {i for i, value in after_base.items() if value < 3}
    priority = current & nonfull
    if len(priority) > maximum_members:
        return None
    pool = nonfull - priority
    draws = min(maximum_members - len(priority), len(pool))
    mandatory_full = sum(after_base[i] == 2 for i in priority)
    one_short = sum(after_base[i] == 2 for i in pool)
    denominator = comb(len(pool), draws)
    cells = len(priority) + draws
    ready = sum(v == 3 for v in after_base.values())
    branches = []
    for hits in range(max(0, draws - (len(pool) - one_short)), min(draws, one_short) + 1):
        conditional = comb(one_short, hits) * comb(len(pool) - one_short, draws - hits) / denominator
        full = mandatory_full + hits
        branches.append({'conditional_probability': conditional, 'probability': probability * conditional,
            'random_new_full_members': hits, 'priority_new_full_members': mandatory_full,
            'additional_full_members': full, 'effective_cells_if_triggered': cells,
            'ready_after_proc': min(5, ready + full)})
    return {'ready_after_base_exact': ready, 'nonfull_after_base': len(nonfull),
        'priority_member_ids': sorted(priority), 'priority_member_count': len(priority),
        'random_pool_count': len(pool), 'random_one_short_count': one_short, 'random_draw_count': draws,
        'members_drawn_if_triggered': cells, 'expected_extra_cells': probability * cells,
        'expected_extra_full_members': sum(b['probability'] * b['additional_full_members'] for b in branches),
        'completes_five_probability_estimate': sum(b['probability'] for b in branches
            if ready < 5 <= ready + b['additional_full_members']),
        'no_trigger_probability': 1 - probability, 'conditional_branches': branches}


def _current_forecast(state, candidate, source, goal, probability):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    enabled = unified_larc_enabled(goal)
    prearc = prearc_model_charge_scope(state, goal)
    if not enabled and not prearc:
        return None
    context = getattr(state, 'scenario_state', {}) or {}
    period = context.get('larc_period') or {}
    value = period.get('value') or {}
    if (getattr(state, 'screen', None) != 'training_menu' or period.get('status') != 'verified'
            or type(value.get('year_index')) is not int or value['year_index'] != 1 and not prearc):
        return None
    packet = context.get('larc_mei_charge_rule') or {}
    if (packet.get('schema_version') != 'larc_mei_charge_rule.v1' or packet.get('status') != 'accepted'
            or packet.get('selection_rule') != 'nonfull_current_training_first'
            or packet.get('model_rule_cn_verified') is not False
            or type(packet.get('maximum_members')) is not int or packet['maximum_members'] != 5
            or not _number(probability) or probability <= 0
            or not _number(packet.get('trigger_probability')) or packet['trigger_probability'] != .4
            or packet['trigger_probability'] != probability):
        return None
    bound = packet.get('source')
    if (not isinstance(source, dict) or not isinstance(bound, dict) or source.get('source_kind') != 'simulation'
            or any(bound.get(k) != source.get(k) for k in KEYS)
            or not isinstance(source.get('run_session_id'), str) or not source['run_session_id']
            or type(getattr(state,'turn',None)) is not int
            or type(source.get('turn_index')) is not int or source['turn_index'] != state.turn
            or type(source.get('step_count')) is not int or source['step_count'] < 0
            or not isinstance(source.get('observation_id'), str) or not source['observation_id']):
        return None
    rows = packet.get('members')
    if not isinstance(rows, list) or len(rows) != 15:
        return None
    batteries = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('member_id'),str) or row['member_id'] not in MEMBERS
                or row['member_id'] in batteries or type(row.get('filled_cells')) is not int
                or not 0 <= row['filled_cells'] <= 3):
            return None
        batteries[row['member_id']] = row['filled_cells']
    if set(batteries) != MEMBERS:
        return None
    preview = ((getattr(candidate, 'scenario_mechanics', {}) or {}).get('larc') or {}).get('training_participants') or {}
    if (preview.get('schema_version') != 'larc_training_participants.v1' or preview.get('status') != 'accepted'
            or preview.get('training_type') != candidate.training_type or not isinstance(preview.get('participants'), list)):
        return None
    if not any((p.get('identity')or{}).get('status')=='known'
            and(p.get('identity')or{}).get('character_id')=='satake_mei'
            and(p.get('role')or{}).get('status')=='accepted'
            and(p.get('role')or{}).get('kind')=='friend'for p in preview['participants']):
        return None
    if isinstance(preview.get('source'), dict) and any(preview['source'].get(k) != source.get(k) for k in KEYS):
        return None
    after = dict(batteries); current = set()
    for person in preview['participants']:
        ref = person.get('member_ref') or {}; member = ref.get('member_id')
        charge = person.get('charge') or {}
        if member not in MEMBERS:
            if charge.get('status') == 'accepted' and charge.get('applicable') is not False:
                return None
            continue
        if (member in current or ref.get('status') != 'accepted' or ref.get('run_session_id') != source['run_session_id']
                or charge.get('status') != 'accepted' or charge.get('capacity') != 3
                or type(charge.get('filled_cells')) is not int or charge['filled_cells'] != batteries[member]
                or type(charge.get('preview_cells')) is not int or not 0 <= charge['preview_cells'] <= 3 - batteries[member]):
            return None
        current.add(member); after[member] += charge['preview_cells']
    forecast = forecast_branches(after, current, probability, 5)
    if forecast is not None:
        forecast.update(source=dict(bound), selection_rule=packet['selection_rule'])
    return forecast


def model_rule_value(state, candidate, source, weights, result, *, goal=None,
        probability=.4, cycle_model=False, base_progress=None, base_actions=3):
    """Replace only the conditional marginal charge estimate in its new arm."""
    # The original caller has already identified Mei and, for cycle_model,
    # returned its separate first-contact/unknown branch before reaching here.
    prearc=prearc_model_charge_scope(state,goal)
    if not cycle_model and not prearc:
        return None
    forecast = _current_forecast(state, candidate, source, goal, probability)
    if forecast is None:
        return None
    if prearc:
        # The first-year caller's contact check lives behind cycle_model.
        # Do not turn on its other startup/cycle behaviour in year2 merely to
        # reach this optional bound model packet. Validate this branch itself.
        from core.auto_trainer.larc_mei_cycle import contact_stage
        stage=contact_stage(state,source)
        if stage!='subsequent':
            return {**result,'score':0.,'expected_extra_cells':0.,'expected_extra_full_members':0.,
                'reason':'first_contact_has_separate_event'if stage=='first_contact_unconfirmed'else'mei_contact_stage_unknown',
                'contact_stage':stage,'basis':'bound_model_rule_requires_confirmed_subsequent_contact',
                'certainty':'not_priced','conditional_model_proc_priced':False,
                'selection_rule':forecast['selection_rule'],'model_rule_cn_verified':False}
    from core.auto_trainer.larc_ss_opportunity import ss_redemption
    ready = min(5, forecast['ready_after_base_exact'])
    # If ordinary base charge already completes five, its own route is2;
    # the proc must not claim acceleration that the base action already earned.
    base_actions = 2 if ready>=5 else max(2, base_actions)
    before = ss_redemption(state, earliest_actions=base_actions)
    ordinary = {k: (base_progress or {}).get(k, 0.) for k in ('net_charge', 'newly_full', 'ss_preparation')}
    ordinary_value = sum(ordinary[k] * before['value_factors'][k] for k in ordinary)
    total = 0.; branches = []
    for branch in forecast['conditional_branches']:
        full = branch['additional_full_members']; cells = branch['effective_cells_if_triggered']
        missing = max(0, 5 - ready - full)
        actions = min(base_actions, max(2 if full else 3, 2 + (missing + 1) // 2))
        redemption = ss_redemption(state, earliest_actions=actions)
        later = ss_redemption(state, ss_steps=2, earliest_actions=max(4, actions + 2))
        immediate = min(full, max(0, 5 - ready))
        full_score = (immediate * weights['newly_full'] * redemption['value_factors']['newly_full']
            + .25 * (full - immediate) * weights['newly_full'] * later['value_factors']['newly_full'])
        prep = weights['ss_preparation'] if ready < 5 <= ready + full else 0.
        cells_now = immediate if ready + full >= 5 else cells
        cell_score = weights['net_charge'] * (cells_now * redemption['value_factors']['net_charge']
            + (cells - cells_now) * later['value_factors']['net_charge'])
        accelerated = max(0., sum(ordinary[k] * redemption['value_factors'][k] for k in ordinary) - ordinary_value)
        marginal = cell_score + full_score + prep * redemption['value_factors']['ss_preparation'] + accelerated
        total += branch['probability'] * marginal
        branches.append({**branch, 'redemption': redemption, 'current_cycle_cells': cells_now,
            'later_cycle_cells': cells - cells_now, 'additional_full_credit': round(full_score, 6),
            'additional_preparation_credit': round(prep * redemption['value_factors']['ss_preparation'], 6),
            'ordinary_progress_acceleration_credit': round(accelerated, 6),
            'marginal_value_if_branch': round(marginal, 6)})
    return {**result, 'score': round(total, 4), 'basis': 'bound_model_nonfull_current_training_first',
        'certainty': 'conditional_model_rule_forecast_not_CN_measurement',
        'reason': 'conditional_mei_current_first_progress', 'selection_rule': forecast['selection_rule'],
        'model_rule_cn_verified': False, 'conditional_on_training_success': True,
        'expected_extra_cells': round(forecast['expected_extra_cells'], 6),
        'expected_extra_full_members': round(forecast['expected_extra_full_members'], 6),
        'trigger_probability': probability, 'trigger_probability_basis': 'bound_model_rule_packet',
        'members_drawn_if_triggered': forecast['members_drawn_if_triggered'], 'observed_members': 15,
        'eligible_observed_members': forecast['nonfull_after_base'], 'member_pool_estimate': 15,
        'priority_member_ids': forecast['priority_member_ids'], 'priority_member_count': forecast['priority_member_count'],
        'random_pool_count': forecast['random_pool_count'], 'random_draw_count': forecast['random_draw_count'],
        'ready_after_base_exact': forecast['ready_after_base_exact'], 'ready_after_base_lower_bound': ready,
        'readiness_basis': 'complete_bound_current_public15_after_actual_base_preview',
        'completes_five_probability_estimate': round(forecast['completes_five_probability_estimate'], 8),
        'no_trigger_probability': forecast['no_trigger_probability'],
        'no_trigger_branch': {'probability': forecast['no_trigger_probability'], 'additional_full_members': 0,
            'effective_cells': 0, 'ordinary_progress_acceleration_credit': 0., 'marginal_value_if_branch': 0.},
        'base_redemption': before, 'redemption': before, 'conditional_branches': branches,
        'base_progress_recounted': False, 'current_ss_reward_credit': 0., 'current_vitality_credit': 0.}
