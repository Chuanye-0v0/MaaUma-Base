"""Production182 cash adapter. Missing quotes retain every original basket.

Current cash remains observed. Conditional income may use the explicit
historical reference scenario from the182 release contract, never wallet credit.
"""
from copy import deepcopy

from . import cash_continuation

DAYS = cash_continuation.DAYS
KEY = 'larc_strategy182_cash_income_contract'
REFERENCE_VERSION = 'larc.AP-reference-scenario182.v1'
REFERENCE_SOURCE = {
    'rule_version': REFERENCE_VERSION,
    'training_rule_url': 'https://github.com/hzyhhzy/UmaAi/blob/d69b0c815e7e9bad503a40f0783ebb24c2f981af/UmaSimulator/Game/Game.cpp',
    'training_rule_lines': [917, 917],
    'reference_table_url': "https://wikiwiki.jp/vip_umamusu/Reach%20for%20the%20stars%20%E3%83%97%E3%83%AD%E3%82%B8%E3%82%A7%E3%82%AF%E3%83%88L%27Arc",
    'checked_date': '2026-11-01',
    'cn_verified': False, 'observed': False, 'forecast_only': True,
}


def enabled(goal):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    return unified_larc_enabled(goal)


def _amount(value):
    return type(value) is int and value >= 0


def conditional_income_quote(candidate, period, *, run_id, turn):
    """Quote success AP from an existing bound observation or measured heads.

    The parser does not observe an AP preview. The reference formula is a
    conditional lower-bound scenario using its actual participant count;
    unknown rainbow count is not turned into an observed zero count.
    """
    from core.auto_trainer.larc_calendar import period_ordinal, is_overseas
    capture = (candidate.raw_data or {}).get('capture') or {}
    if (not run_id or capture.get('run_session_id') != run_id
            or capture.get('turn_index') != turn or type(turn) is not int
            or not isinstance(capture.get('sha256'), str) or not capture['sha256']
            or any(capture.get(k) for k in ('simulation', 'simulation_truth', 'projected', 'hypothetical'))):
        return None
    larc = (candidate.scenario_mechanics or {}).get('larc') or {}
    effects = larc.get('training_effects') or {}
    source = effects.get('source') or {}
    if (effects.get('status') == 'accepted' and _amount(effects.get('adaptation_pt'))
            and all(source.get(k) == capture.get(k) for k in ('run_session_id', 'turn_index', 'sha256'))):
        return {'status': 'accepted', 'conditional_amount': effects['adaptation_pt'],
            'source': deepcopy(source), 'basis': 'observed_training_preview',
            'observed': True, 'forecast_only': True, 'not_current_cash': True}
    day = period_ordinal(period or {})
    if day is None or not is_overseas(day):
        return None
    people = larc.get('training_participants') or {}
    count = people.get('visible_portrait_count')
    rows = people.get('participants')
    if (people.get('schema_version') != 'larc_training_participants.v1'
            or people.get('status') != 'accepted' or people.get('training_type') != candidate.training_type
            or type(count) is not int or not 0 <= count <= 5
            or not isinstance(rows, list) or len(rows) != count):
        return None
    # A bound aggregate True proves at least one rainbow, not an exact count.
    minimum_rainbows = 1 if candidate.friendship is True and count else 0
    amount = (30 if candidate.training_type == 'wisdom' else 50) + 20 * count + 20 * minimum_rainbows
    return {'status': 'forecast', 'conditional_amount': amount, 'source': deepcopy(capture),
        'basis': 'historical_reference_rule_with_current_parser_heads_conservative_rainbow_floor',
        'source_kind': 'reference_rule_conditional_forecast', 'rule_version': REFERENCE_VERSION,
        'rule_source': deepcopy(REFERENCE_SOURCE), 'observed': False, 'cn_verified': False,
        'forecast_only': True, 'not_current_cash': True, 'observed_participant_count': count,
        'rainbow_count_known': False, 'minimum_rainbow_scenario': minimum_rainbows,
        'rainbow_basis': 'aggregate_true_at_least_one' if minimum_rainbows else 'conservative_floor_not_observed_zero',
        'random_Mei_AP_bonus_included': False}


def _reference_rule(run, turn):
    return {'status': 'forecast', 'source_kind': 'reference_rule_conditional_forecast',
        'run_session_id': run, 'turn_index': turn, 'rule_version': REFERENCE_VERSION,
        'basis': 'explicit182_reference_income_scenario_not_current_wallet_or_CN_observation',
        'rest_conditional_AP': 100, 'fixed64_conditional_AP': 100,
        'rest_basis': 'historical_JP_reference_rule',
        'fixed64_basis': 'U2-adopted_declared_scenario_not_CN_verified',
        'rule_source': deepcopy(REFERENCE_SOURCE), 'observed': False, 'cn_verified': False,
        'forecast_only': True, 'not_current_cash': True}


def _unavailable(reason):
    return {'active': False, 'reason': reason, 'whole_pool_legacy_retained': True,
            'new_stop_required': False, 'execution_authorized': False,
            'purchase_authorized': False, 'ledger_written': False,
            'strategy_id': '1.8.2', 'algorithm_build': cash_continuation.VERSION}


def current_context(state, inherited):
    """Use the already accepted same-turn main AP with known owned receipts."""
    value = deepcopy(inherited)
    from .consumer import _source, levels
    run = _source(state)
    if (value.get('status')=='accepted' and value.get('run_session_id')==run
            and value.get('through_turn')==state.turn and value.get('balance_current') is True
            and _amount(value.get('points')) and levels(value.get('levels'))):
        return value  # A current shop quote wins over an earlier main-menu read.
    growth = (state.scenario_state or {}).get('larc_growth_observation') or {}
    source = growth.get('source') or {}
    frame = growth.get('source_frame_sha256')
    bound = (growth.get('status') == 'accepted' and growth.get('turn_index') == state.turn
        and source.get('run_session_id') == run and source.get('turn_index') == state.turn
        and isinstance(frame, str) and bool(frame) and source.get('sha256') == frame
        and _amount(growth.get('adaptation_points')))
    if (bound and value.get('status') == 'accepted' and value.get('run_session_id') == run
            and value.get('through_turn') == state.turn and levels(value.get('levels'))):
        value.update(points=growth['adaptation_points'], balance_current=True,
            balance_source=deepcopy(source),
            basis='same_turn_accepted_main_growth_AP_and_same_run_owned_receipts')
    return value


def _rule(state, run):
    value = (state.scenario_state or {}).get(KEY) or {}
    if not value:
        return _reference_rule(run, state.turn)
    if not (isinstance(value, dict) and value.get('schema_version') == 'larc.production_cash_income.v1'
            and value.get('status') == 'accepted' and value.get('run_session_id') == run
            and value.get('turn_index') == state.turn
            and value.get('source_kind') == 'accepted_current_version_rule'
            and isinstance(value.get('rule_version'), str) and value['rule_version']
            and isinstance(value.get('basis'), str) and value['basis']
            and not any(value.get(k) for k in ('simulation', 'simulation_truth', 'projected', 'hypothetical'))):
        return None
    return value


def _inputs(current, state, records, incomes):
    from .consumer import _source, TABS
    run = _source(state)
    rule = _rule(state, run)
    current_income = {}
    for action in current.actions:
        if not action.allowed:
            continue
        row = incomes.get(action.action_id) or {}
        source = row.get('source') or {}
        if action.kind == 'train':
            valid = (row.get('status') in {'accepted', 'forecast'} and _amount(row.get('conditional_amount'))
                and source.get('run_session_id') == run and source.get('turn_index') == state.turn
                and isinstance(source.get('sha256'), str) and bool(source['sha256']))
            if not valid:
                return None, 'current_conditional_AP_unknown:' + action.action_id
            current_income[action.action_id] = {**deepcopy(row), 'p_percent': action.p_percent,
                'quote_status': row['status'], 'status': 'accepted'}
        else:
            amount = (rule or {}).get('rest_conditional_AP')
            if action.action_id != 'rest.normal' or not _amount(amount):
                return None, 'current_rest_conditional_AP_rule_unknown'
            current_income[action.action_id] = {'status': 'accepted', 'quote_status': rule['status'], 'conditional_amount': amount,
                'p_percent': action.p_percent, 'basis': rule['basis'],
                'rule_version': rule['rule_version'], 'not_current_cash': True,
                'source_kind': rule['source_kind'], 'observed': rule.get('observed', False),
                'cn_verified': rule.get('cn_verified', False), 'forecast_only': True,
                'rule_source': deepcopy(rule.get('rule_source'))}
    future_income = {}
    if current.future_free_days:
        rest = (rule or {}).get('rest_conditional_AP')
        if not _amount(rest):
            return None, 'future_rest_conditional_AP_rule_unknown'
        for menu in current.menus:
            matches = [row for row in records if 'real-past-T' + str(row.get('turn_index')) == menu.source_id
                       and row.get('calendar_ordinal') == menu.source_calendar
                       and row.get('run_session_id') == run and type(row.get('turn_index')) is int
                       and row['turn_index'] < state.turn]
            if len(matches) != 1:
                return None, 'strict_past_income_menu_binding_missing:' + menu.source_id
            record = matches[0]
            values = {}
            for tab in TABS:
                metadata = ((record.get('forecast_metadata') or {}).get(tab) or {})
                row = metadata.get('conditional_income_observation') or metadata.get('conditional_income_reference') or {}
                source = row.get('source') or {}
                risk_source = ((record.get('risk_observations') or {}).get(tab) or {}).get('source') or {}
                if not (row.get('status') in {'accepted', 'forecast'} and _amount(row.get('conditional_amount'))
                        and source.get('run_session_id') == run and source.get('turn_index') == record['turn_index']
                        and isinstance(source.get('sha256'), str) and bool(source['sha256'])
                        and all(source.get(k) == risk_source.get(k) for k in ('run_session_id', 'turn_index', 'sha256'))):
                    return None, 'strict_past_menu_conditional_AP_unknown:' + menu.source_id + ':' + tab
                values['future:' + tab] = row['conditional_amount']
            values['future:rest'] = rest
            future_income[menu.source_id] = values
    fixed = []
    if current.calendar < 64:
        amount = (rule or {}).get('fixed64_conditional_AP')
        if not _amount(amount):
            return None, 'fixed64_conditional_AP_rule_unknown'
        fixed = [{'calendar': 64, 'amount': amount}]
    return {'current': current_income, 'future': future_income, 'fixed': fixed,
            'basis': 'observed_wallet; conditional_AP_observation_or_explicit182_reference_scenario',
            'rule_version': (rule or {}).get('rule_version'),
            'rule_diagnostic': deepcopy(rule),
            'current_quotes': deepcopy(current_income)}, None


def _purchase_events(candidate_levels):
    from core.auto_trainer.larc_adaptation import LEVEL_TWO_COSTS, PHASE_SLOT_COUNTS, build_adaptation_plan
    if len(LEVEL_TWO_COSTS) != 10 or PHASE_SLOT_COUNTS.get('final_arc') != 10:
        return None
    formal = build_adaptation_plan({'levels': list(candidate_levels), 'points': sum(LEVEL_TWO_COSTS),
                                   'pending_cost': 0}, phase='final_arc')
    targets = formal.get('targets') or []
    if len(targets) != 10:
        return None
    return [{'slot': slot, 'target_level': 2, 'cost': LEVEL_TWO_COSTS[slot],
             'original_condition_met': candidate_levels[slot] >= 2 or targets[slot] == 2,
             'condition_basis': 'same_version_formal_final_arc_nonbudget_eligibility'} for slot in range(10)]


def rank_pool(prepared, state, owned, cash, records, incomes, scenario, K, S, max_ops, *, deferred_facility=None):
    try:
        return _rank_pool(prepared,state,owned,cash,records,incomes,scenario,K,S,max_ops,
                          deferred_facility=deferred_facility)
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,ArithmeticError) as error:
        return [row[2] for row in prepared],_unavailable('cash_inputs_unavailable:'+type(error).__name__)


_CASH_RANK_CACHE: dict = {}


def _rank_pool(prepared, state, owned, cash, records, incomes, scenario, K, S, max_ops, *, deferred_facility=None):
    """All current baskets share182 math, or every basket keeps its old rank."""
    if not prepared:
        return [], _unavailable('original_current_basket_pool_empty')
    from core.auto_trainer.larc_calendar import period_ordinal
    context=state.scenario_state or {}
    period=context.get('larc_period') or {}
    day=period_ordinal(period.get('value') or {})
    if (context.get('simulation') or 'simulation_truth' in context
            or period.get('status')!='verified' or day not in DAYS
            or any(item[1].calendar!=day for item in prepared)):
        return [row[2] for row in prepared],_unavailable('not_same_actual_tail_free_action')
    results = []
    metadata = []
    terminal = scenario['terminal']
    term_key = tuple(terminal.get(k) for k in (
        'race_bonus_percent', 'mei_unlocked', 'mei_completed', 'mei_event_multiplier', 'reporter_band'))
    def schedule_for_level(level):
        return S.normal_schedule(final_level=level, **{key: terminal[key] for key in (
            'race_bonus_percent', 'mei_unlocked', 'mei_completed', 'mei_event_multiplier', 'reporter_band')})
    for spec, quoted, legacy, _audit in prepared:
        supplied, problem = _inputs(quoted, state, records, incomes)
        if problem:
            return [row[2] for row in prepared], _unavailable(problem)
        events = _purchase_events(spec['targets'])
        if events is None:
            return [row[2] for row in prepared], _unavailable('same_version_full_final_arc_contract_unknown')
        optional = {}
        if deferred_facility is not None:
            future_events = _purchase_events(deferred_facility['levels'])
            if future_events is None:
                return [row[2] for row in prepared], _unavailable('deferred_final_arc_contract_unknown')
            optional['deferred_facility'] = {**deferred_facility, 'purchase_events': future_events}
        cache_key = (
            quoted,
            tuple(owned),
            tuple(spec['targets']),
            cash,
            spec['cost'],
            term_key,
            max_ops,
            repr(deferred_facility),
            tuple(sorted((k, v.get('conditional_amount'), v.get('p_percent')) for k, v in supplied['current'].items())),
            tuple(sorted((m, tuple(sorted(mv.items()))) for m, mv in supplied['future'].items())),
        )
        cached = _CASH_RANK_CACHE.get(cache_key)
        if cached is not None:
            changed = deepcopy(cached)
        else:
            changed = cash_continuation.rank_cash_current(quoted, owned_levels=owned,
                candidate_levels=spec['targets'], cash_before_candidate_cost=cash,
                candidate_cost=spec['cost'], income_by_action=supplied['current'],
                income_by_menu=supplied['future'], fixed_income_events=supplied['fixed'],
                purchase_events=events, schedule_for_level=schedule_for_level,
                legacy_result=legacy, pure_math=K, max_ops=max_ops, **optional)
            if len(_CASH_RANK_CACHE) >= 64:
                _CASH_RANK_CACHE.clear()
            _CASH_RANK_CACHE[cache_key] = deepcopy(changed)
        detail = changed.get('cash_continuation') or {}
        if detail.get('active') is not True:
            return [row[2] for row in prepared], _unavailable(detail.get('reason', 'cash_value_coverage_unknown'))
        results.append(changed)
        metadata.append({'basket_id': spec['basket_id'], 'cash_before': cash, 'basket_cost': spec['cost'],
                         'conditional_income_basis': supplied['basis'], 'rule_version': supplied['rule_version'],
                         'income_rule_scenario': supplied['rule_diagnostic'], 'current_income_quotes': supplied['current_quotes'],
                         'cash_continuation': detail})
    return results, {'active': True, 'strategy_id': '1.8.2', 'algorithm_build': cash_continuation.VERSION,
        'whole_pool_legacy_retained': False, 'actual_outer_calendar': prepared[0][1].calendar,
        'ranked_current_baskets': len(results), 'basket_diagnostics': metadata,
        'real_CN_fixed_or_rest_rule_assumed': False, 'cash_forecast_is_not_current_wallet': True,
        'reference_rule_forecasts_enabled': True, 'cn_income_verified': False,
        'new_stop_required': False, 'execution_authorized': False, 'purchase_authorized': False,
        'ledger_written': False}
